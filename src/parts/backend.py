#!/usr/bin/env python3
"""Claude Control - app locale pour gerer MCPs et Skills de Claude Desktop."""
# ============================================================================
# FICHIER ASSEMBLE (v1.15.0) - ne pas editer src/app.py directement.
# Sources : src/parts/backend.py + src/parts/ui.html + src/parts/server.py
# Regenerer : python3 scripts/build.py     Verif CI : build.py --check
# Edite app.py par accident ? build.py --split re-derive les parts.
# L'artefact monofichier reste la cle de l'auto-update (git pull + copie
# de src/app.py) et de la philosophie zero-dependance : il est COMMITE.
# ============================================================================
import http.server, io, json, os, re, shutil, socketserver, subprocess, sys, tempfile, threading, time, traceback, unicodedata, webbrowser, zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
import urllib.error
import urllib.request

MAX_ZIP_SIZE = 50 * 1024 * 1024  # 50 Mo

PORT = 8765
HOME = Path.home()
CONFIG_PATH = HOME / "Library/Application Support/Claude/claude_desktop_config.json"
EXTENSIONS_INSTALL_FILE = HOME / "Library/Application Support/Claude/extensions-installations.json"
EXTENSIONS_SETTINGS_DIR = HOME / "Library/Application Support/Claude/Claude Extensions Settings"
CLI_CONFIG_PATH = HOME / ".claude.json"   # v1.14.0 - MCPs de Claude Code CLI
SKILLS_DIR = HOME / ".claude/skills"
SKILLS_DISABLED_DIR = HOME / ".claude/skills-disabled"
COMMANDS_DIR = HOME / ".claude/commands"
COMMANDS_DISABLED_DIR = HOME / ".claude/commands-disabled"
CLAUDE_MD_FILE = HOME / ".claude/CLAUDE.md"
BACKUP_DIR = HOME / ".claude/backups/claude-control"
IMPORTED_REPOS_DIR = HOME / ".claude/imported-mcps"
PRESETS_FILE = HOME / ".claude/claude-control-presets.json"

PLUGINS_DIR = HOME / ".claude/plugins"
INSTALLED_PLUGINS_FILE = PLUGINS_DIR / "installed_plugins.json"
KNOWN_MARKETPLACES_FILE = PLUGINS_DIR / "known_marketplaces.json"
SETTINGS_FILE = HOME / ".claude/settings.json"
ORPHAN_BACKUP_DIR = BACKUP_DIR / "orphan-plugins"
CLAUDE_LOGS_DIR = HOME / "Library/Logs/Claude"

VERSION_FILE = HOME / "dev/claude-control/version.txt"
GITHUB_REPO_FILE = HOME / "dev/claude-control/.github-repo"
LOG_FILE = HOME / "Library/Logs/claude-control.log"

# v1.14.0 - chemin exact du binaire Claude Desktop. Sert d'ancre aux pkill :
# un motif non ancre ("Claude") matche la ligne de commande complete de TOUS
# les process de l'utilisateur (sessions Claude Code CLI, `tail` sur un log
# dans ~/Library/Logs/Claude/, etc.) et pas seulement l'app.
CLAUDE_DESKTOP_BIN = "/Applications/Claude.app/Contents/MacOS/Claude"

# v1.6.6 : tracking en memoire des derniers restarts MCP/extension pour
# afficher "Dernier restart : il y a X min" dans la carte Action rapide.
# Reset au redemarrage du serveur Claude Control.
_LAST_RESTARTS = {}

# v1.14.0 - le serveur est multi-thread (cf. main()). Toute sequence
# lire-modifier-ecrire sur un fichier de config doit tenir ce verrou, sinon
# deux requetes concurrentes (deux toggles rapides, ou un toggle pendant un
# apply_preset) se marchent dessus et la derniere ecriture ecrase l'autre.
_CONFIG_LOCK = threading.RLock()


def _log(msg):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass


def _atomic_write_text(path, text):
    """v1.14.0 - Ecriture atomique : fichier temporaire dans le meme
    repertoire puis os.replace() (atomique sur POSIX).

    Avant, tous les save_* ouvraient la cible en "w", ce qui la tronque
    immediatement. Si le dump echouait a mi-parcours, ou si le process etait
    tue entre-temps (restart_self, pkill), claude_desktop_config.json
    restait tronque et Claude Desktop ne chargeait plus aucun MCP.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _atomic_write_json(path, data, indent=2):
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=indent))


def _osa_quote(s, limit):
    """v1.14.0 - Echappe une chaine destinee a une chaine litterale
    AppleScript.

    L'ordre compte : les antislashs D'ABORD, les guillemets ensuite. L'ordre
    inverse (bug v1.6.6) transformait `"` en `\\"` puis son antislash en
    `\\\\`, soit `\\\\"` -> un antislash echappe SUIVI d'un guillemet nu, qui
    ferme la chaine. Comme AppleScript expose `do shell script`, une chaine
    controlee par un tiers (nom de MCP issu d'un import) devenait une
    execution de commande.

    La troncature se fait AVANT l'echappement, sinon elle peut couper au
    milieu d'une sequence et laisser un antislash orphelin en fin de chaine.
    """
    s = (s or "")[:limit]
    s = re.sub(r"[\r\n\t]", " ", s)
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _notify(title, message):
    """Affiche une notification macOS native via osascript. Best-effort,
    silencieux si echec. v1.6.6 : utilise pour signaler la fin d'un restart
    MCP/extension meme quand l'UI Claude Control n'est pas au premier plan."""
    try:
        script = (f'display notification "{_osa_quote(message, 300)}" '
                  f'with title "{_osa_quote(title, 120)}"')
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=3)
    except Exception:
        pass


def load_config():
    if not CONFIG_PATH.exists():
        return {"mcpServers": {}, "_disabledMcps": {}}
    with open(CONFIG_PATH, encoding="utf-8-sig") as f:
        return json.load(f)


def load_config_safe():
    """v1.14.0 - Variante non levante de load_config, pour les chemins de
    lecture (get_state / get_overview). Retourne (config, error_str|None).

    Avant, un claude_desktop_config.json malforme (virgule en trop apres une
    edition manuelle) faisait lever json.load -> 500 sur /api/state -> le JS
    ne catchait rien -> la liste MCP gardait son dernier rendu SANS message.
    Symptome vecu : "mes MCPs ont disparu", sans explication.
    """
    try:
        return load_config(), None
    except Exception as e:
        _log(f"load_config failed: {type(e).__name__}: {e}")
        return {"mcpServers": {}, "_disabledMcps": {}}, f"{CONFIG_PATH.name} illisible : {e}"


def save_config(config):
    with _CONFIG_LOCK:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        if CONFIG_PATH.exists():
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = BACKUP_DIR / f"config.{ts}.json"
            try:
                shutil.copy2(CONFIG_PATH, backup)
            except Exception:
                pass
        _atomic_write_json(CONFIG_PATH, config)


# === DETECTION DES PROCESS MCP ===

# v1.14.0 - snapshot `ps auxww` mutualise. Avant, chaque /api/state relancait
# un `ps` plus, par extension, jusqu'a 3 `pgrep` + 3 `lsof` + un `ps -p` par
# PID -- soit ~50 fork/exec par appel, toutes les 5 s, et autant via
# /api/overview. Un seul snapshot par fenetre de _PS_TTL secondes suffit.
_PS_CACHE = {"ts": 0.0, "lines": []}
_PS_LOCK = threading.Lock()
_PS_COMPUTE_LOCK = threading.Lock()
_PS_TTL = 8


def _ps_snapshot(force=False):
    """Lignes de `ps auxww`, mises en cache _PS_TTL secondes.

    Single-flight : si plusieurs threads arrivent en meme temps sur un cache
    froid, un seul lance `ps`, les autres attendent et lisent le resultat.
    """
    now = time.time()
    if not force:
        with _PS_LOCK:
            if _PS_CACHE["lines"] and now - _PS_CACHE["ts"] < _PS_TTL:
                return _PS_CACHE["lines"]
    with _PS_COMPUTE_LOCK:
        if not force:
            with _PS_LOCK:
                if _PS_CACHE["lines"] and time.time() - _PS_CACHE["ts"] < _PS_TTL:
                    return _PS_CACHE["lines"]
        try:
            r = subprocess.run(["ps", "auxww"], capture_output=True, text=True, timeout=5)
            lines = r.stdout.splitlines()
        except Exception:
            lines = []
        with _PS_LOCK:
            _PS_CACHE["ts"] = time.time()
            _PS_CACHE["lines"] = lines
        return lines


# Tokens trop generiques pour identifier un MCP : ils matchent n'importe quel
# process node/python de la machine et produiraient des faux "running".
# 'mcp-server' & co sont dedans parce qu'ils sont le basename de tres
# nombreux paquets (@linear/mcp-server, @mailchimp/mcp-server...) : les
# garder ferait passer un MCP pour actif des qu'un AUTRE MCP tourne.
_GENERIC_PROCESS_TOKENS = {
    "npx", "node", "nodejs", "python", "python3", "uvx", "uv", "bun", "deno",
    "java", "ruby", "tsx", "ts-node", "run", "start", "serve", "server",
    "index.js", "server.js", "main.js", "cli.js", "main.py", "server.py",
    "__main__.py", "dist", "build", "src", "bin", "lib", "app.py",
    "mcp", "mcp-server", "mcp_server", "mcpserver", "mcp-servers",
    "server.ts", "index.ts", "stdio", "latest",
}

# Binaires dont la ligne de commande MENTIONNE du texte sans l'executer comme
# un serveur MCP. Sans ce filtre, un `grep sekoia-geo`, un editeur ouvert sur
# la config, ou un `bash -c` portant un script entier en argv suffisent a
# faire passer un MCP pour "running".
_NON_MCP_PROCESS_BINARIES = {
    "bash", "sh", "zsh", "dash", "fish", "csh", "tcsh", "ksh", "login",
    "grep", "egrep", "fgrep", "rg", "ag", "ack", "sed", "awk", "tail", "head",
    "cat", "less", "more", "tee", "xargs", "find", "fd", "watch", "tr",
    "vim", "nvim", "vi", "emacs", "nano", "code", "ps", "pgrep", "pkill",
    "lsof", "open", "osascript", "man", "git", "ssh", "sudo", "env", "which",
}


def _mcp_match_tokens(name, entry):
    """v1.14.0 - Tokens distinctifs permettant de reconnaitre le process d'un
    MCP dans la sortie de `ps`.

    Remplace la table de mots-cles codee en dur de get_running_mcps (6
    entrees, valables uniquement sur une machine precise) : tout MCP absent
    de cette table avait running=False en permanence et atterrissait dans la
    colonne "Inactifs" meme quand il tournait parfaitement.

    On derive les tokens de la command/args reelles du MCP, en ecartant les
    launchers generiques (npx, node, index.js...) qui matcheraient tout.
    """
    tokens = set()
    n = str(name or "").strip()
    if len(n) >= 4 and n.lower() not in _GENERIC_PROCESS_TOKENS:
        tokens.add(n)
    if isinstance(entry, dict):
        parts = [str(entry.get("command") or "")]
        for a in (entry.get("args") or []):
            if isinstance(a, (str, int, float)):
                parts.append(str(a))
        for raw in parts:
            p = raw.strip()
            if not p or p.startswith("-"):
                continue
            if "/" in p:
                tokens.add(p)
                base = Path(p).name
                if len(base) >= 5 and base.lower() not in _GENERIC_PROCESS_TOKENS:
                    tokens.add(base)
            elif len(p) >= 5 and p.lower() not in _GENERIC_PROCESS_TOKENS:
                tokens.add(p)
    return {t for t in tokens if len(t) >= 4}


def _invalidate_process_caches():
    """v1.14.0 - A appeler apres toute action qui change l'etat des process
    (start/stop/restart MCP ou extension). Sans ca, l'UI garderait jusqu'a
    _EXT_RUNNING_TTL secondes un statut perime juste apres l'action de
    l'utilisateur -- le pire moment pour mentir."""
    with _PS_LOCK:
        _PS_CACHE["ts"] = 0.0
        _PS_CACHE["lines"] = []
    with _EXT_RUNNING_LOCK:
        _EXT_RUNNING_CACHE["ts"] = 0.0
        _EXT_RUNNING_CACHE["data"] = {}


def _ps_candidate_lines():
    """Lignes de `ps auxww` susceptibles d'etre un serveur MCP, en minuscules.

    On ecarte notre propre process, le disclaimer Claude, et les binaires de
    _NON_MCP_PROCESS_BINARIES (shells, grep, editeurs...) qui ne font que
    porter du texte dans leur argv.

    Format `ps auxww` : USER PID %CPU %MEM VSZ RSS TT STAT STARTED TIME
    COMMAND -> 10 colonnes avant la commande, identique sur macOS et Linux.
    """
    lines = _ps_snapshot()
    if not lines:
        return []
    my_pid = str(os.getpid())
    out = []
    for line in lines:
        if "Helpers/disclaimer" in line:
            continue
        cols = line.split(None, 10)
        if len(cols) < 11:
            continue
        if cols[1] == my_pid:
            continue
        cmd = cols[10].strip()
        if not cmd:
            continue
        first = cmd.split(None, 1)[0]
        if Path(first).name.lower() in _NON_MCP_PROCESS_BINARIES:
            continue
        if "claude-control/app.py" in cmd:
            continue
        out.append(cmd.lower())
    return out


def _running_from_mapping(mapping, lines=None):
    """Sous-ensemble des cles de `mapping` ({nom: definition}) dont un process
    correspondant est visible dans `ps`."""
    if not isinstance(mapping, dict) or not mapping:
        return set()
    if lines is None:
        lines = _ps_candidate_lines()
    if not lines:
        return set()
    running = set()
    for name, entry in mapping.items():
        toks = [t.lower() for t in _mcp_match_tokens(name, entry)]
        if not toks:
            continue
        for cmd in lines:
            if any(t in cmd for t in toks):
                running.add(name)
                break
    return running


def get_running_mcps(config=None):
    """v1.14.0 - Retourne l'ensemble des noms de MCP (cles de config) dont un
    process correspondant est visible dans `ps auxww`.

    Avant : table de 6 mots-cles codes en dur -> tout autre MCP etait
    signale "pas demarre" a vie. Maintenant : matching sur les tokens
    derives de la config de chaque MCP.
    """
    if config is None:
        config, _err = load_config_safe()
    return _running_from_mapping(config.get("mcpServers") or {})


_AUTO_CAT_RULES = [
    ("Git / GitHub",   ("github", "gitlab", "git ", "pull request", "pull-request", " pr ", "commit", "branch", "repo")),
    ("Claude API",     ("anthropic", "claude api", "claude-api", "claude code", "claude.ai", "sdk", " caching", "agent sdk")),
    ("API integration", ("rest", " http ", "endpoint", "webhook", "fetch", " api ", " apis ")),
    ("Communication",  ("slack", "discord", "email", "gmail", "messenger", "send a message")),
    ("Marketing",      ("mailchimp", "campaign", "newsletter", "subscriber", "audience")),
    ("Database",       ("database", "mongo", "postgres", "mysql", "sql", "query")),
    ("Files / Docs",   ("file system", "drive", "dropbox", "notion", "document")),
    ("Build / Deploy", ("vercel", "deploy", "ci/cd", " ci ", " cd ", "docker", "build", "release", "package")),
    ("Debug / Test",   ("debug", "test", "lint", "review", "audit", "troubleshoot", "fix bug")),
    ("Web / UI",       ("frontend", "html", "css", "react", "vue", "tailwind", "webpage")),
    ("Hooks",          ("session start", "stop hook", "hook", "settings.json")),
    ("Workflow",       ("loop", "schedule", "task", "babysit", "monitor", "recurring")),
    ("Permissions",    ("permission", "allow", "settings.json")),
    ("Config",         ("config", "configure", "preferences")),
]


def _auto_category(description, name=""):
    if not description:
        return None
    text = (description + " " + name).lower()
    for cat, kws in _AUTO_CAT_RULES:
        if any(k in text for k in kws):
            return cat
    return None


# v1.14.17 - cache par mtime : get_state() lit les meta de CHAQUE skill a
# CHAQUE appel (N fichiers x 18 appels/min). Le mtime invalide naturellement
# (une reparation reecrit le fichier), et le cache se vide s'il enfle.
_SKILL_META_CACHE = {}
_SKILL_META_LOCK = threading.Lock()
_SKILL_META_CACHE_MAX = 4096


def read_skill_meta(skill_dir):
    """Lit le frontmatter YAML d'un SKILL.md et retourne {category, description, tags}."""
    meta = {"category": None, "description": None, "tags": []}
    md = skill_dir / "SKILL.md"
    try:
        mtime = md.stat().st_mtime_ns
    except OSError:
        return meta
    cache_key = str(md)
    with _SKILL_META_LOCK:
        hit = _SKILL_META_CACHE.get(cache_key)
        if hit and hit[0] == mtime:
            cached = hit[1]
            return {"category": cached["category"],
                    "description": cached["description"],
                    "tags": list(cached["tags"])}

    def _store(result):
        with _SKILL_META_LOCK:
            if len(_SKILL_META_CACHE) >= _SKILL_META_CACHE_MAX:
                _SKILL_META_CACHE.clear()
            _SKILL_META_CACHE[cache_key] = (mtime, {
                "category": result["category"],
                "description": result["description"],
                "tags": list(result["tags"])})
        return result

    try:
        content = md.read_text(errors="replace")
    except Exception:
        # Erreur de lecture potentiellement transitoire : on ne cache pas.
        return meta
    if not content.startswith("---"):
        return _store(meta)
    end = content.find("\n---", 3)
    if end == -1:
        return _store(meta)
    fm_lines = content[3:end].splitlines()
    for fm_key, start, stop in _frontmatter_entries(fm_lines):
        if fm_key in ("category", "description"):
            meta[fm_key] = _frontmatter_scalar(fm_lines, (fm_key, start, stop)) or None
        elif fm_key == "tags":
            m = re.match(r'^\s*tags\s*:\s*\[([^\]]*)\]\s*$', fm_lines[start])
            if m:
                meta["tags"] = [_fm_unquote(t) for t in m.group(1).split(",")
                                if t.strip()]
    return _store(meta)


# v1.14.5 - Une cle de frontmatter n'occupe pas forcement une seule ligne.
# YAML autorise le scalaire replie (`>`), le litteral (`|`), la valeur sur
# les lignes suivantes et la continuation indentee -- des formes courantes
# des qu'une description depasse la largeur d'ecran. Le parseur ligne-a-ligne
# lisait alors ">-" ou "|" comme valeur (1 a 2 caracteres) et l'app declarait
# "description trop courte" sur des skills parfaitement decrits ; quand la
# valeur commencait a la ligne suivante, elle les declarait carrement sans
# description.
#
# Plus grave : la reecriture remplacait la seule ligne `description:` et
# laissait les lignes de continuation orphelines derriere elle, ce qui donne
# un YAML invalide. Reparer un skill mal juge le cassait pour de bon, avec
# un message de succes.
#
# Lecture et ecriture partagent desormais ce decoupage : ce que l'app sait
# lire, elle sait le remplacer entierement.
_FM_KEY_RE = re.compile(r'^(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:(?P<rest>.*)$')


def _frontmatter_entries(fm_lines):
    """v1.14.5 - Decoupe un frontmatter en (cle, debut, fin).

    Une entree s'etend de sa ligne `cle:` jusqu'a la prochaine cle de
    premier niveau : elle englobe donc toutes ses lignes de continuation.
    """
    entries = []
    for i, line in enumerate(fm_lines):
        m = _FM_KEY_RE.match(line) if line[:1].strip() else None
        if m:
            entries.append([m.group("key"), i, i + 1])
        elif entries:
            entries[-1][2] = i + 1
    return [tuple(e) for e in entries]


def _fm_unquote(v):
    """v1.14.5 - Deballe une valeur scalaire YAML.

    L'app ecrit ses valeurs via json.dumps (_yaml_quote_value) : les relire
    avec un simple strip('"') rendait litteralement les \\" et \\n d'une
    description qui en contenait.
    """
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        try:
            return json.loads(v)
        except Exception:
            return v[1:-1]
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


def _frontmatter_scalar(fm_lines, entry):
    """v1.14.5 - Valeur complete d'une entree, quelle que soit sa forme."""
    _key, start, stop = entry
    m = _FM_KEY_RE.match(fm_lines[start])
    rest = (m.group("rest") if m else "").strip()
    cont = [l.strip() for l in fm_lines[start + 1:stop]]
    style = rest[:1] if rest[:1] in ("|", ">") else ""

    if not style and rest:
        # Scalaire simple, eventuellement continue sur les lignes suivantes.
        return _fm_unquote(" ".join([rest] + [l for l in cont if l]))

    while cont and not cont[-1]:
        cont.pop()
    if style == "|":
        return "\n".join(cont)
    # Replie ('>') ou valeur commencant a la ligne suivante : les lignes se
    # joignent par une espace, une ligne vide separe deux paragraphes.
    paragraphs, current = [], []
    for line in cont:
        if line:
            current.append(line)
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return _fm_unquote("\n".join(paragraphs))


def _yaml_quote_value(s):
    """v1.9.0 - Encode une valeur de string YAML en double-quoted JSON-compat
    (json.dumps produit toujours une string YAML-valide). On force la forme
    quotee pour que les descriptions avec ':' / "'" / '\\n' / accents ne
    cassent pas le parsing.
    """
    return json.dumps(str(s) if s is not None else "", ensure_ascii=False)


def _split_skill_frontmatter(content):
    """v1.9.0 - Separe un SKILL.md en (frontmatter_lines, body_str). Si pas
    de frontmatter ou malforme, retourne ([], content).
    """
    if not content.startswith("---"):
        return [], content
    end = content.find("\n---", 3)
    if end == -1:
        return [], content
    fm_block = content[3:end].strip("\n")
    body = content[end + 4:]
    if body.startswith("\n"):
        body = body[1:]
    return fm_block.splitlines(), body


def _update_skill_frontmatter(content, updates):
    """v1.9.0 - Retourne le contenu SKILL.md avec les cles `updates`
    mises a jour dans le frontmatter. Preserve toutes les autres cles
    et l'ordre. Si pas de frontmatter, en cree un.

    `updates` : dict {key: str}. Les valeurs sont toujours encodees en
    double-quoted YAML (cf. _yaml_quote_value) pour ne pas casser sur
    les caracteres speciaux.
    """
    fm_lines, body = _split_skill_frontmatter(content)
    for key, value in updates.items():
        new_line = f"{key}: {_yaml_quote_value(value)}"
        # v1.14.5 - on remplace l'entree ENTIERE, pas sa premiere ligne. Sur
        # un scalaire multi-ligne, ne remplacer que la ligne `cle:` laissait
        # ses lignes de continuation derriere elle : le frontmatter devenait
        # invalide et le skill cessait d'etre lu, alors que l'app annoncait
        # une reparation reussie. Les index sont recalcules a chaque passe,
        # une entree remplacee changeant la longueur du bloc.
        spans = {k: (s, e) for k, s, e in _frontmatter_entries(fm_lines)}
        if key in spans:
            start, stop = spans[key]
            fm_lines[start:stop] = [new_line]
        else:
            fm_lines.append(new_line)
    new_fm = "---\n" + "\n".join(fm_lines) + "\n---\n"
    return new_fm + body


def _find_skill_dir(name):
    """v1.14.1 - Localise le dossier d'un skill par son nom.

    Cherche dans skills/, skills-disabled/, puis skills/synced/ (les skills
    synchronises depuis le compte Claude). Avant, les deux appelants
    (repair_skill et suggest_skill_description) dupliquaient une boucle qui
    ignorait synced/ : un skill synchronise etait donc affiche mais
    "introuvable" des qu'on essayait de le reparer.
    """
    if not name or "/" in str(name) or "\\" in str(name) or ".." in str(name) or str(name).startswith("."):
        return None
    for base in (SKILLS_DIR, SKILLS_DISABLED_DIR, SKILLS_DIR / SYNCED_SKILLS_DIRNAME):
        candidate = base / name
        if candidate.is_dir():
            return candidate
    return None


def repair_skill(name, description=None, name_override=None):
    """v1.9.0 - Repare un skill en (re-)ecrivant son frontmatter avec la
    description fournie. Backup zip cree avant ecriture (cf. delete_skill).

    - `name` : nom du skill (dossier dans SKILLS_DIR ou SKILLS_DISABLED_DIR)
    - `description` : nouvelle description (string non vide)
    - `name_override` : nom YAML interne du skill (optionnel - si absent,
      on utilise le nom du dossier)

    Pour les skills sans frontmatter du tout, on en cree un avec
    `name:` + `description:`. Le contenu existant est preserve sous le
    nouveau frontmatter.
    """
    if not name or "/" in name or "\\" in name or ".." in name or name.startswith("."):
        return False, "Nom de skill invalide"
    if not description or not str(description).strip():
        return False, "Description requise"
    target = _find_skill_dir(name)
    if not target:
        return False, f"Skill '{name}' introuvable"
    md = target / "SKILL.md"
    if not md.exists():
        # Cree un SKILL.md minimal avec juste le frontmatter
        original_content = ""
    else:
        try:
            original_content = md.read_text(errors="replace")
        except Exception as e:
            return False, f"Erreur lecture SKILL.md : {e}"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_DIR / f"repaired-skill-{name}-{ts}.zip"
    try:
        _zip_dir(target, backup)
    except Exception as e:
        return False, f"Backup echoue : {e}"
    updates = {
        "name": name_override or name,
        "description": str(description).strip(),
    }
    new_content = _update_skill_frontmatter(original_content, updates)
    try:
        # v1.14.0 - ecriture atomique : un write_text interrompu laissait le
        # SKILL.md de l'utilisateur tronque.
        _atomic_write_text(md, new_content)
    except Exception as e:
        return False, f"Erreur ecriture : {e}"
    return True, f"Skill '{name}' repare (description = {len(updates['description'])} chars, backup : {backup.name})"


def _read_skill_content(name):
    """v1.9.0 - Pour la modal de reparation : lit le contenu SKILL.md
    et separe frontmatter / body. Retourne (ok, dict|err_msg)."""
    if not name or "/" in name or "\\" in name or ".." in name or name.startswith("."):
        return False, "Nom de skill invalide"
    target = None
    source = None
    for base, src in ((SKILLS_DIR, "active"), (SKILLS_DISABLED_DIR, "disabled")):
        candidate = base / name
        if candidate.exists() and candidate.is_dir():
            target = candidate
            source = src
            break
    if not target:
        return False, f"Skill '{name}' introuvable"
    md = target / "SKILL.md"
    if not md.exists():
        return True, {"name": name, "source": source, "exists": False, "content": "",
                      "frontmatter": [], "body": "", "meta": {"description": None, "category": None, "tags": []}}
    try:
        content = md.read_text(errors="replace")
    except Exception as e:
        return False, f"Erreur lecture : {e}"
    fm_lines, body = _split_skill_frontmatter(content)
    meta = read_skill_meta(target)
    return True, {
        "name": name, "source": source, "exists": True,
        "content": content, "frontmatter": fm_lines, "body": body, "meta": meta,
    }


def _claude_cli_path():
    """v1.9.3 - Retourne le chemin de la commande 'claude' (Claude Code CLI)
    si disponible, sinon None.

    v1.13.1 - on n'utilise plus uniquement `shutil.which("claude")` qui
    depend du PATH du process. Sur macOS quand Claude Control est lance
    depuis Finder (Launch Services), le PATH par defaut de launchd
    n'inclut PAS /usr/local/bin, /opt/homebrew/bin, ni les dirs npm/nvm/
    volta/fnm utilisateur. Resultat : le bouton "Suggerer via Claude Code"
    se desactive a tort parce que `claude` est introuvable, alors que le
    CLI marche tres bien depuis Terminal. Fallback : on inspecte aussi
    les emplacements communs avant d'abandonner.

    v1.14.12 - entre le PATH du process et les emplacements devines : le
    PATH du shell de connexion. "Ca marche dans mon terminal" designe UN
    binaire precis -- celui que le PATH du terminal resout. Le chercher
    d'abord la ou l'utilisateur le trouve supprime toute la classe de
    pannes "l'app a choisi un autre claude que le mien" : la liste devinee
    ci-dessous ordonnait brew avant nvm, et pouvait donc executer une
    installation plus vieille que celle que l'utilisateur utilise et met a
    jour. Les candidats devines ne restent que pour un shell muet.
    """
    found = shutil.which("claude")
    if found:
        return found
    shell_path = _login_shell_env().get("PATH")
    if shell_path:
        found = shutil.which("claude", path=shell_path)
        if found:
            return found
    candidates = [
        Path("/opt/homebrew/bin/claude"),       # macOS Apple Silicon + brew
        Path("/usr/local/bin/claude"),          # macOS Intel + brew, npm global
        HOME / ".npm-global/bin/claude",
        HOME / ".local/bin/claude",
        HOME / ".bun/bin/claude",
        HOME / ".volta/bin/claude",
        HOME / ".fnm/aliases/default/bin/claude",
        HOME / "n/bin/claude",
        HOME / ".asdf/shims/claude",
    ]
    # nvm : ~/.nvm/versions/node/*/bin/claude (toutes versions installees)
    #
    # v1.14.12 - tri par NUMERO de version, comme _cli_env() depuis v1.14.11.
    # Le tri alphabetique restait ici : "v9.11.2" passe avant "v22.1.0", et
    # l'app choisissait le claude installe sous le node le plus ANCIEN --
    # exactement le defaut corrige en v1.14.11, sur l'autre moitie du chemin.
    # Sur un vieux build x86_64, macOS attend Rosetta : gel sans un octet.
    nvm_dir = HOME / ".nvm/versions/node"
    if nvm_dir.is_dir():
        try:
            for v in sorted(nvm_dir.iterdir(), key=_node_version_key,
                            reverse=True):
                candidates.append(v / "bin/claude")
        except Exception:
            pass
    for c in candidates:
        try:
            if c.is_file() and os.access(c, os.X_OK):
                return str(c)
        except Exception:
            continue
    return None


def open_terminal_claude_login():
    """v1.9.5 - Ouvre Terminal.app et pre-tape 'claude /login' (sans
    presser Enter, l'utilisateur garde le controle final). osascript
    utilise 'tell application Terminal' qui marche sur stock macOS.
    """
    script = (
        'tell application "Terminal"\n'
        '  activate\n'
        '  do script "claude /login"\n'
        'end tell'
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
        return True, "Terminal ouvert avec la commande 'claude /login'. Suis les instructions pour t'authentifier."
    except Exception as e:
        return False, f"Erreur ouverture Terminal : {e}. Lance manuellement 'claude /login'."


# v1.14.4 - Variables dont le CLI a besoin pour JOINDRE l'API. Lance depuis
# Finder, Claude Control herite de l'environnement minimal de launchd : le
# PATH n'est pas la seule chose qui manque. Un proxy d'entreprise, un CA
# interne ou une base URL Anthropic vivent dans le .zshrc de l'utilisateur et
# sont invisibles a l'app -- le CLI part alors joindre une API qu'il ne peut
# pas atteindre et bloque sans rien ecrire.
#
# Liste volontairement fermee : on recupere le reseau et l'authentification,
# pas tout le shell. Importer un environnement entier dans un subprocess
# change son comportement de facons qu'on ne controle plus.
_CLI_ENV_INHERIT = (
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
    "https_proxy", "http_proxy", "all_proxy", "no_proxy",
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CONFIG_DIR",
)

def _node_version_key(path):
    """v1.14.11 - Cle de tri d'un repertoire nvm, par numero de version.

    Un tri alphabetique classait "v9" au-dessus de "v22" : l'app choisissait
    le node le plus ancien installe. Les composants non numeriques (rc, beta)
    ne cassent pas le tri, ils valent 0.
    """
    parts = re.findall(r"\d+", path.name)
    return tuple(int(p) for p in parts[:3]) or (0,)


_SHELL_ENV_CACHE = {"done": False, "env": {}}
# v1.14.12 - single-flight : la capture spawne un shell (jusqu'a 16 s si le
# rc bloque). Deux requetes simultanees au premier chargement de page la
# declenchaient chacune ; le verrou fait payer le shell une seule fois.
_SHELL_ENV_LOCK = threading.Lock()

# Variables recuperees lors du dernier _cli_env(). Le diagnostic les affiche :
# si l'appel ne marche QUE parce qu'on a rapatrie HTTPS_PROXY, c'est ca la
# cause, et l'utilisateur doit le savoir.
_CLI_ENV_RECOVERED = {"vars": []}

# v1.14.6 - vrai des qu'un appel a du se replier sur la version nue. Sert a
# signaler que les options optionnelles sont hors service sur ce CLI : sans
# ca le repli serait invisible et l'app tournerait degradee en silence.
#
# v1.14.12 - `rung` memorise le barreau de l'echelle de repli (cf.
# _CLI_RUNGS) auquel les prochains appels commencent : une forme qui vient
# de caler n'est pas rejouee au prochain skill. L'etat vaut pour la duree du
# process, comme avant.
_CLI_FALLBACK = {"used": False, "rung": 0}

# v1.14.9 - repertoire de travail STABLE pour les appels au CLI.
#
# Chaque appel tournait dans un dossier temporaire neuf. Le CLI traite le
# repertoire courant comme le projet courant : un chemin jamais vu, a chaque
# fois, sous /var/folders. Un repertoire fixe, cree une fois, lui donne un
# projet connu et constant -- et n'a aucun des effets d'un dossier inconnu.
CLI_WORKDIR = HOME / ".claude/claude-control-cli-workdir"


def _cli_workdir():
    """Retourne le cwd des appels CLI, cree si besoin."""
    try:
        CLI_WORKDIR.mkdir(parents=True, exist_ok=True)
        return str(CLI_WORKDIR)
    except OSError:
        return str(HOME)


# v1.14.14 - le CLI traite son cwd comme un projet, et un projet JAMAIS
# approuve peut declencher la question de confiance ("Do you trust the files
# in this folder?") -- observee mot pour mot dans une sortie partielle a
# l'epoque de v1.14.3. Posee via /dev/tty par un process sans terminal, la
# question ne s'affiche nulle part et n'obtient jamais de reponse : gel
# silencieux, zero octet ecrit -- le symptome exact. L'epoque ou "ca
# marchait" (<= v1.13.4) n'imposait aucun cwd : le CLI heritait d'un dossier
# deja connu. Depuis v1.14.1/v1.14.9, il tourne dans un dossier cree par
# l'app, que personne n'a jamais approuve.
#
# On ecrit donc dans ~/.claude.json, AVANT le premier appel, l'entree que le
# CLI enregistre lui-meme quand on repond oui a la question. Best-effort :
# fichier absent (CLI jamais lance) ou structure inattendue -> on ne touche
# a rien. Une seule ecriture par vie du process, et aucune si l'entree est
# deja la -- pour ne pas jouer des coudes avec les ecritures du CLI.
_CLI_WORKDIR_TRUSTED = {"done": False}


def _pretrust_cli_workdir():
    if _CLI_WORKDIR_TRUSTED["done"]:
        return
    _CLI_WORKDIR_TRUSTED["done"] = True
    try:
        if not CLI_CONFIG_PATH.exists():
            return
        data = json.loads(CLI_CONFIG_PATH.read_text(errors="replace") or "{}")
        if not isinstance(data, dict):
            return
        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            return
        wd = _cli_workdir()
        entry = projects.get(wd)
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted"):
            return
        entry = entry if isinstance(entry, dict) else {}
        entry["hasTrustDialogAccepted"] = True
        entry["hasCompletedProjectOnboarding"] = True
        projects[wd] = entry
        _atomic_write_json(CLI_CONFIG_PATH, data)
        _log(f"_pretrust_cli_workdir: {wd} approuve dans {CLI_CONFIG_PATH.name}")
    except Exception as e:
        _log(f"_pretrust_cli_workdir: ignore ({type(e).__name__}: {e})")


def _login_shell_env(timeout=8):
    """v1.14.4 - Environnement du shell de connexion de l'utilisateur.

    Resultat mis en cache : un seul lancement de shell par process.

    `-lic` lit .zprofile ET .zshrc -- la plupart des exports proxy vivent
    dans le second, que seul un shell interactif charge. Mais un shell
    interactif peut lui-meme bloquer sur une invite : stdin est ferme et le
    timeout est court, on ne remplace pas un hang par un autre. En cas
    d'echec on retombe sur `-lc`, puis sur rien du tout.
    """
    with _SHELL_ENV_LOCK:
        if _SHELL_ENV_CACHE["done"]:
            return _SHELL_ENV_CACHE["env"]
        env = {}
        shell = os.environ.get("SHELL") or "/bin/zsh"
        for flags in ("-lic", "-lc"):
            try:
                r = subprocess.run([shell, flags, "env"], capture_output=True,
                                   text=True, timeout=timeout,
                                   stdin=subprocess.DEVNULL)
            except Exception:
                continue
            if r.returncode != 0 or not r.stdout:
                continue
            for line in r.stdout.splitlines():
                k, sep, v = line.partition("=")
                if sep and k and k.isidentifier():
                    env[k] = v
            if env:
                break
        _SHELL_ENV_CACHE["env"] = env
        _SHELL_ENV_CACHE["done"] = True
        return env


def _cli_env():
    """v1.13.1 / v1.14.3 / v1.14.4 - Environnement des subprocess `claude`.

    Augmente le PATH pour inclure les dirs ou vivent les binaires
    npm/node/etc. Necessaire quand Claude Control est lance depuis Finder
    (PATH minimal launchd) : sans ca le CLI claude echoue lui-meme avec
    'node: command not found'.

    v1.14.3 - extrait de _call_claude_cli pour que le diagnostic tourne dans
    exactement le meme environnement que l'appel reel. Un diagnostic qui
    teste un autre PATH que celui du vrai appel ne prouve rien.

    v1.14.4 - le PATH n'etait que la moitie du probleme. Le meme launchd
    minimal prive aussi le CLI des variables reseau/auth du shell : il
    demarre, trouve node, puis bloque a l'aller-retour API sans rien ecrire
    -- exactement le symptome observe (`--version` instantane, ping mort a
    60 s). On rapatrie les variables manquantes depuis le shell de connexion,
    sans jamais ecraser celles deja presentes.
    """
    env = os.environ.copy()
    extra = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(HOME / ".npm-global/bin"),
        str(HOME / ".local/bin"),
        str(HOME / ".bun/bin"),
        str(HOME / ".volta/bin"),
    ]
    # v1.14.11 - tri par NUMERO de version, pas par nom.
    #
    # `sorted(..., reverse=True)` compare des chaines : "v9.11.2" passe avant
    # "v22.1.0", et "v8.17.0" avant "v18.20.0". L'app prependait donc au PATH
    # du CLI le node le plus ANCIEN installe -- un node 8 ou 9 de 2017, sur
    # lequel le CLI Claude Code ne peut pas fonctionner. Sur un Mac Apple
    # Silicon migre depuis un Intel, ces vieux builds sont x86_64 : macOS
    # reclame Rosetta, et le lancement attend une boite de dialogue que
    # personne ne verra jamais.
    nvm_dir = HOME / ".nvm/versions/node"
    if nvm_dir.is_dir():
        try:
            extra += [str(v / "bin")
                      for v in sorted(nvm_dir.iterdir(), key=_node_version_key,
                                      reverse=True)]
        except Exception:
            pass

    # v1.14.11 - les chemins devines passent APRES le PATH existant.
    #
    # Ils etaient prepends : les suppositions de l'app battaient donc le PATH
    # de l'utilisateur, et le CLI tournait sur un autre node que celui de son
    # terminal. Or ces chemins n'existent que pour le cas launchd, ou le PATH
    # est quasi vide -- comme repli, jamais comme priorite. Ce qui marche
    # dans le terminal de l'utilisateur doit marcher dans l'app.
    #
    # v1.14.12 - entre les deux : le PATH du shell de connexion, dans SON
    # ordre. Depuis le Finder le PATH du process est quasi vide, donc les
    # chemins devines decidaient seuls -- brew avant nvm, un ordre arbitraire
    # qui n'est pas celui du terminal de l'utilisateur. Le CLI npm est un
    # script `#!/usr/bin/env node` : c'est ce PATH qui choisit le node qui
    # l'execute. En inserant les entrees du shell avant les suppositions, le
    # subprocess resout `claude` ET `node` exactement comme le terminal ; les
    # chemins devines ne tranchent plus que si le shell n'a rien donne.
    cur = env.get("PATH", "")
    shell_dirs = [d for d in (_login_shell_env().get("PATH") or "").split(":")
                  if d]
    merged, seen = [], set()
    for p in cur.split(":") + shell_dirs + extra:
        if p and p not in seen:
            merged.append(p)
            seen.add(p)
    if merged:
        env["PATH"] = ":".join(merged)

    # v1.14.4 - meme cause que le PATH, autre consequence : sans les
    # variables reseau/auth du shell, le CLI demarre puis bloque a
    # l'aller-retour API. On ne remplit que ce qui manque -- une variable
    # deja definie dans l'environnement de l'app fait autorite.
    missing = [k for k in _CLI_ENV_INHERIT if not env.get(k)]
    recovered = []
    if missing:
        shell_env = _login_shell_env()
        for k in missing:
            v = shell_env.get(k)
            if v:
                env[k] = v
                recovered.append(k)
    _CLI_ENV_RECOVERED["vars"] = recovered

    # v1.14.9 - retirer les marqueurs de session Claude Code parente.
    #
    # Le diagnostic a etabli que depuis l'app, api.anthropic.com repond
    # (HTTP 401), le binaire est sain, et pourtant meme l'appel NU cale --
    # alors que ce meme appel repond dans le terminal de l'utilisateur. Il ne
    # reste que ce que l'app transmet.
    #
    # Si Claude Control a ete demarre depuis une session Claude Code, son
    # environnement porte CLAUDECODE=1 et les CLAUDE_CODE_* de cette session.
    # Le CLI enfant les herite et ne se comporte plus comme un appel
    # autonome. Un terminal ordinaire ne les a pas : d'ou "marche chez moi,
    # cale depuis l'app".
    for key in [k for k in env
                if k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_")]:
        del env[key]

    # v1.14.14 - l'auto-updater du CLI est coupe pour les appels de l'app.
    #
    # Diagnostic reel (rapport machine, CLI 2.1.173 fige depuis des
    # semaines) : jeton ok, reseau ok, node ok, et un journal --debug VIDE
    # -- le CLI gele avant meme sa premiere ligne de log. Ce qui tourne
    # avant le journal, c'est l'updater : coince (verrou, installation
    # partielle), il gele chaque appel -p pendant que --version repond.
    # Un appel de generation n'a aucune raison de declencher une mise a
    # jour ; la sante du CLI appartient a l'utilisateur et au diagnostic.
    # Pose APRES le retrait des CLAUDE_CODE_* ci-dessus : ces deux-la sont
    # NOTRE politique pour NOS subprocess, pas un heritage de session.
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


CLI_DIAG_PING_TIMEOUT = 60
CLI_DIAG_PROBE_TIMEOUT = 45

# v1.14.7 - budget accorde a l'appel optimise. Court par construction : sur
# une machine ou il fonctionne, il repond en une dizaine de secondes ; s'il
# depasse ca, il ne repondra pas, et l'attente ne doit pas etre payee sur le
# budget de l'appel prouve.
CLI_FAST_PATH_TIMEOUT = 30
# Plancher de la bissection : un appel sain repond en une poignee de
# secondes, mais un demarrage a froid merite une marge.
_CLI_DIAG_PROBE_TIMEOUT_MIN = 20

# v1.14.4 - flags de l'appel reel, isoles pour que le diagnostic puisse les
# RETIRER. Le CLI Claude Code se met a jour tout seul : un flag valide hier
# peut changer de semantique aujourd'hui. Un appel qui marchait la veille se
# met alors a bloquer sans rien ecrire, pendant que `--version` reste
# instantane -- et rien dans l'app ne permettait de le voir.
#
# `_CLI_CORE_FLAGS` est le minimum sans lequel l'appel n'a plus de sens
# (mode print, sortie texte). Les autres sont des optimisations : chacune
# peut etre retiree pour un test, ce qui rend le coupable identifiable.
_CLI_CORE_FLAGS = ("-p", "--output-format", "text")

# v1.14.6 - `--safe-mode` retire : c'est LUI qui bloquait.
#
# Bissection sur la machine concernee (CLI 2.1.173) : l'appel nu repond,
# `--safe-mode` ne rend jamais la main et n'ecrit rien. Il avait ete ajoute
# en v1.14.1 pour une seule raison utile -- ne pas demarrer les vingt
# serveurs MCP de l'utilisateur (Serena et sa fenetre de navigateur
# comprises) avant de generer une phrase.
#
# `--strict-mcp-config` avec une config vide fait exactement ce travail, et
# rien d'autre : aucun serveur MCP n'est charge, le reste du demarrage n'est
# pas touche. Verifie en reel, meme reponse et meme temps que `--safe-mode`
# la ou ce dernier fonctionne encore.
#
# Groupes et non chaines plates : une option qui porte une valeur doit etre
# retiree ou ajoutee avec elle, sinon la bissection produirait un
# `--mcp-config` orphelin.
_CLI_MCP_OFF = ("--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}')
_CLI_OPTIONAL_FLAGS = (
    _CLI_MCP_OFF,
    ("--no-session-persistence",),
    ("--disable-slash-commands",),
)

# v1.14.12 - l'echelle de repli, du plus optimise au plus sur.
#
# Le repli v1.14.6 jetait TOUTES les options d'un coup : l'appel nu recharge
# alors la config MCP complete de l'utilisateur -- sur une machine a vingt
# serveurs, c'est reintroduire dans le chemin de secours exactement la
# lenteur que --strict-mcp-config existe pour eviter, et une reparation en
# lot la paie a chaque skill (jusqu'a re-timeout des que la config grossit).
# Le barreau intermediaire ne conserve que la neutralisation des MCP : si
# c'est une AUTRE option qui est devenue toxique, la reparation reste
# rapide. L'appel nu reste le filet final.
_CLI_RUNGS = (
    None,             # 0 - toutes les options optionnelles
    (_CLI_MCP_OFF,),  # 1 - seulement les MCP coupes
    (),               # 2 - appel nu
)


def _cli_cmd(cli_path, optional=None, system_prompt=None):
    """v1.14.4 / v1.14.6 - Construit la commande. `optional=()` donne
    l'appel nu ; sinon une sequence de groupes d'options."""
    flags = list(_CLI_CORE_FLAGS)
    for group in (_CLI_OPTIONAL_FLAGS if optional is None else optional):
        flags += list(group)
    cmd = [cli_path] + flags
    if system_prompt:
        cmd += ["--system-prompt", system_prompt]
    return cmd


API_REACHABILITY_URL = "https://api.anthropic.com/v1/models"


def _check_api_reachable(env=None, timeout=12):
    """v1.14.4 - api.anthropic.com est-elle joignable DEPUIS L'APP ?

    Le ping CLI qui echoue dit que l'aller-retour n'aboutit pas, jamais
    pourquoi. Ce test separe les deux seules causes possibles : soit la
    requete ne sort pas de la machine (reseau, proxy, CA), soit elle sort et
    c'est le CLI qui bloque en aval (authentification).

    Un 401 est un SUCCES ici : on n'envoie aucune clef, et une reponse
    d'erreur de l'API prouve qu'on l'a jointe. Seule l'absence de reponse
    compte comme un echec.
    """
    env = _cli_env() if env is None else env
    proxies = {}
    for scheme in ("http", "https"):
        v = env.get(scheme.upper() + "_PROXY") or env.get(scheme + "_proxy")
        if v:
            proxies[scheme] = v
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(proxies))
    try:
        with opener.open(API_REACHABILITY_URL, timeout=timeout) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return True, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# v1.14.13 - nom du coffre Keychain du CLI Claude Code sur macOS. Si le CLI
# d'une version future range son jeton ailleurs, la sonde repond "missing" --
# un constat neutre, jamais un verdict d'authentification cassee.
CLI_KEYCHAIN_SERVICE = "Claude Code-credentials"


def _check_cli_credentials(timeout=6):
    """v1.14.13 - Le jeton du CLI est-il accessible DEPUIS L'APP ?

    Toutes les preuves accumulees depuis v1.14.3 dessinent le meme trou :
    binaire sain, reseau sain (HTTP 401), et un CLI qui cale AVANT d'ecrire
    le moindre octet -- alors qu'il repond dans le terminal. Ce qui se passe
    avant le premier octet, c'est le chargement du jeton. Sur macOS il vit
    dans le Keychain, et une ACL Keychain est liee au BINAIRE qui accede :
    un node different de celui du terminal (le defaut v1.14.11/12) suffit a
    declencher une demande d'autorisation -- parfois invisible depuis un
    process lance par launchd. Aucune version n'avait teste ce maillon.

    Retourne (status, detail) :
      - "ok"      : jeton lisible (le SECRET N'EST JAMAIS conserve ni logge)
      - "blocked" : le Keychain ne repond pas -> autorisation en attente,
                    c'est le gel observe
      - "missing" : pas d'item sous ce nom (autre emplacement possible)
      - "file"    : jeton en fichier (.credentials.json), Keychain hors jeu
      - "skipped" / "error"
    """
    cfg_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (HOME / ".claude"))
    try:
        if (cfg_dir / ".credentials.json").is_file():
            return "file", ".credentials.json present - le Keychain n'est pas en jeu"
    except OSError:
        pass
    if sys.platform != "darwin":
        return "skipped", "pas de Keychain hors macOS"
    started = time.monotonic()
    try:
        r = subprocess.run(
            ["security", "find-generic-password",
             "-s", CLI_KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "blocked", (
            f"le Keychain n'a pas rendu le jeton en {timeout} s : macOS "
            "attend une autorisation que personne ne voit. C'est tres "
            "probablement LE blocage. Correctif : ouvre Trousseaux d'acces, "
            "ou lance une reparation via le Terminal (bouton dans l'app).")
    except FileNotFoundError:
        return "error", "outil `security` introuvable"
    except Exception as e:
        return "error", f"{type(e).__name__}: {e}"
    # r.stdout contient le jeton : on ne le lit pas, on ne le garde pas.
    if r.returncode == 0:
        # v1.14.18 - "accessible" ne veut pas dire "valide" : une session
        # expiree rend un jeton parfaitement lisible et inutilisable. La
        # validite se lit dans le journal --debug ("login expired"), pas ici.
        return "ok", (f"jeton accessible en {time.monotonic() - started:.1f} s "
                      "(existence, pas validité)")
    if r.returncode == 44:
        return "missing", (f"aucun item '{CLI_KEYCHAIN_SERVICE}' dans le "
                           "Keychain (cette version du CLI le range peut-etre "
                           "ailleurs) - constat neutre")
    return "error", f"security exit {r.returncode}: {(r.stderr or '').strip()[:120]}"


def _check_cli_node():
    """v1.14.13 - Quel node executerait le CLI, et repond-il ?

    Le CLI npm est un script `#!/usr/bin/env node` : le node resolu par le
    PATH du subprocess est celui qui compte. Un node x86_64 sur un Mac arm64
    passe par Rosetta ; si Rosetta n'est pas installee, macOS attend une
    boite de dialogue et le lancement ne rend jamais la main -- un
    `node --version` qui ne repond pas en 6 s EST le verdict.
    """
    env = _cli_env()
    node = shutil.which("node", path=env.get("PATH"))
    out = {"node_path": node, "node_version": None, "node_warning": None}
    if not node:
        out["node_warning"] = ("aucun `node` dans le PATH du subprocess - "
                               "un CLI npm ne peut pas demarrer")
        return out
    try:
        r = subprocess.run([node, "--version"], capture_output=True,
                           text=True, timeout=6, env=env)
        out["node_version"] = (r.stdout or r.stderr or "").strip()[:40] or None
    except subprocess.TimeoutExpired:
        out["node_version"] = None
        out["node_warning"] = ("`node --version` ne repond pas en 6 s : "
                               "binaire gele (Rosetta manquante ?) - c'est "
                               "tres probablement LE blocage")
        return out
    except Exception as e:
        out["node_version"] = f"{type(e).__name__}"
        return out
    if sys.platform == "darwin":
        try:
            arch = subprocess.run(["uname", "-m"], capture_output=True,
                                  text=True, timeout=5)
            kind = subprocess.run(["file", "-b", node], capture_output=True,
                                  text=True, timeout=5)
            if ("arm64" in (arch.stdout or "")
                    and "x86_64" in (kind.stdout or "")
                    and "arm64" not in (kind.stdout or "")):
                out["node_warning"] = ("node x86_64 sur Mac arm64 : execution "
                                       "via Rosetta - lent, et gel garanti si "
                                       "Rosetta n'est pas installee")
        except Exception:
            pass
    return out


def _cli_probe(optional, timeout=CLI_DIAG_PROBE_TIMEOUT):
    """v1.14.4 - Un aller-retour reel avec un jeu de flags donne.

    Meme binaire, meme env, meme cwd que l'appel de production : seuls les
    flags varient. C'est ce qui permet d'attribuer la panne a un flag plutot
    qu'a l'environnement.
    """
    cli = _claude_cli_path()
    if not cli:
        return {"ok": False, "seconds": None, "error": "claude introuvable"}
    # v1.14.7 - la sonde nue doit l'etre VRAIMENT. Elle gardait
    # `--system-prompt`, une option ajoutee par le meme commit que
    # `--safe-mode` et jamais validee sur le CLI concerne : si c'est elle qui
    # bloque, la sonde "appel nu" calait aussi et le diagnostic concluait
    # "authentification" a tort.
    guidance = "Reply with exactly one word. No preamble."
    bare = optional is not None and len(tuple(optional)) == 0
    cmd = _cli_cmd(cli, optional=optional,
                   system_prompt=None if bare else guidance)
    payload = "Reply with the single word: pong"
    if bare:
        payload = f"{guidance}\n\n---\n\n{payload}"
    _pretrust_cli_workdir()
    started = time.monotonic()
    try:
        r = subprocess.run(cmd, input=payload,
                           capture_output=True, text=True,
                           timeout=timeout, env=_cli_env(),
                           cwd=_cli_workdir(), start_new_session=True)
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "seconds": round(time.monotonic() - started, 1),
                "error": f"timeout {timeout}s",
                "partial": _decode_partial(e.stdout) + _decode_partial(e.stderr)}
    except Exception as e:
        return {"ok": False, "seconds": round(time.monotonic() - started, 1),
                "error": f"{type(e).__name__}: {e}"}
    secs = round(time.monotonic() - started, 1)
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        return {"ok": False, "seconds": secs,
                "error": f"exit {r.returncode}: {(r.stderr or '').strip()[:200]}"}
    return {"ok": True, "seconds": secs, "reply": out[:120]}


def _blame_cli_flag(reference_seconds=None):
    """v1.14.4 - Quel flag fait caler l'appel ?

    Appele uniquement quand l'appel complet echoue ET que l'appel nu passe :
    a ce stade la panne est forcement dans les flags optionnels. On les
    ajoute un par un et on s'arrete au premier qui bloque -- le cas courant
    coute une seule sonde.

    Le budget par sonde est deduit de l'appel nu qui vient de reussir : s'il
    a repondu en 7 s, un flag qui depasse 28 s ne repondra pas. Attendre 45 s
    de plus par flag n'apprendrait rien et ferait patienter l'utilisateur
    plusieurs minutes devant un ecran fixe.
    """
    budget = _CLI_DIAG_PROBE_TIMEOUT_MIN if reference_seconds is None else \
        max(_CLI_DIAG_PROBE_TIMEOUT_MIN,
            min(CLI_DIAG_PROBE_TIMEOUT, round(reference_seconds * 4)))
    for group in _CLI_OPTIONAL_FLAGS:
        if not _cli_probe((group,), timeout=budget)["ok"]:
            return " ".join(group)
    return None


def _diagnose_claude_cli():
    """v1.14.14 - Enveloppe publique : ajoute la version de l'app et TRACE le
    diagnostic complet dans le log fichier. Jusqu'ici le verdict ne vivait
    que dans l'UI : impossible de le retrouver apres coup, et l'utilisateur
    devait le retranscrire a la main pour le partager."""
    out = _diagnose_claude_cli_stages()
    out["app_version"] = get_local_version()
    try:
        _log("cli-diagnose: " + json.dumps(out, ensure_ascii=False)[:2000])
    except Exception:
        pass
    return out


def _diagnose_claude_cli_stages():
    """v1.9.4 / v1.14.3 - Diagnostic du CLI Claude Code.

    v1.14.3 - le diagnostic ne lancait que `claude --version`, qui teste le
    binaire local et ne parle jamais a l'API. Sur la panne qu'il etait
    justement cense expliquer -- le CLI qui ne repond pas en 120 s -- il
    repondait "available: YES, version 2.x" et n'apprenait rien a personne.

    Il fait maintenant un vrai aller-retour, avec le binaire, les flags,
    l'environnement et le cwd EXACTS de _call_claude_cli : c'est la seule
    facon de reproduire la panne. Trois etages, du moins cher au plus cher,
    et on s'arrete au premier qui echoue :

      1. le binaire est-il dans le PATH ?
      2. repond-il a --version (process local, 10 s) ?
      3. repond-il a un vrai prompt trivial (aller-retour API) ?

    L'etage 3 est celui qui compte : s'il passe alors que la reparation
    echoue, le CLI va bien et c'est le contenu envoye qui cale. S'il echoue,
    la panne est dans le CLI ou son authentification, et la sortie partielle
    dit sur quoi il bloque.

    v1.14.13 - deux sondes AVANT le ping, rapides et decisives, sur les deux
    maillons que huit versions de correctifs n'avaient jamais testes : le
    node qui execute le script CLI (Rosetta ?), et l'acces au jeton Keychain
    depuis l'app (ACL liee au binaire). Si l'une des deux rend un verdict de
    gel, on repond en quelques secondes au lieu de faire patienter 60 s de
    ping voue a caler sur la meme cause. Et quand tout le reste a echoue
    sans verdict, une derniere sonde `--debug` capture le journal de
    demarrage du CLI : la ou il s'arrete d'ecrire EST l'etape qui bloque.
    """
    cli = _claude_cli_path()
    out = {"available": False, "path": cli or None, "version": None,
           "ping_ok": False, "ping_seconds": None, "ping_reply": None,
           "partial": None, "stage": "path", "error": None,
           # v1.14.4
           "network_ok": None, "network_detail": None,
           "minimal_ok": None, "minimal_seconds": None,
           "blamed_flag": None, "env_recovered": [],
           "not_logged_in": False,
           # v1.14.13
           "creds_status": None, "creds_detail": None,
           "node_path": None, "node_version": None, "node_warning": None,
           "debug_tail": None,
           # v1.14.15
           "latest_cli_version": None}
    if not cli:
        out["error"] = "claude introuvable dans le PATH"
        return out

    out["stage"] = "version"
    try:
        r = subprocess.run([cli, "--version"], capture_output=True, text=True,
                           timeout=10, env=_cli_env(), start_new_session=True)
        out["version"] = (r.stdout or "").strip() or (r.stderr or "").strip()
        if r.returncode != 0:
            out["error"] = (f"--version sort en {r.returncode} : "
                            f"{(r.stderr or '').strip()[:200]}")
            return out
    except subprocess.TimeoutExpired:
        out["error"] = "--version ne repond pas en 10 s (binaire bloque)"
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    out["available"] = True

    # v1.14.13 - le node d'abord (gratuit), le jeton ensuite (<= 6 s).
    out["stage"] = "node"
    node_info = _check_cli_node()
    out.update(node_info)
    if node_info.get("node_warning") and "ne repond pas" in node_info["node_warning"]:
        out["error"] = node_info["node_warning"]
        return out

    out["stage"] = "creds"
    out["creds_status"], out["creds_detail"] = _check_cli_credentials()
    if out["creds_status"] == "blocked":
        out["error"] = out["creds_detail"]
        return out

    out["stage"] = "ping"
    started = time.monotonic()
    try:
        reply = _call_claude_cli(
            "Reply with the single word: pong",
            timeout=CLI_DIAG_PING_TIMEOUT,
            system_prompt="Reply with exactly one word. No preamble.",
            # v1.14.6 - le diagnostic ne se rattrape pas : une sonde qui se
            # replie mesurerait autre chose que l'appel de production.
            allow_fallback=False)
        out["ping_seconds"] = round(time.monotonic() - started, 1)
        out["ping_ok"] = True
        out["ping_reply"] = reply[:120]
    except ClaudeCliTimeout as e:
        out["ping_seconds"] = round(time.monotonic() - started, 1)
        out["partial"] = e.partial or None
        out["error"] = str(e)
    except ClaudeCliNotLoggedIn as e:
        # Le CLI a repondu et dit lui-meme ce qui ne va pas : verdict acquis,
        # inutile de payer les sondes suivantes.
        out["ping_seconds"] = round(time.monotonic() - started, 1)
        out["not_logged_in"] = True
        out["error"] = str(e)
        return out
    except Exception as e:
        out["ping_seconds"] = round(time.monotonic() - started, 1)
        out["error"] = f"{type(e).__name__}: {e}"
    out["env_recovered"] = list(_CLI_ENV_RECOVERED["vars"])
    if out["ping_ok"]:
        return out

    # v1.14.4 - le ping echoue : jusqu'ici le diagnostic s'arretait ici, sur
    # "il ne repond pas", ce qui est le point de depart du probleme et non
    # une reponse. Deux sondes de plus separent les trois causes possibles.

    # 1. La requete sort-elle seulement de la machine ?
    out["stage"] = "network"
    out["network_ok"], out["network_detail"] = _check_api_reachable()
    if not out["network_ok"]:
        return out

    # v1.14.15 - le reseau est bon : combien de versions de retard a le
    # CLI ? Un binaire tres en retard dont l'updater est coince est LA
    # cause etablie sur la machine de reference ; le chiffre rend le
    # verdict indiscutable.
    out["latest_cli_version"] = _check_cli_freshness()

    # 2. Le reseau est bon : l'appel NU passe-t-il ? Si oui, la panne est
    #    dans les flags de l'app -- typiquement un flag dont une mise a jour
    #    du CLI a change la semantique, ce qui explique un appel qui
    #    fonctionnait la veille et bloque le lendemain.
    out["stage"] = "minimal"
    minimal = _cli_probe(())
    out["minimal_ok"] = minimal["ok"]
    out["minimal_seconds"] = minimal.get("seconds")
    if minimal["ok"]:
        out["stage"] = "blame"
        out["blamed_flag"] = _blame_cli_flag()
    else:
        # v1.14.13 - binaire sain, node sain, jeton lisible, reseau joignable,
        # appel nu qui cale quand meme : plus aucun etage fidele n'a de
        # verdict. Dernier recours : demander au CLI lui-meme ou il s'arrete.
        out["stage"] = "debug"
        out["debug_tail"] = _cli_debug_tail()
        # v1.14.18 - si le journal du CLI dit "login expired", le verdict est
        # la reconnexion, pas le binaire : la sonde jeton ne teste que
        # l'EXISTENCE du jeton, pas sa validite -- c'etait l'angle mort qui a
        # fait passer une session expiree pour un binaire en panne.
        if _looks_not_logged_in(out["debug_tail"]):
            out["not_logged_in"] = True
            out["error"] = _srv("diag_not_logged_in",
                                tail=out["debug_tail"][:160])
    return out


CLI_REGISTRY_URL = "https://registry.npmjs.org/@anthropic-ai/claude-code/latest"


def _check_cli_freshness(env=None, timeout=6):
    """v1.14.15 - Derniere version PUBLIEE du CLI, pour mesurer le retard.

    Cas reel : un binaire fige a 2.1.173 pendant que le public est a
    2.1.232 -- 59 versions de retard, updater coince. Sans ce chiffre, le
    diagnostic disait "version 2.1.173" comme un fait sain et l'utilisateur
    n'avait aucune raison de s'en mefier. Best-effort : hors ligne ou
    registre injoignable -> None, jamais d'erreur. Le numero npm suit le
    meme produit que l'installation native.
    """
    env = _cli_env() if env is None else env
    proxies = {}
    for scheme in ("http", "https"):
        v = env.get(scheme.upper() + "_PROXY") or env.get(scheme + "_proxy")
        if v:
            proxies[scheme] = v
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    try:
        with opener.open(CLI_REGISTRY_URL, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        v = str(data.get("version") or "").strip()
        return v or None
    except Exception:
        return None


# v1.14.16 - installeur officiel du CLI. Domaine epingle : on n'execute que
# le script d'installation d'Anthropic, jamais une URL venue d'une requete.
CLI_INSTALLER_CMD = "curl -fsSL https://claude.ai/install.sh | bash"


def update_claude_cli():
    """v1.14.16 - Met a jour le CLI Claude Code depuis l'app.

    Demande utilisateur directe apres le cas reel : binaire fige a 2.1.173
    (public a 2.1.232), updater interne coince, et une reinstallation
    manuelle dans le terminal comme seul remede -- contraire a la promesse
    de l'app ("one click, zero terminal"). Le bouton execute l'installeur
    OFFICIEL (meme commande que la doc Anthropic), verifie la version
    obtenue, puis rearme l'app : echelle de repli remise en haut (nouveau
    binaire = les options optimisees meritent un nouvel essai) et capture
    du shell invalidee (le PATH a pu changer).

    Ne touche pas Claude Desktop : seul l'outil `claude` du terminal est
    reinstalle.
    """
    before = None
    cli = _claude_cli_path()
    if cli:
        try:
            r = subprocess.run([cli, "--version"], capture_output=True,
                               text=True, timeout=10, env=_cli_env(),
                               start_new_session=True)
            before = (r.stdout or r.stderr or "").strip()[:60] or None
        except Exception:
            before = None
    try:
        r = subprocess.run(["/bin/bash", "-c", CLI_INSTALLER_CMD],
                           capture_output=True, text=True, timeout=300,
                           env=_cli_env(), start_new_session=True)
    except subprocess.TimeoutExpired:
        return False, ("L'installeur n'a pas fini en 5 min (reseau lent ou "
                       "bloque). Réessaie, ou lance la commande dans un "
                       f"terminal : {CLI_INSTALLER_CMD}")
    except Exception as e:
        return False, f"Installation impossible : {type(e).__name__}: {e}"
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip()[-300:]
        return False, f"L'installeur a echoue (exit {r.returncode}) : {tail}"
    # Nouveau binaire : la capture de shell peut etre perimee (PATH modifie
    # par l'installeur) et l'echelle de repli repart du haut.
    with _SHELL_ENV_LOCK:
        _SHELL_ENV_CACHE["done"] = False
        _SHELL_ENV_CACHE["env"] = {}
    _CLI_FALLBACK.update({"used": False, "rung": 0})
    cli = _claude_cli_path()
    if not cli:
        return False, ("Installation terminee mais `claude` reste introuvable "
                       "-- ouvre un terminal et verifie `which claude`.")
    try:
        r = subprocess.run([cli, "--version"], capture_output=True, text=True,
                           timeout=10, env=_cli_env(), start_new_session=True)
        after = (r.stdout or r.stderr or "").strip()[:60] or "?"
    except Exception as e:
        return False, (f"Installe, mais `claude --version` ne repond pas "
                       f"({type(e).__name__}) : le binaire est peut-etre "
                       f"encore en cours d'ecriture, reessaie dans un instant.")
    if before and after == before:
        return False, (f"L'installeur a tourne mais la version n'a pas bouge "
                       f"({after}). Un autre binaire masque peut-etre le "
                       f"nouveau : verifie `which claude` dans un terminal.")
    _log(f"update_claude_cli: {before or '?'} -> {after} ({cli})")
    return True, (f"CLI mis a jour : {before or 'version inconnue'} → {after}. "
                  f"Relance la reparation des skills.")


# v1.14.18 - auto-reparation du CLI, branchee dans le flux de reparation.
#
# Les versions 1.14.15/16/17 diagnostiquaient la panne (binaire perime dont
# l'updater est coince) et offraient des boutons -- mais l'utilisateur, lui,
# clique "Suggerer via Claude Code" et attend que CA marche. Alors quand un
# appel timeout ET que la signature etablie est confirmee (version installee
# differente de la derniere publiee), l'app execute la mise a jour
# elle-meme et reessaie. Une seule tentative par vie de process : si la
# mise a jour ne repare pas, on ne boucle pas. Le consentement est celui du
# proprietaire de l'app, donne explicitement ("l'app devrait checker la
# derniere version du cli et l'installer").
# "armed" n'est vrai que dans le VRAI serveur (pose par main()) : la suite
# de tests, qui appelle les memes fonctions directement, ne peut jamais
# declencher un vrai installeur sur la machine d'un developpeur -- une
# invariante de construction, pas une garde ambiante (cf. la flake v1.14.17
# sur tests/__init__ qui ne s'execute pas sous `unittest discover`).
_CLI_AUTO_HEAL = {"attempted": False, "armed": False}


def _cli_auto_heal_after_timeout():
    """Retourne (healed, detail). healed=True => CLI remplace, on peut
    reessayer l'appel. detail porte le message a montrer a l'utilisateur."""
    if not _CLI_AUTO_HEAL["armed"] or _CLI_AUTO_HEAL["attempted"]:
        return False, None
    _CLI_AUTO_HEAL["attempted"] = True
    latest = _check_cli_freshness()
    installed = None
    cli = _claude_cli_path()
    if cli:
        try:
            r = subprocess.run([cli, "--version"], capture_output=True,
                               text=True, timeout=10, env=_cli_env(),
                               start_new_session=True)
            installed = (r.stdout or r.stderr or "").strip()
        except Exception:
            installed = None
    if not latest or not installed or latest in installed:
        # Pas la signature etablie (versions inconnues ou deja a jour) : on
        # ne remplace pas un binaire sans preuve qu'il est en retard.
        _log(f"auto-heal: signature absente (installed={installed!r} "
             f"latest={latest!r}), pas de mise a jour")
        return False, None
    _log(f"auto-heal: CLI perime detecte ({installed} vs {latest}), "
         f"mise a jour automatique")
    ok, msg = update_claude_cli()
    if ok:
        return True, msg
    _log(f"auto-heal: echec de la mise a jour : {msg}")
    return False, msg


# v1.14.19 - "si c'est la cause, l'app devrait me le demander" (demande
# utilisateur). Le jeton OAuth du CLI porte sa date d'expiration : la lire
# coute zero appel API et zero reseau. On extrait UNIQUEMENT expiresAt et on
# jette le reste ; le secret n'est jamais conserve ni logge. Une expiration
# depassee est un INDICE fort (le refresh peut theoriquement encore sauver
# la session), donc l'app le demande -- elle ne l'affirme qu'apres l'avoir
# constate sur un vrai appel (v1.14.18).
_CLI_SESSION = {"status": "unknown", "detail": None, "checked_ts": 0.0,
                "days": None, "hours": None}
_CLI_SESSION_LOCK = threading.Lock()
_CLI_SESSION_TTL = 300
# Marge sur l'expiration : un jeton perime de quelques minutes se rafraichit
# souvent tout seul au prochain appel. Au-dela d'une heure, la session est
# morte en pratique (cas reel : expiree depuis des semaines).
_CLI_SESSION_EXPIRY_GRACE = 3600
# last_ts None = jamais montre. PAS 0.0 : time.monotonic() compte depuis le
# boot de la machine, et sur un Mac redemarre depuis moins de 30 min,
# `monotonic - 0 < cooldown` aurait silencieusement supprime le dialogue --
# le piege exact deja corrige dans les tests v1.14.17, retrouve ici en
# production par le test de cette version.
_RELOGIN_DIALOG = {"last_ts": None, "armed": False}
_RELOGIN_DIALOG_COOLDOWN = 1800


def _read_cli_oauth_expiry():
    """Retourne expiresAt (secondes epoch) ou None. Ne conserve JAMAIS le
    contenu des credentials au-dela de l'extraction de ce seul nombre."""
    raw = None
    cfg_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (HOME / ".claude"))
    cred_file = cfg_dir / ".credentials.json"
    try:
        if cred_file.is_file():
            raw = cred_file.read_text(errors="replace")
    except OSError:
        raw = None
    if raw is None and sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["security", "find-generic-password",
                 "-s", CLI_KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=6)
            if r.returncode == 0:
                raw = r.stdout
        except Exception:
            raw = None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    finally:
        del raw
    try:
        exp = (data.get("claudeAiOauth") or {}).get("expiresAt")
        if exp is None:
            return None
        exp = float(exp)
        # Les versions du CLI stockent des millisecondes.
        return exp / 1000.0 if exp > 4e10 else exp
    except Exception:
        return None
    finally:
        del data


def _check_cli_session(force=False):
    """Statut de session {status: expired|ok|unknown, detail}. Cache court."""
    now = time.time()
    with _CLI_SESSION_LOCK:
        if not force and now - _CLI_SESSION["checked_ts"] < _CLI_SESSION_TTL:
            return dict(_CLI_SESSION)
    exp = _read_cli_oauth_expiry()
    days = hours = None
    if exp is None:
        status, detail = "unknown", None
    elif now - exp > _CLI_SESSION_EXPIRY_GRACE:
        # v1.15.0 - jours/heures exposes en plus du texte : l'UI construit
        # la phrase dans SA langue (le detail francais reste pour les logs
        # et la boite de dialogue macOS).
        days = int((now - exp) // 86400)
        hours = int((now - exp) // 3600)
        since = f"depuis {days} jour(s)" if days else f"depuis {hours} h"
        status = "expired"
        detail = (f"Session Claude CLI expirée {since} — reconnecte-toi "
                  f"avec `claude /login`.")
    else:
        status, detail = "ok", None
    with _CLI_SESSION_LOCK:
        _CLI_SESSION.update({"status": status, "detail": detail,
                             "checked_ts": now, "days": days, "hours": hours})
        return dict(_CLI_SESSION)


def _ask_relogin_dialog(reason=None):
    """v1.14.19 - l'app DEMANDE la reconnexion : boite de dialogue macOS
    native avec un bouton qui ouvre Terminal pre-rempli de `claude /login`.
    Armee par main() seulement (jamais depuis les tests), au plus une fois
    par _RELOGIN_DIALOG_COOLDOWN pour ne pas harceler."""
    if not _RELOGIN_DIALOG["armed"]:
        return
    now = time.monotonic()
    last = _RELOGIN_DIALOG["last_ts"]
    if last is not None and now - last < _RELOGIN_DIALOG_COOLDOWN:
        return
    _RELOGIN_DIALOG["last_ts"] = now

    def _ask():
        msg = _osa_quote(
            (reason or "Ta session Claude CLI est expirée.")
            + " La réparation des skills ne peut pas fonctionner sans "
            "reconnexion.", 300)
        script = (
            f'display dialog "{msg}" '
            f'buttons {{"Plus tard", "Ouvrir le Terminal"}} '
            f'default button 2 with title "Claude Control" with icon caution')
        try:
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=120)
            if "Ouvrir le Terminal" in (r.stdout or ""):
                open_terminal_claude_login()
        except Exception:
            pass

    threading.Thread(target=_ask, name="claude-control-relogin-ask",
                     daemon=True).start()


def _cli_debug_tail(timeout=25):
    """v1.14.13 - Le journal de demarrage du CLI, par le CLI.

    `--debug` fait ecrire au CLI chaque etape de son demarrage sur stderr.
    Quand il cale sans rien dire, la DERNIERE ligne ecrite avant le timeout
    nomme l'etape ou il s'est arrete -- exactement l'information que huit
    versions de correctifs ont du deviner. Sonde ADDITIONNELLE, clairement
    etiquetee : elle ne remplace jamais les sondes fideles a la production
    (elle ajoute un flag), elle temoigne seulement.
    """
    cli = _claude_cli_path()
    if not cli:
        return None
    guidance = "Reply with exactly one word. No preamble."
    payload = f"{guidance}\n\n---\n\nReply with the single word: pong"
    cmd = [cli, "--debug"] + list(_CLI_CORE_FLAGS)
    _pretrust_cli_workdir()
    try:
        r = subprocess.run(cmd, input=payload, capture_output=True, text=True,
                           timeout=timeout, env=_cli_env(), cwd=_cli_workdir(),
                           start_new_session=True)
        tail = ((r.stderr or "") + "\n" + (r.stdout or "")).strip()
    except subprocess.TimeoutExpired as e:
        tail = (_decode_partial(e.stderr) + "\n" + _decode_partial(e.stdout)).strip()
        if not tail:
            # v1.14.14 - un --debug MUET est un verdict en soi, pas une
            # absence de donnee : le CLI gele avant d'ecrire la premiere
            # ligne de son journal. Ce qui tourne avant le journal, c'est
            # l'auto-updater / le bootstrap du binaire. Cas reel : CLI fige
            # a une version vieille de plusieurs semaines, updater coince.
            return ("AUCUNE sortie, meme en --debug : le CLI gele avant "
                    "d'ecrire son journal. Cause typique : binaire perime "
                    "dont l'auto-updater est coince. Correctif : reinstalle "
                    "le CLI (curl -fsSL https://claude.ai/install.sh | bash) "
                    "puis verifie `claude --version`.")
        tail = (tail + "\n[coupe au timeout de la sonde --debug : la "
                "derniere ligne ci-dessus est l'etape qui bloque]").strip()
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return tail[-800:] if tail else None


class ClaudeCliNotLoggedIn(Exception):
    """v1.9.5 - Cas specifique : le CLI Claude Code repond mais signale
    'Not logged in'. C'est un setup utilisateur (une seule fois) : il
    doit run 'claude /login' dans un terminal pour s'authentifier."""


# v1.14.18 - marqueurs d'authentification a re-faire, TOUTES formes
# confondues. Le cas resolu sur la machine de reference : session OAuth
# EXPIREE -- le CLI 2.1.173 ne sortait pas en erreur, il tentait de poser la
# question de re-authentification sur un tty que personne ne voyait, d'ou
# des semaines de "timeout" muets. Depuis que start_new_session (v1.14.14)
# lui interdit le tty, le message atterrit dans la sortie capturee :
# "Anthropic profile login expired · Re-authenticate your Anthropic
# profile". Un gel qui PORTE ce message n'est pas un timeout : c'est une
# reconnexion a faire.
_NOT_LOGGED_IN_MARKERS = (
    "not logged in", "please run /login", "please log in",
    "login expired", "re-authenticate", "profile login",
)


def _looks_not_logged_in(text):
    t = (text or "").lower()
    return any(m in t for m in _NOT_LOGGED_IN_MARKERS)


def _decode_partial(raw):
    """v1.14.3 - subprocess rend la sortie partielle d'un TimeoutExpired en
    bytes meme quand run() tourne en text=True. Sans decodage, le message
    d'erreur affichait b'...' a l'utilisateur."""
    if not raw:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return raw.strip()


class ClaudeCliTimeout(Exception):
    """v1.14.3 - Le CLI n'a pas repondu dans le temps imparti.

    Porte la sortie partielle : c'est elle qui distingue un CLI qui n'a
    jamais demarre d'un CLI bloque sur une invite interactive.
    """

    def __init__(self, timeout, partial=""):
        self.timeout = timeout
        self.partial = partial or ""
        msg = f"Timeout : le CLI Claude n'a pas répondu en {timeout} s."
        if self.partial:
            msg += f" Il avait commencé à écrire : « {self.partial[:300]} »"
        else:
            msg += " Il n'a rien écrit du tout avant de caler."
        super().__init__(msg)


def _call_claude_cli(prompt, timeout=120, system_prompt=None,
                     allow_fallback=True):
    """v1.9.3 / v1.9.4 / v1.9.5 / v1.14.1 / v1.14.6 - Invoque le CLI Claude
    Code en mode print.

    v1.14.1 - trois changements, chacun corrige une facon dont l'appel
    pouvait "ne pas repondre" :

    1. LE PROMPT PASSE PAR STDIN, plus par argv. `claude -p <prompt>` casse
       des que le prompt commence par un tiret : commander le prend pour une
       option et sort en `error: unknown option`. Un SKILL.md commence par
       `---` (frontmatter), donc des qu'on passe du contenu de skill
       directement, l'appel echoue avant meme de demarrer. Passer par stdin
       supprime aussi toute limite ARG_MAX.

    2. Pas de serveurs MCP. Le CLI lancait sinon TOUTE la config MCP de
       l'utilisateur avant de repondre -- sur une machine qui en a une
       vingtaine, le demarrage seul pouvait depasser le timeout. On genere
       une phrase de description : aucun de ces composants n'est utile.

       v1.14.1 obtenait ca avec `--safe-mode`. v1.14.6 le retire : sur CLI
       2.1.173, cette option ne rend jamais la main et n'ecrit rien -- c'est
       elle qui cassait la reparation. `--strict-mcp-config` avec une config
       vide fait le meme travail utile sans toucher au reste du demarrage.

    3. `--no-session-persistence` : sans ca, chaque suggestion ecrivait une
       session JSONL dans ~/.claude/projects -- le repertoire meme que
       get_skill_usage parcourt. L'app alimentait le volume qui la ralentit.

    `system_prompt` remplace le system prompt par defaut (--system-prompt),
    ce qui reduit le contexte et rend la consigne beaucoup plus fiable que
    de la prefixer au message utilisateur.
    """
    cli_path = _claude_cli_path()
    if not cli_path:
        raise FileNotFoundError("claude CLI not in PATH")
    env = _cli_env()
    _pretrust_cli_workdir()

    def _attempt(optional, budget):
        # v1.14.4 - flags isoles dans _cli_cmd : le diagnostic doit pouvoir
        # rejouer exactement cet appel en les retirant, sinon une option
        # devenue toxique reste indetectable.
        #
        # v1.14.7 - l'appel nu est NU. `--system-prompt` etait conservé par
        # le repli alors qu'il a ete ajoute par le meme commit que
        # `--safe-mode` et n'a jamais ete valide sur le CLI concerne : le
        # repli pouvait donc heriter de l'option qui bloque. La consigne
        # repasse dans le prompt, qui est le seul canal prouve.
        bare = optional is not None and len(tuple(optional)) == 0
        cmd = _cli_cmd(cli_path, optional=optional,
                       system_prompt=None if bare else system_prompt)
        payload = prompt
        if bare and system_prompt:
            payload = f"{system_prompt}\n\n---\n\n{prompt}"
        _log(f"_call_claude_cli: cli={cli_path} prompt_len={len(prompt)} "
             f"timeout={budget} options={'completes' if optional is None else 'nues'}")
        # v1.14.1 - cwd neutre. Sinon le CLI herite du cwd du serveur ('/'
        # quand Claude Control est lance depuis Finder) et traite ce
        # repertoire comme le projet courant.
        #
        # v1.14.14 - start_new_session : le subprocess n'a AUCUN terminal de
        # controle, meme quand l'app est lancee depuis un terminal de dev.
        # Toute question interactive que le CLI tenterait via /dev/tty
        # echoue alors immediatement au lieu d'attendre une reponse que
        # personne ne verra : un gel invisible devient une erreur visible.
        started = time.monotonic()
        try:
            res = subprocess.run(cmd, input=payload, capture_output=True,
                                 text=True, timeout=budget, env=env,
                                 cwd=_cli_workdir(), start_new_session=True)
        except subprocess.TimeoutExpired as e:
            # v1.14.3 - la sortie partielle est la seule trace de CE QUE le
            # CLI faisait quand il a cale, et on la jetait : l'utilisateur
            # voyait "n'a pas repondu en 120 s" sans jamais savoir pourquoi.
            # subprocess remplit e.stdout/e.stderr avec ce qui avait ete
            # ecrit avant le timeout (en bytes, meme avec text=True).
            partial = _decode_partial(e.stdout) + _decode_partial(e.stderr)
            _log(f"_call_claude_cli: TIMEOUT after {budget}s partial={partial[:400]!r}")
            # v1.14.18 - un gel dont la sortie partielle dit "login expired"
            # n'est PAS un timeout : le CLI attend une re-authentification
            # que personne ne peut donner ici. On le route vers l'UX de
            # reconnexion (claude /login) au lieu de l'echelle de repli --
            # redescendre les barreaux ne reconnecte personne.
            if _looks_not_logged_in(partial):
                raise ClaudeCliNotLoggedIn(
                    "Ta session Anthropic est expirée : le CLI attendait une "
                    "re-authentification au lieu de répondre. Ouvre un "
                    "terminal et lance : claude /login "
                    f"(message du CLI : « {partial[:160]} »)")
            raise ClaudeCliTimeout(budget, partial)
        _log(f"_call_claude_cli: exit={res.returncode} in "
             f"{time.monotonic() - started:.1f}s "
             f"out_len={len((res.stdout or '').strip())}")
        return res

    started_total = time.monotonic()
    # v1.14.6 / v1.14.7 / v1.14.12 - repli automatique le long de _CLI_RUNGS.
    #
    # `--safe-mode` a fait perdre cinq semaines : l'option bloquait le
    # CLI indefiniment, l'app rendait "n'a pas repondu en 120 s", et la
    # fonctionnalite etait morte sans recours. Une option qui devient
    # toxique -- mise a jour du CLI, version qui diverge -- ne doit pas
    # pouvoir remettre l'app dans ce cul-de-sac : les options sont des
    # optimisations, jamais des conditions de fonctionnement.
    #
    # v1.14.7 - budgets inverses : chaque barreau intermediaire n'a droit
    # qu'a un essai court, le dernier (l'appel nu, la seule forme garantie)
    # recoit tout le budget.
    #
    # v1.14.8 - un barreau qui a cale n'est pas rejoue : les appels suivants
    # partent du barreau memorise (_CLI_FALLBACK["rung"]). L'etat vaut pour
    # la duree du process : un redemarrage de l'app reprend tout en haut,
    # donc une mise a jour du CLI qui reparerait la situation est reprise
    # sans rien avoir a configurer.
    #
    # v1.14.12 - la descente couvre aussi l'echec DUR d'une option. Un CLI
    # mis a jour qui ne connait plus un flag sort en `unknown option` : ce
    # n'est pas un timeout, mais c'est la meme panne -- une option devenue
    # toxique -- et elle doit emprunter la meme echelle au lieu de tuer la
    # reparation avec un message qui accuse le prompt.
    #
    # Le repli est refuse au diagnostic (allow_fallback=False) : une sonde
    # qui se rattrape toute seule mesurerait autre chose que l'appel de
    # production et ne prouverait plus rien.
    if not allow_fallback:
        r = _attempt(None, timeout)
    else:
        start_rung = _CLI_FALLBACK["rung"]
        if start_rung:
            _log(f"_call_claude_cli: barreau {start_rung} deja connu comme "
                 f"point de depart (echec anterieur memorise)")
        last_rung = len(_CLI_RUNGS) - 1
        waited = 0.0
        partial = ""
        for rung in range(start_rung, len(_CLI_RUNGS)):
            budget = timeout if rung == last_rung \
                else min(CLI_FAST_PATH_TIMEOUT, timeout)
            try:
                r = _attempt(_CLI_RUNGS[rung], budget)
            except ClaudeCliTimeout as e:
                waited += budget
                partial = partial or e.partial
                _CLI_FALLBACK["used"] = True
                _CLI_FALLBACK["rung"] = min(rung + 1, last_rung)
                if rung == last_rung:
                    # Tous les barreaux ont cale : on annonce l'attente
                    # TOTALE. Remonter le budget d'un seul essai
                    # sous-declarerait ce que l'utilisateur a patiente.
                    raise ClaudeCliTimeout(round(waited), partial)
                _log(f"_call_claude_cli: timeout au barreau {rung}, "
                     f"descente au barreau {rung + 1}")
                continue
            combined_err = ((r.stdout or "") + " " + (r.stderr or "")).lower()
            if (r.returncode != 0 and rung != last_rung
                    and "unknown option" in combined_err):
                _CLI_FALLBACK["used"] = True
                _CLI_FALLBACK["rung"] = rung + 1
                _log(f"_call_claude_cli: option inconnue du CLI au barreau "
                     f"{rung}, descente au barreau {rung + 1}")
                continue
            break
    elapsed = time.monotonic() - started_total
    stdout = (r.stdout or "").strip()
    stderr = (r.stderr or "").strip()
    if r.returncode != 0:
        _log(f"_call_claude_cli: stdout={stdout[:300]!r} stderr={stderr[:300]!r}")
        combined = (stdout + " " + stderr).lower()
        # v1.9.5 / v1.14.18 - cas 'Not logged in' et 'login expired' detectes
        # par les memes marqueurs que la sortie partielle d'un gel.
        if _looks_not_logged_in(combined):
            raise ClaudeCliNotLoggedIn(
                "Claude Code CLI n'est pas authentifie sur ce systeme "
                "(ou sa session est expirée). "
                "Ouvre un terminal et lance la commande : claude /login"
            )
        # v1.14.1 - deux erreurs de demarrage tres bavardes cote CLI mais
        # opaques cote UI : on les nomme.
        #
        # v1.14.12 - le prompt passe par stdin depuis v1.14.1 : il ne peut
        # plus etre pris pour une option. Arriver ici avec `unknown option`
        # apres l'echelle de repli signifie que le CLI refuse une option de
        # BASE (-p / --output-format) : un CLI trop ancien pour l'app.
        if "unknown option" in combined:
            raise RuntimeError(
                f"Le CLI refuse une option de l'appel (exit {r.returncode}), "
                f"probablement parce qu'il est trop ancien. Mets-le a jour : "
                f"npm install -g @anthropic-ai/claude-code. "
                f"Detail : {(stderr or stdout)[:200]}"
            )
        if "credit balance" in combined or "rate limit" in combined or "quota" in combined:
            raise RuntimeError(
                f"Le CLI Claude a refuse la requete (quota / rate limit) : "
                f"{(stderr or stdout)[:200]}"
            )
        if not stdout and not stderr:
            raise RuntimeError(
                f"claude CLI exit {r.returncode} sans aucune sortie apres "
                f"{elapsed:.0f}s. Verifie l'authentification avec : claude /login"
            )
        details = []
        if stderr: details.append(f"stderr: {stderr[:300]}")
        if stdout: details.append(f"stdout: {stdout[:300]}")
        raise RuntimeError(f"claude CLI exit {r.returncode}. " + " | ".join(details))
    return stdout


# v1.14.1 - prompt reecrit.
#
# L'ancien demandait explicitement "a single sentence (40-150 chars)" et
# "Keep it under 150 chars". C'est la cause directe des skills a description
# trop courte : l'app demandait des descriptions courtes, puis _skill_quality
# les notait "excellent" des 30 caracteres. Les deux bouts poussaient dans le
# mauvais sens.
#
# Une description n'est pas un resume : c'est le SEUL signal qui decide si
# Claude declenche le skill. Elle doit contenir les mots que l'utilisateur
# va reellement employer. D'ou : ce que fait le skill + quand le declencher
# + les termes declencheurs concrets.
_SUGGEST_SYSTEM_PROMPT = (
    "You write the `description` field of a Claude Code SKILL.md frontmatter. "
    "That description is the ONLY signal Claude uses to decide whether to "
    "trigger the skill, so it must be discoverable, not merely accurate.\n\n"
    "Rules:\n"
    "- Say WHAT the skill does AND WHEN to trigger it.\n"
    "- Include the concrete words a user would actually type: synonyms, "
    "domain jargon, tool and product names, file extensions, error strings.\n"
    "- Aim for 200-450 characters, one to three sentences. Start with a verb.\n"
    "- End with an explicit list of trigger terms.\n"
    "- Describe ONLY capabilities present in the SKILL.md. Invent nothing.\n"
    "- Output ONLY the description text: no quotes, no preamble, no markdown, "
    "no key name, no explanation."
)


def _suggestion_prompts(name, body_max_chars=4000, lang=None):
    """v1.14.13 - Construit (system_prompt, prompt) pour la suggestion d'un
    skill. Extrait de suggest_skill_description pour que la reparation via
    le Terminal envoie EXACTEMENT les memes prompts que le chemin
    subprocess. Retourne (ok, dict|message)."""
    if not name:
        return False, "Nom de skill requis"
    target = _find_skill_dir(name)
    if not target:
        return False, f"Skill '{name}' introuvable"
    md = target / "SKILL.md"
    if md.exists():
        try:
            content = md.read_text(errors="replace")
        except Exception as e:
            return False, f"Erreur lecture : {e}"
    else:
        content = ""
    if len(content) > body_max_chars:
        content = content[:body_max_chars] + "\n\n[...truncated]"
    # v1.9.6 - directive de langue dans le system prompt si lang fourni.
    lang_directive = ""
    if lang == "fr":
        lang_directive = "\n\nIMPORTANT: Write the description in French."
    elif lang == "en":
        lang_directive = "\n\nIMPORTANT: Write the description in English."
    # v1.14.1 - la consigne part dans --system-prompt, le message utilisateur
    # ne porte plus que la matiere. Avant, tout etait concatene dans le
    # prompt utilisateur, ce qui diluait la consigne et rendait la sortie
    # beaucoup plus bavarde (d'ou l'heuristique de nettoyage).
    return True, {
        "system_prompt": _SUGGEST_SYSTEM_PROMPT + lang_directive,
        "prompt": (
            f"Skill folder name: {name}\n\n"
            f"SKILL.md content:\n```\n{content}\n```\n\n"
            f"Write the description field value now, and nothing else."
        ),
        "content": content,
    }


def suggest_skill_description(name, body_max_chars=4000, lang=None):
    """v1.9.3 / v1.9.6 - Genere une description suggeree pour un skill via
    le CLI Claude Code.

    v1.9.6 : ajout du parametre `lang` ('fr'|'en'|None). Le system prompt
    inclut une directive 'Respond in {lang}' pour que la suggestion match
    la langue voulue par l'utilisateur (par defaut, langue de l'UI Claude
    Control). Sans lang, le LLM suit ses propres instincts (souvent EN
    parce que le system prompt est en anglais).
    """
    if not _claude_cli_path():
        return False, ("Claude Code CLI ('claude') introuvable dans le PATH. "
                       "Installer avec : npm install -g @anthropic-ai/claude-code")
    ok, built = _suggestion_prompts(name, body_max_chars=body_max_chars,
                                    lang=lang)
    if not ok:
        return False, built
    system_prompt, prompt, content = (built["system_prompt"],
                                      built["prompt"], built["content"])
    _log(f"suggest_skill_description: name={name} chars_sent={len(content)} lang={lang}")
    try:
        raw_response = _call_claude_cli(prompt, system_prompt=system_prompt)
    except ClaudeCliNotLoggedIn as e:
        # v1.9.5 - cas specifique 'Not logged in' : on retourne un code
        # error_code que l'UI peut utiliser pour afficher des instructions
        # ciblees + un bouton 'Copier la commande' au lieu d'un dump
        # technique.
        # v1.14.19 - et l'app le DEMANDE : dialogue macOS avec bouton qui
        # ouvre Terminal pre-rempli, + etat de session rafraichi pour que le
        # bandeau apparaisse au prochain /api/state.
        _log(f"suggest_skill_description: CLI not logged in")
        _check_cli_session(force=True)
        _invalidate_state_cache()
        _ask_relogin_dialog(str(e)[:200])
        return False, {
            "error": str(e),
            "error_code": "cli_not_logged_in",
            "fix_command": "claude /login",
        }
    except ClaudeCliTimeout as e:
        # v1.14.18 - avant de rendre le timeout a l'utilisateur : si le CLI
        # porte la signature du binaire perime, l'app le met a jour
        # ELLE-MEME et reessaie. L'utilisateur a clique "Suggerer" ; c'est
        # "Suggerer" qui doit aboutir, pas un parcours de boutons.
        healed, heal_msg = _cli_auto_heal_after_timeout()
        if healed:
            _log(f"suggest_skill_description: auto-heal ok ({heal_msg}), "
                 f"nouvelle tentative")
            try:
                raw_response = _call_claude_cli(prompt,
                                                system_prompt=system_prompt)
            except Exception as e2:
                return False, {
                    "error": (f"{heal_msg} Mais l'appel suivant a encore "
                              f"échoué : {e2}"),
                    "error_code": "cli_timeout",
                    "partial": getattr(e2, "partial", "")}
            suggestion = _clean_suggestion(raw_response)
            if suggestion:
                return True, {"suggestion": suggestion,
                              "source": "claude_cli",
                              "chars_sent": len(content),
                              "raw_chars": len(raw_response or ""),
                              "lang": lang, "healed": heal_msg}
        # v1.14.3 - on ne renvoie plus l'utilisateur vers un terminal : le
        # message porte ce que le CLI avait ecrit, et l'UI enchaine toute
        # seule sur un diagnostic live (meme binaire, memes flags, meme env).
        _log(f"suggest_skill_description: timeout, partial={e.partial[:200]!r}")
        err = str(e)
        if heal_msg and not healed:
            err = f"{err} (Tentative de mise à jour du CLI : {heal_msg})"
        return False, {"error": err, "error_code": "cli_timeout",
                       "partial": e.partial}
    except FileNotFoundError as e:
        _log(f"suggest_skill_description: CLI not found : {e}")
        return False, ("Claude Code CLI introuvable dans le PATH. "
                       "Installer : npm install -g @anthropic-ai/claude-code")
    except Exception as e:
        _log(f"suggest_skill_description: exception {type(e).__name__} : {e}")
        return False, f"Erreur appel CLI ({type(e).__name__}) : {e}"
    _log(f"suggest_skill_description: raw response len={len(raw_response or '')} preview={(raw_response or '')[:200]!r}")
    suggestion = _clean_suggestion(raw_response)
    if not suggestion:
        _log(f"suggest_skill_description: empty after sanitization (raw was {raw_response[:200]!r})")
        return False, f"API a retourne une suggestion vide ou non parsable. Raw : {(raw_response or '')[:150]!r}"
    return True, {
        "suggestion": suggestion,
        "source": "claude_cli",
        "chars_sent": len(content),
        "raw_chars": len(raw_response or ""),
        "lang": lang,
    }


# === SUGGESTION UNITAIRE EN JOB DE FOND ===
#
# v1.15.0 - "Suggerer" sur UN skill tenait dans une seule requete HTTP qui
# pouvait durer des minutes (echelle a 3 barreaux + auto-heal + relance) :
# un thread serveur bloque, un fetch a la merci d'un timeout navigateur, et
# un simple rechargement de page perdait tout. Meme motif que le lot
# (v1.14.1) : le POST -start retourne immediatement un id de job, le
# travail tourne en thread de fond, l'UI interroge /api/suggest-job/<id>
# toutes les 2 s avec le temps ecoule affiche. Fermer la modale ou
# recharger la page ne tue pas le job -- il finit et son resultat reste
# lisible. suggest_skill_description reste inchangee : le job n'est qu'un
# porteur.
_SUGGEST_JOBS = {}
_SUGGEST_JOBS_LOCK = threading.Lock()
_SUGGEST_JOBS_KEEP = 32
_SUGGEST_JOB_SEQ = {"n": 0}


def start_suggest_job(name, lang=None):
    """Retourne (True, {"job": id, ...}) immediatement. Un job deja en cours
    pour le meme skill est reutilise (double-clic idempotent)."""
    if not name:
        return False, "Nom de skill requis"
    with _SUGGEST_JOBS_LOCK:
        for jid, job in _SUGGEST_JOBS.items():
            if job["name"] == name and job["status"] == "running":
                return True, {"job": jid, "already_running": True}
        _SUGGEST_JOB_SEQ["n"] += 1
        jid = f"sg-{_SUGGEST_JOB_SEQ['n']}"
        job = {"name": name, "lang": lang, "status": "running",
               "started_mono": time.monotonic(),
               "result": None, "seconds": None}
        _SUGGEST_JOBS[jid] = job
        # Borne memoire : on ne garde que les _SUGGEST_JOBS_KEEP plus
        # recents TERMINES (les jobs en cours ne sont jamais purges).
        finished = [k for k, v in _SUGGEST_JOBS.items()
                    if v["status"] != "running"]
        for k in finished[:max(0, len(_SUGGEST_JOBS) - _SUGGEST_JOBS_KEEP)]:
            _SUGGEST_JOBS.pop(k, None)

    def _run():
        started = time.monotonic()
        try:
            ok, payload = suggest_skill_description(name, lang=lang)
        except Exception as e:
            _log(f"suggest_job {jid}: {type(e).__name__}: {e}")
            ok, payload = False, f"{type(e).__name__} : {e}"
        # Meme enveloppe que la route synchrone (do_POST) : l'UI applique
        # le meme rendu au resultat, quel que soit le chemin.
        final = ({"success": ok, **payload} if isinstance(payload, dict)
                 else {"success": ok, "message": str(payload)})
        with _SUGGEST_JOBS_LOCK:
            job.update({"status": "done", "result": final,
                        "seconds": round(time.monotonic() - started, 1)})

    threading.Thread(target=_run, name=f"claude-control-suggest-{jid}",
                     daemon=True).start()
    return True, {"job": jid}


def suggest_job_status(job_id):
    """Photo du job, ou None si inconnu (serveur redemarre entre-temps)."""
    with _SUGGEST_JOBS_LOCK:
        job = _SUGGEST_JOBS.get(job_id)
        if job is None:
            return None
        if job["status"] == "running":
            return {"running": True, "name": job["name"],
                    "seconds": round(time.monotonic() - job["started_mono"],
                                     1)}
        return {"running": False, "name": job["name"],
                "seconds": job["seconds"], "result": job["result"]}


def _clean_suggestion(raw_response):
    """v1.9.2 / v1.14.1 / v1.14.13 - Nettoie une reponse de modele en une
    description exploitable. Extrait de suggest_skill_description pour que la
    reparation via le Terminal (v1.14.13) applique EXACTEMENT le meme
    nettoyage que le chemin subprocess. Retourne "" si rien d'exploitable.

    Le modele peut retourner : du markdown (## prefix, **bold**, > quote),
    des prefixes 'Description:' / 'Here is the description:', des quotes
    triple ou single, du preamble multi-ligne.
    """
    def _strip_line(s):
        s = s.strip()
        while s and s[0] in '#>*-':
            s = s.lstrip('#>*- ').strip()
        for prefix in ("Description:", "description:", "Here's the description:",
                       "Here is the description:", "Suggested description:"):
            if s.lower().startswith(prefix.lower()):
                s = s[len(prefix):].strip()
        s = s.strip('"').strip("'").strip("`").strip()
        return s

    raw = (raw_response or "")
    cleaned_lines = [_strip_line(ln) for ln in raw.splitlines()]
    cleaned_lines = [ln for ln in cleaned_lines if ln]
    # v1.14.1 - on JOINT les lignes restantes au lieu de n'en garder qu'une.
    #
    # L'ancienne heuristique triait par longueur et prenait la ligne la plus
    # longue. C'etait cohérent tant que la description tenait en une phrase
    # de moins de 150 caracteres ; maintenant qu'on demande 200-450
    # caracteres sur une a trois phrases, un retour a la ligne du modele
    # aurait silencieusement ampute la description.
    preamble_starts = ("sure", "here", "of course", "absolutely",
                       "certainly", "yes,", "let me", "i'll", "let's",
                       "voici", "bien sur", "d'accord", "parfait")
    # v1.14.1 - les cloture aussi : maintenant qu'on joint les lignes au lieu
    # d'en garder une seule, un "Hope this helps." final finirait dans la
    # description ecrite sur disque.
    closing_starts = ("hope this helps", "hope that helps", "let me know",
                      "feel free", "n'hesite", "n'hésite", "j'espere",
                      "j'espère", "dis-moi", "note that", "note :", "note:")

    def _is_filler(line):
        ll = line.lower().strip()
        if ll.endswith(":"):
            return True
        return (any(ll.startswith(p) for p in preamble_starts)
                or any(ll.startswith(c) for c in closing_starts))

    kept = [ln for ln in cleaned_lines if not _is_filler(ln)]
    if not kept:
        kept = cleaned_lines
    suggestion = re.sub(r"\s+", " ", " ".join(kept)).strip()
    if not suggestion:
        suggestion = _strip_line(raw)
    # Garde-fou : une description qui depasse largement la consigne trahit un
    # modele parti en explication. On coupe a la derniere phrase complete.
    if len(suggestion) > 700:
        cut = suggestion[:700]
        dot = max(cut.rfind(". "), cut.rfind(" ! "), cut.rfind(" ? "))
        suggestion = (cut[:dot + 1] if dot > 200 else cut).strip()
    return suggestion


# === I18N DES MESSAGES SERVEUR ===
#
# v1.15.0 - l'UI est bilingue depuis longtemps (dicts fr/en cote JS) mais
# les messages construits COTE SERVEUR (abandon de lot, preflight, verdicts,
# nettoyage des orphelins) partaient en francais chez les utilisateurs en
# anglais. Petit catalogue serveur : la langue vient soit du parametre lang
# deja transporte par les routes de reparation, soit de l'en-tete X-CC-Lang
# que api() ajoute a chaque POST (stocke par requete dans un threading.local
# -- le serveur est thread-par-requete). Les jobs de fond capturent la
# langue au lancement. Defaut : francais, la langue historique de l'app.
_REQ_LANG = threading.local()

_SRV_MSG = {
    "fr": {
        "bulk_already_running": "Une réparation est déjà en cours",
        "bulk_running": "Une réparation est en cours",
        "bulk_dismissed": "Compte rendu effacé",
        "bulk_none": "Aucun skill à réparer",
        "bulk_preflight": "Vérification du CLI, puis {n} skill(s)",
        "cli_not_found": ("Claude Code CLI ('claude') introuvable dans le "
                          "PATH. Installer avec : npm install -g "
                          "@anthropic-ai/claude-code"),
        "probe_timeout": ("Le CLI Claude n'a pas répondu à un appel trivial "
                          "en {s} s. Le lot n'a pas été lancé : il aurait "
                          "échoué skill après skill.{detail}"),
        "probe_partial": " Il avait commencé à écrire : « {p} »",
        "probe_nothing": " Il n'a rien écrit du tout avant de caler.",
        "probe_empty": ("Le CLI Claude a répondu sans aucun texte. Le lot "
                        "n'a pas été lancé. Teste : echo ping | claude -p "
                        "--output-format text"),
        "heal_suffix": " (Tentative de mise à jour du CLI : {msg})",
        "abort_consecutive": ("Arrêt après {n} échecs consécutifs (dernier : "
                              "{err}).{hint} Les skills restants n'ont pas "
                              "été touchés."),
        "abort_hint_fast_probe": (" Le CLI avait pourtant répondu en {s} s à "
                                  "un appel trivial : la panne vient "
                                  "probablement du contenu envoyé, pas du "
                                  "CLI."),
        "notify_bulk_stopped": "Réparation des skills arrêtée",
        "notify_bulk_cancelled": "Réparation des skills annulée",
        "notify_bulk_finished": "Réparation des skills terminée",
        "notify_bulk_summary": "{r} réparés, {f} en échec, sur {t}",
        "diag_not_logged_in": ("Ta session Anthropic est expirée : "
                               "reconnecte-toi avec `claude /login` dans un "
                               "terminal. (journal du CLI : « {tail} »)"),
        "orphans_listing_failed": "Listing des plugins impossible : {e}",
        "orphans_none": "Aucune version orpheline à nettoyer",
        "orphans_cleaned": "{n} version(s) orpheline(s) nettoyée(s)",
        "orphans_failed_suffix": ", {f} en échec",
        "orphans_backup_suffix": (" — backups : "
                                  "~/.claude/backups/claude-control/"
                                  "orphan-plugins/"),
        "suggest_job_lost": ("Suggestion perdue (serveur redémarré ?) — "
                             "relance « Suggérer »."),
    },
    "en": {
        "bulk_already_running": "A repair is already running",
        "bulk_running": "A repair is running",
        "bulk_dismissed": "Report cleared",
        "bulk_none": "No skill needs repair",
        "bulk_preflight": "Checking the CLI, then {n} skill(s)",
        "cli_not_found": ("Claude Code CLI ('claude') not found in PATH. "
                          "Install with: npm install -g "
                          "@anthropic-ai/claude-code"),
        "probe_timeout": ("The Claude CLI did not answer a trivial call "
                          "within {s} s. The batch was not started: it "
                          "would have failed skill after skill.{detail}"),
        "probe_partial": " It had started writing: “{p}”",
        "probe_nothing": " It wrote nothing at all before stalling.",
        "probe_empty": ("The Claude CLI answered with no text. The batch "
                        "was not started. Try: echo ping | claude -p "
                        "--output-format text"),
        "heal_suffix": " (CLI update attempt: {msg})",
        "abort_consecutive": ("Stopped after {n} consecutive failures "
                              "(last: {err}).{hint} Remaining skills were "
                              "not touched."),
        "abort_hint_fast_probe": (" Yet the CLI had answered a trivial call "
                                  "in {s} s: the failure most likely comes "
                                  "from the content sent, not the CLI."),
        "notify_bulk_stopped": "Skills repair stopped",
        "notify_bulk_cancelled": "Skills repair cancelled",
        "notify_bulk_finished": "Skills repair finished",
        "notify_bulk_summary": "{r} repaired, {f} failed, out of {t}",
        "diag_not_logged_in": ("Your Anthropic session has expired: sign in "
                               "again with `claude /login` in a terminal. "
                               "(CLI log: “{tail}”)"),
        "orphans_listing_failed": "Could not list plugins: {e}",
        "orphans_none": "No orphan version to clean up",
        "orphans_cleaned": "{n} orphan plugin version(s) cleaned up",
        "orphans_failed_suffix": ", {f} failed",
        "orphans_backup_suffix": (" — backups: "
                                  "~/.claude/backups/claude-control/"
                                  "orphan-plugins/"),
        "suggest_job_lost": ("Suggestion lost (server restarted?) — click "
                             "“Suggest” again."),
    },
}


def _norm_lang(lang):
    return "en" if str(lang or "").strip().lower().startswith("en") else "fr"


def _srv(key, lang=None, **fmt):
    """Message serveur traduit. lang explicite > langue de la requete en
    cours (X-CC-Lang) > francais. Un {placeholder} manquant ne casse jamais
    un message : on rend le texte brut plutot que lever."""
    if lang is None:
        lang = getattr(_REQ_LANG, "lang", None)
    txt = _SRV_MSG[_norm_lang(lang)].get(key) or _SRV_MSG["fr"].get(key) or key
    try:
        return txt.format(**fmt)
    except Exception:
        return txt


# === REPARATION EN LOT ===
#
# v1.14.1 - reparer 24 skills a la main = 24 clics et 24 modales. Chaque
# appel CLI prend ~7 s, donc un lot depasse largement le timeout d'une
# requete HTTP (et bloquerait un thread du serveur). D'ou : un job de fond
# avec un endpoint de progression que l'UI interroge.
_BULK_REPAIR = {
    "running": False, "total": 0, "done": 0, "current": None,
    "results": [], "started_at": None, "finished_at": None,
    "cancelled": False, "lang": None,
    # v1.14.2 - phase / arret. Sans ca, un lot dont le CLI ne repond jamais
    # affichait "Reparation en cours 1/93" pendant plus de trois heures
    # (93 skills x 120 s de timeout) avant de dire quoi que ce soit.
    "phase": "idle",          # idle | preflight | running | finished
    "aborted": False,
    "abort_reason": None,
    "probe_seconds": None,    # duree mesuree d'un appel CLI trivial
    "started_monotonic": None,
    # v1.14.13 - "cli" (subprocess) ou "terminal" (run dans Terminal.app).
    # L'UI de progression est la meme ; le mode change le sous-texte et le
    # message d'annulation.
    "mode": "cli",
}
_BULK_REPAIR_LOCK = threading.Lock()

# v1.14.2 - au-dela de ce nombre d'echecs consecutifs, on arrete le lot. Si le
# CLI ne repond pas 3 fois de suite, il ne repondra pas les 90 fois suivantes :
# continuer ne produit rien et occupe la machine pendant des heures.
BULK_REPAIR_MAX_CONSECUTIVE_FAILURES = 3
# Timeout du probe de preflight. Plus court que l'appel reel : on ne cherche
# pas une bonne reponse, seulement la preuve que le CLI en rend une.
BULK_REPAIR_PROBE_TIMEOUT = 90


def _repairable_user_skills(include_synced=False):
    """Skills dont la description ne suffit pas a declencher le skill.

    Par defaut : uniquement les skills locaux de l'utilisateur. Les skills
    de plugin et Desktop sont toujours exclus (on n'ecrit pas dans leur
    source).

    `include_synced` ajoute ceux de skills/synced/. C'est un opt-in explicite
    et non le defaut : ces skills sont synchronises depuis le compte Claude,
    donc une reecriture locale peut etre ecrasee a la prochaine synchro. Le
    backup .zip est cree comme pour les autres, mais l'utilisateur doit
    savoir ce qu'il fait.
    """
    out = []
    bases = [(SKILLS_DIR, True), (SKILLS_DISABLED_DIR, False)]
    if include_synced:
        bases.append((SKILLS_DIR / SYNCED_SKILLS_DIRNAME, True))
    for base, active in bases:
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            # v1.14.1 - un dossier sans SKILL.md n'est pas un skill mais un
            # conteneur (cf. skills/synced/). Sans ce filtre, la reparation
            # en lot ecrivait un SKILL.md a la racine du conteneur.
            if not (d / "SKILL.md").exists():
                continue
            meta = read_skill_meta(d)
            quality, reason = _skill_quality_detail(meta.get("description"))
            if d.name == SYNCED_SKILLS_DIRNAME:
                continue
            if quality in ("broken", "enrich"):
                out.append({"name": d.name, "quality": quality, "reason": reason,
                            "active": active,
                            "source": "synced" if base.name == SYNCED_SKILLS_DIRNAME else "user",
                            "description": meta.get("description") or ""})
    return out


def _bulk_repair_outcome(snap):
    """v1.14.2 - Etat terminal explicite, calcule cote serveur pour que l'UI
    n'ait pas a le deviner.

    Avant, l'UI affichait "Reparation terminee {done}/{total}" dans tous les
    cas. Sur 93 skills dont 60 en echec, ca lisait "terminee 93/93" : le
    compteur mesurait les skills TRAITES, pas les skills REPARES. Un lot
    annule et un lot integralement en echec produisaient le meme texte.
    """
    if snap["phase"] == "preflight":
        return "preflight"
    if snap["running"]:
        return "running"
    if not snap["results"] and not snap["aborted"]:
        return "idle"
    if snap["aborted"]:
        return "aborted"
    if snap["cancelled"]:
        return "cancelled"
    if snap["ok_count"] == 0 and snap["failed_count"]:
        return "all_failed"
    if snap["failed_count"]:
        return "partial"
    return "success"


def bulk_repair_status():
    with _BULK_REPAIR_LOCK:
        snap = dict(_BULK_REPAIR)
        snap["results"] = list(_BULK_REPAIR["results"])
        started_mono = _BULK_REPAIR["started_monotonic"]
    snap["ok_count"] = sum(1 for r in snap["results"] if r.get("ok"))
    snap["failed_count"] = sum(1 for r in snap["results"] if not r.get("ok"))
    snap["remaining"] = max(0, snap["total"] - snap["done"])
    # v1.14.2 - ETA mesuree, pas estimee a la louche. Le tooltip annoncait
    # "~7 s par skill" en dur ; sur une machine ou le CLI met 120 s, l'ecart
    # entre l'annonce et le vecu est le probleme.
    snap["elapsed_seconds"] = (
        round(time.monotonic() - started_mono) if started_mono else None)
    per_item = None
    if snap["done"] and snap["elapsed_seconds"]:
        per_item = snap["elapsed_seconds"] / snap["done"]
    elif snap["probe_seconds"]:
        per_item = snap["probe_seconds"]
    snap["seconds_per_skill"] = round(per_item, 1) if per_item else None
    snap["eta_seconds"] = (
        round(per_item * snap["remaining"])
        if (per_item and snap["running"] and snap["remaining"]) else None)
    snap["outcome"] = _bulk_repair_outcome(snap)
    if not snap["running"] and not snap["results"]:
        snap["pending"] = len(_repairable_user_skills())
        snap["pending_synced"] = len(
            [s for s in _repairable_user_skills(include_synced=True)
             if s["source"] == "synced"])
    return snap


def cancel_bulk_repair():
    with _BULK_REPAIR_LOCK:
        if not _BULK_REPAIR["running"]:
            return False, "Aucune réparation en cours"
        _BULK_REPAIR["cancelled"] = True
        mode = _BULK_REPAIR.get("mode", "cli")
    if mode == "terminal":
        # v1.14.13 - l'app cesse d'appliquer les resultats ; le script, lui,
        # tourne dans la fenetre Terminal de l'utilisateur, qu'on ne ferme
        # pas a sa place.
        return True, ("Annulation demandée — ferme aussi la fenêtre Terminal "
                      "pour arrêter les appels en cours")
    return True, "Annulation demandée — le skill en cours se termine"


def dismiss_bulk_repair():
    """v1.14.2 - Efface le compte rendu du dernier lot. Sans ca, le panneau
    de resultats restait affiche jusqu'au prochain lot, sans moyen de le
    fermer."""
    with _BULK_REPAIR_LOCK:
        if _BULK_REPAIR["running"]:
            return False, _srv("bulk_running")
        _BULK_REPAIR.update({
            "results": [], "done": 0, "total": 0, "phase": "idle",
            "aborted": False, "abort_reason": None, "cancelled": False,
            "started_at": None, "finished_at": None, "started_monotonic": None,
            "mode": "cli",
        })
    return True, _srv("bulk_dismissed")


def _bulk_repair_probe(lang=None):
    """v1.14.2 - Verifie que le CLI rend une reponse AVANT de lancer le lot.

    Motivation concrete : sur une machine ou le CLI ne repond pas, un lot de
    93 skills enchainait 93 timeouts de 120 s -- plus de trois heures a
    afficher "Reparation en cours 1/93" pour finir sur zero description
    reecrite. Un seul appel trivial en tete de lot transforme ca en message
    immediat, et sa duree donne l'ETA reelle du lot.

    Retourne (ok, seconds, error_message).
    """
    started = time.monotonic()
    try:
        out = _call_claude_cli(
            "ping",
            system_prompt="Reply with the single word: pong. Nothing else.",
            timeout=BULK_REPAIR_PROBE_TIMEOUT)
    except ClaudeCliTimeout as e:
        # v1.14.3 - la sortie partielle dit sur quoi le CLI bloque ; sans
        # elle le message se reduisait a "teste a la main dans un terminal".
        # v1.14.12 - la duree annoncee est l'attente TOTALE portee par
        # l'exception (tous les barreaux de repli), pas le budget d'un seul.
        detail = (_srv("probe_partial", lang=lang, p=e.partial[:200])
                  if e.partial else _srv("probe_nothing", lang=lang))
        return False, time.monotonic() - started, _srv(
            "probe_timeout", lang=lang, s=e.timeout, detail=detail)
    except ClaudeCliNotLoggedIn as e:
        # v1.14.19 - l'app demande la reconnexion au lieu de seulement
        # l'ecrire dans le compte rendu du lot.
        _check_cli_session(force=True)
        _invalidate_state_cache()
        _ask_relogin_dialog(str(e)[:200])
        return False, time.monotonic() - started, str(e)
    except Exception as e:
        return False, time.monotonic() - started, f"{type(e).__name__} : {e}"
    elapsed = time.monotonic() - started
    if not (out or "").strip():
        # v1.14.12 - la commande suggeree citait encore `--safe-mode`,
        # l'option retiree en v1.14.6 parce qu'elle GELAIT le CLI : suivre le
        # conseil bloquait le terminal de l'utilisateur.
        return False, elapsed, _srv("probe_empty", lang=lang)
    return True, elapsed, None


def _bulk_repair_abort(reason, lang=None):
    with _BULK_REPAIR_LOCK:
        _BULK_REPAIR.update({
            "running": False, "current": None, "aborted": True,
            "abort_reason": reason, "phase": "finished",
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        })
    _log(f"bulk_repair: arret - {reason}")
    _notify(_srv("notify_bulk_stopped", lang=lang), reason)


def _bulk_repair_worker(targets, lang):
    ok, probe_seconds, probe_error = _bulk_repair_probe(lang=lang)
    with _BULK_REPAIR_LOCK:
        _BULK_REPAIR["probe_seconds"] = round(probe_seconds, 1)
    if not ok:
        # v1.14.18 - binaire perime detecte ? L'app le met a jour elle-meme
        # et refait le preflight, au lieu d'abandonner le lot avec un
        # message. C'est le lot qui doit aboutir.
        healed, heal_msg = _cli_auto_heal_after_timeout()
        if healed:
            _notify("CLI mis a jour automatiquement", heal_msg or "")
            _log(f"bulk_repair: auto-heal ok, nouveau preflight")
            ok, probe_seconds, probe_error = _bulk_repair_probe(lang=lang)
            with _BULK_REPAIR_LOCK:
                _BULK_REPAIR["probe_seconds"] = round(probe_seconds, 1)
        elif heal_msg:
            probe_error = f"{probe_error}" + _srv("heal_suffix", lang=lang,
                                                  msg=heal_msg)
    if not ok:
        _bulk_repair_abort(probe_error, lang=lang)
        return
    with _BULK_REPAIR_LOCK:
        _BULK_REPAIR["phase"] = "running"
    consecutive_failures = 0
    for item in targets:
        with _BULK_REPAIR_LOCK:
            if _BULK_REPAIR["cancelled"]:
                break
            _BULK_REPAIR["current"] = item["name"]
        entry = {"name": item["name"], "before": item["description"]}
        try:
            ok, payload = suggest_skill_description(item["name"], lang=lang)
            if not ok:
                msg = payload.get("error") if isinstance(payload, dict) else str(payload)
                entry.update({"ok": False, "error": msg})
            else:
                suggestion = payload["suggestion"]
                ok2, msg2 = repair_skill(item["name"], description=suggestion)
                entry.update({"ok": bool(ok2), "after": suggestion,
                              "error": None if ok2 else str(msg2)})
        except Exception as e:
            _log(f"bulk_repair: {item['name']} : {type(e).__name__} : {e}")
            entry.update({"ok": False, "error": f"{type(e).__name__} : {e}"})
        with _BULK_REPAIR_LOCK:
            _BULK_REPAIR["results"].append(entry)
            _BULK_REPAIR["done"] += 1
        consecutive_failures = 0 if entry.get("ok") else consecutive_failures + 1
        if consecutive_failures >= BULK_REPAIR_MAX_CONSECUTIVE_FAILURES:
            # v1.14.2 - la duree du probe distingue deux pannes tres
            # differentes : un CLI mort (probe lent lui aussi) et un CLI sain
            # qui cale sur le contenu envoye (probe rapide, appels reels en
            # timeout). Sans ca, les deux donnent le meme "Timeout".
            hint = ""
            if probe_seconds and probe_seconds < 30:
                hint = _srv("abort_hint_fast_probe", lang=lang,
                            s=f"{probe_seconds:.0f}")
            _bulk_repair_abort(
                _srv("abort_consecutive", lang=lang, n=consecutive_failures,
                     err=str(entry.get('error') or '')[:140], hint=hint),
                lang=lang)
            return
    with _BULK_REPAIR_LOCK:
        _BULK_REPAIR["running"] = False
        _BULK_REPAIR["current"] = None
        _BULK_REPAIR["phase"] = "finished"
        _BULK_REPAIR["finished_at"] = datetime.now().isoformat(timespec="seconds")
        total = _BULK_REPAIR["total"]
        repaired = sum(1 for r in _BULK_REPAIR["results"] if r.get("ok"))
        failed = sum(1 for r in _BULK_REPAIR["results"] if not r.get("ok"))
        cancelled = _BULK_REPAIR["cancelled"]
    head = _srv("notify_bulk_cancelled" if cancelled else
                "notify_bulk_finished", lang=lang)
    _notify(head, _srv("notify_bulk_summary", lang=lang,
                       r=repaired, f=failed, t=total))


def start_bulk_repair(lang=None, include_synced=False):
    """Lance la reparation de tous les skills utilisateur a description
    insuffisante. Retourne immediatement ; l'UI suit via /api/repair-all-status."""
    with _BULK_REPAIR_LOCK:
        if _BULK_REPAIR["running"]:
            return False, _srv("bulk_already_running", lang=lang)
    if not _claude_cli_path():
        return False, _srv("cli_not_found", lang=lang)
    targets = _repairable_user_skills(include_synced=include_synced)
    if not targets:
        return True, {"message": _srv("bulk_none", lang=lang),
                      "total": 0, "started": False}
    with _BULK_REPAIR_LOCK:
        _BULK_REPAIR.update({
            "running": True, "total": len(targets), "done": 0,
            "current": None, "results": [], "cancelled": False, "lang": lang,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None, "phase": "preflight", "aborted": False,
            "abort_reason": None, "probe_seconds": None,
            "started_monotonic": time.monotonic(), "mode": "cli",
        })
    threading.Thread(target=_bulk_repair_worker, args=(targets, lang),
                     name="claude-control-bulk-repair", daemon=True).start()
    return True, {"message": _srv("bulk_preflight", lang=lang,
                                  n=len(targets)),
                  "total": len(targets), "started": True}


# === REPARATION VIA LE TERMINAL ===
#
# v1.14.13 - huit versions ont tente de faire ressembler le subprocess de
# l'app au terminal de l'utilisateur (PATH, proxy, session imbriquee, cwd,
# node, binaire, flags). Chaque correctif a elimine UNE difference ; la
# classe des differences possibles, elle, est sans fond -- et "ca marche
# dans mon terminal, ca cale depuis l'app" a survecu a tous. Ce mode
# retourne le probleme : au lieu de reconstruire l'environnement du
# terminal, il execute les appels DANS le terminal. Terminal.app lance un
# script genere par l'app ; `claude` y tourne avec le vrai shell, le vrai
# PATH, le vrai node et le vrai contexte Keychain de l'utilisateur --
# l'environnement prouve par definition. L'app surveille les fichiers de
# sortie et applique les descriptions elle-meme : les prompts, le nettoyage
# et l'ecriture des frontmatters restent le code teste cote app. Ce chemin
# fonctionne PAR CONSTRUCTION, quelle que soit la difference residuelle.
TERMINAL_REPAIR_DIR = HOME / ".claude/claude-control-terminal-repair"
# Au-dela de ce silence (aucun nouveau resultat), on considere que la
# fenetre Terminal a ete fermee -- ou que le CLI y est bloque aussi, ce qui
# serait en soi un verdict interessant.
TERMINAL_REPAIR_STALL_SECONDS = 240


def _shell_display_label(s):
    """Nom de skill embarque dans le script bash pour affichage : on retire
    tout caractere qui pourrait s'echapper d'une chaine double-quotee."""
    return re.sub(r'[^A-Za-z0-9 ._-]', '_', str(s))[:80]


def _build_terminal_repair_run(targets, lang):
    """Ecrit prompts + plan.json + run.sh dans un dossier de run horodate.

    Le script est volontairement TRIVIAL : une boucle de
    `claude -p --output-format text < prompt > out`, la forme nue prouvee.
    Chaque sortie passe par un .tmp puis `mv` (atomique sur le meme volume)
    pour que le watcher ne lise jamais un fichier a moitie ecrit. err-N.txt
    existe des le lancement (redirection) : il ne prouve un echec qu'une
    fois le run termine sans out-N.txt.
    """
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = TERMINAL_REPAIR_DIR / ts
    run_dir.mkdir(parents=True, exist_ok=False)
    entries = []
    for i, item in enumerate(targets):
        ok, built = _suggestion_prompts(item["name"], lang=lang)
        if not ok:
            entries.append({"index": i, "name": item["name"],
                            "skipped": str(built)})
            continue
        # Meme convention que l'appel nu de _call_claude_cli : la consigne
        # voyage dans le prompt, seul canal prouve.
        payload = f"{built['system_prompt']}\n\n---\n\n{built['prompt']}"
        (run_dir / f"prompt-{i:03d}.txt").write_text(payload)
        entries.append({"index": i, "name": item["name"]})
    _atomic_write_json(run_dir / "plan.json",
                       {"created": ts, "lang": lang, "targets": entries})
    runnable = [e for e in entries if "skipped" not in e]
    lines = [
        "#!/bin/bash",
        "# Genere par Claude Control - reparation des skills via le Terminal.",
        "# Chaque appel `claude` tourne dans TON environnement de terminal,",
        "# celui ou le CLI fonctionne. Claude Control lit les out-*.txt et",
        "# ecrit les descriptions lui-meme. Fermer cette fenetre annule.",
        'DIR="$(cd "$(dirname "$0")" && pwd)"',
        f"TOTAL={len(runnable)}",
        'echo "Claude Control - reparation de $TOTAL skill(s) via ton terminal."',
        "echo",
    ]
    for k, e in enumerate(runnable, start=1):
        i = e["index"]
        label = _shell_display_label(e["name"])
        lines += [
            f'echo "[{k}/$TOTAL] {label}"',
            (f'claude -p --output-format text'
             f' < "$DIR/prompt-{i:03d}.txt"'
             f' > "$DIR/.out-{i:03d}.tmp"'
             f' 2> "$DIR/err-{i:03d}.txt"'
             f' && mv "$DIR/.out-{i:03d}.tmp" "$DIR/out-{i:03d}.txt"'
             f' || echo "  ECHEC (voir err-{i:03d}.txt)"'),
        ]
    lines += [
        'touch "$DIR/DONE"',
        "echo",
        'echo "Termine - retourne dans Claude Control, les resultats y sont appliques."',
        "",
    ]
    script = run_dir / "run.sh"
    script.write_text("\n".join(lines))
    script.chmod(0o755)
    return run_dir, script


def _terminal_repair_watcher(run_dir, targets, lang):
    """Suit les out-*.txt du run Terminal et applique les descriptions.

    Reutilise l'etat _BULK_REPAIR : l'UI de progression existante suit ce
    mode sans changement. La cloture (resultats, notification) est la meme
    que _bulk_repair_worker.
    """
    by_name = {t["name"]: t for t in targets}
    try:
        plan = json.loads((run_dir / "plan.json").read_text())
    except Exception as e:
        _bulk_repair_abort(f"Plan de réparation illisible : {e}")
        return
    runnable, skipped = {}, []
    for e in plan.get("targets", []):
        if "skipped" in e:
            skipped.append(e)
        else:
            runnable[int(e["index"])] = e["name"]
    with _BULK_REPAIR_LOCK:
        for e in skipped:
            _BULK_REPAIR["results"].append(
                {"name": e["name"], "ok": False, "error": e["skipped"]})
            _BULK_REPAIR["done"] += 1

    def _apply(i, name):
        out_file = run_dir / f"out-{i:03d}.txt"
        raw = ""
        if out_file.exists():
            try:
                raw = out_file.read_text(errors="replace")
            except Exception:
                raw = ""
        entry = {"name": name,
                 "before": (by_name.get(name) or {}).get("description", "")}
        suggestion = _clean_suggestion(raw)
        if not suggestion:
            err_file = run_dir / f"err-{i:03d}.txt"
            tail = []
            if err_file.exists():
                try:
                    tail = _read_log_tail(err_file, max_lines=4,
                                          max_bytes=4000)
                except Exception:
                    tail = []
            detail = " / ".join(t.strip() for t in tail if t.strip())[:200]
            entry.update({"ok": False,
                          "error": ("Aucune sortie exploitable du Terminal."
                                    + (f" stderr : {detail}" if detail else ""))})
        else:
            ok2, msg2 = repair_skill(name, description=suggestion)
            entry.update({"ok": bool(ok2), "after": suggestion,
                          "error": None if ok2 else str(msg2)})
        with _BULK_REPAIR_LOCK:
            _BULK_REPAIR["results"].append(entry)
            _BULK_REPAIR["done"] += 1

    processed = set()
    last_progress = time.monotonic()
    cancelled = False
    while True:
        with _BULK_REPAIR_LOCK:
            cancelled = _BULK_REPAIR["cancelled"]
        if cancelled:
            break
        progressed = False
        for i in sorted(set(runnable) - processed):
            if (run_dir / f"out-{i:03d}.txt").exists():
                processed.add(i)
                progressed = True
                _apply(i, runnable[i])
        if progressed:
            last_progress = time.monotonic()
        if (run_dir / "DONE").exists():
            # Le run est fini : ce qui n'a pas de out-N.txt a echoue dans le
            # Terminal -- err-N.txt porte le pourquoi.
            for i in sorted(set(runnable) - processed):
                processed.add(i)
                _apply(i, runnable[i])
            break
        if time.monotonic() - last_progress > TERMINAL_REPAIR_STALL_SECONDS:
            _bulk_repair_abort(
                f"Aucun résultat du Terminal depuis "
                f"{TERMINAL_REPAIR_STALL_SECONDS // 60} min — fenêtre fermée, "
                f"ou le CLI y est bloqué aussi (ce qui serait un vrai verdict : "
                f"lance le diagnostic). Les skills déjà traités sont appliqués.")
            return
        time.sleep(1.5)
    with _BULK_REPAIR_LOCK:
        _BULK_REPAIR["running"] = False
        _BULK_REPAIR["current"] = None
        _BULK_REPAIR["phase"] = "finished"
        _BULK_REPAIR["finished_at"] = datetime.now().isoformat(timespec="seconds")
        total = _BULK_REPAIR["total"]
        repaired = sum(1 for r in _BULK_REPAIR["results"] if r.get("ok"))
        failed = sum(1 for r in _BULK_REPAIR["results"] if not r.get("ok"))
        was_cancelled = _BULK_REPAIR["cancelled"]
    head = ("Reparation des skills annulee" if was_cancelled
            else "Reparation des skills terminee")
    _notify(head, f"{repaired} repares, {failed} en echec, sur {total} (via Terminal)")


def start_terminal_repair(lang=None, include_synced=False, names=None):
    """v1.14.13 - Lance la reparation dans Terminal.app (cf. bloc ci-dessus).

    `names` optionnel : restreint aux skills nommes (bouton de la modal de
    reparation d'un skill unique).
    """
    with _BULK_REPAIR_LOCK:
        if _BULK_REPAIR["running"]:
            return False, "Une réparation est déjà en cours"
    targets = _repairable_user_skills(include_synced=include_synced)
    if names:
        wanted = {str(n) for n in names}
        targets = [t for t in targets if t["name"] in wanted]
    if not targets:
        return True, {"message": "Aucun skill à réparer", "total": 0,
                      "started": False}
    try:
        run_dir, script = _build_terminal_repair_run(targets, lang)
    except Exception as e:
        return False, f"Préparation du run impossible : {e}"
    # Le chemin est genere par l'app sous HOME : pas de guillemet dedans en
    # pratique ; les doubles quotes du shell sont echappees pour AppleScript
    # par _osa_quote.
    shell_cmd = f'bash "{script}"'
    osa = ('tell application "Terminal"\n'
           '  activate\n'
           f'  do script "{_osa_quote(shell_cmd, 500)}"\n'
           'end tell')
    try:
        r = subprocess.run(["osascript", "-e", osa], capture_output=True,
                           text=True, timeout=10)
        if r.returncode != 0:
            return False, ("Ouverture du Terminal impossible : "
                           f"{(r.stderr or '').strip()[:200]}")
    except Exception as e:
        return False, f"Ouverture du Terminal impossible : {e}"
    with _BULK_REPAIR_LOCK:
        _BULK_REPAIR.update({
            "running": True, "total": len(targets), "done": 0,
            "current": None, "results": [], "cancelled": False, "lang": lang,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None, "phase": "running", "aborted": False,
            "abort_reason": None, "probe_seconds": None,
            "started_monotonic": time.monotonic(), "mode": "terminal",
        })
    threading.Thread(target=_terminal_repair_watcher,
                     args=(run_dir, targets, lang),
                     name="claude-control-terminal-repair",
                     daemon=True).start()
    return True, {"message": f"Terminal ouvert — {len(targets)} skill(s) en cours",
                  "total": len(targets), "started": True}


# v1.14.17 - le scan Desktop etait le dernier rglob NON BORNE d'un chemin
# polle : l'arborescence <session-uuid>/<task-uuid>/ grossit a chaque session
# Claude Desktop et n'est jamais purgee, et get_state() la re-parcourait a
# CHAQUE appel (18/min). Sur des mois de sessions, c'est plusieurs secondes
# par appel -- la premiere salve d'un rechargement de page depassait le
# timeout du fetch et faisait flasher des bandeaux d'erreur. Un TTL de 2 min
# suffit largement : cette liste ne bouge qu'a l'ouverture d'une session.
_DESKTOP_SKILLS_CACHE = {"ts": 0.0, "data": None, "home": None}
_DESKTOP_SKILLS_LOCK = threading.Lock()
_DESKTOP_SKILLS_TTL = 120


def _list_desktop_skills():
    """v1.13.3 - Skills decompresses par Claude Desktop pour ses sessions
    actives. Path concret repere via diag fait sur la machine de Fabien :
      ~/Library/Application Support/Claude/local-agent-mode-sessions/
        skills-plugin/<session-uuid>/<task-uuid>/skills/<NAME>/SKILL.md

    Le dossier contient les skills uploades via Settings > Customize >
    Skills + les skills bundles Anthropic (logo-design, schedule, etc.).
    Limitation : c'est session-dependent. Si Claude Desktop n'a aucune
    session ouverte, le dir peut etre absent ou vide. Quand on detecte
    un SKILL.md, on considere le skill 'actif' (charge actuellement par
    Claude Desktop).

    v1.14.17 - resultat mis en cache _DESKTOP_SKILLS_TTL secondes (cf. bloc
    ci-dessus).
    """
    home_key = str(HOME)
    with _DESKTOP_SKILLS_LOCK:
        c = _DESKTOP_SKILLS_CACHE
        if (c["data"] is not None and c["home"] == home_key
                and time.monotonic() - c["ts"] < _DESKTOP_SKILLS_TTL):
            return list(c["data"])
    items = []
    base = HOME / "Library/Application Support/Claude/local-agent-mode-sessions/skills-plugin"
    if not base.is_dir():
        with _DESKTOP_SKILLS_LOCK:
            _DESKTOP_SKILLS_CACHE.update({"ts": time.monotonic(), "data": [],
                                          "home": home_key})
        return items
    seen = set()
    try:
        for skill_md in base.rglob("SKILL.md"):
            try:
                skill_dir = skill_md.parent
                if skill_dir.parent.name != "skills":
                    continue
                name = skill_dir.name
                if name in seen or name.startswith("."):
                    continue
                seen.add(name)
                meta = read_skill_meta(skill_dir)
                items.append({"name": name, "_dir": skill_dir,
                              "_source": "desktop", "_enabled": True,
                              "meta": meta})
            except Exception:
                continue
    except Exception:
        pass
    with _DESKTOP_SKILLS_LOCK:
        _DESKTOP_SKILLS_CACHE.update({"ts": time.monotonic(),
                                      "data": list(items), "home": home_key})
    return items


def _list_plugin_skills():
    """Retourne la liste des skills fournis par chaque plugin (active ou non).
    Chaque item porte _enabled (True/False) qui reflete l'etat du plugin parent.

    v1.13.0 - on n'exclut plus les plugins desactives. Avant, leurs skills
    disparaissaient totalement de la liste, ce qui rendait le decompte
    'Inactifs' systematiquement = 0 dans la sidebar et trompait l'utilisateur
    qui pensait que tous ses skills etaient actifs.
    """
    items = []
    try:
        installed = _load_installed_plugins()
    except Exception:
        return items
    settings = _load_settings()
    enabled_map = settings.get("enabledPlugins", {}) if isinstance(settings, dict) else {}
    for full_name, entries in installed.items():
        plugin_enabled = bool(enabled_map.get(full_name, True))
        if not isinstance(entries, list) or not entries:
            continue
        entry = entries[0]
        if not isinstance(entry, dict):
            continue
        ip = Path(entry.get("installPath", ""))
        for sk_dir in (ip / "skills", ip / ".claude-plugin/skills"):
            if not sk_dir.is_dir():
                continue
            for d in sorted(sk_dir.iterdir()):
                if not d.is_dir() or d.name.startswith("."):
                    continue
                if not (d / "SKILL.md").exists():
                    continue
                meta = read_skill_meta(d)
                items.append({"name": d.name, "_dir": d, "_source": f"plugin:{full_name}",
                              "_enabled": plugin_enabled, "meta": meta})
    return items


# v1.14.1 - Claude Code range les skills synchronises depuis le compte de
# l'utilisateur dans ~/.claude/skills/synced/<nom>/SKILL.md.
#
# C'est un dossier CONTENEUR, pas un skill. Consequence avant ce correctif :
#   - 'synced' apparaissait comme UN skill sans SKILL.md donc "casse",
#   - et les dizaines de skills qu'il contient etaient invisibles dans l'app.
# Ils sont en lecture seule ici : la source de verite est le compte, une
# edition locale serait ecrasee a la prochaine synchro.
SYNCED_SKILLS_DIRNAME = "synced"


def _list_synced_skills():
    items = []
    base = SKILLS_DIR / SYNCED_SKILLS_DIRNAME
    if not base.is_dir():
        return items
    try:
        for d in sorted(base.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if not (d / "SKILL.md").exists():
                continue
            items.append({"name": d.name, "_dir": d, "_source": "synced",
                          "_enabled": True, "meta": read_skill_meta(d)})
    except Exception as e:
        _log(f"_list_synced_skills: {e}")
    return items


# v1.14.1 - une description ne declenche de facon fiable que si elle dit
# QUAND l'utiliser. Ces marqueurs couvrent les tournures FR et EN usuelles.
#
# Les marqueurs sont ecrits SANS accent et compares sur une forme repliee
# (cf. _fold_accents) : sinon "A utiliser" et "À utiliser" divergent. Ils
# sont aussi volontairement tronques -- "declench" couvre declencher,
# declencheur et declenchement ; "des qu" couvre "des que" comme
# "des qu'on". Une premiere version listait les formes completes et
# classait "enrich" des descriptions parfaitement bonnes : un skill
# fraichement repare restait rouge, ce qui donnait l'impression que la
# reparation ne marchait pas.
_TRIGGER_CUES = (
    "use when", "use this", "used when", "when the user", "when you",
    "trigger", "for any request", "whenever", "invoke", "call this",
    "utiliser", "declench", "quand ", "des qu", "lorsqu", "pour toute",
    "s'applique", "mobiliser", "sert a", "en cas de",
)


def _fold_accents(s):
    """Minuscule sans accents, pour comparer des marqueurs FR de facon stable."""
    decomposed = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")

# Seuil de longueur. 30 caracteres (valeur v1.7.7) qualifiait "excellent"
# une description comme "Debug Railway" -- assez pour remplir le champ, pas
# assez pour que Claude sache quand declencher le skill. 120 caracteres est
# le plancher observe des descriptions qui portent reellement un quand + des
# termes declencheurs.
_SKILL_DESC_MIN_CHARS = 120


def _skill_quality(description):
    """v1.7.7 / v1.14.1 - Classifie un skill par qualite de description, qui
    est le seul facteur qui determine si Claude le declenche automatiquement.
    - 'broken'    : pas de description -> ne se declenche jamais
    - 'enrich'    : trop courte, ou ne dit pas QUAND declencher
    - 'excellent' : assez longue ET porte un marqueur de declenchement
    """
    return _skill_quality_detail(description)[0]


def _skill_quality_detail(description):
    """Retourne (quality, reason). `reason` est un code stable que l'UI
    traduit, pour que l'utilisateur sache quoi corriger."""
    desc = (description or "").strip()
    if not desc:
        return "broken", "no_description"
    folded = _fold_accents(desc)
    has_cue = any(cue in folded for cue in _TRIGGER_CUES)
    if len(desc) < _SKILL_DESC_MIN_CHARS:
        return "enrich", "too_short"
    if not has_cue:
        return "enrich", "no_trigger_terms"
    return "excellent", "ok"


# v1.14.0 - etat "running" des Desktop Extensions, mis en cache.
#
# Avant, get_state() appelait _extension_pids() pour CHAQUE extension a chaque
# requete. _extension_pids essaie jusqu'a 3 empreintes via pgrep (timeout 3 s
# chacune) puis 3 fallbacks lsof + un `ps -p` par PID. Or le code documente
# lui-meme (cf. dc_status) que sur Claude Desktop moderne les extensions
# tournent dans des Helper Node anonymes que pgrep ne matche jamais : le pire
# cas etait donc le cas nominal, soit ~6 fork/exec par extension, 12 fois par
# minute via /api/state et autant via /api/overview.
_EXT_RUNNING_CACHE = {"ts": 0.0, "data": {}}
_EXT_RUNNING_LOCK = threading.Lock()
_EXT_RUNNING_COMPUTE_LOCK = threading.Lock()
_EXT_RUNNING_TTL = 15


def _compute_extension_running_map(extensions):
    out = {}
    for e in extensions:
        running = False
        try:
            running = bool(_extension_pids(e["name"]))
        except Exception:
            running = False
        if not running:
            # Critere UI permissif (cf. v1.8.3) : log present et pas de
            # shutdown gracieux = extension chargee, en standby legitime.
            try:
                log = _find_mcp_log(e["name"])
                if log and log.exists() and not _log_shows_graceful_shutdown(log):
                    running = True
            except Exception:
                pass
        out[e["id"]] = running
    return out


def _extension_running_map(extensions, force=False):
    """Map {extension_id: running}, mise en cache _EXT_RUNNING_TTL secondes.

    Single-flight, comme _ps_snapshot : sur cache froid un seul thread paie
    le cout des pgrep/lsof, les autres attendent et lisent le resultat.
    """
    if not extensions:
        return {}
    if not force:
        with _EXT_RUNNING_LOCK:
            if _EXT_RUNNING_CACHE["data"] and time.time() - _EXT_RUNNING_CACHE["ts"] < _EXT_RUNNING_TTL:
                return dict(_EXT_RUNNING_CACHE["data"])
    with _EXT_RUNNING_COMPUTE_LOCK:
        if not force:
            with _EXT_RUNNING_LOCK:
                if _EXT_RUNNING_CACHE["data"] and time.time() - _EXT_RUNNING_CACHE["ts"] < _EXT_RUNNING_TTL:
                    return dict(_EXT_RUNNING_CACHE["data"])
        data = _compute_extension_running_map(extensions)
        with _EXT_RUNNING_LOCK:
            _EXT_RUNNING_CACHE["ts"] = time.time()
            _EXT_RUNNING_CACHE["data"] = dict(data)
        return data


def _list_cli_mcps():
    """v1.14.0 - MCPs declares pour Claude Code CLI dans ~/.claude.json.

    Couvre le scope utilisateur (`claude mcp add`) et le scope projet
    (cle `projects.<chemin>.mcpServers`). Ces MCPs n'apparaissaient nulle
    part dans l'app : seul claude_desktop_config.json etait lu, alors que
    l'onglet Plugins parlait deja de Claude Code. Lecture seule : la source
    de verite est ~/.claude.json, pas la config Desktop.
    """
    if not CLI_CONFIG_PATH.exists():
        return []
    try:
        data = json.loads(CLI_CONFIG_PATH.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception as e:
        _log(f"_list_cli_mcps: {CLI_CONFIG_PATH} illisible : {e}")
        return []
    if not isinstance(data, dict):
        return []
    found = {}   # nom -> (definition, scope)
    user_servers = data.get("mcpServers")
    if isinstance(user_servers, dict):
        for n, v in user_servers.items():
            if isinstance(v, dict):
                found[n] = (v, "cli")
    projects = data.get("projects")
    if isinstance(projects, dict):
        for proj_path, proj in projects.items():
            if not isinstance(proj, dict):
                continue
            servers = proj.get("mcpServers")
            if not isinstance(servers, dict):
                continue
            for n, v in servers.items():
                if isinstance(v, dict) and n not in found:
                    found[n] = (v, f"cli:{Path(str(proj_path)).name or proj_path}")
    if not found:
        return []
    running = _running_from_mapping({n: d for n, (d, _s) in found.items()})
    return [
        {"name": n, "active": True, "running": n in running,
         "type": "cli", "source": scope, "editable": False}
        for n, (_d, scope) in sorted(found.items())
    ]


def _list_plugin_mcps():
    """v1.14.0 - MCPs fournis par les plugins Claude Code (<plugin>/.mcp.json).

    Ils etaient lus depuis v1.11.0 mais n'apparaissaient que dans l'onglet
    Plugins. Lecture seule ici : pour que Claude Desktop les charge il faut
    passer par le bouton "bridge" existant.
    """
    try:
        installed = _load_installed_plugins()
    except Exception:
        return []
    if not installed:
        return []
    settings = _load_settings()
    enabled_map = settings.get("enabledPlugins", {}) if isinstance(settings, dict) else {}
    found = {}
    for full_name, entries in installed.items():
        if not enabled_map.get(full_name, True):
            continue
        if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
            continue
        try:
            servers = _read_plugin_mcp_servers(entries[0].get("installPath", ""))
        except Exception:
            continue
        for n, v in servers.items():
            if n not in found:
                found[n] = (v, f"plugin:{full_name}")
    if not found:
        return []
    running = _running_from_mapping({n: d for n, (d, _s) in found.items()})
    return [
        {"name": n, "active": True, "running": n in running,
         "type": "plugin", "source": scope, "editable": False}
        for n, (_d, scope) in sorted(found.items())
    ]


# v1.14.17 - cache court + single-flight sur get_state().
#
# La page de boot tire ~12 requetes d'un coup, et /api/overview refaisait un
# get_state() complet en plus de celui de /api/state : 18 calculs
# integraux/minute dont un tiers redondant, chacun relisant les meta de tous
# les skills et rescannant le dossier Desktop. La premiere salve d'un
# rechargement pouvait depasser le timeout du fetch -> bandeaux d'erreur
# furtifs. 2,5 s de TTL suffisent a fusionner une salve sans rendre l'UI
# perceptiblement perimee ; toute route POST qui change l'etat invalide le
# cache (cf. do_POST) pour que le loadState() qui suit un toggle soit frais.
_STATE_CACHE = {"ts": 0.0, "data": None, "key": None}
_STATE_CACHE_LOCK = threading.Lock()
_STATE_COMPUTE_LOCK = threading.Lock()
_STATE_TTL = 2.5


def _state_cache_key():
    """v1.14.17 - le cache n'est valide QUE pour les chemins depuis lesquels
    il a ete calcule. En production ils ne bougent jamais ; dans les tests,
    chaque cas repatche SKILLS_DIR & co vers son tempdir -- sans cette cle,
    un test pouvait recevoir l'etat calcule pour le tempdir du test
    precedent (flake reelle observee sous `unittest discover`)."""
    return (str(CONFIG_PATH), str(SKILLS_DIR), str(SKILLS_DISABLED_DIR),
            str(HOME))


def _invalidate_state_cache():
    with _STATE_CACHE_LOCK:
        _STATE_CACHE["ts"] = 0.0
        _STATE_CACHE["data"] = None
        _STATE_CACHE["key"] = None


def get_state():
    key = _state_cache_key()
    with _STATE_CACHE_LOCK:
        c = _STATE_CACHE
        if (c["data"] is not None and c["key"] == key
                and time.monotonic() - c["ts"] < _STATE_TTL):
            return c["data"]
    with _STATE_COMPUTE_LOCK:
        with _STATE_CACHE_LOCK:
            c = _STATE_CACHE
            if (c["data"] is not None and c["key"] == key
                    and time.monotonic() - c["ts"] < _STATE_TTL):
                return c["data"]
        data = _compute_state()
        with _STATE_CACHE_LOCK:
            _STATE_CACHE["ts"] = time.monotonic()
            _STATE_CACHE["data"] = data
            _STATE_CACHE["key"] = _state_cache_key()
        return data


def _compute_state():
    config, config_error = load_config_safe()
    active = config.get("mcpServers", {})
    disabled = config.get("_disabledMcps", {})
    running = get_running_mcps(config)
    # v1.15.0 - plus de mkdir ici : un GET /api/state ne doit rien ecrire
    # sur le disque. Les dossiers sont crees par main() au demarrage et par
    # chaque route POST qui en a besoin ; s'ils manquent, l'etat est
    # simplement vide.
    active_skills = sorted([d.name for d in SKILLS_DIR.iterdir()
                            if d.is_dir() and (d / "SKILL.md").exists()
                            and not d.name.startswith(".")
                            and d.name != SYNCED_SKILLS_DIRNAME]) \
        if SKILLS_DIR.is_dir() else []
    disabled_skills = sorted([d.name for d in SKILLS_DISABLED_DIR.iterdir()
                              if d.is_dir() and not d.name.startswith(".")
                              and (d / "SKILL.md").exists()]) \
        if SKILLS_DISABLED_DIR.is_dir() else []
    # v1.7.7 - usage counts par skill via parsing JSONL des sessions Claude Code,
    # injecte par skill pour rendre actionable la qualite vs usage dans la tab.
    try:
        usage = get_skill_usage(days=30)
        usage_counts = usage.get("counts", {}) if isinstance(usage, dict) else {}
    except Exception:
        usage_counts = {}

    def _skill_entry(name, base, active, source):
        meta = read_skill_meta(base / name)
        return {
            "name": name, "active": active,
            "category": meta["category"],
            "auto_category": _auto_category(meta["description"], name) if not meta["category"] else None,
            "description": meta["description"],
            "tags": meta["tags"],
            "source": source,
            "editable": source == "user",
            "quality": _skill_quality_detail(meta["description"])[0],
            "quality_reason": _skill_quality_detail(meta["description"])[1],
            "usage_count": int(usage_counts.get(name, 0)),
        }
    skills = [_skill_entry(n, SKILLS_DIR, True, "user") for n in active_skills]
    skills += [_skill_entry(n, SKILLS_DISABLED_DIR, False, "user") for n in disabled_skills]
    user_names = {s["name"] for s in skills}
    for it in _list_plugin_skills():
        if it["name"] in user_names:
            continue  # the user version takes precedence in the listing; duplicate is reported in overview
        meta = it["meta"]
        # v1.13.0 - active reflete l'etat enabled du plugin parent (avant :
        # toujours True). Cohere avec le mental model de l'utilisateur :
        # desactiver un plugin rend ses skills "Inactifs".
        skills.append({
            "name": it["name"], "active": bool(it.get("_enabled", True)),
            "category": meta["category"],
            "auto_category": _auto_category(meta["description"], it["name"]) if not meta["category"] else None,
            "description": meta["description"],
            "tags": meta["tags"],
            "source": it["_source"],
            "editable": False,
            "quality": _skill_quality_detail(meta["description"])[0],
            "quality_reason": _skill_quality_detail(meta["description"])[1],
            "usage_count": int(usage_counts.get(it["name"], 0)),
        })
    # v1.13.3 - skills decompresses par Claude Desktop dans son dir local-
    # agent-mode-sessions. Inclut les skills uploades via Customize > Skills
    # ainsi que les bundled Anthropic. Precedence : user > plugin > desktop
    # (si meme nom existe deja, on n'ajoute pas le desktop pour eviter le
    # bruit visuel).
    # v1.14.1 - skills synchronises depuis le compte (skills/synced/).
    seen_names = {s["name"] for s in skills}
    for it in _list_synced_skills():
        if it["name"] in seen_names:
            continue
        seen_names.add(it["name"])
        meta = it["meta"]
        skills.append({
            "name": it["name"], "active": True,
            "category": meta["category"],
            "auto_category": _auto_category(meta["description"], it["name"]) if not meta["category"] else None,
            "description": meta["description"],
            "tags": meta["tags"],
            "source": "synced",
            "editable": False,
            "quality": _skill_quality_detail(meta["description"])[0],
            "quality_reason": _skill_quality_detail(meta["description"])[1],
            "usage_count": int(usage_counts.get(it["name"], 0)),
        })
    for it in _list_desktop_skills():
        if it["name"] in seen_names:
            continue
        seen_names.add(it["name"])
        meta = it["meta"]
        skills.append({
            "name": it["name"], "active": True,
            "category": meta["category"],
            "auto_category": _auto_category(meta["description"], it["name"]) if not meta["category"] else None,
            "description": meta["description"],
            "tags": meta["tags"],
            "source": it["_source"],  # "desktop"
            "editable": False,
            "quality": _skill_quality_detail(meta["description"])[0],
            "quality_reason": _skill_quality_detail(meta["description"])[1],
            "usage_count": int(usage_counts.get(it["name"], 0)),
        })
    mcps_list = (
        [{"name": n, "active": True, "running": n in running, "type": "classic",
          "source": "desktop", "editable": True} for n in sorted(active.keys())]
        + [{"name": n, "active": False, "running": False, "type": "classic",
            "source": "desktop", "editable": True} for n in sorted(disabled.keys())]
    )
    # v1.14.0 - MCPs qui n'apparaissaient nulle part dans l'onglet MCP :
    #   - ~/.claude.json          -> ceux ajoutes via `claude mcp add` (user + projet)
    #   - <plugin>/.mcp.json      -> lus depuis toujours, mais affiches uniquement
    #                                dans l'onglet Plugins
    # Ils sont en lecture seule ici : leur source de verite n'est pas
    # claude_desktop_config.json, donc ni toggle ni suppression.
    desktop_names = {m["name"] for m in mcps_list}
    for extra in _list_cli_mcps() + _list_plugin_mcps():
        if extra["name"] in desktop_names:
            # deja gere cote Claude Desktop (cas d'un MCP "bridge")
            continue
        desktop_names.add(extra["name"])
        mcps_list.append(extra)
    extensions = _list_extensions()
    ext_running = _extension_running_map(extensions)
    for e in extensions:
        # v1.8.3 - critere "running" pour AFFICHAGE UI : permissif sans
        # seuil mtime court. Bug D 2026-05-06 : 8 extensions MCPB en
        # standby legitime (Filesystem, Control Chrome, Word, etc.) ont
        # ete classees "Inactifs" parce que leur log mtime depassait 120s
        # (silence entre 2 appels agent, ce qui est normal pour des MCPs
        # peu sollicites). Ce seuil a un sens pour le watchdog (detecter
        # un freeze sur cible monitoree) mais pas pour l'UI (statut
        # "chargee et prete a etre appelee").
        # v1.14.0 - le calcul lui-meme (et son cache) vit dans
        # _extension_running_map / _compute_extension_running_map.
        mcps_list.append({
            "name": e["name"],
            "active": e["enabled"],
            "running": bool(ext_running.get(e["id"], False)),
            "type": "extension",
            "source": "extension",
            "editable": True,
            "extension_id": e["id"],
            "version": e["version"],
        })
    out = {
        "mcps": mcps_list,
        "skills": skills,
    }
    # v1.14.19 - la session CLI expiree est annoncee AVANT tout essai de
    # reparation : lecture locale de la date d'expiration du jeton (cache
    # 5 min, jamais le secret), bandeau + bouton "Se reconnecter" cote UI.
    session = _check_cli_session()
    if session["status"] == "expired":
        out["cli_session_expired"] = True
        out["cli_session_detail"] = session["detail"]
        # v1.15.0 - l'UI traduit la phrase elle-meme a partir des chiffres.
        out["cli_session_days"] = session.get("days")
        out["cli_session_hours"] = session.get("hours")
    # v1.14.0 - remonte l'erreur de config au lieu de la laisser lever : le JS
    # affiche desormais un bandeau au lieu de garder silencieusement la
    # derniere liste rendue.
    if config_error:
        out["config_error"] = config_error
    return out


def toggle_mcp(name):
    config = load_config()
    active = config.setdefault("mcpServers", {})
    disabled = config.setdefault("_disabledMcps", {})
    if name in active:
        disabled[name] = active.pop(name)
        msg = f"MCP '{name}' desactive"
    elif name in disabled:
        active[name] = disabled.pop(name)
        msg = f"MCP '{name}' active"
    else:
        return False, f"MCP '{name}' introuvable"
    save_config(config)
    return True, msg


def toggle_skill(name):
    # v1.14.12 - meme garde que delete_skill/repair_skill/_read_skill_content.
    # toggle_skill etait la seule fonction de la famille sans validation :
    # `SKILLS_DIR / "../../X"` suivi de `.rename()` deplacait n'importe quel
    # dossier de la machine vers skills-disabled/ (le nom vient d'un POST).
    if not _safe_component(name):
        return False, "Nom de skill invalide"
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    SKILLS_DISABLED_DIR.mkdir(parents=True, exist_ok=True)
    src_a = SKILLS_DIR / name
    src_d = SKILLS_DISABLED_DIR / name
    if src_a.exists():
        target = SKILLS_DISABLED_DIR / name
        if target.exists():
            return False, f"Conflit : {target} existe deja"
        src_a.rename(target)
        return True, f"Skill '{name}' desactive"
    elif src_d.exists():
        target = SKILLS_DIR / name
        if target.exists():
            return False, f"Conflit : {target} existe deja"
        src_d.rename(target)
        return True, f"Skill '{name}' active"
    return False, f"Skill '{name}' introuvable"


def _zip_dir(folder, zip_path):
    """Zippe un dossier vers zip_path (helper backup)."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in Path(folder).rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(folder))


def delete_skill(name):
    # v1.7.8 - on autorise le prefixe underscore (ex. '_archived-...') qui est
    # une convention utilisateur legitime. On garde les vraies protections
    # path-traversal : '/', '\\', '..', et le prefixe '.' (fichiers caches).
    if not name or "/" in name or "\\" in name or ".." in name or name.startswith("."):
        return False, "Nom de skill invalide"
    target = None
    for base in (SKILLS_DIR, SKILLS_DISABLED_DIR):
        candidate = base / name
        if candidate.exists() and candidate.is_dir():
            target = candidate
            break
    if not target:
        return False, f"Skill '{name}' introuvable"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_DIR / f"deleted-skill-{name}-{ts}.zip"
    try:
        _zip_dir(target, backup)
    except Exception as e:
        return False, f"Backup échoué : {e}"
    shutil.rmtree(target)
    return True, f"Skill '{name}' supprimé (backup : {backup.name})"


def delete_user_skill_duplicates():
    """v1.7.4 - Bulk-delete des skills cote utilisateur quand le meme nom
    existe aussi cote plugin. Le plugin devient la source de verite (les
    plugins sont gerables / mis a jour via leur marketplace, alors que les
    copies utilisateur peuvent rapidement diverger). Backup zip individuel
    par skill avant suppression (cf. delete_skill).
    """
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    SKILLS_DISABLED_DIR.mkdir(parents=True, exist_ok=True)
    user_names = set()
    for d in SKILLS_DIR.iterdir():
        if d.is_dir() and not d.name.startswith(".") and (d / "SKILL.md").exists():
            user_names.add(d.name)
    for d in SKILLS_DISABLED_DIR.iterdir():
        if d.is_dir() and not d.name.startswith("."):
            user_names.add(d.name)
    plugin_names = set()
    try:
        for it in _list_plugin_skills():
            plugin_names.add(it["name"])
    except Exception:
        return False, "Erreur listing skills plugin", []
    duplicates = sorted(user_names & plugin_names)
    if not duplicates:
        return True, {"message": "Aucun doublon a supprimer", "deleted": [], "failed": []}
    deleted, failed = [], []
    for name in duplicates:
        ok, _msg = delete_skill(name)
        if ok:
            deleted.append(name)
        else:
            failed.append(name)
    if failed:
        return False, {
            "message": f"{len(deleted)} supprimes, {len(failed)} en echec : {', '.join(failed[:3])}",
            "deleted": deleted, "failed": failed,
        }
    return True, {
        "message": f"{len(deleted)} skill(s) utilisateur supprime(s) (backup zip individuel par skill)",
        "deleted": deleted, "failed": [],
    }


def restart_mcp(name):
    """Redémarre un MCP ou Desktop Extension sans toucher à Claude Desktop."""
    # v1.14.0 - l'action change l'etat des process : on vide les caches
    # pour que le prochain /api/state dise la verite immediatement.
    _invalidate_process_caches()
    if not name:
        return False, "Nom MCP requis"
    config = load_config()
    is_active = name in config.get("mcpServers", {})
    is_disabled = name in config.get("_disabledMcps", {})
    if not (is_active or is_disabled):
        # Try as extension
        extensions = _list_extensions()
        if any(e["name"] == name or e["id"] == name for e in extensions):
            return restart_extension(name)
        return False, f"MCP / Extension '{name}' introuvable"
    pids = _mcp_pids(name) if "_mcp_pids" in globals() else []
    my_pid = os.getpid()
    killed = 0
    for pid in pids:
        if pid == my_pid:
            continue
        try:
            os.kill(pid, 9)
            killed += 1
        except Exception:
            pass
    if is_active:
        config = load_config()
        active = config.setdefault("mcpServers", {})
        disabled = config.setdefault("_disabledMcps", {})
        disabled[name] = active.pop(name)
        save_config(config)
        time.sleep(1.5)
        config = load_config()
        active = config.setdefault("mcpServers", {})
        disabled = config.setdefault("_disabledMcps", {})
        if name in disabled:
            active[name] = disabled.pop(name)
            save_config(config)
        _LAST_RESTARTS[name] = datetime.now().isoformat(timespec="seconds")
        _notify(f"{name} redémarré", f"{killed} process tué - Claude Desktop intact")
        return True, f"MCP '{name}' redémarré (killed {killed}, config togglée)"
    return True, f"MCP '{name}' inactif : {killed} process killed, rien d'autre à faire"


def start_mcp(name):
    """v1.7.6 - Demarrage a chaud sans toucher a Claude Desktop : si le MCP
    est actif dans le config mais pas en cours d'execution, on toggle son
    entree off->on (avec backup) pour que Claude Desktop le respawn via
    FSEvents. Pas de kill (rien a tuer). Pour les extensions : toggle
    settings off->on de la meme maniere.
    """
    # v1.14.0 - l'action change l'etat des process : on vide les caches
    # pour que le prochain /api/state dise la verite immediatement.
    _invalidate_process_caches()
    if not name:
        return False, "Nom MCP requis"
    config = load_config()
    is_active = name in config.get("mcpServers", {})
    is_disabled = name in config.get("_disabledMcps", {})
    if is_active:
        active = config.setdefault("mcpServers", {})
        disabled = config.setdefault("_disabledMcps", {})
        disabled[name] = active.pop(name)
        save_config(config)
        time.sleep(1.5)
        config = load_config()
        active = config.setdefault("mcpServers", {})
        disabled = config.setdefault("_disabledMcps", {})
        if name in disabled:
            active[name] = disabled.pop(name)
            save_config(config)
        return True, f"MCP '{name}' demarre a chaud (config togglee off->on, Claude Desktop respawn via FSEvents)"
    if is_disabled:
        return False, f"MCP '{name}' est dans _disabledMcps - coche-le d'abord pour l'activer"
    extensions = _list_extensions()
    target = next((e for e in extensions if e["name"] == name or e["id"] == name), None)
    if target:
        if not target["enabled"]:
            return False, f"Extension '{name}' est desactivee - active-la d'abord"
        settings = _load_extension_settings(target["id"])
        was_enabled = _read_extension_enabled(settings)
        _set_extension_enabled(settings, False)
        try:
            _save_extension_settings(target["id"], settings)
            time.sleep(1.5)
            _set_extension_enabled(settings, was_enabled)
            _save_extension_settings(target["id"], settings)
        except Exception as e:
            return False, f"Erreur toggle settings : {e}"
        return True, f"Extension '{name}' demarree a chaud (settings togglee off->on)"
    return False, f"MCP / Extension '{name}' introuvable"


def stop_mcp(name):
    """v1.7.6 - Arret a chaud sans toucher au config : kill les PIDs du
    process MCP / Extension. Le config reste intact (le checkbox 'actif au
    prochain restart' reste coche). Claude Desktop ne respawn pas tant que
    le config ne change pas, donc le MCP reste stoppe jusqu'au prochain
    redemarrage CD ou jusqu'au prochain Restart manuel.

    Pour les extensions : v1.6.6 a documente que les Helper Nodes sont
    anonymes - kill PID peut ne rien faire (no PIDs trouves). Dans ce cas
    on retourne un message honnete plutot qu'un faux succes.
    """
    # v1.14.0 - l'action change l'etat des process : on vide les caches
    # pour que le prochain /api/state dise la verite immediatement.
    _invalidate_process_caches()
    if not name:
        return False, "Nom MCP requis"
    config = load_config()
    is_classic = name in config.get("mcpServers", {}) or name in config.get("_disabledMcps", {})
    if is_classic:
        pids = _mcp_pids(name) if "_mcp_pids" in globals() else []
        my_pid = os.getpid()
        killed = 0
        for pid in pids:
            if pid == my_pid:
                continue
            try:
                os.kill(pid, 9)
                killed += 1
            except Exception:
                pass
        if killed == 0:
            return True, f"MCP '{name}' : aucun process en cours d'execution (deja stoppe ?)"
        return True, f"MCP '{name}' stoppe ({killed} process tue) - config intact, sera relance au prochain Redemarrer ou prochain demarrage Claude Desktop"
    extensions = _list_extensions()
    target = next((e for e in extensions if e["name"] == name or e["id"] == name), None)
    if target:
        pids = _extension_pids(name)
        my_pid = os.getpid()
        killed = 0
        for pid in pids:
            if pid == my_pid:
                continue
            try:
                os.kill(pid, 9)
                killed += 1
            except Exception:
                pass
        if killed == 0:
            return True, (f"Extension '{name}' : aucun PID identifiable (Helper Node anonyme - "
                          f"limitation Claude Desktop moderne, cf. v1.6.6). "
                          f"Pour stopper vraiment l'extension, decoche-la (config off) ou redemarre Claude Desktop.")
        return True, f"Extension '{name}' stoppee ({killed} process tue) - settings intacts"
    return False, f"MCP / Extension '{name}' introuvable"


def delete_extension(name):
    """v1.7.6 - Supprimer une Desktop Extension : retire l'entree de
    extensions-installations.json + supprime le dossier d'install + le
    fichier de settings. Backup zip de l'install dir avant suppression.

    Note : si l'extension est managee par Claude Desktop / Anthropic
    (PowerPoint, Word, Control Mac, etc.), Claude Desktop peut la
    re-installer automatiquement au prochain restart. C'est documente
    dans le message de retour pour que l'utilisateur soit avertit.
    """
    if not name:
        return False, "Nom requis"
    extensions = _list_extensions()
    target = next((e for e in extensions if e["name"] == name or e["id"] == name), None)
    if not target:
        return False, f"Extension '{name}' introuvable"
    ext_id = target["id"]
    install_dir = HOME / "Library/Application Support/Claude/Claude Extensions" / ext_id
    settings_file = EXTENSIONS_SETTINGS_DIR / f"{ext_id}.json"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_id = re.sub(r'[^a-zA-Z0-9_\-.]', '_', ext_id)
    if install_dir.exists() and install_dir.is_dir():
        try:
            _zip_dir(install_dir, BACKUP_DIR / f"deleted-extension-{safe_id}-{ts}.zip")
        except Exception as e:
            return False, f"Backup install dir echoue : {e}"
        try:
            shutil.rmtree(install_dir)
        except Exception as e:
            return False, f"Suppression install dir echouee : {e}"
    if settings_file.exists():
        try:
            shutil.copy2(settings_file, BACKUP_DIR / f"deleted-extension-settings-{safe_id}-{ts}.json")
            settings_file.unlink()
        except Exception:
            pass
    # Retire l'entree de extensions-installations.json
    if EXTENSIONS_INSTALL_FILE.exists():
        try:
            data = json.loads(EXTENSIONS_INSTALL_FILE.read_text(errors="replace"))
            shutil.copy2(EXTENSIONS_INSTALL_FILE, BACKUP_DIR / f"extensions-installations.{ts}.json")
            changed = False
            if isinstance(data, list):
                new_list = [e for e in data if not (isinstance(e, dict) and e.get("id") == ext_id)]
                if len(new_list) != len(data):
                    data = new_list
                    changed = True
            elif isinstance(data, dict):
                for key in ("installations", "extensions"):
                    if isinstance(data.get(key), list):
                        new_list = [e for e in data[key] if not (isinstance(e, dict) and e.get("id") == ext_id)]
                        if len(new_list) != len(data[key]):
                            data[key] = new_list
                            changed = True
                    elif isinstance(data.get(key), dict) and ext_id in data[key]:
                        del data[key][ext_id]
                        changed = True
                if ext_id in data:
                    del data[ext_id]
                    changed = True
            if changed:
                _atomic_write_json(EXTENSIONS_INSTALL_FILE, data)
        except Exception as e:
            return False, f"Suppression registry echouee : {e}"
    return True, (f"Extension '{name}' supprimee (backup zip de l'install dir + json registry). "
                  f"Note : si Claude Desktop la re-installe automatiquement au prochain restart "
                  f"(cas des extensions Anthropic-managed), desinstalle-la via Settings -> Extensions de Claude Desktop.")


def delete_mcp(name):
    if not name:
        return False, "Nom requis"
    config = load_config()
    found = False
    for bucket in ("mcpServers", "_disabledMcps"):
        if name in config.get(bucket, {}):
            del config[bucket][name]
            found = True
    if not found:
        return False, f"MCP '{name}' introuvable"
    save_config(config)
    return True, f"MCP '{name}' supprimé de la config (backup horodaté créé)"


def delete_plugin(full_name, delete_files=False):
    if not full_name:
        return False, "Nom de plugin requis"
    if not INSTALLED_PLUGINS_FILE.exists():
        return False, "installed_plugins.json introuvable"
    try:
        with open(INSTALLED_PLUGINS_FILE) as f:
            data = json.load(f)
    except Exception as e:
        return False, f"installed_plugins.json invalide : {e}"
    plugins_dict = data.get("plugins", {}) if isinstance(data, dict) else {}
    if full_name not in plugins_dict:
        return False, f"Plugin '{full_name}' introuvable"
    entries = plugins_dict[full_name]
    install_paths = []
    if isinstance(entries, list):
        for e in entries:
            if isinstance(e, dict) and e.get("installPath"):
                install_paths.append(e["installPath"])
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(INSTALLED_PLUGINS_FILE, BACKUP_DIR / f"installed_plugins.json.{ts}")
    del plugins_dict[full_name]
    data["plugins"] = plugins_dict
    _atomic_write_json(INSTALLED_PLUGINS_FILE, data)
    settings = _load_settings()
    if isinstance(settings.get("enabledPlugins"), dict) and full_name in settings["enabledPlugins"]:
        del settings["enabledPlugins"][full_name]
        _save_settings(settings)
    deleted_dirs = []
    if delete_files:
        cache_root = (HOME / ".claude/plugins/cache").resolve()
        safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', full_name)
        for ip in install_paths:
            try:
                p = Path(ip).resolve()
                p.relative_to(cache_root)
            except (ValueError, OSError):
                continue
            walk = p
            while walk.parent != cache_root and walk != cache_root and walk != Path("/"):
                walk = walk.parent
                if walk == cache_root:
                    break
            target = walk if walk.exists() and walk != cache_root else p
            if target.exists() and target.is_dir():
                ORPHAN_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                backup_zip = ORPHAN_BACKUP_DIR / f"deleted-{safe_name}-{ts}.zip"
                try:
                    _zip_dir(target, backup_zip)
                except Exception:
                    pass
                shutil.rmtree(target, ignore_errors=True)
                deleted_dirs.append(str(target))
    msg = f"Plugin '{full_name}' supprimé"
    if delete_files:
        msg += f" + {len(deleted_dirs)} dossier(s) du cache effacé(s)"
    return True, msg


# ─── v1.15.0 - LE clone git de l'app ────────────────────────────────────────
# Trois importeurs (plugin, MCP, skill) portaient chacun leur copie du meme
# `git clone --depth 1` avec des timeouts divergents (120/90/90 s) et des
# gestions d'erreurs differentes -- import_skill_git attrapait meme le
# timeout dans un `except Exception` au message inexploitable. Un seul
# chemin desormais : memes messages, meme timeout, memes exceptions.
_GIT_CLONE_TIMEOUT = 120


def _git_url_ok(url):
    return (url or "").strip().startswith(("https://", "http://", "git@"))


def _git_repo_name(url):
    """Nom de repo derive du dernier segment de l'URL, ou None s'il n'est
    pas un composant de chemin sur.

    Garde v1.14.12 : '..' donnait target=~/.claude et le rmtree du 'clone
    existant' EFFACAIT tout ~/.claude ; un segment vide ('https://x/.git')
    rasait le dossier d'import lui-meme. _safe_component ferme les deux.
    """
    name = re.sub(r'\.git$', '',
                  (url or "").strip().rstrip('/').split('/')[-1])
    return name if _safe_component(name) else None


def _git_clone_shallow(url, target):
    """Clone --depth 1 de `url` vers `target`. Retourne (ok, erreur)."""
    if not _git_url_ok(url):
        return False, "URL Git invalide (doit commencer par https:// ou git@)"
    try:
        r = subprocess.run(
            ["git", "clone", "--depth", "1", (url or "").strip(), str(target)],
            capture_output=True, text=True, timeout=_GIT_CLONE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"Timeout git clone ({_GIT_CLONE_TIMEOUT}s)"
    except FileNotFoundError:
        return False, "git n'est pas installé"
    if r.returncode != 0:
        return False, f"git clone : {(r.stderr or '')[:200]}"
    return True, ""


def add_plugin_from_git(url):
    url = (url or "").strip()
    if not _git_url_ok(url):
        return False, "URL Git invalide (doit commencer par https:// ou git@)"
    repo_name = _git_repo_name(url)
    if repo_name is None:
        return False, "Impossible de déterminer le nom du repo depuis l'URL"
    cache_dir = HOME / ".claude/plugins/cache/manual" / repo_name
    if cache_dir.exists():
        return False, f"Un plugin existe déjà dans {cache_dir}. Supprime-le d'abord."
    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    cloned, err = _git_clone_shallow(url, cache_dir)
    if not cloned:
        return False, err
    candidates = list(cache_dir.rglob("plugin.json"))
    if not candidates:
        shutil.rmtree(cache_dir, ignore_errors=True)
        return False, "Aucun plugin.json trouvé dans le repo"
    plugin_json = candidates[0]
    install_path = plugin_json.parent
    if install_path.name == ".claude-plugin":
        install_path = install_path.parent
    try:
        meta = json.loads(plugin_json.read_text(errors="replace"))
    except Exception as e:
        shutil.rmtree(cache_dir, ignore_errors=True)
        return False, f"plugin.json invalide : {e}"
    if not isinstance(meta, dict):
        shutil.rmtree(cache_dir, ignore_errors=True)
        return False, "plugin.json doit être un objet JSON"
    name = meta.get("name") or repo_name
    if not re.match(r'^[a-zA-Z0-9_\-]+$', name):
        shutil.rmtree(cache_dir, ignore_errors=True)
        return False, f"Nom de plugin invalide : '{name}'"
    version = str(meta.get("version") or "1.0.0")
    full_name = f"{name}@manual"
    if INSTALLED_PLUGINS_FILE.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(INSTALLED_PLUGINS_FILE, BACKUP_DIR / f"installed_plugins.json.{ts}")
        try:
            with open(INSTALLED_PLUGINS_FILE) as f:
                data = json.load(f)
        except Exception:
            data = {"version": 2, "plugins": {}}
    else:
        data = {"version": 2, "plugins": {}}
    if not isinstance(data.get("plugins"), dict):
        data["plugins"] = {}
    if full_name in data["plugins"]:
        shutil.rmtree(cache_dir, ignore_errors=True)
        return False, f"Plugin '{full_name}' déjà enregistré. Supprime-le d'abord."
    now_iso = datetime.now().isoformat() + "Z"
    data["plugins"][full_name] = [{
        "scope": "user",
        "installPath": str(install_path),
        "version": version,
        "installedAt": now_iso,
        "lastUpdated": now_iso,
        "gitCommitSha": "",
    }]
    INSTALLED_PLUGINS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(INSTALLED_PLUGINS_FILE, data)
    settings = _load_settings()
    settings.setdefault("enabledPlugins", {})[full_name] = True
    _save_settings(settings)
    return True, f"Plugin '{full_name}' v{version} ajouté et activé"


def list_commands():
    """Liste les commands utilisateur (~/.claude/commands/) + celles fournies par
    chaque plugin actif. Les commands plugin sont en lecture seule."""
    commands = []
    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    COMMANDS_DISABLED_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(COMMANDS_DIR.glob("*.md")):
        commands.append({"name": f.stem, "source": "user", "active": True,
                         "path": str(f), "editable": True})
    for f in sorted(COMMANDS_DISABLED_DIR.glob("*.md")):
        commands.append({"name": f.stem, "source": "user", "active": False,
                         "path": str(f), "editable": True})
    try:
        installed = _load_installed_plugins()
    except Exception:
        installed = {}
    settings = _load_settings()
    enabled_map = settings.get("enabledPlugins", {}) if isinstance(settings, dict) else {}
    for full_name, entries in installed.items():
        if not enabled_map.get(full_name, True):
            continue
        if not isinstance(entries, list) or not entries:
            continue
        entry = entries[0]
        if not isinstance(entry, dict):
            continue
        install_path = Path(entry.get("installPath", ""))
        cmd_dir = install_path / "commands"
        if not cmd_dir.is_dir():
            continue
        for f in sorted(cmd_dir.glob("*.md")):
            commands.append({"name": f.stem, "source": f"plugin:{full_name}",
                             "active": True, "path": str(f), "editable": False})
    return commands


def _safe_component(name):
    """v1.14.0 - True si `name` peut servir de composant de chemin sans
    s'echapper du repertoire parent. Les noms viennent de requetes HTTP."""
    n = str(name or "")
    return bool(n) and "/" not in n and "\\" not in n and ".." not in n and not n.startswith(".")


def get_command(name, source):
    # v1.14.0 - `name` venait de la query string et etait interpole dans un
    # chemin sans validation : '../../..' permettait de lire n'importe quel
    # fichier .md de la machine via /api/command/.
    if not _safe_component(name):
        return False, "Nom de command invalide"
    if source == "user":
        for base in (COMMANDS_DIR, COMMANDS_DISABLED_DIR):
            p = base / f"{name}.md"
            if p.exists():
                return True, {"name": name, "source": "user", "content": p.read_text(errors="replace"),
                              "path": str(p), "active": (base == COMMANDS_DIR), "editable": True}
        return False, "Command introuvable"
    if source.startswith("plugin:"):
        full_name = source[len("plugin:"):]
        installed = _load_installed_plugins()
        if full_name not in installed:
            return False, "Plugin introuvable"
        entry = installed[full_name][0]
        cmd_path = Path(entry.get("installPath", "")) / "commands" / f"{name}.md"
        if not cmd_path.is_file():
            return False, "Command introuvable dans le plugin"
        return True, {"name": name, "source": source, "content": cmd_path.read_text(errors="replace"),
                      "path": str(cmd_path), "active": True, "editable": False}
    return False, "Source invalide"


def toggle_command(name):
    """Toggle une command utilisateur entre commands/ et commands-disabled/."""
    if not name or "/" in name or name.startswith("."):
        return False, "Nom de command invalide"
    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    COMMANDS_DISABLED_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{name}.md"
    src_a = COMMANDS_DIR / fname
    src_d = COMMANDS_DISABLED_DIR / fname
    if src_a.exists():
        target = COMMANDS_DISABLED_DIR / fname
        if target.exists():
            return False, f"Conflit : {target} existe déjà"
        src_a.rename(target)
        return True, f"Command '{name}' désactivée"
    if src_d.exists():
        target = COMMANDS_DIR / fname
        if target.exists():
            return False, f"Conflit : {target} existe déjà"
        src_d.rename(target)
        return True, f"Command '{name}' activée"
    return False, f"Command '{name}' introuvable"


PROJECTS_LOGS_DIR = HOME / ".claude/projects"
WATCHDOG_FILE = HOME / ".claude/claude-control-watchdog.json"
_WATCHDOG_EVENTS = []
_WATCHDOG_EVENTS_LOCK = threading.Lock()
_WATCHDOG_MAX_EVENTS = 100  # v1.7.0 - tab Watchdog affiche 50 events recents


def _watchdog_event(action, detail=""):
    ev = {"ts": datetime.now().isoformat(timespec="seconds"), "action": action, "detail": detail}
    with _WATCHDOG_EVENTS_LOCK:
        _WATCHDOG_EVENTS.append(ev)
        if len(_WATCHDOG_EVENTS) > _WATCHDOG_MAX_EVENTS:
            del _WATCHDOG_EVENTS[:-_WATCHDOG_MAX_EVENTS]
    _log(f"watchdog: {action} {detail}")


_DEFAULT_WATCHDOG = {
    "enabled": False,
    "auto_restart_on_crash": True,
    "freeze_detection": False,
    "interval_seconds": 30,
    "freeze_timeout": 5,
    "target": "claude_desktop",
    "target_pattern": "",
    # v1.7.0 - DC auto-remediation (orthogonal au target principal). Opt-in.
    # Detecte un freeze isole de Desktop Commander (log silencieux >N s ET
    # Claude Desktop responsive) puis tente une remediation graduee :
    # 1. toggle settings off/on (best-effort, sans kill PID)
    # 2. si echec apres N s, dialog macOS proposant restart Claude Desktop
    "dc_auto_remediation": False,
    "dc_inactivity_threshold_seconds": 120,
    "dc_verify_after_toggle_seconds": 30,
    "dc_cooldown_after_dismiss_seconds": 300,
}


def load_watchdog_config():
    cfg = dict(_DEFAULT_WATCHDOG)
    if WATCHDOG_FILE.exists():
        try:
            with open(WATCHDOG_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update({k: data[k] for k in _DEFAULT_WATCHDOG if k in data})
        except Exception:
            pass
    return cfg


def save_watchdog_config(updates):
    cfg = load_watchdog_config()
    if isinstance(updates, dict):
        for k in _DEFAULT_WATCHDOG:
            if k in updates:
                cfg[k] = updates[k]
    cfg["interval_seconds"] = max(5, int(cfg.get("interval_seconds", 30) or 30))
    cfg["freeze_timeout"] = max(1, int(cfg.get("freeze_timeout", 5) or 5))
    cfg["enabled"] = bool(cfg["enabled"])
    cfg["auto_restart_on_crash"] = bool(cfg["auto_restart_on_crash"])
    cfg["freeze_detection"] = bool(cfg["freeze_detection"])
    cfg["target"] = (cfg.get("target") or "claude_desktop").strip()
    cfg["target_pattern"] = (cfg.get("target_pattern") or "").strip()
    # v1.7.0 - bornes prudentes pour DC auto-remediation (faux positif >> faux negatif)
    cfg["dc_auto_remediation"] = bool(cfg.get("dc_auto_remediation", False))
    cfg["dc_inactivity_threshold_seconds"] = max(60, int(cfg.get("dc_inactivity_threshold_seconds", 120) or 120))
    cfg["dc_verify_after_toggle_seconds"] = max(10, int(cfg.get("dc_verify_after_toggle_seconds", 30) or 30))
    cfg["dc_cooldown_after_dismiss_seconds"] = max(120, int(cfg.get("dc_cooldown_after_dismiss_seconds", 300) or 300))
    _atomic_write_json(WATCHDOG_FILE, cfg)
    return True, cfg


def _mcp_process_fingerprint(name):
    """Construit une regex de matching pour un MCP en se basant sur sa command/args."""
    config = load_config()
    mcp = config.get("mcpServers", {}).get(name) or config.get("_disabledMcps", {}).get(name)
    if not isinstance(mcp, dict):
        return None
    cmd = mcp.get("command", "")
    args = mcp.get("args") or []
    parts = [str(cmd)] + [str(a) for a in args]
    distinctive = [p for p in parts if "/" in p or len(p) > 8 or p.endswith(".js") or p.endswith(".py")]
    if distinctive:
        return distinctive[-1]
    return cmd or None


def _find_mcp_log(name, return_method=False):
    """Trouve le fichier mcp-server-<X>.log correspondant au MCP. Tolère les
    noms avec espaces, casse différente, suffixes, etc. via normalisation.

    Si return_method=True, retourne (path|None, method) où method est:
      "exact_match"  - normalized log_name == normalized query
      "fuzzy_in"     - substring match dans une direction ou l'autre
      "none"         - aucun candidat trouvé
    """
    def _r(p, m):
        return (p, m) if return_method else p

    if not CLAUDE_LOGS_DIR.exists() or not name:
        return _r(None, "none")
    name_lc = re.sub(r'[^a-z0-9]', '', str(name).lower())
    if not name_lc:
        return _r(None, "none")
    # v1.8.2 - bug observe 2026-05-06 : si plusieurs fichiers log normalisent
    # vers le meme nom (ex. 'desktop-commander' npx legacy + 'Desktop Commander'
    # MCPB extension recent, tous deux -> 'desktopcommander'), l'ancienne logique
    # retournait le PREMIER match dans glob (ordre filesystem). Si c'etait le
    # log obsolete, le mtime check echouait -> running_by_log = False ->
    # extension affichee a tort en 'Inactifs'. Fix : collecter tous les exact
    # matches et retourner le plus recent par mtime.
    exacts = []
    fuzzy = []
    for f in CLAUDE_LOGS_DIR.glob("mcp-server-*.log"):
        stem = f.stem
        log_name = stem[len("mcp-server-"):] if stem.startswith("mcp-server-") else stem
        log_name_lc = re.sub(r'[^a-z0-9]', '', log_name.lower())
        if log_name_lc == name_lc:
            exacts.append(f)
        elif log_name_lc and (name_lc in log_name_lc or log_name_lc in name_lc):
            fuzzy.append(f)
    if exacts:
        return _r(max(exacts, key=lambda f: f.stat().st_mtime), "exact_match")
    if fuzzy:
        return _r(max(fuzzy, key=lambda f: f.stat().st_mtime), "fuzzy_in")
    return _r(None, "none")


def _pids_via_lsof(log_path):
    """Retourne les PIDs des process qui ont le fichier ouvert, en filtrant
    via la meme allow-list que _safe_pids_for_fingerprint.

    v1.6.6 : sans ce filtre, lsof peut retourner le PID du binaire principal
    /Applications/Claude.app/Contents/MacOS/Claude (qui pipe le log de ses
    extensions). Un kill -9 sur ce PID tuait Claude Desktop entier au lieu
    de redemarrer juste l'extension. Le filtre ne garde que les PIDs dont
    le first token de la cmdline est un launcher MCP connu (node, python,
    npx, etc.), ce qui correspond aux Helper Node enfants de Claude Desktop
    qui hostent reellement le code de l'extension."""
    if not log_path or not Path(log_path).exists():
        return []
    try:
        r = subprocess.run(["lsof", "-t", str(log_path)], capture_output=True, text=True, timeout=3)
    except Exception:
        return []
    my_pid = os.getpid()
    raw_pids = []
    for line in r.stdout.split():
        try:
            pid = int(line.strip())
        except Exception:
            continue
        if pid != my_pid:
            raw_pids.append(pid)
    if not raw_pids:
        return []
    safe = []
    for pid in raw_pids:
        try:
            ps = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                capture_output=True, text=True, timeout=2)
        except Exception:
            continue
        cmd = (ps.stdout or "").strip()
        if not cmd:
            continue
        if "claude-control/app.py" in cmd or "Applications/claude-control/app.py" in cmd:
            continue
        first_token = cmd.split(None, 1)[0]
        bn = Path(first_token).name.lower()
        if bn in _KILL_ALLOWED_LAUNCHERS:
            safe.append(pid)
    return safe


def _mcp_pids(name):
    fp = _mcp_process_fingerprint(name)
    if fp:
        pids = _safe_pids_for_fingerprint(fp)
        if pids:
            return pids
    log = _find_mcp_log(name)
    if log:
        pids = _pids_via_lsof(log)
        if pids:
            return pids
    return []


def _mcp_log_says_frozen(name, within_seconds):
    """Detecte un freeze via le log Claude — uniquement sur signal POSITIF
    (markers d'erreur dans la queue du log).

    v1.6.5 : on ne traite PLUS un mtime ancien comme un freeze. Beaucoup de
    MCPs n'ecrivent dans leur log que quand ils traitent une requete, donc un
    MCP sain mais idle a un log vieux de plusieurs minutes — l'ancienne
    heuristique le tuait alors qu'il etait fonctionnel. Maintenant un freeze
    n'est declare que si la queue du log contient un marker d'erreur explicite
    ('transport closed unexpectedly', 'process exiting early', ...).
    """
    log = _find_mcp_log(name)
    if not log or not log.exists():
        return False
    # v1.14.0 - _read_log_tail (seek borne) au lieu de read_text(). Avant, on
    # chargeait le fichier ENTIER en memoire pour n'en lire que les 4000
    # derniers caracteres -- sur un mcp-server-*.log de plusieurs centaines de
    # Mo, dans la boucle watchdog.
    tail = "\n".join(_read_log_tail(log, max_lines=80, max_bytes=8_000)).lower()
    if not tail:
        return False
    return any(k in tail for k in (
        "transport closed unexpectedly",
        "process exiting early",
        "process exited early",
        "server disconnected",
    ))


def _claude_pids():
    try:
        r = subprocess.run(["pgrep", "-f", "Claude.app/Contents/MacOS/Claude"],
                           capture_output=True, text=True, timeout=3)
        if r.returncode != 0:
            return []
        return [int(p) for p in r.stdout.split() if p.strip().isdigit()]
    except Exception:
        return []


def _claude_responsive(timeout=5):
    """Renvoie True si Claude répond au ping AppleScript dans le délai."""
    try:
        r = subprocess.run(
            ["osascript", "-e", 'tell application "Claude" to return name'],
            capture_output=True, timeout=timeout,
        )
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except FileNotFoundError:
        return True
    except Exception:
        return True


def _list_known_mcps():
    config = load_config()
    return sorted(set(list(config.get("mcpServers", {}).keys()) + list(config.get("_disabledMcps", {}).keys())))


# === DESKTOP EXTENSIONS ===

def _extension_settings_file(ext_id):
    return EXTENSIONS_SETTINGS_DIR / f"{ext_id}.json"


def _load_extension_settings(ext_id):
    f = _extension_settings_file(ext_id)
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(errors="replace"))
    except Exception:
        return {}


def _set_extension_enabled(settings, value):
    """v1.8.2 - Helper qui ecrit isEnabled (canonique) ET enabled (legacy)
    sur le dict in-memory pour qu'aucun consumer ne soit surpris quel que
    soit la cle qu'il lit. _save_extension_settings le fait aussi sur
    disque, mais on synchronise aussi en memoire pour que les fonctions
    qui lisent settings entre les writes voient une valeur coherente.
    """
    if not isinstance(settings, dict):
        return
    settings["isEnabled"] = bool(value)
    settings["enabled"] = bool(value)


def _read_extension_enabled(settings, fallback_entry=None):
    """v1.8.2 - Lit le statut enabled d'une extension en respectant le
    schema heterogene Anthropic : 'isEnabled' est le NOUVEAU nom canonique
    (extensions recentes), 'enabled' est l'ancien (legacy). Certains
    fichiers ont les 2 (anciens migres), d'autres seulement isEnabled
    (recents), d'autres rien (defaults).

    Bug observe 2026-05-06 : Filesystem et Stripe avaient {'isEnabled':
    false} mais Claude Control regardait 'enabled' (absent) -> tombait
    sur le fallback (status != disabled) -> True -> UI affiche 'cochee'
    -> Claude Desktop lit 'isEnabled: false' et ne lance pas l'extension.
    Resultat : reboot CD ne demarre pas l'extension.

    Priorite : isEnabled > enabled > fallback_entry (d'extensions-
    installations.json) > status != 'disabled' > True.
    """
    if not isinstance(settings, dict):
        settings = {}
    if "isEnabled" in settings:
        return bool(settings["isEnabled"])
    if "enabled" in settings:
        return bool(settings["enabled"])
    if isinstance(fallback_entry, dict):
        if "enabled" in fallback_entry:
            return bool(fallback_entry["enabled"])
        if fallback_entry.get("status") == "disabled":
            return False
    return True


def _save_extension_settings(ext_id, settings):
    f = _extension_settings_file(ext_id)
    if f.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r'[^a-zA-Z0-9_\-.]', '_', ext_id)
        try:
            shutil.copy2(f, BACKUP_DIR / f"ext-settings-{safe}.{ts}.json")
        except Exception:
            pass
    # v1.8.2 - le schema Anthropic a migre 'enabled' -> 'isEnabled'.
    # Pour eviter la desync, on synchronise les 2 cles : si l'appelant a
    # passe l'une OU l'autre OU les deux, le fichier final aura la meme
    # valeur dans isEnabled ET enabled. Claude Desktop lit isEnabled
    # (canonique), donc c'est le seul qui compte pour le runtime ; on
    # garde enabled en sync pour la retro-compat des outils tiers.
    out = dict(settings) if isinstance(settings, dict) else {}
    canonical = None
    if "isEnabled" in out:
        canonical = bool(out["isEnabled"])
    elif "enabled" in out:
        canonical = bool(out["enabled"])
    if canonical is not None:
        out["isEnabled"] = canonical
        out["enabled"] = canonical
    _atomic_write_json(f, out)


def _list_extensions():
    """Liste les Desktop Extensions installées. Tolerant aux variations de
    schéma (peut être un objet, une liste, ou un dict de dicts)."""
    if not EXTENSIONS_INSTALL_FILE.exists():
        return []
    try:
        data = json.loads(EXTENSIONS_INSTALL_FILE.read_text(errors="replace"))
    except Exception:
        return []
    entries = []
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        for key in ("installations", "extensions"):
            if isinstance(data.get(key), list):
                entries = data[key]
                break
            if isinstance(data.get(key), dict):
                entries = list(data[key].values())
                break
        if not entries and all(isinstance(v, dict) for v in data.values()):
            entries = list(data.values())
    items = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        ext_id = e.get("id") or e.get("extensionId") or e.get("identifier")
        if not ext_id:
            continue
        # v1.6.3: lire manifest.display_name (puis manifest.name) en priorite.
        # Le name racine de l'entry est en fait absent (le nom humain est imbrique
        # dans manifest), donc l'ancien fallback retombait toujours sur le slug
        # technique de l'id, ce qui cassait le matching log<->extension pour
        # toute extension dont le display_name differe de son slug
        # (ex: chrome-control vs "Control Chrome", osascript vs "Control your Mac",
        # ms_office_word vs "Word (By Anthropic)", etc).
        manifest = e.get("manifest") if isinstance(e.get("manifest"), dict) else {}
        name = (
            manifest.get("display_name")
            or manifest.get("name")
            or e.get("name")
            or e.get("displayName")
            or str(ext_id).split(".")[-1]
        )
        version = str(e.get("version") or e.get("manifestVersion") or manifest.get("version") or "")
        settings = _load_extension_settings(ext_id)
        # v1.8.2 - lit isEnabled en priorite (cf. _read_extension_enabled).
        enabled = _read_extension_enabled(settings, fallback_entry=e)
        env_keys = list(settings.get("env", {}).keys()) if isinstance(settings.get("env"), dict) else []
        items.append({
            "id": ext_id,
            "name": str(name),
            "version": version,
            "enabled": enabled,
            "type": "extension",
            "env_keys": env_keys,
        })
    items.sort(key=lambda x: x["name"].lower())
    return items


def _list_raw_extensions():
    """v1.8.1 - Variante de _list_extensions qui retourne aussi le manifest
    brut, utilise par _detect_mcp_conflicts pour comparer avec les args du
    classic MCP (matcher npm package name comme signal de conflit fort)."""
    if not EXTENSIONS_INSTALL_FILE.exists():
        return []
    try:
        data = json.loads(EXTENSIONS_INSTALL_FILE.read_text(errors="replace"))
    except Exception:
        return []
    entries = []
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        for key in ("installations", "extensions"):
            if isinstance(data.get(key), list):
                entries = data[key]
                break
            if isinstance(data.get(key), dict):
                entries = list(data[key].values())
                break
        if not entries and all(isinstance(v, dict) for v in data.values()):
            entries = list(data.values())
    out = []
    for e in entries:
        if isinstance(e, dict) and (e.get("id") or e.get("extensionId") or e.get("identifier")):
            out.append(e)
    return out


def _normalize_mcp_name(s):
    """v1.8.1 - Normalisation cohérente avec _find_mcp_log (v1.6.3) :
    lowercase + suppression de tout char non alphanum. Permet de matcher
    'Desktop Commander' (display_name MCPB) avec 'desktop-commander'
    (clé config) -> 'desktopcommander' des deux cotes."""
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


_NPM_PACKAGE_RE = re.compile(r"@[a-z0-9][a-z0-9\-_.]*/[a-z0-9][a-z0-9\-_.]+", re.IGNORECASE)


def _extract_npm_packages(value):
    """v1.8.1 - Extrait recursivement les patterns @scope/package des
    valeurs (str, list, dict). Retourne un set de packages normalises
    en lowercase."""
    found = set()

    def _walk(v):
        if isinstance(v, str):
            for m in _NPM_PACKAGE_RE.findall(v):
                found.add(m.lower())
        elif isinstance(v, list):
            for it in v:
                _walk(it)
        elif isinstance(v, dict):
            for it in v.values():
                _walk(it)

    _walk(value)
    return found


def _detect_mcp_conflicts():
    """v1.8.1 - Croise Desktop Extensions et entrees classic dans
    claude_desktop_config.json -> mcpServers / _disabledMcps. Retourne
    la liste des conflits detectes.

    Apprentissage 2026-05-06 : DC tournait en double (extension MCPB
    'ant.dir.gh.wonderwhy-er.desktopcommandermcp' + cle 'desktop-commander'
    en npx dans le config). 16 Helper Nodes au lieu de 8. Cause probable
    du freeze Type B (concurrence + duplicate tool calls).

    Critere de conflit (l'un des deux suffit, both=match fort) :
      - name : nom normalise extension == nom normalise cle config
        ('Desktop Commander' -> 'desktopcommander', 'desktop-commander'
        -> 'desktopcommander')
      - package : meme @scope/package dans args du config ET dans
        manifest extension (signal fort, pas de faux positifs).

    Pour chaque conflit, recommandation par defaut : remove_classic.
    Les extensions sont managees (marketplace), les entrees manuelles
    config dupliquent des extensions et peuvent rester orphelines apres
    une mise a jour MCPB. Garder l'extension comme source de verite.
    """
    config = load_config()
    classic_active = config.get("mcpServers", {}) or {}
    classic_disabled = config.get("_disabledMcps", {}) or {}
    raw_exts = _list_raw_extensions()

    # Pour chaque extension : nom normalise + packages npm extraits du manifest
    ext_index = []
    for e in raw_exts:
        eid = e.get("id") or e.get("extensionId") or e.get("identifier")
        manifest = e.get("manifest") if isinstance(e.get("manifest"), dict) else {}
        display = (
            manifest.get("display_name")
            or manifest.get("name")
            or e.get("name")
            or e.get("displayName")
            or str(eid).split(".")[-1]
        )
        ext_index.append({
            "id": eid,
            "name": str(display),
            "norm_name": _normalize_mcp_name(display),
            "norm_id_slug": _normalize_mcp_name(str(eid).split(".")[-1]),
            "packages": _extract_npm_packages(manifest) | _extract_npm_packages(e),
            "enabled": _read_extension_enabled(_load_extension_settings(eid), fallback_entry=e),
        })

    conflicts = []
    for bucket_name, bucket in (("active", classic_active), ("disabled", classic_disabled)):
        for cfg_name, cfg_entry in bucket.items():
            if not isinstance(cfg_entry, dict):
                continue
            cfg_norm = _normalize_mcp_name(cfg_name)
            cfg_packages = _extract_npm_packages(cfg_entry)
            for ext in ext_index:
                name_match = (
                    cfg_norm == ext["norm_name"]
                    or cfg_norm == ext["norm_id_slug"]
                )
                package_match = bool(cfg_packages & ext["packages"])
                if not (name_match or package_match):
                    continue
                if name_match and package_match:
                    match_type = "both"
                elif package_match:
                    match_type = "package"
                else:
                    match_type = "name"
                conflicts.append({
                    "classic_name": cfg_name,
                    "classic_active": bucket_name == "active",
                    "classic_command": cfg_entry.get("command", ""),
                    "classic_args": cfg_entry.get("args") or [],
                    "extension_id": ext["id"],
                    "extension_name": ext["name"],
                    "extension_enabled": ext["enabled"],
                    "match_type": match_type,
                    "matched_packages": sorted(cfg_packages & ext["packages"]),
                    "recommendation": "remove_classic",
                })
    return conflicts


def resolve_mcp_conflict(classic_name, action="remove_classic"):
    """v1.8.1 - Resout un conflit MCP detecte par _detect_mcp_conflicts.
    Pour 'remove_classic' : supprime l'entree classic (active ou disabled)
    de claude_desktop_config.json. Backup horodate cree par save_config.
    Garde-fou : on ne supprime que l'entree classic confirmee comme etant
    en conflit avec une extension active. Pas d'action automatique sur
    l'extension (elle reste source de verite).
    """
    if action != "remove_classic":
        return False, f"Action '{action}' non supportee (seul 'remove_classic' implementee)"
    if not classic_name:
        return False, "Nom du MCP classic requis"
    conflicts = _detect_mcp_conflicts()
    target = next((c for c in conflicts if c["classic_name"] == classic_name), None)
    if not target:
        return False, f"Aucun conflit detecte pour '{classic_name}' (deja resolu ?)"
    config = load_config()
    found = False
    for bucket in ("mcpServers", "_disabledMcps"):
        if classic_name in config.get(bucket, {}):
            del config[bucket][classic_name]
            found = True
    if not found:
        return False, f"Entree '{classic_name}' introuvable dans le config"
    save_config(config)
    return True, (f"Conflit resolu : entree classic '{classic_name}' retiree "
                  f"de claude_desktop_config.json (backup horodate). L'extension "
                  f"'{target['extension_name']}' reste en place comme source de verite.")


def toggle_extension(name, enabled=None):
    """Toggle une extension via son fichier de settings (avec backup)."""
    # v1.14.0 - l'action change l'etat des process : on vide les caches
    # pour que le prochain /api/state dise la verite immediatement.
    _invalidate_process_caches()
    if not name:
        return False, "Nom requis"
    extensions = _list_extensions()
    target = next((e for e in extensions if e["name"] == name or e["id"] == name), None)
    if not target:
        return False, f"Extension '{name}' introuvable"
    settings = _load_extension_settings(target["id"])
    if enabled is None:
        enabled = not _read_extension_enabled(settings)
    # v1.8.2 - ecrit isEnabled (canonique). _save_extension_settings
    # synchronise enabled = isEnabled pour la retro-compat.
    _set_extension_enabled(settings, enabled)
    try:
        _save_extension_settings(target["id"], settings)
    except Exception as e:
        return False, f"Erreur écriture settings : {e}"
    label = "activée" if enabled else "désactivée"
    return True, f"Extension '{name}' {label}"


_KILL_ALLOWED_LAUNCHERS = {"node", "python", "python3", "npx", "bun", "deno",
                            "java", "ruby", "uvx", "uv", "go", "rust", "cargo",
                            "electron", "tsx", "ts-node"}


def _safe_pids_for_fingerprint(fingerprint):
    """Allow-list approach: a PID is killable only if its command's first token
    is a known MCP launcher (node/python/npx/...) OR if the fingerprint appears
    in the binary path itself. Anything else (shells, curl wrappers, timeout,
    bash -c chains containing the fingerprint as an arg) is left alone."""
    try:
        r = subprocess.run(["pgrep", "-fla", fingerprint], capture_output=True, text=True, timeout=3)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    my_pid = os.getpid()
    fp_lc = fingerprint.lower()
    pids = []
    for line in r.stdout.splitlines():
        sp = line.strip().split(None, 1)
        if len(sp) != 2:
            continue
        try:
            pid = int(sp[0])
        except Exception:
            continue
        if pid == my_pid:
            continue
        cmd = sp[1]
        if "claude-control/app.py" in cmd or "Applications/claude-control/app.py" in cmd:
            continue
        first_token = cmd.split(None, 1)[0]
        bn = Path(first_token).name.lower()
        if bn in _KILL_ALLOWED_LAUNCHERS:
            pids.append(pid)
        elif fp_lc in first_token.lower():
            pids.append(pid)
    return pids


def _extension_pids(ext_name_or_id):
    """Trouve les PIDs d'une extension via fingerprints (du plus spécifique au
    moins) puis fallback sur lsof du log file Claude correspondant."""
    extensions = _list_extensions()
    target = next((e for e in extensions if e["name"] == ext_name_or_id or e["id"] == ext_name_or_id), None)
    if not target:
        return []
    candidates = [target["id"], target["id"].split(".")[-1], target["name"]]
    seen = set()
    ordered = []
    for fp in candidates:
        if fp and len(fp) >= 6 and fp not in seen:
            seen.add(fp)
            ordered.append(fp)
    for fp in ordered:
        pids = _safe_pids_for_fingerprint(fp)
        if pids:
            return pids
    for nm in (target["name"], target["id"], target["id"].split(".")[-1]):
        log = _find_mcp_log(nm)
        if log:
            pids = _pids_via_lsof(log)
            if pids:
                return pids
    return []


def diagnose_extensions():
    """v1.6.4 - Surface tous les mismatches manifest <-> log <-> process pour
    chaque Desktop Extension installee. Le bug v1.6.3 (5 extensions actives
    affichees running:false sans aucune indication) est typique de ces
    mismatches silencieux : un display_name qui ne matche pas le log file, un
    process qui n'apparait dans aucune fingerprint connue, un log inactif
    depuis longtemps, etc.

    Retourne un dict {extensions: [...], summary: {...}}.
    Pour chaque extension, on tente d'abord de matcher le log via le nom
    (display_name), puis via le slug d'id si echec.
    """
    if not EXTENSIONS_INSTALL_FILE.exists():
        return {"extensions": [], "summary": {"total": 0, "with_warnings": 0}}
    try:
        raw = json.loads(EXTENSIONS_INSTALL_FILE.read_text(errors="replace"))
    except Exception:
        return {"extensions": [], "summary": {"total": 0, "with_warnings": 0}}

    raw_entries = []
    if isinstance(raw, list):
        raw_entries = raw
    elif isinstance(raw, dict):
        for key in ("installations", "extensions"):
            if isinstance(raw.get(key), list):
                raw_entries = raw[key]
                break
            if isinstance(raw.get(key), dict):
                raw_entries = list(raw[key].values())
                break
        if not raw_entries and all(isinstance(v, dict) for v in raw.values()):
            raw_entries = list(raw.values())

    raw_by_id = {}
    for e in raw_entries:
        if isinstance(e, dict):
            eid = e.get("id") or e.get("extensionId") or e.get("identifier")
            if eid:
                raw_by_id[eid] = e

    out = []
    for ext in _list_extensions():
        ext_id = ext["id"]
        name = ext["name"]
        raw_entry = raw_by_id.get(ext_id, {})
        manifest = raw_entry.get("manifest") if isinstance(raw_entry.get("manifest"), dict) else None
        has_manifest = manifest is not None
        has_display_name = bool(manifest.get("display_name")) if has_manifest else False

        id_slug = str(ext_id).split(".")[-1]
        log, method = _find_mcp_log(name, return_method=True)
        if not log and id_slug and id_slug.lower() != name.lower():
            log2, _m2 = _find_mcp_log(id_slug, return_method=True)
            if log2:
                log = log2
                method = "fallback_id_slug"
        log_path = str(log) if log else None
        log_mtime_age = None
        if log:
            try:
                log_mtime_age = int(time.time() - log.stat().st_mtime)
            except Exception:
                log_mtime_age = None

        pids = []
        pid_method = "none"
        candidates = [ext_id, id_slug, name]
        for fp in candidates:
            if fp and len(fp) >= 6:
                p = _safe_pids_for_fingerprint(fp)
                if p:
                    pids = p
                    pid_method = "fingerprint"
                    break
        if not pids and log:
            p = _pids_via_lsof(log)
            if p:
                pids = p
                pid_method = "lsof"

        manifest_version = (manifest or {}).get("version") if has_manifest else None
        top_version = ext.get("version") or ""

        warnings = []
        main_log_hints = None
        if ext["enabled"] and not log:
            warnings.append("enabled_but_no_log")
            # v1.8.3 Bug E - scan main.log pour comprendre pourquoi cette
            # extension cochee n'a pas de log file (jamais demarree).
            # Hypothese principale : allowlist dxt: qui bloque silencieusement
            # (ajoute par Anthropic apres la faille Ace of Aces 2026-02).
            try:
                main_log_hints = (
                    _scan_main_log_for_extension_failures(name)
                    or _scan_main_log_for_extension_failures(ext_id)
                )
            except Exception:
                main_log_hints = None
        if log and log_mtime_age is not None and log_mtime_age > 300:
            warnings.append("log_inactive_5min")
        if log and log_mtime_age is not None and log_mtime_age <= 300 and not pids:
            warnings.append("log_active_no_pid")
        if has_manifest and not has_display_name:
            warnings.append("display_name_missing")
        if not has_manifest:
            warnings.append("manifest_missing")
        if manifest_version and top_version and str(manifest_version) != str(top_version):
            warnings.append("version_mismatch")

        out.append({
            "id": ext_id,
            "name": name,
            "enabled": ext["enabled"],
            "has_manifest": has_manifest,
            "has_display_name": has_display_name,
            "version": top_version,
            "manifest_version": manifest_version,
            "log_path": log_path,
            "log_match_method": method,
            "log_mtime_age_seconds": log_mtime_age,
            "pids": pids,
            "pid_method": pid_method,
            "warnings": warnings,
            "main_log_hints": main_log_hints,  # v1.8.3 Bug E - lignes main.log si jamais demarre
        })

    with_warnings = sum(1 for e in out if e["warnings"])
    return {
        "extensions": out,
        "summary": {"total": len(out), "with_warnings": with_warnings},
    }


def restart_extension(name):
    """Bounce une extension : kill le process via fingerprints, puis toggle off/on
    son setting (Claude Desktop respawn)."""
    # v1.14.0 - l'action change l'etat des process : on vide les caches
    # pour que le prochain /api/state dise la verite immediatement.
    _invalidate_process_caches()
    extensions = _list_extensions()
    target = next((e for e in extensions if e["name"] == name or e["id"] == name), None)
    if not target:
        return False, f"Extension '{name}' introuvable"
    pids = _extension_pids(name)
    killed = 0
    for pid in pids:
        try:
            os.kill(pid, 9)
            killed += 1
        except Exception:
            pass
    settings = _load_extension_settings(target["id"])
    was_enabled = _read_extension_enabled(settings)
    _set_extension_enabled(settings, False)
    try:
        _save_extension_settings(target["id"], settings)
        time.sleep(1.5)
        _set_extension_enabled(settings, was_enabled)
        _save_extension_settings(target["id"], settings)
    except Exception as e:
        return False, f"Erreur toggle settings : {e}"
    _LAST_RESTARTS[name] = datetime.now().isoformat(timespec="seconds")
    msg = f"Extension '{name}' redémarrée (killed {killed}, settings togglée)"
    _notify(f"{name} redémarré", f"{killed} process tué - Claude Desktop intact")
    return True, msg


def dc_status():
    """v1.6.6 - Etat detaille de Desktop Commander pour la carte Action rapide
    en haut de la tab MCPs. Retourne null si DC n'est pas installe."""
    extensions = _list_extensions()
    target = next((e for e in extensions
                   if e["name"] == "Desktop Commander"
                   or "desktopcommander" in e["id"].lower().replace("-", "")
                   or "desktopcommander" in e["name"].lower().replace(" ", "")),
                  None)
    if not target:
        return None
    log_path, log_method = _find_mcp_log(target["name"], return_method=True)
    log_age = None
    if log_path and log_path.exists():
        try:
            log_age = int(time.time() - log_path.stat().st_mtime)
        except Exception:
            pass
    pids = _extension_pids(target["name"])
    last_restart_iso = _LAST_RESTARTS.get(target["name"]) or _LAST_RESTARTS.get(target["id"])
    last_restart_age = None
    if last_restart_iso:
        try:
            dt = datetime.fromisoformat(last_restart_iso)
            last_restart_age = int((datetime.now() - dt).total_seconds())
        except Exception:
            pass
    # v1.6.6 : sur Claude Desktop moderne, DC tourne dans un Helper Node embarque
    # (node.mojom.NodeService) anonyme parmi 7 helpers identiques. lsof retourne
    # le main Claude (filtre par allow-list) et pgrep ne match pas car la cmdline
    # est generique. Le seul indicateur fiable de "DC est vivant" devient le
    # mtime du log : si DC a ecrit dans son log dans les 120 dernieres secondes,
    # on considere qu'il tourne. Au-dela, suspect ou freeze.
    running_by_pid = len(pids) > 0
    running_by_log = log_age is not None and log_age <= 120
    running = running_by_pid or running_by_log
    return {
        "name": target["name"],
        "id": target["id"],
        "version": target["version"],
        "enabled": target["enabled"],
        "running": running,
        "running_by_pid": running_by_pid,
        "running_by_log": running_by_log,
        "pids": pids,
        "log_path": str(log_path) if log_path else None,
        "log_age_seconds": log_age,
        "last_restart_iso": last_restart_iso,
        "last_restart_age_seconds": last_restart_age,
        "arch_note": (
            "DC tourne dans un Helper Node embarque de Claude Desktop "
            "(node.mojom.NodeService). Pas de PID granulaire identifiable. "
            "Le statut est inferre via le mtime du log."
        ) if not running_by_pid else None,
    }


def restart_claude_desktop():
    """v1.6.6 - Option nucleaire : redemarre Claude Desktop entier.

    Sur Claude Desktop moderne, redemarrer une extension granulairement n'est
    pas possible (les Helper Node embarques sont anonymes). La seule garantie
    de debloquer une extension freezee est de redemarrer Claude Desktop. Cela
    ferme toutes les conversations en cours - a utiliser quand le toggle
    settings best-effort n'a pas suffi.
    """
    # v1.14.0 - l'action change l'etat des process : on vide les caches
    # pour que le prochain /api/state dise la verite immediatement.
    _invalidate_process_caches()
    try:
        subprocess.run(["pkill", "-9", "-f", CLAUDE_DESKTOP_BIN],
                       capture_output=True, timeout=5)
        time.sleep(2)
        subprocess.run(["open", "-a", "Claude"], capture_output=True, timeout=5)
        _LAST_RESTARTS["__claude_desktop__"] = datetime.now().isoformat(timespec="seconds")
        _notify("Claude Desktop redemarre", "Tous les MCPs et extensions vont reapparaitre dans 5-10s")
        return True, "Claude Desktop redemarre - patience 5-10s"
    except Exception as e:
        return False, f"Erreur restart Claude Desktop : {e}"


def scan_processes(pattern):
    """Recherche des process matchant un pattern via `pgrep -fla`. Retourne
    une liste [{pid, cmd}] limitée et tronquée. Defensive."""
    pattern = (pattern or "").strip()
    if not pattern or len(pattern) < 2:
        return {"matches": [], "pattern": pattern, "error": "Pattern trop court (>= 2 caractères)"}
    try:
        r = subprocess.run(["pgrep", "-fla", pattern], capture_output=True, text=True, timeout=4)
    except FileNotFoundError:
        return {"matches": [], "pattern": pattern, "error": "pgrep introuvable"}
    except Exception as e:
        return {"matches": [], "pattern": pattern, "error": str(e)[:80]}
    matches = []
    my_pid = os.getpid()
    for line in (r.stdout or "").splitlines()[:30]:
        line = line.strip()
        if not line:
            continue
        sp = line.split(None, 1)
        if len(sp) != 2:
            continue
        try:
            pid = int(sp[0])
        except Exception:
            continue
        if pid == my_pid:
            continue
        cmd = sp[1]
        if cmd.startswith("/usr/bin/python3") and "src/app.py" in cmd:
            continue
        matches.append({"pid": pid, "cmd": cmd[:200]})
    return {"matches": matches, "pattern": pattern, "count": len(matches)}


def _custom_target_pids(pattern):
    if not pattern or len(pattern) < 2:
        return []
    return _safe_pids_for_fingerprint(pattern)


def get_watchdog_status():
    cfg = load_watchdog_config()
    target = cfg.get("target", "claude_desktop")
    # v1.8.2 - bug observe 2026-05-06 : Fabien a retire 'desktop-commander'
    # de claude_desktop_config.json, mais le bandeau watchdog continuait
    # d'afficher 'desktop-commander - Claude arrete' 1h apres. Le watchdog
    # ne re-validait pas que sa cible existait toujours dans la config MCP.
    # Fix : si la cible n'est plus dans _list_known_mcps() ni dans les
    # extensions installees ET ce n'est pas claude_desktop/custom, on
    # auto-reset a 'claude_desktop' et on log un event pour la trace.
    if target not in ("claude_desktop", "custom"):
        known = set(_list_known_mcps())
        try:
            ext_names = {e["name"] for e in _list_extensions()}
        except Exception:
            ext_names = set()
        if target not in known and target not in ext_names:
            _watchdog_event(
                "target_auto_reset",
                f"Cible '{target}' disparue (plus dans claude_desktop_config.json "
                f"ni dans les extensions). Auto-reset vers 'claude_desktop'."
            )
            save_watchdog_config({"target": "claude_desktop"})
            cfg = load_watchdog_config()
            target = "claude_desktop"
    if target == "claude_desktop":
        pids = _claude_pids()
        target_label = "Claude Desktop"
    elif target == "custom":
        pids = _custom_target_pids(cfg.get("target_pattern", ""))
        target_label = (cfg.get("target_pattern") or "?").strip() or "?"
    else:
        pids = _mcp_pids(target)
        target_label = target
    with _WATCHDOG_EVENTS_LOCK:
        # v1.7.0 - 50 derniers events pour la tab Watchdog. Le widget compact
        # n'utilise toujours que les 3 premiers, donc pas de regression.
        events = list(_WATCHDOG_EVENTS[-50:][::-1])
    return {
        "config": cfg,
        "claude_running": len(pids) > 0,
        "claude_pids": pids,
        "target_label": target_label,
        "available_targets": ["claude_desktop"] + _list_known_mcps() + ["custom"],
        "events": events,
    }


# v1.7.0 - state machine de la remediation DC. Garde en memoire le dernier
# verdict pour eviter le spam : "Plus tard" -> cooldown long, toggle reussi
# -> cooldown court de stabilisation, toggle echoue -> escalade dialog.
# Tout est in-memory : si l'app redemarre, on repart d'une page blanche.
_DC_REMEDIATION_STATE = {
    "last_action_ts": 0.0,
    "cooldown_until_ts": 0.0,
    "last_toggle_log_mtime": 0.0,
    "pending_verify_until_ts": 0.0,
    "dialog_in_flight": False,
    "dialog_lock": threading.Lock(),
}


_DC_GRACEFUL_SHUTDOWN_MARKERS = (
    "server transport closed (intentional shutdown)",
    "shutting down server",
    "process exited gracefully",
    "client transport closed",
)

# v1.8.4 - markers d'activite recente. Si l'un d'eux apparait APRES un
# marker de shutdown dans le tail du log, ca signale qu'une nouvelle
# instance s'est demarree depuis le shutdown - le log est en realite
# vivant. Critere positif fort : init explicite ou message protocolaire.
_MCP_ACTIVE_MARKERS = (
    "initializing server",
    "server started",
    "server connected",
    "message from client",
    "message from server",
)


def _read_log_tail(path, max_lines=200, max_bytes=64_000):
    """v1.8.3 - Lit les `max_lines` dernieres lignes en seekant depuis la
    fin du fichier (max `max_bytes` bytes lus). Defensive contre les gros
    logs : main.log Claude Desktop peut faire des dizaines de Mo, et
    f.readlines() chargerait tout en memoire (et l'apprentissage du
    freeze Type B nous a appris a ne pas lire de blob > 25k chars sans
    raison). On jette la premiere ligne potentiellement tronquee a
    mi-chemin par le seek.
    Retourne la liste des lignes (sans CR/LF). Tolerant aux erreurs.
    """
    try:
        path_obj = Path(path)
        if not path_obj.exists():
            return []
        with open(path_obj, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            offset = max(0, size - max_bytes)
            f.seek(offset)
            blob = f.read(max_bytes)
        text = blob.decode("utf-8", errors="replace")
        # Si on n'a pas lu depuis le debut, jeter la premiere ligne
        # (probablement coupee a mi-chemin par le seek).
        if offset > 0 and "\n" in text:
            text = text.split("\n", 1)[1]
        lines = text.splitlines()
    except Exception:
        return []
    if max_lines and len(lines) > max_lines:
        lines = lines[-max_lines:]
    return lines


def _scan_main_log_for_extension_failures(name_or_id, max_lines=200, max_bytes=128_000):
    """v1.8.3 - Scan tail de Claude Desktop main.log pour des erreurs liees
    a une extension qui n'a JAMAIS demarre (pas de mcp-server-X.log).
    Bug E observe 2026-05-06 : Stripe avait isEnabled:true dans son
    settings mais aucun log -> CD ne tente meme pas de la demarrer.
    Hypothese : allowlist dxt: qui bloque silencieusement.

    Cherche dans le tail :
      - le nom de l'extension (case-insensitive et normalise)
      - + un keyword d'echec : error, failed, rejected, blocked, denied,
        allowlist, not allowed, unable to load

    Retourne :
      - None si main.log absent ou aucun match
      - list[str] (max 3 dernieres lignes matchant) sinon

    Defensive : utilise _read_log_tail (seek-based, 128 KB cap par
    defaut, jette la 1ere ligne potentiellement tronquee). Pas de
    risque de freeze meme si main.log fait 100+ Mo.
    """
    main_log = CLAUDE_LOGS_DIR / "main.log"
    if not main_log.exists() or not name_or_id:
        return None
    name_lc = str(name_or_id).lower()
    norm = re.sub(r"[^a-z0-9]", "", name_lc)
    if not norm or len(norm) < 3:
        return None
    tail = _read_log_tail(main_log, max_lines=max_lines, max_bytes=max_bytes)
    failure_kws = ("error", "failed", "rejected", "blocked", "denied",
                   "allowlist", "not allowed", "unable to load",
                   "cannot load", "not started")
    matches = []
    for line in tail:
        line_lc = line.lower()
        line_norm = re.sub(r"[^a-z0-9]", "", line_lc)
        if norm in line_norm or name_lc in line_lc:
            if any(kw in line_lc for kw in failure_kws):
                matches.append(line.strip()[:300])
    if matches:
        return matches[-3:]
    return None


def _log_shows_graceful_shutdown(path, max_lines=20):
    """v1.8.0 / v1.8.4 - Retourne True ssi le log se TERMINE par un
    shutdown gracieux. Apprentissage 2026-05-06 (Bug v1.8.3) : un log
    persiste au reboot Claude Desktop. Apres reboot, le tail contient
    typiquement :
      [ancien CD] Shutting down server...
      [ancien CD] Client transport closed
      [nouveau CD] Initializing server...
      [nouveau CD] Server started and connected successfully
      [nouveau CD] Message from client: {...}
    L'ancienne logique 'any shutdown marker in tail' classifiait ce
    log comme shut down, alors que la nouvelle instance est vivante.
    Resultat : 7/14 MCPs MCPB en standby legitime classees 'Inactifs'.

    Fix v1.8.4 : on scanne le tail en ordre INVERSE, on s'arrete au
    premier marker trouve. Si le dernier marker est un activity (init,
    Server started, Message from ...), on retourne False (vivant). Si
    c'est un shutdown, True. Si aucun marker connu, conservateur :
    False (statu quo, log considere comme vivant tant qu'on n'a pas
    de signal explicite).
    """
    tail = _read_log_tail(path, max_lines=max_lines)
    if not tail:
        return False
    for line in reversed(tail):
        line_lc = line.lower()
        if any(m in line_lc for m in _MCP_ACTIVE_MARKERS):
            return False  # activite recente, log vivant
        if any(m in line_lc for m in _DC_GRACEFUL_SHUTDOWN_MARKERS):
            return True  # shutdown recent, log mort
    return False  # aucun marker connu, conservateur


def _classify_dc_log_freeze_type(path, max_lines=200):
    """v1.8.0 - Distingue Type A (backend frozen) vs Type B (UI rendering
    frozen) en parcourant les `max_lines` dernieres lignes du log Claude.

    Type A : le client a envoye une requete et le serveur n'a JAMAIS
    repondu (id client sans server response matching). DC backend gele.

    Type B : DC a repondu a tout, mais le client n'envoie plus de
    nouvelles requetes (silence cote Claude Desktop). UI rendering gele,
    backend en bonne sante.

    Signaux bonus (n'affectent pas la decision Type A/B mais loggees
    dans details pour l'observabilite) :
      - duplicate_read_file : meme path lu 2+ fois en < 5s
      - large_payload : reponse server > 20 000 chars
      - track_ui_event_burst : > 10 track_ui_event en < 5s

    Retourne dict {type, details}. Le type peut etre :
      'frozen_backend' (Type A), 'frozen_ui_rendering' (Type B), ou
      'inconclusive' (pas assez de donnees parsables).
    """
    lines = _read_log_tail(path, max_lines=max_lines)
    if not lines:
        return {"type": "inconclusive", "details": {"reason": "empty_log"}}

    client_re = re.compile(r"Message from client:\s*(\{.*?\})\s*\{ metadata")
    server_re = re.compile(r"Message from server:\s*(\{.*?\})\s*\{ metadata")
    ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)")

    client_ids = []
    server_ids = []
    read_file_paths = []  # liste de (ts_str, path)
    track_ui_events = []  # liste de ts_str
    max_payload_size = 0

    for line in lines:
        ts_match = ts_re.match(line)
        ts_str = ts_match.group(1) if ts_match else None
        cm = client_re.search(line)
        if cm:
            try:
                payload = json.loads(cm.group(1))
                cid = payload.get("id")
                if cid is not None:
                    client_ids.append(cid)
                params = payload.get("params") or {}
                tool_name = params.get("name")
                args = params.get("arguments") or {}
                if tool_name == "read_file":
                    p = args.get("path")
                    if p:
                        read_file_paths.append((ts_str, p))
                if tool_name == "track_ui_event":
                    track_ui_events.append(ts_str)
            except Exception:
                pass
            continue
        sm = server_re.search(line)
        if sm:
            try:
                payload = json.loads(sm.group(1))
                sid = payload.get("id")
                if sid is not None and ("result" in payload or "error" in payload):
                    server_ids.append(sid)
                # Estimation taille payload : longueur JSON + reconstitution si
                # le contenu a ete tronque (marqueur '[N chars truncated]').
                size = len(sm.group(1))
                trunc_match = re.search(r"\[(\d+) chars truncated\]", line)
                if trunc_match:
                    size += int(trunc_match.group(1))
                if size > max_payload_size:
                    max_payload_size = size
            except Exception:
                pass

    unanswered = sorted(set(client_ids) - set(server_ids))

    # Bonus : duplicate read_file en < 5s. Compare timestamps si dispo.
    duplicate_read_file = False
    by_path = {}
    for ts_str, p in read_file_paths:
        by_path.setdefault(p, []).append(ts_str)
    for p, ts_list in by_path.items():
        if len(ts_list) < 2:
            continue
        # Si 2+ occurrences avec timestamps parsables et span < 5s -> duplicate.
        parsed = []
        for t in ts_list:
            if not t:
                continue
            try:
                parsed.append(datetime.fromisoformat(t.replace("Z", "+00:00")))
            except Exception:
                pass
        if len(parsed) >= 2:
            span = (max(parsed) - min(parsed)).total_seconds()
            if span <= 5.0:
                duplicate_read_file = True
                break
        elif len(ts_list) >= 2:
            # Sans timestamps fiables, on flagge quand meme si 2+ duplicates
            # consecutifs sur le meme path (cas du log type B).
            duplicate_read_file = True
            break

    # Bonus : burst de track_ui_event >= 10 events dans une fenetre <= 5s.
    # Sliding window 10 events / 5s : conservateur mais matche un burst typique
    # (apprentissage 2026-05-05 : 14 events en 6.2s avec 10 events en 4.25s).
    track_ui_event_burst = False
    if len(track_ui_events) >= 10:
        parsed = []
        for t in track_ui_events:
            if not t:
                continue
            try:
                parsed.append(datetime.fromisoformat(t.replace("Z", "+00:00")))
            except Exception:
                pass
        if len(parsed) >= 10:
            parsed.sort()
            for i in range(len(parsed) - 9):
                if (parsed[i + 9] - parsed[i]).total_seconds() <= 5.0:
                    track_ui_event_burst = True
                    break
        else:
            track_ui_event_burst = True  # 10+ events, ts non parsable -> burst probable

    large_payload = max_payload_size > 20000

    details = {
        "client_ids_count": len(client_ids),
        "server_ids_count": len(server_ids),
        "unanswered_client_ids": unanswered[:10],
        "duplicate_read_file": duplicate_read_file,
        "large_payload": large_payload,
        "track_ui_event_burst": track_ui_event_burst,
        "max_payload_chars": max_payload_size,
    }

    if not client_ids:
        return {"type": "inconclusive", "details": details}
    if unanswered:
        return {"type": "frozen_backend", "details": details}
    return {"type": "frozen_ui_rendering", "details": details}


def _dc_freeze_classify(cfg, now=None):
    """v1.7.0 + v1.8.0 - Retourne un dict classifiant l'etat actuel de DC :
      verdict in {
        'no_dc',              # DC pas installe
        'cooldown',           # cooldown actif suite a un dismiss/toggle recent
        'pending_verify',     # toggle vient d'etre fait, on attend verify
        'dialog_in_flight',   # dialog deja ouvert quelque part
        'idle_legitimate',    # DC + Claude responsive + log frais (rien a faire)
        'global_freeze',      # Claude Desktop unresponsive (pas DC isole, ne pas agir)
        'no_log',             # DC enabled mais pas de log file (ou shutdown gracieux)
        'frozen_isolated',    # legacy alias (Type A non distingue) - garde pour retro-compat
        'frozen_backend',     # v1.8.0 Type A : DC backend gele (id client sans reponse)
        'frozen_ui_rendering',# v1.8.0 Type B : DC backend OK, Claude Desktop UI gele
      }
    Donnees brutes incluses pour les events watchdog.
    """
    now = now if now is not None else time.time()
    threshold = cfg.get("dc_inactivity_threshold_seconds", 120)
    state = _DC_REMEDIATION_STATE
    if now < state["cooldown_until_ts"]:
        return {"verdict": "cooldown", "cooldown_remaining": int(state["cooldown_until_ts"] - now)}
    if state["dialog_in_flight"]:
        return {"verdict": "dialog_in_flight"}
    if now < state["pending_verify_until_ts"]:
        return {"verdict": "pending_verify",
                "pending_remaining": int(state["pending_verify_until_ts"] - now)}

    dc = dc_status()
    if not dc:
        return {"verdict": "no_dc"}
    log_path = dc.get("log_path")
    log_age = dc.get("log_age_seconds")
    if log_path is None or log_age is None:
        return {"verdict": "no_log", "dc": dc}
    if log_age <= threshold:
        # v1.8.0 P1 - filtre shutdown gracieux : si la queue du log montre un
        # transport closed intentionnel, l'instance s'est arretee (mtime
        # trompeur) - on classe no_log plutot que idle_legitimate.
        try:
            if _log_shows_graceful_shutdown(log_path):
                return {"verdict": "no_log", "dc": dc, "log_age": log_age,
                        "graceful_shutdown": True}
        except Exception:
            pass
        return {"verdict": "idle_legitimate", "dc": dc, "log_age": log_age, "threshold": threshold}
    if not _claude_responsive(timeout=2):
        return {"verdict": "global_freeze", "dc": dc, "log_age": log_age, "threshold": threshold}
    # v1.8.0 P0 - distinguer Type A (frozen_backend) vs Type B (frozen_ui_rendering)
    # par parsing des correspondances client_ids <-> server_ids dans le log.
    try:
        type_info = _classify_dc_log_freeze_type(log_path)
    except Exception as e:
        type_info = {"type": "inconclusive", "details": {"error": str(e)}}
    if type_info["type"] == "frozen_backend":
        verdict = "frozen_backend"
    elif type_info["type"] == "frozen_ui_rendering":
        verdict = "frozen_ui_rendering"
    else:
        # v1.15.1 (2026-08-20) - Un log silencieux n'est PAS un gel.
        # L'ancien fallback classait tout 'inconclusive' en Type A et togglait
        # l'extension. Or 'inconclusive' arrive surtout quand la fenetre analysee
        # ne contient AUCUN appel client : personne ne parle a DC, il est au repos.
        # Mesure du 2026-08-20 sur 2 591 appels : 0 appel sans reponse, DC ne gele
        # jamais cote backend. Le toggle cassait donc le transport MCP sans raison
        # et enclenchait la boucle toggle -> toggle_failed -> dialog -> cooldown.
        # Sans appel client dans la fenetre (ou en cas d'erreur de parsing), on ne
        # touche a rien : ne rien faire est toujours moins nuisible qu'un toggle.
        det = type_info.get("details", {})
        if not det.get("client_ids_count"):
            return {"verdict": "idle_legitimate", "dc": dc, "log_age": log_age,
                    "threshold": threshold, "log_path": log_path,
                    "idle_reason": "log_silencieux_sans_appel_client",
                    "type_details": det}
        verdict = "frozen_isolated"
    return {"verdict": verdict, "dc": dc, "log_age": log_age, "threshold": threshold,
            "log_path": log_path, "type_details": type_info.get("details", {})}


def _dc_toggle_settings_remediation(dc_info):
    """v1.7.0 - Etape 1 : toggle off/on des settings DC. Best-effort, NE
    KILL AUCUN PID (le bug pre-v1.6.6 ne doit pas revenir). Renvoie (ok, msg).
    """
    target_id = dc_info["id"]
    target_name = dc_info["name"]
    settings = _load_extension_settings(target_id)
    was_enabled = _read_extension_enabled(settings)
    _set_extension_enabled(settings, False)
    try:
        _save_extension_settings(target_id, settings)
        time.sleep(1.5)
        _set_extension_enabled(settings, was_enabled)
        _save_extension_settings(target_id, settings)
    except Exception as e:
        return False, f"Erreur toggle settings : {e}"
    _LAST_RESTARTS[target_name] = datetime.now().isoformat(timespec="seconds")
    return True, f"Toggle settings DC effectue (was_enabled={was_enabled})"


def _dc_dialog_prompt_restart_async(reason_detail):
    """v1.7.0 - Etape 2 : dialog macOS avec 2 boutons. Tourne dans un thread
    pour ne pas bloquer la boucle watchdog (display dialog est sync). Sur
    'Redemarrer maintenant' -> restart_claude_desktop(). Sur 'Plus tard' ou
    timeout -> cooldown.
    """
    cfg = load_watchdog_config()
    cooldown = cfg.get("dc_cooldown_after_dismiss_seconds", 300)

    def _run():
        try:
            # v1.8.0 - message specifique selon le type de freeze :
            # - frozen_ui_rendering : DC repond, c'est l'UI Claude qui gele.
            # - autre (toggle_failed, toggle_io_error) : DC backend gele.
            if reason_detail == "frozen_ui_rendering":
                text = (
                    "Claude Desktop UI semble bloquee (Desktop Commander "
                    "repond normalement). Le toggle settings ne reveillerait "
                    "pas l'UI - seule la relance complete fonctionne.\n\n"
                    "Deux choix :\n"
                    "* Redemarrer Claude Desktop maintenant (ferme les "
                    "conversations en cours)\n"
                    "* Plus tard ({}s de cooldown)"
                ).format(cooldown)
            else:
                text = (
                    "Desktop Commander semble fige (log silencieux et le toggle "
                    "settings n'a pas suffi).\n\nDeux choix :\n"
                    "* Redemarrer Claude Desktop maintenant (ferme les "
                    "conversations en cours)\n"
                    "* Plus tard ({}s de cooldown)"
                ).format(cooldown)
            safe_text = text.replace('"', '\\"').replace('\\', '\\\\')
            script = (
                'display dialog "' + safe_text + '" '
                'with title "Claude Control - DC fige" '
                'buttons {"Plus tard", "Redemarrer Claude Desktop"} '
                'default button "Plus tard" '
                'with icon caution '
                'giving up after 60'
            )
            r = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=75
            )
            chose_restart = (
                r.returncode == 0
                and "Redemarrer Claude Desktop" in (r.stdout or "")
                and "gave up:true" not in (r.stdout or "")
            )
            if chose_restart:
                _watchdog_event(
                    "dc_user_chose_restart",
                    f"User accepte restart Claude Desktop (reason={reason_detail})"
                )
                ok, msg = restart_claude_desktop()
                _watchdog_event("dc_restart_claude_result", msg if ok else f"failed: {msg}")
                _DC_REMEDIATION_STATE["cooldown_until_ts"] = time.time() + cooldown
            else:
                _watchdog_event(
                    "dc_user_dismissed",
                    f"User differe (reason={reason_detail}), cooldown {cooldown}s"
                )
                _DC_REMEDIATION_STATE["cooldown_until_ts"] = time.time() + cooldown
        except Exception as e:
            _watchdog_event("dc_dialog_error", str(e))
            _DC_REMEDIATION_STATE["cooldown_until_ts"] = time.time() + cooldown
        finally:
            _DC_REMEDIATION_STATE["dialog_in_flight"] = False

    with _DC_REMEDIATION_STATE["dialog_lock"]:
        if _DC_REMEDIATION_STATE["dialog_in_flight"]:
            return False
        _DC_REMEDIATION_STATE["dialog_in_flight"] = True
    threading.Thread(target=_run, name="dc-remediation-dialog", daemon=True).start()
    return True


def _dc_auto_remediation_step(cfg):
    """v1.7.0 - Une iteration de la remediation DC. Appelee depuis le
    watchdog loop. Idempotente : si rien a faire, retourne sans bruit."""
    state = _DC_REMEDIATION_STATE
    now = time.time()

    # Cas particulier : on attend la verification post-toggle.
    if now < state["pending_verify_until_ts"]:
        # Verifie si le log a bouge depuis le toggle.
        dc = dc_status()
        if dc and dc.get("log_path"):
            try:
                cur_mtime = Path(dc["log_path"]).stat().st_mtime
            except Exception:
                cur_mtime = 0.0
            if cur_mtime > state["last_toggle_log_mtime"]:
                _watchdog_event(
                    "dc_toggle_success",
                    f"Log a bouge apres toggle (age={dc.get('log_age_seconds')}s)"
                )
                state["pending_verify_until_ts"] = 0.0
                state["cooldown_until_ts"] = now + 120  # stabilisation 2 min
                return
        # Pas encore expire, on patiente.
        return

    # Fin de la fenetre de verification.
    if state["pending_verify_until_ts"] > 0 and now >= state["pending_verify_until_ts"]:
        state["pending_verify_until_ts"] = 0.0
        dc = dc_status()
        # v1.15.1 (2026-08-20) - Ne plus conclure a l'echec sur un log muet.
        # Le toggle n'a d'effet OBSERVABLE que si quelqu'un appelle DC : le
        # serveur n'ecrit dans son log qu'en reponse a un appel client. Une
        # fenetre de verification sans appel client ne prouve donc rien -
        # l'ancien code y voyait un echec et escaladait vers un dialogue a
        # chaque fois. C'est le symetrique du correctif applique juste
        # precedent dans _dc_freeze_classify : on ne conclut pas d'une
        # absence de signal.
        client_calls = None
        log_path = dc.get("log_path") if dc else None
        if log_path:
            try:
                info = _classify_dc_log_freeze_type(log_path)
                client_calls = info.get("details", {}).get("client_ids_count")
            except Exception:
                client_calls = None
        if not client_calls:
            _watchdog_event(
                "dc_verify_inconclusive_idle",
                "Fenetre de verification sans appel client : le toggle n'est "
                "ni confirme ni infirme, aucune escalade."
            )
            state["cooldown_until_ts"] = now + 120
            return
        _watchdog_event(
            "dc_toggle_failed",
            "Log silencieux malgre {} appel(s) client apres le toggle".format(client_calls)
        )
        if dc:
            _dc_dialog_prompt_restart_async("toggle_failed")
        return

    # Cas normal : classifier la situation.
    cls = _dc_freeze_classify(cfg, now=now)
    verdict = cls.get("verdict")

    # v1.8.0 P0 - Type B (Claude Desktop UI gele, DC backend OK) : skip toggle
    # qui ne sert a rien dans ce cas, escalade directe au dialog avec un
    # message specifique. La remediation toggle ne reveille jamais l'UI Claude.
    if verdict == "frozen_ui_rendering":
        dc = cls["dc"]
        details = cls.get("type_details", {})
        signals = []
        if details.get("duplicate_read_file"): signals.append("duplicate_read_file")
        if details.get("large_payload"): signals.append("large_payload>20k")
        if details.get("track_ui_event_burst"): signals.append("track_ui_event_burst>10")
        sig_str = ", ".join(signals) if signals else "no_bonus_signals"
        _watchdog_event(
            "dc_freeze_ui_rendering",
            f"Claude Desktop UI semble gele (DC backend OK, "
            f"client_ids={details.get('client_ids_count', 0)} answered, "
            f"bonus={sig_str}) -> dialog direct restart Claude Desktop"
        )
        _dc_dialog_prompt_restart_async("frozen_ui_rendering")
        return

    if verdict in ("frozen_isolated", "frozen_backend"):
        dc = cls["dc"]
        log_age = cls["log_age"]
        threshold = cls["threshold"]
        details = cls.get("type_details", {})
        unanswered = details.get("unanswered_client_ids", [])
        _watchdog_event(
            "dc_freeze_detected",
            f"DC backend gele - log inactif {log_age}s > seuil {threshold}s, "
            f"unanswered client_ids={unanswered[:5]} -> tentative toggle settings"
        )
        try:
            cur_mtime = Path(dc["log_path"]).stat().st_mtime if dc.get("log_path") else 0.0
        except Exception:
            cur_mtime = 0.0
        ok, msg = _dc_toggle_settings_remediation(dc)
        if not ok:
            _watchdog_event("dc_toggle_error", msg)
            # Aller direct au dialog si le toggle ne s'est meme pas fait.
            _dc_dialog_prompt_restart_async("toggle_io_error")
            return
        _watchdog_event("dc_toggle_attempted", msg)
        state["last_toggle_log_mtime"] = cur_mtime
        state["last_action_ts"] = now
        state["pending_verify_until_ts"] = now + cfg.get("dc_verify_after_toggle_seconds", 30)
        return

    if verdict == "global_freeze":
        # Pas notre probleme - le watchdog Claude Desktop principal s'en occupe.
        # On log juste une fois par cycle pour la trace, sans agir.
        _watchdog_event(
            "dc_global_freeze_skipped",
            f"Claude Desktop ne repond pas - freeze global, pas DC isole, no-op"
        )
        return

    # Tous les autres verdicts (idle_legitimate, no_dc, no_log, cooldown,
    # pending_verify, dialog_in_flight) -> rien a faire, on n'inonde pas le log.


def _watchdog_loop():
    while True:
        try:
            cfg = load_watchdog_config()
            interval = cfg["interval_seconds"]
            # v1.7.0 - DC auto-remediation tourne orthogonalement au target principal.
            # Active independamment ; ne kill aucun PID ; n'agit que si DC isole.
            if cfg.get("dc_auto_remediation"):
                try:
                    _dc_auto_remediation_step(cfg)
                except Exception as e:
                    _watchdog_event("dc_remediation_error", str(e))
            if cfg["enabled"]:
                target = cfg.get("target", "claude_desktop")
                if target == "claude_desktop":
                    pids = _claude_pids()
                    if not pids:
                        if cfg["auto_restart_on_crash"]:
                            _watchdog_event("start", "Claude Desktop not running, launching")
                            # v1.14.12 - timeout : ces trois appels etaient les
                            # seuls subprocess sans borne du fichier, dans LA
                            # boucle qui tourne sans surveillance. Un `open`
                            # bloque par LaunchServices figeait le thread
                            # watchdog pour toujours, en silence.
                            subprocess.run(["open", "-a", "Claude"], check=False,
                                           timeout=15)
                    elif cfg["freeze_detection"]:
                        if not _claude_responsive(timeout=cfg["freeze_timeout"]):
                            _watchdog_event("restart", f"Claude Desktop unresponsive >{cfg['freeze_timeout']}s, restarting")
                            # v1.14.0 - motif ancre sur le binaire (cf.
                            # CLAUDE_DESKTOP_BIN). Avant : `pkill -9 -f Claude`,
                            # execute AUTOMATIQUEMENT toutes les 30 s des qu'un
                            # osascript lent faisait croire a un freeze -- ce qui
                            # tuait aussi les sessions Claude Code CLI.
                            subprocess.run(["pkill", "-9", "-f", CLAUDE_DESKTOP_BIN], check=False,
                                           timeout=10)
                            time.sleep(2)
                            subprocess.run(["open", "-a", "Claude"], check=False,
                                           timeout=15)
                elif target == "custom":
                    pattern = cfg.get("target_pattern", "")
                    if not pattern or len(pattern) < 2:
                        pass  # no-op, user hasn't set the pattern
                    else:
                        pids = _custom_target_pids(pattern)
                        if not pids:
                            if cfg["auto_restart_on_crash"]:
                                _watchdog_event("custom_down", f"Process matching '{pattern}' not found")
                        elif cfg["freeze_detection"]:
                            # No log path for arbitrary process — kill any zombie/uninterruptible PIDs.
                            # Pragmatic: just kill all matching PIDs to force them to be respawned by
                            # whoever launched them (Claude Desktop usually).
                            _watchdog_event("kill_custom", f"Pattern '{pattern}' freeze detection -> killing {len(pids)} pid(s)")
                            for pid in pids:
                                try:
                                    os.kill(pid, 9)
                                except Exception:
                                    pass
                else:
                    pids = _mcp_pids(target)
                    if not pids:
                        if cfg["auto_restart_on_crash"]:
                            _watchdog_event("crash_detected", f"MCP '{target}' down, restarting")
                            try:
                                ok, msg = restart_mcp(target)
                                _watchdog_event("restart_mcp_result", msg if ok else f"failed: {msg}")
                            except Exception as e:
                                _watchdog_event("restart_mcp_error", str(e))
                    elif cfg["freeze_detection"]:
                        window = max(60, interval * 2)
                        if _mcp_log_says_frozen(target, within_seconds=window):
                            _watchdog_event("restart_mcp", f"MCP '{target}' shows freeze markers, restarting")
                            try:
                                ok, msg = restart_mcp(target)
                                _watchdog_event("restart_mcp_result", msg if ok else f"failed: {msg}")
                            except Exception as e:
                                _watchdog_event("restart_mcp_error", str(e))
        except Exception as e:
            _log(f"watchdog loop error: {e}")
            interval = 30
        time.sleep(max(5, int(interval)))


def start_watchdog():
    t = threading.Thread(target=_watchdog_loop, name="claude-watchdog", daemon=True)
    t.start()





# === USAGE DES SKILLS ===

# v1.14.0 - _compute_skill_usage parcourt TOUT ~/.claude/projects/**/*.jsonl
# et fait un json.loads par ligne. Sur une machine qui utilise Claude Code
# quotidiennement ce repertoire pese plusieurs Go.
#
# Avant, il n'y avait aucun cache et trois appelants sur le chemin chaud :
#   - get_state()     -> /api/state,    poll toutes les 5 s
#   - get_overview()  -> /api/overview, poll toutes les 10 s, et il appelait
#                        get_skill_usage DEUX fois (via get_state puis
#                        directement)
# soit 24 scans complets par minute. Des que le scan depassait 5 s, chaque
# tick relancait le scan entier avant la fin du precedent : les threads
# s'empilaient, le GIL saturait, l'app cessait de repondre -- et le seuil
# etait franchi progressivement a mesure que l'historique grossissait.
#
# Correctif : cache TTL + single-flight (un seul scan a la fois, les autres
# threads attendent et lisent le resultat) + budget temps sur le scan lui-meme
# pour qu'un cache froid reste borne.
_SKILL_USAGE_CACHE = {"ts": 0.0, "days": None, "data": None}
_SKILL_USAGE_LOCK = threading.Lock()
_SKILL_USAGE_COMPUTE_LOCK = threading.Lock()
_SKILL_USAGE_TTL = 300          # 5 min : c'est une statistique, pas un etat temps reel
_SKILL_USAGE_MAX_SECONDS = 20   # budget d'un scan a froid


def _skill_usage_cached(days):
    with _SKILL_USAGE_LOCK:
        c = _SKILL_USAGE_CACHE
        if (c["data"] is not None and c["days"] == days
                and time.time() - c["ts"] < _SKILL_USAGE_TTL):
            return c["data"]
    return None


def get_skill_usage(days=30, force=False):
    """Usage des skills, avec cache TTL et single-flight (cf. bloc ci-dessus).

    La signature reste `get_skill_usage(days=30)` : elle est monkeypatchee
    telle quelle par les tests.
    """
    if not force:
        cached = _skill_usage_cached(days)
        if cached is not None:
            return cached
    with _SKILL_USAGE_COMPUTE_LOCK:
        if not force:
            # un autre thread a pu calculer pendant qu'on attendait le verrou
            cached = _skill_usage_cached(days)
            if cached is not None:
                return cached
        data = _compute_skill_usage(days=days)
        with _SKILL_USAGE_LOCK:
            _SKILL_USAGE_CACHE.update({"ts": time.time(), "days": days, "data": data})
        return data


def _compute_skill_usage(days=30):
    """Parcourt ~/.claude/projects/*/*.jsonl et compte les invocations du tool
    `Skill`. Tolerant aux changements de schema : ignore silencieusement chaque
    ligne malformee. Retourne un classement et de la metadata pour le fallback.

    v1.10.4 : ajoute aussi un breakdown 'tool_name_counts' (top 20) de tous
    les tool names rencontres dans les tool_use blocks. Diagnostic pour
    comprendre quand 0 invocations 'Skill' sont trouvees : soit l'utilisateur
    n'a vraiment pas de Skill triggers, soit le format JSONL a change et le
    nom du tool n'est plus 'Skill'.
    """
    counts = {}
    tool_name_counts = {}
    event_type_counts = {}  # v1.10.5 - diag : compte tous les obj.type
    sessions_seen = set()
    files_scanned = 0
    lines_scanned = 0
    parse_errors = 0
    assistant_msgs = 0
    tool_use_blocks = 0
    cutoff = None
    if days and days > 0:
        cutoff = datetime.now().timestamp() - days * 86400
    if not PROJECTS_LOGS_DIR.exists():
        return {"counts": {}, "ranked": [], "files_scanned": 0, "lines_scanned": 0,
                "parse_errors": 0, "sessions": 0, "ok": False,
                "reason": "projects_dir_missing",
                "projects_path": str(PROJECTS_LOGS_DIR)}
    # v1.14.0 - budget temps : meme a froid, le scan ne monopolise pas un
    # thread indefiniment. On scanne les fichiers les plus recents d'abord
    # pour que la troncature eventuelle sacrifie les donnees les moins utiles,
    # et on signale la troncature dans le payload plutot que de mentir.
    deadline = time.monotonic() + _SKILL_USAGE_MAX_SECONDS
    truncated = False
    try:
        all_files = sorted(PROJECTS_LOGS_DIR.rglob("*.jsonl"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        all_files = list(PROJECTS_LOGS_DIR.rglob("*.jsonl"))
    for jsonl in all_files:
        if time.monotonic() > deadline:
            truncated = True
            break
        try:
            mtime = jsonl.stat().st_mtime
        except Exception:
            continue
        if cutoff is not None and mtime < cutoff:
            continue
        files_scanned += 1
        try:
            f = jsonl.open("r", errors="replace")
        except Exception:
            continue
        with f:
            for line in f:
                lines_scanned += 1
                line = line.strip()
                if not line or line[0] != "{":
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    parse_errors += 1
                    continue
                if not isinstance(obj, dict):
                    continue
                sid = obj.get("sessionId")
                if sid:
                    sessions_seen.add(sid)
                # v1.10.5 - track tous les obj.type pour diag
                ot = obj.get("type", "_no_type")
                event_type_counts[ot] = event_type_counts.get(ot, 0) + 1
                if obj.get("type") != "assistant":
                    continue
                assistant_msgs += 1
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_use":
                        continue
                    tool_use_blocks += 1
                    name = block.get("name", "")
                    # v1.10.4 - track tous les tool names pour le diag
                    if name:
                        tool_name_counts[name] = tool_name_counts.get(name, 0) + 1
                    # v1.10.7 - real fix : on cherche les Read de SKILL.md
                    # comme proxy d'invocation. Claude Code moderne n'a plus
                    # de tool 'Skill' -> les skills sont charges via Read
                    # quand Claude les utilise. Pattern :
                    # ~/.claude/skills/<NAME>/SKILL.md ou
                    # .claude/skills/<NAME>/SKILL.md (dans args.path / file_path).
                    inp = block.get("input", {})
                    if not isinstance(inp, dict):
                        continue
                    if name in ("Read", "mcp__1d4a6474-72c9-4934-a111-78a5ca57d3dd__read_file_content"):
                        path = inp.get("file_path") or inp.get("path") or ""
                        # v1.13.0 - regex permissive : matche aussi les plugin
                        # skills (ex: ~/.claude/plugins/<plugin>/skills/<name>/SKILL.md
                        # ou .../.claude-plugin/skills/<name>/SKILL.md). Avant,
                        # le regex etait ancre sur '.claude/skills/' qui ne
                        # matchait QUE les skills user, donc tous les triggers
                        # de plugin skills etaient invisibles -> Top 10 vide,
                        # 'Jamais declenches' surevaluee, etc.
                        m = re.search(r"[/\\]skills[/\\]([^/\\]+)[/\\]SKILL\.md", path)
                        if m:
                            sk = m.group(1)
                            counts[sk] = counts.get(sk, 0) + 1
                            continue
                    # Legacy : tool 'Skill' direct (vieux Claude Code)
                    if name == "Skill":
                        skill = inp.get("skill") or inp.get("name")
                        if skill:
                            counts[str(skill)] = counts.get(str(skill), 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    tool_ranked = sorted(tool_name_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
    return {
        "counts": counts,
        "ranked": [{"name": k, "count": v} for k, v in ranked],
        "files_scanned": files_scanned,
        "lines_scanned": lines_scanned,
        "parse_errors": parse_errors,
        "sessions": len(sessions_seen),
        "days_window": days,
        "ok": True,
        # v1.14.0 - True si le budget temps a coupe le scan avant la fin.
        "truncated": truncated,
        # v1.10.4 - diag breakdown
        "assistant_msgs": assistant_msgs,
        "tool_use_blocks": tool_use_blocks,
        "tool_name_counts": dict(tool_ranked),
        # v1.10.5 - event types pour comprendre la structure JSONL
        "event_type_counts": dict(sorted(event_type_counts.items(),
                                         key=lambda kv: (-kv[1], kv[0]))[:15]),
    }


def skill_optimization_suggestions():
    """Suggestions basees sur usage + heuristiques. Si l'usage tracking ne ramene
    rien (jamais utilise, format change, etc.), bascule sur les heuristiques pures."""
    state = get_state()
    skills = state["skills"]
    usage = get_skill_usage(days=30)
    counts = usage.get("counts", {})
    suggestions = []
    user_active_skills = [s for s in skills if s["source"] == "user" and s["active"]]
    if usage.get("ok") and counts:
        unused_active = [s for s in user_active_skills if counts.get(s["name"], 0) == 0]
        if unused_active:
            suggestions.append({
                "kind": "unused",
                "severity": "info",
                "items": sorted(s["name"] for s in unused_active)[:20],
                "message_fr": "Skills actifs mais jamais activés ces 30 derniers jours — désactiver pour réduire le bruit dans le contexte.",
                "message_en": "Skills enabled but never invoked in the last 30 days — disable to reduce noise in the model's context.",
            })
        top_used = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
        if top_used:
            suggestions.append({
                "kind": "top",
                "severity": "info",
                "items": [f"{name} ({n})" for name, n in top_used],
                "message_fr": "Top 3 skills les plus utilisés ces 30 jours — vérifier qu'ils sont activés et que leur description est précise pour faciliter l'auto-déclenchement.",
                "message_en": "Top 3 skills used in the last 30 days — make sure they're enabled and their description is precise to help auto-triggering.",
            })
    no_desc = [s for s in skills if not (s.get("description") or "").strip()]
    if no_desc:
        suggestions.append({
            "kind": "no_description",
            "severity": "warn",
            "items": sorted(s["name"] for s in no_desc)[:20],
            "message_fr": "Skills sans `description:` dans leur frontmatter — ils ne se déclencheront jamais automatiquement. Ajoute une description.",
            "message_en": "Skills without a `description:` in their frontmatter — they will never auto-trigger. Add one.",
        })
    short_desc = [s for s in skills if (s.get("description") or "").strip() and len(s["description"]) < 30]
    if short_desc:
        suggestions.append({
            "kind": "short_description",
            "severity": "info",
            "items": sorted(s["name"] for s in short_desc)[:20],
            "message_fr": "Descriptions très courtes (<30 caractères) — peu de chance que Claude les déclenche correctement, enrichis-les.",
            "message_en": "Very short descriptions (<30 chars) — Claude is unlikely to auto-trigger them; enrich them.",
        })
    # v1.7.5 - bug fix : auparavant on calculait user_names & plugin_names a
    # partir de state['skills'], mais get_state() dedoublonne deja en SKIPPANT
    # les plugin skills dont le nom existe deja cote user (cf. _list_plugin_skills
    # iteration). Resultat : l'intersection etait toujours vide et le suggestion
    # 'duplicate' ne tirait jamais. On lit directement les sources reelles.
    user_names = {s["name"] for s in skills if s["source"] == "user"}
    plugin_names_all = set()
    try:
        for it in _list_plugin_skills():
            plugin_names_all.add(it["name"])
    except Exception:
        plugin_names_all = {s["name"] for s in skills if s["source"] != "user"}
    duplicates = sorted(user_names & plugin_names_all)
    if 0 < len(duplicates) <= 5:
        suggestions.append({
            "kind": "duplicate",
            "severity": "warn",
            "items": duplicates,
            "message_fr": "Skills dupliqués entre version utilisateur et plugin — la version utilisateur prend le dessus, supprime celle d'un côté ou de l'autre.",
            "message_en": "Skills duplicated between user version and plugin version — the user version wins; delete one side or the other.",
        })
    elif len(duplicates) > 5:
        suggestions.append({
            "kind": "duplicate_many",
            "severity": "info",
            "items": duplicates[:8],
            "message_fr": f"{len(duplicates)} skills ont le même nom dans tes skills utilisateur et dans des plugins. Probablement un overlap massif — envisage de supprimer en masse les copies utilisateur si tu veux laisser les plugins faire foi.",
            "message_en": f"{len(duplicates)} skills share names between your user skills and plugins. Likely a massive overlap — consider bulk-deleting the user copies if you want plugins to be the source of truth.",
        })
    return {
        "usage": usage,
        "suggestions": suggestions,
        "fallback": not usage.get("ok") or not usage.get("counts"),
    }


def get_overview():
    """Vue agregee : stats + health checks (orphans, MCPs en erreur, doublons,
    skills sans frontmatter, preset actif si applicable)."""
    state = get_state()
    plugins = []
    try:
        plugins = list_plugins()
    except Exception:
        pass
    commands = []
    try:
        commands = list_commands()
    except Exception:
        pass
    presets = list_presets()
    active_preset = None
    if isinstance(presets, dict):
        active_mcp_names = sorted(m["name"] for m in state["mcps"] if m["active"])
        for pname, pmcps in presets.items():
            if sorted(pmcps) == active_mcp_names:
                active_preset = pname
                break
    plugin_orphans = []
    for p in plugins:
        for v in p.get("extra_versions", []):
            plugin_orphans.append({"plugin": p["full_name"], "version": v})
    # v1.7.4 - 'sans frontmatter' faisait double emploi avec le suggestion
    # 'no_description' (meme set de skills, deux warnings cote a cote dans la
    # Vue d'ensemble). Le suggestion 'no_description' est plus actionable et
    # localise mieux le probleme - on garde uniquement celui-la.
    # v1.7.5 - les doublons skill/plugin sont maintenant remontes UNIQUEMENT
    # via skill_optimization_suggestions (kind:duplicate / duplicate_many) qui
    # porte le bouton d'action 'Supprimer les versions utilisateur en doublon'.
    # On ne renvoie plus duplicate_names dans health pour eviter le warning
    # double sans action.
    skill_issues = []
    mcps_active = [m for m in state["mcps"] if m["active"]]
    mcps_running = [m for m in mcps_active if m["running"]]
    mcps_failing = [m["name"] for m in mcps_active if not m["running"]]
    plugins_enabled = sum(1 for p in plugins if p.get("enabled"))
    try:
        usage = get_skill_usage(days=30)
    except Exception:
        usage = {"ok": False, "counts": {}, "ranked": []}
    top_skills = (usage.get("ranked") or [])[:5]
    return {
        "stats": {
            "mcps_total": len(state["mcps"]),
            "mcps_active": len(mcps_active),
            "mcps_running": len(mcps_running),
            "mcps_failing": len(mcps_failing),
            "skills_total": len(state["skills"]),
            "skills_active": sum(1 for s in state["skills"] if s["active"]),
            # v1.10.2 - cohérence avec le Health banner de la tab Skills :
            # 'prets a se declencher' = quality excellent. Fournit aussi
            # le compte broken+enrich pour permettre a l'UI de signaler
            # les skills a corriger meme depuis la Vue d'ensemble.
            "skills_excellent": sum(1 for s in state["skills"] if s.get("quality") == "excellent"),
            "skills_broken": sum(1 for s in state["skills"]
                                  if s.get("quality") == "broken" and s.get("source") == "user"),
            "skills_enrich": sum(1 for s in state["skills"]
                                  if s.get("quality") == "enrich" and s.get("source") == "user"),
            "plugins_total": len(plugins),
            "plugins_enabled": plugins_enabled,
            "commands_total": len(commands),
            "commands_user": sum(1 for c in commands if c["source"] == "user"),
        },
        "active_preset": active_preset,
        "presets_count": len(presets),
        "top_skills": top_skills,
        "usage_meta": {
            "ok": usage.get("ok", False),
            "files_scanned": usage.get("files_scanned", 0),
            "sessions": usage.get("sessions", 0),
            "days_window": usage.get("days_window", 30),
            # v1.10.4 - diag pour comprendre pourquoi 0 invocations 'Skill'
            "assistant_msgs": usage.get("assistant_msgs", 0),
            "tool_use_blocks": usage.get("tool_use_blocks", 0),
            "tool_name_counts": usage.get("tool_name_counts", {}),
            "event_type_counts": usage.get("event_type_counts", {}),
        },
        "health": {
            "plugin_orphans": plugin_orphans,
            "mcps_failing": mcps_failing,
            "skill_issues": skill_issues,
        },
    }


def read_settings_raw():
    if not SETTINGS_FILE.exists():
        return {"content": "{}", "exists": False, "size": 2, "path": str(SETTINGS_FILE)}
    try:
        text = SETTINGS_FILE.read_text(errors="replace")
    except Exception as e:
        return {"content": "", "exists": True, "size": 0, "path": str(SETTINGS_FILE), "error": str(e)}
    return {"content": text, "exists": True, "size": len(text), "path": str(SETTINGS_FILE)}


def save_settings(content):
    if not isinstance(content, str):
        return False, "Contenu invalide"
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        return False, f"JSON invalide : {e.msg} (ligne {e.lineno})"
    if not isinstance(parsed, dict):
        return False, "settings.json doit être un objet JSON"
    if SETTINGS_FILE.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = BACKUP_DIR / f"settings.json.{ts}"
        try:
            shutil.copy2(SETTINGS_FILE, backup)
        except Exception:
            pass
    _atomic_write_text(SETTINGS_FILE, content)
    return True, f"settings.json enregistré ({len(content)} caractères)"


def read_claude_md():
    if not CLAUDE_MD_FILE.exists():
        return {"content": "", "exists": False, "size": 0, "path": str(CLAUDE_MD_FILE)}
    try:
        text = CLAUDE_MD_FILE.read_text(errors="replace")
    except Exception as e:
        return {"content": "", "exists": True, "size": 0, "path": str(CLAUDE_MD_FILE), "error": str(e)}
    return {"content": text, "exists": True, "size": len(text), "path": str(CLAUDE_MD_FILE)}


def save_claude_md(content):
    if not isinstance(content, str):
        return False, "Contenu invalide"
    if CLAUDE_MD_FILE.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = BACKUP_DIR / f"CLAUDE.md.{ts}"
        try:
            shutil.copy2(CLAUDE_MD_FILE, backup)
        except Exception:
            pass
    _atomic_write_text(CLAUDE_MD_FILE, content)
    return True, f"CLAUDE.md enregistré ({len(content)} caractères)"


def save_command(name, content):
    """Sauvegarde une command utilisateur (avec backup si elle existait)."""
    if not name or not re.match(r'^[a-zA-Z0-9_\-]+$', name):
        return False, "Nom invalide (a-z, A-Z, 0-9, - et _ uniquement)"
    if not isinstance(content, str):
        return False, "Contenu invalide"
    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    target_active = COMMANDS_DIR / f"{name}.md"
    target_disabled = COMMANDS_DISABLED_DIR / f"{name}.md"
    target = target_active if target_active.exists() or not target_disabled.exists() else target_disabled
    if target.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = BACKUP_DIR / f"command-{name}.{ts}.md"
        try:
            shutil.copy2(target, backup)
        except Exception:
            pass
    _atomic_write_text(target, content)
    return True, f"Command '{name}' enregistrée"


def restart_claude():
    """v1.14.0 - delegue a restart_claude_desktop().

    Avant, cette fonction faisait `pkill -9 -f Claude` : un motif non ancre,
    qui matche la ligne de commande complete de TOUS les process de
    l'utilisateur. Etaient tues au passage, entre autres, le subprocess
    `claude -p <contenu de SKILL.md>` lance par _call_claude_cli (le contenu
    d'un skill contient presque toujours le mot "Claude"), toute session
    Claude Code lancee depuis un chemin contenant "Claude", ou un
    `tail -f ~/Library/Logs/Claude/main.log`.

    restart_claude_desktop() ancre sur le chemin complet du binaire. Une
    seule implementation, une seule portee.
    """
    return restart_claude_desktop()


_MCP_FIX_RULES = [
    ("auth",
     ("401", "403", "unauthorized", "forbidden", "invalid api key", "invalid_api_key", "authentication failed", "auth failed"),
     {"fr": "Authentification : la clé API est invalide ou expirée. Vérifie / régénère-la dans la config MCP.",
      "en": "Authentication: API key is invalid or expired. Check or regenerate it in the MCP config."}),
    ("deps",
     ("modulenotfounderror", "cannot find module", "importerror"),
     {"fr": "Dépendance manquante. `npm install` ou `pip install` dans le dossier du MCP.",
      "en": "Missing dependency. Run `npm install` or `pip install` in the MCP folder."}),
    ("binary",
     ("enoent", "no such file", "command not found", "cannot find"),
     {"fr": "Binaire introuvable : vérifie le chemin de 'command' dans la config MCP.",
      "en": "Binary not found: check the 'command' path in the MCP config."}),
    ("port",
     ("eaddrinuse", "address already in use"),
     {"fr": "Port déjà utilisé. Un autre process tient ce port.",
      "en": "Port already in use. Another process is holding this port."}),
    ("perm",
     ("permission denied", "eacces"),
     {"fr": "Permission refusée : `chmod +x` sur le binaire ou vérifie les droits.",
      "en": "Permission denied: `chmod +x` on the binary, or check file permissions."}),
    ("net",
     ("econnreset", "etimedout", "getaddrinfo", "enotfound", "network error"),
     {"fr": "Réseau : vérifie ta connexion ou la dispo du service distant.",
      "en": "Network issue: check your connection or the remote service availability."}),
    ("rate",
     ("rate limit", "429", "quota"),
     {"fr": "Rate limit atteint sur l'API. Attends ou augmente ton quota.",
      "en": "API rate limit hit. Wait it out or raise your quota."}),
    ("ssl",
     ("ssl", "certificate", "cert"),
     {"fr": "Problème certificat SSL/TLS. Vérifie l'horloge système et les CA.",
      "en": "SSL/TLS certificate issue. Check the system clock and root CAs."}),
    ("syntax",
     ("syntaxerror", "unexpected token"),
     {"fr": "Erreur de syntaxe dans le code MCP. Le binaire est probablement corrompu.",
      "en": "Syntax error in the MCP code. The binary is probably corrupted."}),
    ("exit_early",
     ("transport closed unexpectedly", "process exiting early", "process exited early", "server disconnected", "client transport closed"),
     {"fr": "Le MCP a quitté immédiatement après son lancement, sans rien écrire sur stderr. "
            "Causes les plus fréquentes : variable d'env manquante (clé 'env' dans claude_desktop_config.json), "
            "binaire au mauvais chemin, mauvaise version de Node/Python attendue, ou un import/require "
            "qui plante au chargement. Pour voir l'erreur réelle : clique « Tester maintenant » dans cette "
            "modale — l'app fait un vrai handshake JSON-RPC initialize.",
      "en": "The MCP exited immediately after launch, without writing anything to stderr. "
            "Most common causes: a missing env variable ('env' key in claude_desktop_config.json), "
            "wrong binary path, expected Node/Python version mismatch, or an import/require crashing "
            "at load time. To see the real error: click 'Test now' in this modal — the app runs "
            "a real JSON-RPC initialize handshake."}),
]


def _suggest_mcp_fix(error_text, lang="fr"):
    """Retourne (suggestion, kind) ou (None, None) si pas de pattern reconnu."""
    e = (error_text or "").lower()
    for kind, keywords, translations in _MCP_FIX_RULES:
        if any(k in e for k in keywords):
            return translations.get(lang) or translations.get("fr"), kind
    return None, None


def _scan_log_for_error(content, name=None):
    lines = content.splitlines()
    if name:
        lines = [l for l in lines if name.lower() in l.lower()]
    recent = lines[-300:]
    keywords = ("error", "exception", "failed", "fatal", "traceback")
    for i in range(len(recent) - 1, -1, -1):
        if any(k in recent[i].lower() for k in keywords):
            start = max(0, i - 1)
            end = min(len(recent), i + 6)
            return "\n".join(recent[start:end])
    return None


def read_mcp_error(name, lang="fr"):
    """Trouve le dernier message d'erreur du MCP <name> dans les logs Claude
    Desktop et propose une suggestion de fix selon le pattern."""
    if not name:
        msg = "Nom MCP requis" if lang == "fr" else "MCP name required"
        return {"name": name, "error": None, "suggestion": msg, "log_path": None}
    if not CLAUDE_LOGS_DIR.exists():
        msg = (f"Dossier {CLAUDE_LOGS_DIR} introuvable. Claude Desktop n'a probablement jamais démarré." if lang == "fr"
               else f"Folder {CLAUDE_LOGS_DIR} not found. Claude Desktop probably never started.")
        return {"name": name, "error": None, "suggestion": msg, "log_path": None}
    # v1.14.0 - `name` vient de l'URL et etait interpole tel quel dans un
    # glob : un nom contenant '/' ou '..' sortait de CLAUDE_LOGS_DIR. On
    # neutralise les separateurs et les metacaracteres de glob.
    safe_name = re.sub(r"[/\\\[\]*?]", "", str(name)).replace("..", "")
    if not safe_name:
        msg = "Nom MCP invalide" if lang == "fr" else "Invalid MCP name"
        return {"name": name, "error": None, "suggestion": msg, "log_path": None}
    candidates = sorted(CLAUDE_LOGS_DIR.glob(f"mcp-server-{safe_name}*.log"))
    candidates += sorted(CLAUDE_LOGS_DIR.glob(f"*{safe_name}*.log"))
    candidates += [CLAUDE_LOGS_DIR / "mcp.log", CLAUDE_LOGS_DIR / "main.log"]
    seen = set()
    for log_file in candidates:
        if not log_file.exists() or log_file in seen:
            continue
        seen.add(log_file)
        # v1.14.0 - _read_log_tail au lieu de read_text(). Les candidats
        # incluent mcp.log et main.log, qui atteignent facilement plusieurs
        # centaines de Mo ; on chargeait le fichier entier puis splitlines()
        # puis une comprehension de filtrage (~3x la taille en RAM) pour
        # n'utiliser que les 300 dernieres lignes (cf. _scan_log_for_error).
        content = "\n".join(_read_log_tail(log_file, max_lines=600, max_bytes=256_000))
        if not content:
            continue
        filter_name = name if log_file.name in ("mcp.log", "main.log") else None
        excerpt = _scan_log_for_error(content, filter_name)
        if excerpt:
            suggestion, kind = _suggest_mcp_fix(excerpt, lang)
            return {
                "name": name,
                "error": excerpt,
                "suggestion": suggestion,
                "kind": kind,
                "log_path": str(log_file),
                "log_file": log_file.name,
            }
    return {
        "name": name,
        "error": None,
        "suggestion": None,
        "kind": "no_log",
        "log_path": str(CLAUDE_LOGS_DIR),
        "log_file": None,
    }


_ENV_VAR_PATTERNS = [
    re.compile(r"\b([A-Z][A-Z0-9_]{2,})\s+(?:[Ee]nvironment\s+variable|env\s*var)\s+(?:is\s+)?(?:[Rr]equired|not\s+set|[Mm]issing|undefined|not\s+provided|must\s+be\s+set|not\s+defined)"),
    re.compile(r"(?:[Ee]nvironment\s+variable|env\s*var)\s+(?:not\s+(?:set|provided|defined)\s*)?:?\s*['\"`]?([A-Z][A-Z0-9_]{2,})['\"`]?\b"),
    re.compile(r"(?:[Mm]issing|[Uu]ndefined)\s+(?:required\s+)?(?:[Ee]nvironment\s+variable\s+)?['\"`]?([A-Z][A-Z0-9_]{2,})['\"`]?\b"),
    re.compile(r"(?:[Pp]lease\s+)?[Ss]et\s+(?:the\s+)?['\"`]?([A-Z][A-Z0-9_]{2,})['\"`]?\s+(?:[Ee]nvironment\s+variable|env\s*var)"),
    re.compile(r"\b([A-Z][A-Z0-9_]{2,})\s+(?:is\s+)?(?:not\s+set|not\s+defined|[Rr]equired|must\s+be\s+set|not\s+provided)\b"),
]
_ENV_VAR_BLACKLIST = {"DEBUG", "ERROR", "WARN", "INFO", "PATH", "HOME", "USER", "TMPDIR",
                     "PWD", "SHELL", "TERM", "LOG", "FATAL", "ENV", "API", "MCP", "VAR",
                     "VARIABLE", "URL", "URI", "ID", "KEY", "TOKEN"}


def _detect_missing_env_var(text):
    if not text:
        return None
    for pat in _ENV_VAR_PATTERNS:
        for m in pat.finditer(text):
            v = m.group(1).strip()
            if v in _ENV_VAR_BLACKLIST:
                continue
            if "_" in v or (len(v) >= 6 and v.isupper()):
                return v
    return None


def test_mcp(name, lang="fr"):
    """Lance le MCP comme le ferait Claude Desktop (handshake JSON-RPC initialize)
    et observe sa reponse. C'est le test reel, pas une simulation avec stdin/dev/null."""
    en = (lang == "en")
    if not name:
        return False, {"error": "MCP name required" if en else "Nom MCP requis"}
    config = load_config()
    mcp = config.get("mcpServers", {}).get(name) or config.get("_disabledMcps", {}).get(name)
    if not mcp:
        return False, {"error": f"MCP '{name}' not found in config" if en else f"MCP '{name}' introuvable dans la config"}
    if not isinstance(mcp, dict) or not mcp.get("command"):
        return False, {"error": "Invalid MCP config (missing 'command')" if en else "Config MCP invalide (manque 'command')"}
    command = mcp["command"]
    args = mcp.get("args", [])
    if not isinstance(args, list):
        args = []
    env_extra = mcp.get("env", {}) or {}
    env = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")
    if isinstance(env_extra, dict):
        for k, v in env_extra.items():
            env[str(k)] = "" if v is None else str(v)

    init_msg = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "claude-control-test", "version": "1.0"},
        },
    }) + "\n"

    try:
        proc = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, bufsize=1,
        )
    except FileNotFoundError:
        return True, {
            "name": name, "exit_code": 127, "stdout": "",
            "stderr": (f"command not found: {command}" if en else f"command introuvable : {command}"),
            "suggestion": (f"Binary '{command}' does not exist. Check the path in the MCP config." if en
                           else f"Le binaire '{command}' n'existe pas. Vérifie le chemin dans la config MCP."),
            "kind": "binary", "missing_env_var": None,
            "handshake": None, "configured_env_keys": list((env_extra or {}).keys()),
        }
    except Exception as e:
        return True, {
            "name": name, "exit_code": -1, "stdout": "", "stderr": str(e),
            "suggestion": (f"Execution error: {e}" if en else f"Erreur d'exécution : {e}"),
            "kind": None, "missing_env_var": None,
            "handshake": None, "configured_env_keys": list((env_extra or {}).keys()),
        }

    stdout_text = ""
    stderr_text = ""
    handshake_response = None
    try:
        stdout_text, stderr_text = proc.communicate(input=init_msg, timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            stdout_text, stderr_text = proc.communicate(timeout=2)
        except Exception:
            stdout_text, stderr_text = "", ""

    for line in (stdout_text or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and obj.get("id") == 1:
                handshake_response = obj
                break
        except Exception:
            pass

    exit_code = proc.returncode
    combined = (stderr_text or "") + "\n" + (stdout_text or "")
    pattern_suggestion, pattern_kind = _suggest_mcp_fix(combined, lang)
    missing_var = _detect_missing_env_var(combined)

    if handshake_response and "result" in handshake_response:
        kind = "handshake_ok"
        server_info = handshake_response.get("result", {}).get("serverInfo", {})
        sname = server_info.get("name", "?")
        sver = server_info.get("version", "?")
        if en:
            suggestion = (
                f"Your MCP server responds correctly to the `initialize` handshake "
                f"(serverInfo: {sname} v{sver}). The server is healthy. If Claude Desktop still "
                f"shows 'not running', the issue is in Claude Desktop itself: restart it via the top-right "
                f"button, or check that your Claude Desktop version is compatible with this MCP."
            )
        else:
            suggestion = (
                f"Ton serveur MCP répond correctement au handshake `initialize` "
                f"(serverInfo: {sname} v{sver}). Le serveur est sain. Si Claude Desktop affiche quand même "
                f"« pas démarré », le problème est dans Claude Desktop lui-même : redémarre-le via le bouton "
                f"en haut à droite, ou vérifie que ta version de Claude Desktop est compatible avec ton MCP."
            )
    elif handshake_response and "error" in handshake_response:
        kind = "handshake_error"
        err = handshake_response["error"]
        msg = err.get("message", "unknown" if en else "inconnue")
        code = err.get("code")
        suggestion = (f"The server replied to the handshake with an error: {msg} (code {code})." if en
                      else f"Le serveur a répondu au handshake avec une erreur : {msg} (code {code}).")
    elif pattern_suggestion:
        kind = pattern_kind
        suggestion = pattern_suggestion
    elif exit_code is not None and exit_code != 0:
        kind = "nonzero_exit"
        suggestion = (f"The server exited with code {exit_code} before replying to the handshake. Read stderr below." if en
                      else f"Le serveur a quitté avec code {exit_code} avant de répondre au handshake. Lis stderr ci-dessous.")
    elif exit_code == 0:
        kind = "exit_zero"
        if en:
            suggestion = (
                "The server exited with code 0 without responding to the `initialize` handshake. "
                "Either the code lacks a blocking stdio loop, or the `mcp` library version in the venv "
                "no longer matches the API used in the code (try `pip install --upgrade mcp` in the MCP's venv)."
            )
        else:
            suggestion = (
                "Le serveur a quitté avec code 0 sans répondre au handshake `initialize`. "
                "Soit le code n'a pas de boucle stdio bloquante, soit la version de la lib `mcp` "
                "dans le venv ne correspond plus à l'API utilisée dans le code (essaie `pip install --upgrade mcp` "
                "dans le venv du MCP)."
            )
    else:
        kind = "no_response"
        suggestion = ("No response to the handshake after 5s, but the server is still running. "
                      "Probably a bug in the server logic (`initialize` handler not implemented)." if en
                      else "Aucune réponse au handshake après 5s, mais le serveur tourne. "
                           "Probable bug dans la logique du serveur (handler `initialize` non implémenté).")

    return True, {
        "name": name,
        "exit_code": exit_code,
        "stdout": (stdout_text or "")[-3000:],
        "stderr": (stderr_text or "")[-3000:],
        "suggestion": suggestion,
        "kind": kind,
        "missing_env_var": missing_var,
        "handshake": handshake_response is not None,
        "configured_env_keys": list((env_extra or {}).keys()),
    }


def set_mcp_env(name, var, value):
    """Patch la config Claude Desktop pour ajouter/maj une env var sur un MCP."""
    if not name or not var:
        return False, "Nom MCP et variable requis"
    if not re.match(r'^[A-Z_][A-Z0-9_]*$', var):
        return False, "Nom de variable invalide (A-Z, 0-9, _)"
    config = load_config()
    target = None
    for bucket in ("mcpServers", "_disabledMcps"):
        if name in config.get(bucket, {}):
            target = config[bucket][name]
            break
    if not isinstance(target, dict):
        return False, f"MCP '{name}' introuvable"
    target.setdefault("env", {})[var] = value
    save_config(config)
    return True, f"Variable '{var}' enregistree pour MCP '{name}'"


# === IMPORTS ===

def import_mcp_json(json_str):
    try:
        data = json.loads(json_str)
    except Exception as e:
        return False, f"JSON invalide : {e}"
    new_servers = data["mcpServers"] if "mcpServers" in data else data
    if not isinstance(new_servers, dict) or not new_servers:
        return False, "Format inattendu (besoin d'un objet de serveurs)"
    for k, v in new_servers.items():
        if not isinstance(v, dict) or "command" not in v:
            return False, f"'{k}' : il manque 'command'"
    config = load_config()
    config.setdefault("mcpServers", {}).update(new_servers)
    save_config(config)
    return True, f"{len(new_servers)} MCP(s) ajoute(s) : {', '.join(new_servers.keys())}"


def import_mcp_file(path):
    p = Path(path).expanduser()
    if not p.exists():
        return False, f"Fichier introuvable : {p}"
    try:
        return import_mcp_json(p.read_text())
    except Exception as e:
        return False, f"Erreur lecture : {e}"


def import_mcp_git(url):
    url = (url or "").strip()
    if not _git_url_ok(url):
        return False, "URL Git invalide (doit commencer par https:// ou git@)"
    IMPORTED_REPOS_DIR.mkdir(parents=True, exist_ok=True)
    # Garde v1.14.12 (voir _git_repo_name) : le rmtree ci-dessous rendait un
    # nom '..' ou vide catastrophique.
    name = _git_repo_name(url)
    if name is None:
        return False, "Nom de repo indéductible de l'URL"
    target = IMPORTED_REPOS_DIR / name
    if target.exists():
        shutil.rmtree(target)
    cloned, err = _git_clone_shallow(url, target)
    if not cloned:
        return False, err
    candidates = list(target.rglob("claude_desktop_config*.json")) + list(target.rglob("mcp.json"))
    if candidates:
        ok, msg = import_mcp_file(str(candidates[0]))
        return ok, f"Repo clone, {msg}"
    return True, f"Repo clone dans {target}. Aucun config MCP detecte, ajoute via JSON."


def import_skill_folder(path):
    src = Path(path).expanduser()
    if not src.exists() or not src.is_dir():
        return False, "Dossier introuvable"
    if not (src / "SKILL.md").exists():
        return False, f"SKILL.md manquant dans {src.name}"
    target = SKILLS_DIR / src.name
    if target.exists():
        return False, f"Skill '{src.name}' existe deja"
    shutil.copytree(src, target)
    return True, f"Skill '{src.name}' importe"


def import_skill_git(url):
    url = (url or "").strip()
    if not _git_url_ok(url):
        return False, "URL Git invalide (doit commencer par https:// ou git@)"
    # Garde v1.14.12 (voir _git_repo_name) : un nom '..' ou vide sortait de
    # skills/ (ici sans rmtree, mais le clone atterrissait hors du dossier).
    name = _git_repo_name(url)
    if name is None:
        return False, "Nom de repo indéductible de l'URL"
    target = SKILLS_DIR / name
    if target.exists():
        return False, f"Skill '{name}' existe deja"
    cloned, err = _git_clone_shallow(url, target)
    if not cloned:
        return False, err
    if not (target / "SKILL.md").exists():
        nested = list(target.rglob("SKILL.md"))
        if not nested:
            shutil.rmtree(target)
            return False, "Aucun SKILL.md dans le repo"
        skill_root = nested[0].parent
        if skill_root != target:
            tmp = SKILLS_DIR / f".tmp_{name}"
            shutil.copytree(skill_root, tmp)
            shutil.rmtree(target)
            tmp.rename(target)
    return True, f"Skill '{name}' importe depuis Git"


def import_skill_markdown(name, content):
    if not name or not re.match(r'^[a-zA-Z0-9_\-]+$', name):
        return False, "Nom invalide (a-z, A-Z, 0-9, - et _ uniquement)"
    if "---" not in content[:10]:
        return False, "Le markdown doit commencer par un YAML frontmatter ---"
    target = SKILLS_DIR / name
    if target.exists():
        return False, f"Skill '{name}' existe deja"
    target.mkdir(parents=True)
    # v1.14.12 - seule ecriture de SKILL.md qui restait non atomique.
    _atomic_write_text(target / "SKILL.md", content)
    return True, f"Skill '{name}' cree"


def _safe_extract_zip(blob, dest):
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for member in zf.namelist():
            parts = Path(member).parts
            if member.startswith('/') or '..' in parts or (parts and parts[0].startswith('/')):
                raise ValueError(f"Entree non sure : {member}")
        zf.extractall(dest)


def _zip_name_from_filename(filename, fallback):
    base = re.sub(r'\.zip$', '', Path(filename or "").name, flags=re.I)
    return base or fallback


def import_skill_zip(blob, filename):
    if not blob:
        return False, "Fichier vide"
    if len(blob) > MAX_ZIP_SIZE:
        return False, f"Trop volumineux (max {MAX_ZIP_SIZE // 1024 // 1024} Mo)"
    name = _zip_name_from_filename(filename, "imported-skill")
    if not re.match(r'^[a-zA-Z0-9_\-]+$', name):
        return False, "Nom de fichier invalide (a-z, A-Z, 0-9, - et _ uniquement)"
    target = SKILLS_DIR / name
    if target.exists():
        return False, f"Skill '{name}' existe deja"
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            _safe_extract_zip(blob, tmp_path)
        except zipfile.BadZipFile:
            return False, "Fichier ZIP invalide"
        except Exception as e:
            return False, f"Extraction : {e}"
        candidates = list(tmp_path.rglob("SKILL.md"))
        if not candidates:
            return False, "Aucun SKILL.md trouve dans le ZIP"
        skill_root = candidates[0].parent
        shutil.copytree(skill_root, target)
    return True, f"Skill '{name}' importe depuis ZIP"


def import_mcp_zip(blob, filename):
    if not blob:
        return False, "Fichier vide"
    if len(blob) > MAX_ZIP_SIZE:
        return False, f"Trop volumineux (max {MAX_ZIP_SIZE // 1024 // 1024} Mo)"
    name = _zip_name_from_filename(filename, "imported-mcp")
    if not re.match(r'^[a-zA-Z0-9_\-]+$', name):
        return False, "Nom de fichier invalide"
    IMPORTED_REPOS_DIR.mkdir(parents=True, exist_ok=True)
    target_dir = IMPORTED_REPOS_DIR / name
    if target_dir.exists():
        shutil.rmtree(target_dir)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            _safe_extract_zip(blob, tmp_path)
        except zipfile.BadZipFile:
            return False, "Fichier ZIP invalide"
        except Exception as e:
            return False, f"Extraction : {e}"
        entries = [p for p in tmp_path.iterdir() if not p.name.startswith("__MACOSX")]
        src = entries[0] if len(entries) == 1 and entries[0].is_dir() else tmp_path
        shutil.copytree(src, target_dir)
    candidates = list(target_dir.rglob("claude_desktop_config*.json")) + list(target_dir.rglob("mcp.json"))
    if candidates:
        ok, msg = import_mcp_file(str(candidates[0]))
        return ok, f"ZIP extrait, {msg}"
    return True, f"ZIP extrait dans {target_dir}. Aucun config MCP detecte, ajoute via JSON."


# === PRESETS ===

def load_presets():
    if not PRESETS_FILE.exists():
        return {"presets": {}}
    try:
        with open(PRESETS_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict) or "presets" not in data:
            return {"presets": {}}
        return data
    except Exception:
        return {"presets": {}}


def save_presets_file(data):
    _atomic_write_json(PRESETS_FILE, data)


def list_presets():
    return load_presets().get("presets", {})


def save_preset(name, mcps):
    name = (name or "").strip()
    if not name:
        return False, "Nom de preset requis"
    if not isinstance(mcps, list):
        return False, "Liste de MCPs invalide"
    data = load_presets()
    data.setdefault("presets", {})[name] = sorted({str(m) for m in mcps if m})
    save_presets_file(data)
    return True, f"Preset '{name}' sauvegarde ({len(data['presets'][name])} MCP(s))"


def apply_preset(name):
    presets = list_presets()
    if name not in presets:
        return False, f"Preset '{name}' introuvable"
    target_names = set(presets[name])
    config = load_config()
    active = config.setdefault("mcpServers", {})
    disabled = config.setdefault("_disabledMcps", {})
    all_mcps = {**active, **disabled}
    new_active, new_disabled = {}, {}
    for n, cfg in all_mcps.items():
        if n in target_names:
            new_active[n] = cfg
        else:
            new_disabled[n] = cfg
    config["mcpServers"] = new_active
    config["_disabledMcps"] = new_disabled
    save_config(config)
    missing = sorted(target_names - set(all_mcps.keys()))
    msg = f"Preset '{name}' applique : {len(new_active)} actif(s), {len(new_disabled)} desactive(s)"
    if missing:
        msg += f" (introuvables : {', '.join(missing)})"
    return True, msg


def delete_preset(name):
    data = load_presets()
    presets = data.setdefault("presets", {})
    if name not in presets:
        return False, f"Preset '{name}' introuvable"
    del presets[name]
    save_presets_file(data)
    return True, f"Preset '{name}' supprime"


# === PLUGINS ===

def _load_settings():
    if not SETTINGS_FILE.exists():
        return {}
    try:
        with open(SETTINGS_FILE, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings(data):
    # v1.14.12 - refus d'ecraser un settings.json existant mais illisible.
    #
    # _load_settings() rend {} sur un JSON casse (virgule en trop d'une
    # edition manuelle). Les ecrivains -- toggle_plugin en tete -- repartaient
    # de ce {} et reecrivaient un fichier ne contenant QUE enabledPlugins :
    # hooks, permissions, env, statusLine, model, tout etait perdu en cochant
    # une case. Le meme durcissement existe deja pour l'autre config
    # (load_config_safe, v1.14.0) ; il manquait ici.
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, encoding="utf-8-sig") as f:
                json.load(f)
        except Exception as e:
            raise RuntimeError(
                "~/.claude/settings.json existe mais n'est pas un JSON "
                f"lisible ({type(e).__name__}). Écrire par-dessus détruirait "
                "hooks, permissions et env : corrige le fichier puis réessaie.")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if SETTINGS_FILE.exists():
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = BACKUP_DIR / f"settings.json.backup.{ts}"
        # v1.14.12 - copy2 au lieu d'un read/write manuel : la copie manuelle
        # pouvait laisser un backup tronque si le process mourait en route --
        # le filet de securite lui-meme n'etait pas fiable.
        shutil.copy2(SETTINGS_FILE, backup)
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(SETTINGS_FILE, data)


def _read_plugin_mcp_servers(install_path):
    """Lit le .mcp.json d'un plugin et retourne un dict {name: server_def}.
    Cherche dans <install>/.mcp.json puis <install>/.claude-plugin/.mcp.json.
    Retourne {} si rien trouve / parse error."""
    p = Path(install_path)
    if not p.exists():
        return {}
    for mf in (p / ".mcp.json", p / ".claude-plugin/.mcp.json"):
        if mf.is_file():
            try:
                data = json.loads(mf.read_text())
            except Exception:
                continue
            servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
            if isinstance(servers, dict):
                return {k: v for k, v in servers.items() if isinstance(v, dict)}
    return {}


def _scan_plugin_contents(install_path):
    p = Path(install_path)
    if not p.exists():
        return {"skills_count": 0, "mcp_count": 0, "commands_count": 0, "hooks_count": 0,
                "skills": [], "mcps": [], "commands": [], "missing": True}
    candidates_skills = [p / "skills", p / ".claude-plugin/skills"]
    skills = []
    for sd in candidates_skills:
        if sd.is_dir():
            skills.extend(sorted(d.name for d in sd.iterdir() if d.is_dir() and not d.name.startswith(".")))
    servers = _read_plugin_mcp_servers(install_path)
    # v1.11.0 - on annote chaque MCP plugin avec son etat 'bridged' (deja
    # present dans claude_desktop_config.json -> Claude Desktop le charge).
    # Permet a l'UI d'afficher soit un bouton 'Ajouter a Claude Desktop'
    # soit un badge 'Deja dans Claude Desktop'.
    cd_names = set()
    try:
        cd_config = load_config()
        for bucket in ("mcpServers", "_disabledMcps"):
            cd_names.update(cd_config.get(bucket, {}).keys())
    except Exception:
        pass
    mcps = [{"name": n, "bridged": n in cd_names} for n in sorted(servers.keys())]
    cmd_dir = p / "commands"
    commands = sorted(f.stem for f in cmd_dir.glob("*.md")) if cmd_dir.is_dir() else []
    hooks_count = 0
    plugin_json = p / "plugin.json"
    if not plugin_json.is_file():
        plugin_json = p / ".claude-plugin/plugin.json"
    if plugin_json.is_file():
        try:
            data = json.loads(plugin_json.read_text())
            hooks = data.get("hooks", {})
            if isinstance(hooks, dict):
                hooks_count = sum(len(v) if isinstance(v, list) else 1 for v in hooks.values())
        except Exception:
            pass
    return {"skills_count": len(skills), "mcp_count": len(mcps),
            "commands_count": len(commands), "hooks_count": hooks_count,
            "skills": skills, "mcps": mcps, "commands": commands, "missing": False}


def bridge_plugin_mcp_to_desktop(full_name, mcp_name):
    """v1.11.0 - Copie la definition d'un MCP declare par un plugin Claude
    Code dans claude_desktop_config.json, pour que Claude Desktop le charge
    aussi. Le plugin garde la sienne (lecture seule cote plugin). Backup
    horodate de claude_desktop_config.json via save_config.

    Refus si :
      - plugin / MCP introuvable
      - un MCP du meme nom existe deja dans claude_desktop_config.json
        (mcpServers ou _disabledMcps)
    """
    if not full_name or not mcp_name:
        return False, "Nom de plugin et nom de MCP requis"
    installed = _load_installed_plugins()
    if full_name not in installed:
        return False, f"Plugin '{full_name}' introuvable"
    entries = installed[full_name]
    if not isinstance(entries, list) or not entries:
        return False, f"Plugin '{full_name}' sans entree valide"
    install_path = entries[0].get("installPath", "") if isinstance(entries[0], dict) else ""
    servers = _read_plugin_mcp_servers(install_path)
    if mcp_name not in servers:
        return False, f"MCP '{mcp_name}' introuvable dans le plugin '{full_name}'"
    server_def = servers[mcp_name]
    config = load_config()
    for bucket in ("mcpServers", "_disabledMcps"):
        if mcp_name in config.get(bucket, {}):
            return False, (f"Un MCP nomme '{mcp_name}' existe deja dans "
                           f"claude_desktop_config.json. Renomme ou supprime "
                           f"l'existant avant de bridger celui du plugin.")
    config.setdefault("mcpServers", {})[mcp_name] = server_def
    save_config(config)
    return True, (f"MCP '{mcp_name}' du plugin '{full_name}' ajoute a "
                  f"claude_desktop_config.json. Redemarre Claude Desktop "
                  f"pour qu'il le charge.")


def package_plugin_skill_for_desktop(full_name, skill_name):
    """v1.12.0 - Zippe un skill (dossier <plugin>/skills/<skill>/) pour
    upload manuel dans Claude Desktop > Settings > Customize > Skills.

    Claude Desktop ne lit pas ~/.claude/skills/ comme Claude Code : il
    necessite un .zip uploade via son UI. On genere donc le .zip dans
    ~/Downloads/ (ou TMPDIR fallback) et on revele le fichier dans Finder
    pour que l'user n'ait qu'a le drag-drop dans Customize > Skills.

    Refus si plugin / skill introuvable. Limite MAX_ZIP_SIZE pour eviter
    de zipper des dossiers absurdes.
    """
    if not full_name or not skill_name:
        return False, "Nom de plugin et nom de skill requis"
    if "/" in skill_name or ".." in skill_name or skill_name.startswith("."):
        return False, f"Nom de skill invalide : '{skill_name}'"
    installed = _load_installed_plugins()
    if full_name not in installed:
        return False, f"Plugin '{full_name}' introuvable"
    entries = installed[full_name]
    if not isinstance(entries, list) or not entries:
        return False, f"Plugin '{full_name}' sans entree valide"
    install_path = entries[0].get("installPath", "") if isinstance(entries[0], dict) else ""
    p = Path(install_path)
    skill_dir = None
    for cand in (p / "skills" / skill_name, p / ".claude-plugin/skills" / skill_name):
        if cand.is_dir():
            skill_dir = cand
            break
    if skill_dir is None:
        return False, f"Skill '{skill_name}' introuvable dans le plugin '{full_name}'"
    total = 0
    for f in skill_dir.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
            if total > MAX_ZIP_SIZE:
                return False, f"Skill '{skill_name}' depasse {MAX_ZIP_SIZE//1024//1024} Mo (taille max .zip)"
    out_dir = HOME / "Downloads"
    if not out_dir.is_dir():
        out_dir = Path(tempfile.gettempdir())
    plugin_short, _ = _split_plugin_name(full_name)
    safe_plugin = re.sub(r"[^A-Za-z0-9._-]", "_", plugin_short or "plugin")
    safe_skill = re.sub(r"[^A-Za-z0-9._-]", "_", skill_name)
    zip_path = out_dir / f"{safe_plugin}-{safe_skill}.zip"
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for f in skill_dir.rglob("*"):
                if f.is_file():
                    arcname = Path(skill_name) / f.relative_to(skill_dir)
                    z.write(f, arcname.as_posix())
    except Exception as e:
        return False, f"Echec creation .zip : {e}"
    # v1.13.2 - on n'ouvre plus Finder automatiquement. Sur macOS un
    # 'open -R' coute 0.5-2s pour faire apparaitre la fenetre, et pour
    # 8 skills d'un meme plugin l'utilisateur se tapait 8 fenetres
    # Finder. Maintenant le toast UI expose un bouton "Reveler dans
    # Finder" qui appelle /api/reveal-path uniquement si l'user veut.
    return True, {"message": (f"Skill '{skill_name}' zippe : {zip_path}. "
                              f"Glisse-le dans Settings > Customize > Skills "
                              f"de Claude Desktop."),
                  "zip_path": str(zip_path),
                  "skill": skill_name,
                  "plugin": full_name}


def pick_native_folder(prompt_text="Choisis le dossier du skill"):
    """v1.13.4 - Native macOS folder picker via osascript. L'user n'a plus
    a taper le path a la main dans le champ texte du tab "Dossier" de
    "+ Ajouter un Skill". Bouton "Parcourir..." appelle cet endpoint qui
    ouvre une vraie boite de dialogue Finder.

    Retourne (success, message). Sur success, message est le path
    POSIX du dossier choisi. Sur cancel utilisateur, success=False et
    message est neutre ('Annule') pour qu'on n'affiche pas une erreur
    rouge inutilement cote UI.
    """
    if sys.platform != "darwin":
        return False, "Picker natif disponible seulement sur macOS"
    safe_prompt = (prompt_text or "").replace('"', '').replace("\\", "")[:200]
    try:
        r = subprocess.run(
            ["osascript", "-e",
             f'POSIX path of (choose folder with prompt "{safe_prompt}")'],
            capture_output=True, text=True, timeout=300,
        )
    except Exception as e:
        return False, f"Erreur lancement picker : {e}"
    if r.returncode != 0:
        stderr = (r.stderr or "").strip().lower()
        if "user canceled" in stderr or "user cancelled" in stderr or "-128" in stderr:
            return False, "Annule"
        return False, f"Erreur picker : {r.stderr.strip()[:200]}"
    path = (r.stdout or "").strip()
    if not path:
        return False, "Aucun chemin retourne"
    # osascript renvoie souvent un trailing slash, on enleve pour
    # coherence avec ce que le backend attend dans add_skill_from_folder
    return True, path.rstrip("/")


def reveal_path_in_finder(path):
    """v1.13.2 - Revele un fichier dans Finder sur macOS. Appele a la
    demande par le toast UI ('Reveler dans Finder'), plus
    automatiquement apres chaque import de skill (Finder lent + spam
    pour les imports multiples).

    Refus si le path n'est pas un fichier ou s'echappe de ~/Downloads
    ou de TMPDIR (defense en profondeur, le path vient quand meme
    d'un endpoint cote serveur).
    """
    if not path:
        return False, "Chemin manquant"
    try:
        p = Path(path).resolve()
    except Exception as e:
        return False, f"Chemin invalide : {e}"
    if not p.is_file():
        return False, f"Fichier introuvable : {p}"
    allowed_roots = [
        (HOME / "Downloads").resolve(),
        Path(tempfile.gettempdir()).resolve(),
    ]
    if not any(str(p).startswith(str(root) + os.sep) for root in allowed_roots):
        return False, f"Chemin hors zone autorisee : {p}"
    if sys.platform != "darwin":
        return False, "Reveler dans Finder n'est disponible que sur macOS"
    try:
        subprocess.Popen(["open", "-R", str(p)])
        return True, f"Finder ouvert sur {p.name}"
    except Exception as e:
        return False, f"Echec ouverture Finder : {e}"


def _split_plugin_name(full_name):
    """github@claude-plugins-official -> ('github', 'claude-plugins-official')"""
    if "@" in full_name:
        n, m = full_name.split("@", 1)
        return n, m
    return full_name, ""


def _load_installed_plugins():
    if not INSTALLED_PLUGINS_FILE.exists():
        return {}
    try:
        with open(INSTALLED_PLUGINS_FILE) as f:
            data = json.load(f)
        return data.get("plugins", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _find_orphan_versions(install_path, all_install_paths):
    p = Path(install_path)
    parts = p.parts
    version_idx = None
    for i, part in enumerate(parts):
        if re.match(r'^\d+\.\d+\.\d+', part):
            version_idx = i
    if version_idx is None:
        return []
    version_dir = Path(*parts[: version_idx + 1])
    parent = version_dir.parent
    if not parent.exists():
        return []
    siblings = [d for d in parent.iterdir() if d.is_dir() and re.match(r'^\d+\.\d+\.\d+', d.name)]
    referenced = set()
    parent_str = str(parent)
    for ip in all_install_paths:
        ip_path = Path(ip)
        if str(ip_path).startswith(parent_str + "/") or str(ip_path) == parent_str:
            for part in ip_path.parts:
                if re.match(r'^\d+\.\d+\.\d+', part):
                    referenced.add(part)
    return sorted(s.name for s in siblings if s.name not in referenced)


def list_plugins():
    installed = _load_installed_plugins()
    settings = _load_settings()
    enabled_map = settings.get("enabledPlugins", {}) if isinstance(settings, dict) else {}
    all_install_paths = []
    for entries in installed.values():
        if isinstance(entries, list):
            for e in entries:
                if isinstance(e, dict) and e.get("installPath"):
                    all_install_paths.append(e["installPath"])
    plugins = []
    for full_name, entries in installed.items():
        if not isinstance(entries, list) or not entries:
            continue
        entry = entries[0]
        if not isinstance(entry, dict):
            continue
        name, marketplace = _split_plugin_name(full_name)
        install_path = entry.get("installPath", "")
        contents = _scan_plugin_contents(install_path)
        plugins.append({
            "name": name,
            "marketplace": marketplace,
            "full_name": full_name,
            "version": entry.get("version", ""),
            "enabled": bool(enabled_map.get(full_name, True)),
            "installPath": install_path,
            "installedAt": entry.get("installedAt", ""),
            "lastUpdated": entry.get("lastUpdated", ""),
            "scope": entry.get("scope", ""),
            "contents": contents,
            "extra_versions": _find_orphan_versions(install_path, all_install_paths),
        })
    plugins.sort(key=lambda x: (x["marketplace"], x["name"]))
    return plugins


def cleanup_plugin_orphan(full_name, version):
    if not full_name or not version:
        return False, "Nom et version requis"
    if not re.match(r'^\d+\.\d+\.\d+[a-zA-Z0-9._\-]*$', version):
        return False, "Version invalide"
    installed = _load_installed_plugins()
    if full_name not in installed:
        return False, f"Plugin '{full_name}' introuvable"
    entry = installed[full_name][0]
    if version == entry.get("version", ""):
        return False, "Refus : cette version est la version installee"
    install_path = entry.get("installPath", "")
    p = Path(install_path)
    parts = p.parts
    version_idx = None
    for i, part in enumerate(parts):
        if re.match(r'^\d+\.\d+\.\d+', part):
            version_idx = i
    if version_idx is None:
        return False, "Impossible de localiser la version dans installPath"
    version_dir_parent = Path(*parts[:version_idx])
    orphan_dir = version_dir_parent / version
    if not orphan_dir.exists() or not orphan_dir.is_dir():
        return False, f"Dossier orphelin '{orphan_dir}' introuvable"
    ORPHAN_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', full_name)
    backup_path = ORPHAN_BACKUP_DIR / f"{safe_name}-{version}-{ts}.zip"
    with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in orphan_dir.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(orphan_dir))
    shutil.rmtree(orphan_dir)
    return True, f"Version orpheline {version} de '{full_name}' supprimee (backup : {backup_path.name})"


def cleanup_all_plugin_orphans():
    """v1.14.20 - Nettoie TOUTES les versions orphelines en un clic.

    Demande utilisateur : la Vue d'ensemble annoncait "8 version(s)
    orpheline(s) de plugin" mais la correction demandait huit clics et huit
    confirmations, un par carte de plugin. Ce lot reutilise, orphelin par
    orphelin, le chemin eprouve de cleanup_plugin_orphan : backup .zip dans
    ~/.claude/backups/claude-control/orphan-plugins/ puis suppression du
    dossier de cache -- la version active n'est jamais touchee (refus
    explicite dans cleanup_plugin_orphan). Un echec sur un orphelin
    n'empeche pas les autres ; le compte rendu liste les deux camps.
    """
    try:
        plugins = list_plugins()
    except Exception as e:
        return False, _srv("orphans_listing_failed", e=e)
    targets = [(p["full_name"], v) for p in plugins
               for v in (p.get("extra_versions") or [])]
    if not targets:
        return True, {"message": _srv("orphans_none"),
                      "cleaned": [], "failed": []}
    cleaned, failed = [], []
    for full_name, version in targets:
        try:
            ok, msg = cleanup_plugin_orphan(full_name, version)
        except Exception as e:
            ok, msg = False, f"{type(e).__name__}: {e}"
        if ok:
            cleaned.append({"plugin": full_name, "version": version})
        else:
            failed.append({"plugin": full_name, "version": version,
                           "error": str(msg)[:200]})
        _log(f"cleanup_all_plugin_orphans: {full_name} v{version} -> "
             f"{'ok' if ok else msg}")
    head = _srv("orphans_cleaned", n=len(cleaned))
    if failed:
        head += _srv("orphans_failed_suffix", f=len(failed))
    head += _srv("orphans_backup_suffix")
    return bool(cleaned) or not failed, {
        "message": head, "cleaned": cleaned, "failed": failed}


def toggle_plugin(full_name):
    if not full_name:
        return False, "Nom de plugin requis"
    installed = _load_installed_plugins()
    if full_name not in installed:
        return False, f"Plugin '{full_name}' introuvable"
    settings = _load_settings()
    enabled_map = settings.setdefault("enabledPlugins", {})
    current = bool(enabled_map.get(full_name, True))
    enabled_map[full_name] = not current
    _save_settings(settings)
    state = "active" if not current else "desactive"
    return True, f"Plugin '{full_name}' {state}"


def get_plugin_detail(full_name):
    installed = _load_installed_plugins()
    if full_name not in installed:
        return False, f"Plugin '{full_name}' introuvable"
    entry = installed[full_name][0]
    contents = _scan_plugin_contents(entry.get("installPath", ""))
    return True, {
        "full_name": full_name,
        "installPath": entry.get("installPath", ""),
        "version": entry.get("version", ""),
        "contents": contents,
    }


# === AUTO-UPDATE (interroge GitHub releases) ===

def get_github_repo():
    if GITHUB_REPO_FILE.exists():
        return GITHUB_REPO_FILE.read_text().strip()
    return None


def get_local_version():
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text().strip()
    return "1.0.0"


def check_update():
    import urllib.request, urllib.error
    repo = get_github_repo()
    if not repo:
        return {"local": get_local_version(), "latest": None,
                "update_available": False, "error": "GitHub repo non configure"}
    try:
        url = f"https://api.github.com/repos/{repo}/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "claude-control"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        latest = data.get("tag_name", "").lstrip("v")
        local = get_local_version()
        return {
            "local": local,
            "latest": latest,
            "update_available": bool(latest and latest != local),
            "release_url": data.get("html_url", ""),
            "release_notes": data.get("body", "")[:500],
        }
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"local": get_local_version(), "latest": None,
                    "update_available": False, "error": "Aucune release publiee"}
        return {"local": get_local_version(), "latest": None,
                "update_available": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"local": get_local_version(), "latest": None,
                "update_available": False, "error": str(e)[:100]}


def apply_update():
    repo_dir = HOME / "dev/claude-control"
    if not repo_dir.exists():
        return False, "Repo local introuvable : ~/dev/claude-control"
    try:
        r = subprocess.run(["git", "-C", str(repo_dir), "pull", "--ff-only"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False, f"git pull : {r.stderr[:200]}"
    except Exception as e:
        return False, f"Erreur git : {e}"
    src = repo_dir / "src/app.py"
    dst = HOME / "Applications/claude-control/app.py"
    if src.exists():
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            return False, f"Echec copie : {e}"
    return True, "Mis a jour. Redemarrage automatique..."


def _apply_update_then_restart():
    ok, msg = apply_update()
    if ok:
        restart_self()
        return True, msg + " (redemarrage en cours)"
    return ok, msg


def restart_self():
    """Replace the running process image with a fresh python running this script.
    The .app launcher sees the same PID continuing — no "n'est plus ouverte" dialog."""
    script = str(Path(__file__).resolve())
    def _execv():
        time.sleep(0.4)
        try:
            os.execv(sys.executable, [sys.executable, script])
        except Exception as e:
            _log(f"restart_self execv failed: {e}")
            os._exit(0)
    threading.Thread(target=_execv, daemon=True).start()
    return True, "Redemarrage en cours..."


# v1.7.2 - favicon SVG inline pour eviter le 404 /favicon.ico et donner une
# identite visuelle dans l'onglet du navigateur. Mirror la charte du .app :
# squircle vert degrade (#2C5F3F -> #14301F) + monogramme C blanc + accent
# orange Sekoia (#D97757). Pas de PNG/ICO commit (stdlib-only, pas de Pillow).
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0" stop-color="#2C5F3F"/>'
    '<stop offset="1" stop-color="#14301F"/>'
    '</linearGradient></defs>'
    '<rect x="0" y="0" width="64" height="64" rx="14" ry="14" fill="url(#g)"/>'
    '<text x="32" y="44" text-anchor="middle" '
    'font-family="-apple-system,system-ui,sans-serif" '
    'font-size="38" font-weight="700" fill="#fff">C</text>'
    '<circle cx="50" cy="14" r="6" fill="#D97757"/>'
    '</svg>'
)


