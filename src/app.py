#!/usr/bin/env python3
"""Claude Control - app locale pour gerer MCPs et Skills de Claude Desktop."""
import http.server, io, json, os, re, shutil, socketserver, subprocess, sys, tempfile, threading, time, traceback, webbrowser, zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

MAX_ZIP_SIZE = 50 * 1024 * 1024  # 50 Mo

PORT = 8765
HOME = Path.home()
CONFIG_PATH = HOME / "Library/Application Support/Claude/claude_desktop_config.json"
EXTENSIONS_INSTALL_FILE = HOME / "Library/Application Support/Claude/extensions-installations.json"
EXTENSIONS_SETTINGS_DIR = HOME / "Library/Application Support/Claude/Claude Extensions Settings"
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

# v1.6.6 : tracking en memoire des derniers restarts MCP/extension pour
# afficher "Dernier restart : il y a X min" dans la carte Action rapide.
# Reset au redemarrage du serveur Claude Control.
_LAST_RESTARTS = {}


def _log(msg):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass


def _notify(title, message):
    """Affiche une notification macOS native via osascript. Best-effort,
    silencieux si echec. v1.6.6 : utilise pour signaler la fin d'un restart
    MCP/extension meme quand l'UI Claude Control n'est pas au premier plan."""
    try:
        safe_title = (title or "").replace('"', '\\"').replace('\\', '\\\\')[:120]
        safe_msg = (message or "").replace('"', '\\"').replace('\\', '\\\\')[:300]
        script = f'display notification "{safe_msg}" with title "{safe_title}"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=3)
    except Exception:
        pass


def load_config():
    if not CONFIG_PATH.exists():
        return {"mcpServers": {}, "_disabledMcps": {}}
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(config):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = BACKUP_DIR / f"config.{ts}.json"
        with open(CONFIG_PATH) as src, open(backup, "w") as dst:
            dst.write(src.read())
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def get_running_mcps():
    try:
        r = subprocess.run(["ps", "auxww"], capture_output=True, text=True, timeout=5)
    except Exception:
        return set()
    keywords = {"mongodb-mcp": "klide-mongodb", "mailchimp-mcp": "mailchimp",
                "sekoia-geo": "sekoia-geo", "compass-mcp": "compass",
                "thedotmack/plugin": "claude-mem-search", "mcp-pdf": "pdf"}
    running = set()
    for line in r.stdout.splitlines():
        if "Helpers/disclaimer" in line:
            continue
        for kw, label in keywords.items():
            if kw in line:
                running.add(label)
    return running


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


def read_skill_meta(skill_dir):
    """Lit le frontmatter YAML d'un SKILL.md et retourne {category, description, tags}."""
    meta = {"category": None, "description": None, "tags": []}
    md = skill_dir / "SKILL.md"
    if not md.exists():
        return meta
    try:
        content = md.read_text(errors="replace")
    except Exception:
        return meta
    if not content.startswith("---"):
        return meta
    end = content.find("\n---", 3)
    if end == -1:
        return meta
    for line in content[3:end].splitlines():
        for key in ("category", "description"):
            m = re.match(rf'^\s*{key}\s*:\s*(.+?)\s*$', line)
            if m:
                v = m.group(1).strip().strip('"').strip("'")
                meta[key] = v or None
        m = re.match(r'^\s*tags\s*:\s*\[([^\]]*)\]\s*$', line)
        if m:
            meta["tags"] = [t.strip().strip('"').strip("'") for t in m.group(1).split(",") if t.strip()]
    return meta


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
    # Map cle -> index pour replace en place
    existing_idx = {}
    for i, line in enumerate(fm_lines):
        m = re.match(r'^\s*([a-zA-Z_][a-zA-Z0-9_-]*)\s*:', line)
        if m:
            existing_idx[m.group(1)] = i
    for key, value in updates.items():
        new_line = f"{key}: {_yaml_quote_value(value)}"
        if key in existing_idx:
            fm_lines[existing_idx[key]] = new_line
        else:
            fm_lines.append(new_line)
    new_fm = "---\n" + "\n".join(fm_lines) + "\n---\n"
    return new_fm + body


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
    target = None
    for base in (SKILLS_DIR, SKILLS_DISABLED_DIR):
        candidate = base / name
        if candidate.exists() and candidate.is_dir():
            target = candidate
            break
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
        md.write_text(new_content)
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
    si disponible, sinon None. shutil.which respecte le PATH du process.
    """
    return shutil.which("claude")


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


def _diagnose_claude_cli():
    """v1.9.4 - Sanity check : lance 'claude --version' pour verifier que
    le CLI est non seulement present mais aussi fonctionnel. Retourne dict :
      {available: bool, path: str?, version: str?, error: str?}
    """
    cli = _claude_cli_path()
    if not cli:
        return {"available": False, "path": None, "version": None,
                "error": "claude not in PATH"}
    try:
        r = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=10)
        version = (r.stdout or "").strip() or (r.stderr or "").strip()
        if r.returncode != 0:
            return {"available": False, "path": cli, "version": None,
                    "error": f"--version exit {r.returncode}: {(r.stderr or '').strip()[:200]}"}
        return {"available": True, "path": cli, "version": version, "error": None}
    except subprocess.TimeoutExpired:
        return {"available": False, "path": cli, "version": None,
                "error": "--version timeout 10s"}
    except Exception as e:
        return {"available": False, "path": cli, "version": None,
                "error": f"{type(e).__name__}: {e}"}


class ClaudeCliNotLoggedIn(Exception):
    """v1.9.5 - Cas specifique : le CLI Claude Code repond mais signale
    'Not logged in'. C'est un setup utilisateur (une seule fois) : il
    doit run 'claude /login' dans un terminal pour s'authentifier."""


def _call_claude_cli(prompt, timeout=60):
    """v1.9.3 / v1.9.4 / v1.9.5 - Invoque le CLI Claude Code en mode print.

    v1.9.5 : detecte specifiquement le cas 'Not logged in' (cas reel
    observe sur la machine de Fabien) et leve ClaudeCliNotLoggedIn pour
    qu'on puisse afficher une instruction claire cote UI plutot qu'un
    message generique.
    """
    cli_path = _claude_cli_path()
    if not cli_path:
        raise FileNotFoundError("claude CLI not in PATH")
    cmd = [cli_path, "-p", prompt, "--output-format", "text"]
    _log(f"_call_claude_cli: argv={cmd[:2]} prompt_len={len(prompt)}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    stdout = (r.stdout or "").strip()
    stderr = (r.stderr or "").strip()
    if r.returncode != 0:
        _log(f"_call_claude_cli: exit={r.returncode} stdout={stdout[:300]!r} stderr={stderr[:300]!r}")
        # v1.9.5 - cas 'Not logged in' detecte par pattern
        combined = (stdout + " " + stderr).lower()
        if "not logged in" in combined or "please run /login" in combined or "please log in" in combined:
            raise ClaudeCliNotLoggedIn(
                "Claude Code CLI n'est pas authentifie sur ce systeme. "
                "Ouvre un terminal et lance la commande : claude /login"
            )
        if not stdout and not stderr:
            hint = (
                "Sortie vide. Causes possibles : (1) le CLI necessite une "
                "session OAuth deja active (run 'claude' dans un terminal "
                "pour t'authentifier), (2) le CLI est lance hors d'un TTY "
                "et refuse de prompter, (3) le PATH du process Claude "
                "Control ne contient pas l'emplacement attendu de la "
                "config CLI."
            )
            raise RuntimeError(f"claude CLI exit {r.returncode} (stdout+stderr vides). {hint}")
        details = []
        if stderr: details.append(f"stderr: {stderr[:300]}")
        if stdout: details.append(f"stdout: {stdout[:300]}")
        raise RuntimeError(f"claude CLI exit {r.returncode}. " + " | ".join(details))
    return stdout


_SUGGEST_SYSTEM_PROMPT = (
    "You write concise, action-oriented descriptions for Claude Code skills. "
    "A skill description is a single sentence (40-150 chars) that tells Claude "
    "WHEN to trigger this skill. It should start with a verb in present tense "
    "('Use this skill when...', 'Helps with...', 'Generates...'). Avoid generic "
    "phrasing. Read the SKILL.md content provided and produce ONLY the "
    "description text - no quotes, no preamble, no explanation, just the "
    "description string itself. Keep it under 150 chars."
)


def suggest_skill_description(name, body_max_chars=4000, lang=None):
    """v1.9.3 / v1.9.6 - Genere une description suggeree pour un skill via
    le CLI Claude Code.

    v1.9.6 : ajout du parametre `lang` ('fr'|'en'|None). Le system prompt
    inclut une directive 'Respond in {lang}' pour que la suggestion match
    la langue voulue par l'utilisateur (par defaut, langue de l'UI Claude
    Control). Sans lang, le LLM suit ses propres instincts (souvent EN
    parce que le system prompt est en anglais).
    """
    if not name:
        return False, "Nom de skill requis"
    if not _claude_cli_path():
        return False, ("Claude Code CLI ('claude') introuvable dans le PATH. "
                       "Installer avec : npm install -g @anthropic-ai/claude-code")
    target = None
    for base in (SKILLS_DIR, SKILLS_DISABLED_DIR):
        candidate = base / name
        if candidate.exists() and candidate.is_dir():
            target = candidate
            break
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
        lang_directive = "\n\nIMPORTANT: Respond in French (Francais)."
    elif lang == "en":
        lang_directive = "\n\nIMPORTANT: Respond in English."
    prompt = (
        _SUGGEST_SYSTEM_PROMPT + lang_directive + "\n\n"
        f"Skill name (folder): {name}\n\n"
        f"SKILL.md content:\n```\n{content}\n```\n\n"
        f"Generate the description string."
    )
    _log(f"suggest_skill_description: name={name} chars_sent={len(content)} lang={lang}")
    try:
        raw_response = _call_claude_cli(prompt)
    except ClaudeCliNotLoggedIn as e:
        # v1.9.5 - cas specifique 'Not logged in' : on retourne un code
        # error_code que l'UI peut utiliser pour afficher des instructions
        # ciblees + un bouton 'Copier la commande' au lieu d'un dump
        # technique.
        _log(f"suggest_skill_description: CLI not logged in")
        return False, {
            "error": str(e),
            "error_code": "cli_not_logged_in",
            "fix_command": "claude /login",
        }
    except subprocess.TimeoutExpired:
        _log("suggest_skill_description: claude CLI timeout 60s")
        return False, "Timeout : le CLI Claude n'a pas repondu en 60s"
    except FileNotFoundError as e:
        _log(f"suggest_skill_description: CLI not found : {e}")
        return False, ("Claude Code CLI introuvable dans le PATH. "
                       "Installer : npm install -g @anthropic-ai/claude-code")
    except Exception as e:
        _log(f"suggest_skill_description: exception {type(e).__name__} : {e}")
        return False, f"Erreur appel CLI ({type(e).__name__}) : {e}"
    _log(f"suggest_skill_description: raw response len={len(raw_response or '')} preview={(raw_response or '')[:200]!r}")
    # v1.9.2 - sanitize plus agressif. Haiku peut retourner :
    # - du markdown (## prefix, **bold**, > quote, * list)
    # - des prefixes 'Description:', 'Here is the description:'
    # - des quotes triple ou single
    # - du preamble multi-ligne ('Sure! Here is...\n\nUse this skill for X')
    # On nettoie chaque ligne puis on prend la plus longue / la plus
    # substantive (heuristique : la 'vraie' description est generalement
    # la ligne la plus longue dans la reponse).
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
    if cleaned_lines:
        # Heuristique : prefer lines that don't end with ':' (preamble) and
        # don't start with 'Sure', 'Here', 'Of course' (filler). Sort by
        # (is_preamble ASC, length DESC) -> first non-preamble + longest.
        preamble_starts = ("sure", "here", "of course", "absolutely",
                           "certainly", "yes,", "let me", "i'll", "let's")
        def _is_preamble(line):
            ll = line.lower().strip()
            if ll.endswith(":"):
                return True
            for p in preamble_starts:
                if ll.startswith(p):
                    return True
            return False
        cleaned_lines.sort(key=lambda ln: (_is_preamble(ln), -len(ln)))
        suggestion = cleaned_lines[0].strip()
    else:
        suggestion = _strip_line(raw)
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


def _list_plugin_skills():
    """Retourne la liste des skills fournis par chaque plugin actif sous forme de
    dicts identiques aux skills utilisateur (mais source != 'user', editable=False)."""
    items = []
    try:
        installed = _load_installed_plugins()
    except Exception:
        return items
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
                items.append({"name": d.name, "_dir": d, "_source": f"plugin:{full_name}", "meta": meta})
    return items


def _skill_quality(description):
    """v1.7.7 - Classifie un skill par qualite de description, qui est le
    seul facteur qui determine si Claude le declenche automatiquement.
    - 'broken'    : pas de description -> ne se declenche jamais
    - 'enrich'    : description < 30 chars -> peu fiable
    - 'excellent' : description >= 30 chars
    """
    desc = (description or "").strip()
    if not desc:
        return "broken"
    if len(desc) < 30:
        return "enrich"
    return "excellent"


def get_state():
    config = load_config()
    active = config.get("mcpServers", {})
    disabled = config.get("_disabledMcps", {})
    running = get_running_mcps()
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    SKILLS_DISABLED_DIR.mkdir(parents=True, exist_ok=True)
    active_skills = sorted([d.name for d in SKILLS_DIR.iterdir()
                            if d.is_dir() and (d / "SKILL.md").exists() and not d.name.startswith(".")])
    disabled_skills = sorted([d.name for d in SKILLS_DISABLED_DIR.iterdir()
                              if d.is_dir() and not d.name.startswith(".")])
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
            "quality": _skill_quality(meta["description"]),
            "usage_count": int(usage_counts.get(name, 0)),
        }
    skills = [_skill_entry(n, SKILLS_DIR, True, "user") for n in active_skills]
    skills += [_skill_entry(n, SKILLS_DISABLED_DIR, False, "user") for n in disabled_skills]
    user_names = {s["name"] for s in skills}
    for it in _list_plugin_skills():
        if it["name"] in user_names:
            continue  # the user version takes precedence in the listing; duplicate is reported in overview
        meta = it["meta"]
        skills.append({
            "name": it["name"], "active": True,
            "category": meta["category"],
            "auto_category": _auto_category(meta["description"], it["name"]) if not meta["category"] else None,
            "description": meta["description"],
            "tags": meta["tags"],
            "source": it["_source"],
            "editable": False,
            "quality": _skill_quality(meta["description"]),
            "usage_count": int(usage_counts.get(it["name"], 0)),
        })
    mcps_list = (
        [{"name": n, "active": True, "running": n in running, "type": "classic"} for n in sorted(active.keys())]
        + [{"name": n, "active": False, "running": False, "type": "classic"} for n in sorted(disabled.keys())]
    )
    extensions = _list_extensions()
    for e in extensions:
        # v1.8.3 - critere "running" pour AFFICHAGE UI : permissif sans
        # seuil mtime court. Bug D 2026-05-06 : 8 extensions MCPB en
        # standby legitime (Filesystem, Control Chrome, Word, etc.) ont
        # ete classees "Inactifs" parce que leur log mtime depassait 120s
        # (silence entre 2 appels agent, ce qui est normal pour des MCPs
        # peu sollicites). Ce seuil a un sens pour le watchdog (detecter
        # un freeze sur cible monitoree) mais pas pour l'UI (statut
        # "chargee et prete a etre appelee").
        # Critere UI permissif :
        #   1. PIDs trouves via fingerprint -> running (rare pour les
        #      Helper Nodes anonymes, mais valide quand applicable)
        #   2. Sinon, log file existe ET pas de shutdown gracieux dans
        #      la queue -> running (extension chargee, en standby)
        #   3. Sinon -> not running
        running_ext = bool(_extension_pids(e["name"]))
        if not running_ext:
            try:
                log = _find_mcp_log(e["name"])
                if log and log.exists() and not _log_shows_graceful_shutdown(log):
                    running_ext = True
            except Exception:
                pass
        mcps_list.append({
            "name": e["name"],
            "active": e["enabled"],
            "running": running_ext,
            "type": "extension",
            "extension_id": e["id"],
            "version": e["version"],
        })
    return {
        "mcps": mcps_list,
        "skills": skills,
    }


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
                EXTENSIONS_INSTALL_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
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
    with open(INSTALLED_PLUGINS_FILE, "w") as f:
        json.dump(data, f, indent=2)
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


def add_plugin_from_git(url):
    url = (url or "").strip()
    if not (url.startswith("https://") or url.startswith("http://") or url.startswith("git@")):
        return False, "URL Git invalide (doit commencer par https:// ou git@)"
    repo_name = re.sub(r'\.git$', '', url.rstrip('/').split('/')[-1])
    if not repo_name or "/" in repo_name:
        return False, "Impossible de déterminer le nom du repo depuis l'URL"
    cache_dir = HOME / ".claude/plugins/cache/manual" / repo_name
    if cache_dir.exists():
        return False, f"Un plugin existe déjà dans {cache_dir}. Supprime-le d'abord."
    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(["git", "clone", "--depth", "1", url, str(cache_dir)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return False, f"git clone : {r.stderr[:200]}"
    except subprocess.TimeoutExpired:
        return False, "Timeout git clone (120s)"
    except FileNotFoundError:
        return False, "git n'est pas installé"
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
    with open(INSTALLED_PLUGINS_FILE, "w") as f:
        json.dump(data, f, indent=2)
    settings = _load_settings()
    settings.setdefault("enabledPlugins", {})[full_name] = True
    _save_settings(settings)
    return True, f"Plugin '{full_name}' v{version} ajouté et activé"


def _read_command_preview(path, max_chars=400):
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return ""
    return text if len(text) <= max_chars else text[:max_chars] + "\n..."


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


def get_command(name, source):
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
    WATCHDOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(WATCHDOG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
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
    try:
        text = log.read_text(errors="replace")
    except Exception:
        return False
    tail = text[-4000:].lower()
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
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "w") as out_f:
        json.dump(out, out_f, indent=2)


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
    try:
        subprocess.run(["pkill", "-9", "-f", "/Applications/Claude.app/Contents/MacOS/Claude"],
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
        # Pas assez de donnees pour distinguer - on garde l'ancien comportement
        # (Type A par defaut) pour ne pas regresser le watchdog DC.
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

    # Si on vient d'expirer la fenetre de verify, le toggle a echoue.
    if state["pending_verify_until_ts"] > 0 and now >= state["pending_verify_until_ts"]:
        state["pending_verify_until_ts"] = 0.0
        _watchdog_event(
            "dc_toggle_failed",
            "Log toujours silencieux apres verify_after_toggle_seconds"
        )
        # Escalade -> dialog
        dc = dc_status()
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
                            subprocess.run(["open", "-a", "Claude"], check=False)
                    elif cfg["freeze_detection"]:
                        if not _claude_responsive(timeout=cfg["freeze_timeout"]):
                            _watchdog_event("restart", f"Claude Desktop unresponsive >{cfg['freeze_timeout']}s, restarting")
                            subprocess.run(["pkill", "-9", "-f", "Claude"], check=False)
                            time.sleep(2)
                            subprocess.run(["open", "-a", "Claude"], check=False)
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





def get_skill_usage(days=30):
    """Parcourt ~/.claude/projects/*/*.jsonl et compte les invocations du tool
    `Skill`. Tolerant aux changements de schema : ignore silencieusement chaque
    ligne malformee. Retourne un classement et de la metadata pour le fallback."""
    counts = {}
    sessions_seen = set()
    files_scanned = 0
    lines_scanned = 0
    parse_errors = 0
    cutoff = None
    if days and days > 0:
        cutoff = datetime.now().timestamp() - days * 86400
    if not PROJECTS_LOGS_DIR.exists():
        return {"counts": {}, "ranked": [], "files_scanned": 0, "lines_scanned": 0,
                "parse_errors": 0, "sessions": 0, "ok": False,
                "reason": "projects_dir_missing",
                "projects_path": str(PROJECTS_LOGS_DIR)}
    for jsonl in PROJECTS_LOGS_DIR.rglob("*.jsonl"):
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
                if obj.get("type") != "assistant":
                    continue
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
                    name = block.get("name", "")
                    if name != "Skill":
                        continue
                    inp = block.get("input", {})
                    if not isinstance(inp, dict):
                        continue
                    skill = inp.get("skill") or inp.get("name")
                    if not skill:
                        continue
                    counts[str(skill)] = counts.get(str(skill), 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "counts": counts,
        "ranked": [{"name": k, "count": v} for k, v in ranked],
        "files_scanned": files_scanned,
        "lines_scanned": lines_scanned,
        "parse_errors": parse_errors,
        "sessions": len(sessions_seen),
        "days_window": days,
        "ok": True,
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
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(content)
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
    CLAUDE_MD_FILE.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_MD_FILE.write_text(content)
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
    target.write_text(content)
    return True, f"Command '{name}' enregistrée"


def restart_claude():
    subprocess.run(["pkill", "-9", "-f", "Claude"], check=False)
    time.sleep(2.5)
    subprocess.run(["open", "-a", "Claude"], check=False)
    return True, "Claude Desktop redemarre"


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
    candidates = sorted(CLAUDE_LOGS_DIR.glob(f"mcp-server-{name}*.log"))
    candidates += sorted(CLAUDE_LOGS_DIR.glob(f"*{name}*.log"))
    candidates += [CLAUDE_LOGS_DIR / "mcp.log", CLAUDE_LOGS_DIR / "main.log"]
    seen = set()
    for log_file in candidates:
        if not log_file.exists() or log_file in seen:
            continue
        seen.add(log_file)
        try:
            content = log_file.read_text(errors="replace")
        except Exception:
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
    url = url.strip()
    if not (url.startswith("https://") or url.startswith("http://") or url.startswith("git@")):
        return False, "URL Git invalide (doit commencer par https:// ou git@)"
    IMPORTED_REPOS_DIR.mkdir(parents=True, exist_ok=True)
    name = re.sub(r'\.git$', '', url.rstrip('/').split('/')[-1])
    target = IMPORTED_REPOS_DIR / name
    if target.exists():
        shutil.rmtree(target)
    try:
        r = subprocess.run(["git", "clone", "--depth", "1", url, str(target)],
                           capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            return False, f"git clone : {r.stderr[:200]}"
    except subprocess.TimeoutExpired:
        return False, "Timeout git clone (90s)"
    except FileNotFoundError:
        return False, "git n'est pas installe"
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
    url = url.strip()
    if not (url.startswith("https://") or url.startswith("http://") or url.startswith("git@")):
        return False, "URL Git invalide"
    name = re.sub(r'\.git$', '', url.rstrip('/').split('/')[-1])
    target = SKILLS_DIR / name
    if target.exists():
        return False, f"Skill '{name}' existe deja"
    try:
        r = subprocess.run(["git", "clone", "--depth", "1", url, str(target)],
                           capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            return False, f"git clone : {r.stderr[:200]}"
    except Exception as e:
        return False, f"Erreur git : {e}"
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
    (target / "SKILL.md").write_text(content)
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
    PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PRESETS_FILE, "w") as f:
        json.dump(data, f, indent=2)


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
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings(data):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if SETTINGS_FILE.exists():
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = BACKUP_DIR / f"settings.json.backup.{ts}"
        with open(SETTINGS_FILE) as src, open(backup, "w") as dst:
            dst.write(src.read())
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _plugin_root(install_path):
    """L'install_path pointe vers <cache>/<owner>/<repo>/<version>/plugins/<plugin>.
    On veut souvent inspecter <plugin> directement, ou son sous-dossier .claude-plugin."""
    p = Path(install_path)
    if (p / ".claude-plugin").is_dir():
        return p
    return p


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
    candidates_mcp = [p / ".mcp.json", p / ".claude-plugin/.mcp.json"]
    mcps = []
    for mf in candidates_mcp:
        if mf.is_file():
            try:
                data = json.loads(mf.read_text())
                servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
                if isinstance(servers, dict):
                    mcps.extend(sorted(servers.keys()))
            except Exception:
                pass
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


HTML = r"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"><title>Claude Control</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;utf8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2064%2064%22%3E%3Cdefs%3E%3ClinearGradient%20id%3D%22g%22%20x1%3D%220%22%20y1%3D%220%22%20x2%3D%220%22%20y2%3D%221%22%3E%3Cstop%20offset%3D%220%22%20stop-color%3D%22%232C5F3F%22%2F%3E%3Cstop%20offset%3D%221%22%20stop-color%3D%22%2314301F%22%2F%3E%3C%2FlinearGradient%3E%3C%2Fdefs%3E%3Crect%20x%3D%220%22%20y%3D%220%22%20width%3D%2264%22%20height%3D%2264%22%20rx%3D%2214%22%20ry%3D%2214%22%20fill%3D%22url(%23g)%22%2F%3E%3Ctext%20x%3D%2232%22%20y%3D%2244%22%20text-anchor%3D%22middle%22%20font-family%3D%22-apple-system%2Csystem-ui%2Csans-serif%22%20font-size%3D%2238%22%20font-weight%3D%22700%22%20fill%3D%22%23fff%22%3EC%3C%2Ftext%3E%3Ccircle%20cx%3D%2250%22%20cy%3D%2214%22%20r%3D%226%22%20fill%3D%22%23D97757%22%2F%3E%3C%2Fsvg%3E"/>
<script src="https://cdn.tailwindcss.com"></script>
<style>
body{background:linear-gradient(180deg,#fafaf9 0%,#f5f5f4 100%);}
.card{background:white;border:1px solid #e7e5e4;border-radius:12px;}
.running-dot{animation:pulse 2s infinite;}
@keyframes pulse{0%,100%{opacity:1;}50%{opacity:0.5;}}
.tab-btn{transition:all .15s;}
.tab-btn.active{background:white;box-shadow:0 1px 2px rgba(0,0,0,0.05);}
.update-badge{background:linear-gradient(135deg,#D97757,#C15F3C);}
.modal-bg{background:rgba(0,0,0,0.4);}
.lang-btn{transition:all .1s;}
.lang-btn.active{background:#1c1917;color:white;}
</style></head><body class="min-h-screen text-stone-900">
<div class="max-w-5xl mx-auto px-6 py-10">
<header class="flex justify-between items-start mb-8 gap-4">
<div><h1 class="text-3xl font-semibold">Claude Control</h1>
<p class="text-sm text-stone-500 mt-1">Sekoia &middot; <span data-i18n="header_subtitle">Contrôle de Claude Desktop</span> &middot; <span id="version" class="font-mono">v?</span></p>
<div id="update-banner" class="hidden mt-3"><button onclick="applyUpdate()" class="update-badge text-white text-xs px-3 py-1.5 rounded-full font-medium hover:opacity-90"><span id="update-text">Update disponible</span></button></div>
</div>
<div class="flex items-center gap-2 shrink-0">
<div class="flex text-xs font-mono border border-stone-200 rounded-md overflow-hidden">
<button id="lang-fr" onclick="setLang('fr')" class="lang-btn px-2 py-1 text-stone-600 hover:bg-stone-50">FR</button>
<button id="lang-en" onclick="setLang('en')" class="lang-btn px-2 py-1 text-stone-600 hover:bg-stone-50 border-l border-stone-200">EN</button>
</div>
<button onclick="restartSelf()" class="bg-stone-100 hover:bg-stone-200 text-stone-700 px-3 py-2.5 rounded-lg text-sm font-medium" data-i18n-title="restart_self_title" title="Redémarrer le serveur Claude Control">&#x21bb; <span data-i18n="restart_app_short">App</span></button>
<button onclick="restartClaude()" class="bg-stone-900 hover:bg-stone-800 text-white px-5 py-2.5 rounded-lg font-medium flex items-center gap-2">
<span>&#x21bb;</span><span data-i18n="restart_claude">Redémarrer Claude</span></button>
</div></header>
<div id="banner" class="hidden mb-4 p-3 rounded-lg text-sm border"></div>
<div id="watchdog-widget" class="mb-4"></div>
<nav id="main-tabs" class="flex gap-1 mb-6 bg-stone-100 p-1 rounded-lg overflow-x-auto">
<button class="main-tab-btn flex-1 min-w-[80px] px-3 py-2 text-sm rounded-md font-medium" data-main-tab="overview" onclick="setMainTab('overview')" data-i18n="tab_overview">Vue d'ensemble</button>
<button class="main-tab-btn flex-1 min-w-[80px] px-3 py-2 text-sm rounded-md font-medium" data-main-tab="mcps" onclick="setMainTab('mcps')" data-i18n="mcp_section">Serveurs MCP</button>
<button class="main-tab-btn flex-1 min-w-[80px] px-3 py-2 text-sm rounded-md font-medium" data-main-tab="skills" onclick="setMainTab('skills')" data-i18n="skills">Skills</button>
<button class="main-tab-btn flex-1 min-w-[80px] px-3 py-2 text-sm rounded-md font-medium" data-main-tab="plugins" onclick="setMainTab('plugins')" data-i18n="plugins">Plugins</button>
<button class="main-tab-btn flex-1 min-w-[80px] px-3 py-2 text-sm rounded-md font-medium" data-main-tab="commands" onclick="setMainTab('commands')" data-i18n="commands">Commands</button>
<button class="main-tab-btn flex-1 min-w-[80px] px-3 py-2 text-sm rounded-md font-medium" data-main-tab="watchdog" onclick="setMainTab('watchdog')" data-i18n="tab_watchdog">Watchdog</button>
<button class="main-tab-btn flex-1 min-w-[80px] px-3 py-2 text-sm rounded-md font-medium" data-main-tab="advanced" onclick="setMainTab('advanced')" data-i18n="tab_advanced">Avancé</button>
</nav>
<div data-main-tab="overview">
<section class="card p-6 mb-6">
<div class="flex items-baseline justify-between mb-3">
<h2 class="text-lg font-semibold" data-i18n="overview">Vue d'ensemble</h2>
<span id="overview-preset" class="text-xs text-stone-500"></span>
</div>
<div id="overview-stats" class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4"></div>
<div id="overview-top-skills" class="mb-3"></div>
<div id="overview-suggestions" class="mb-3"></div>
<div id="overview-health"></div>
</section>
</div>
<div data-main-tab="mcps" class="hidden">
<div id="mcp-conflicts-banner" class="mb-4 hidden"></div>
<section class="card p-6">
<h2 class="text-lg font-semibold mb-1" data-i18n="mcp_section">Serveurs MCP</h2>
<p class="text-xs text-stone-500 mb-4" data-i18n="mcp_help">Coché = chargé au démarrage de Claude Desktop</p>
<div class="mb-4 p-3 bg-stone-50 rounded-lg border border-stone-200">
<div class="flex items-center justify-between mb-2">
<span class="text-xs font-semibold uppercase tracking-wide text-stone-600" data-i18n="presets">PRESETS</span>
<button onclick="openSavePreset()" class="text-xs text-stone-700 hover:text-stone-900 font-medium" data-i18n="preset_save_current">+ Sauvegarder l'actuel</button>
</div>
<div id="presets-list" class="space-y-1.5"></div>
</div>
<div class="grid grid-cols-1 md:grid-cols-2 gap-4">
<div>
<div class="flex items-baseline justify-between mb-2 px-1">
<h3 class="text-sm font-semibold text-green-700"><span class="inline-block w-2 h-2 rounded-full bg-green-600 mr-1.5 align-middle"></span><span data-i18n="mcp_col_running">En cours d'execution</span></h3>
<span id="mcps-running-count" class="text-xs text-stone-400"></span>
</div>
<div id="mcps-running" class="space-y-2"></div>
</div>
<div>
<div class="flex items-baseline justify-between mb-2 px-1">
<h3 class="text-sm font-semibold text-stone-600"><span class="inline-block w-2 h-2 rounded-full bg-stone-400 mr-1.5 align-middle"></span><span data-i18n="mcp_col_stopped">Inactifs</span></h3>
<span id="mcps-stopped-count" class="text-xs text-stone-400"></span>
</div>
<div id="mcps-stopped" class="space-y-2"></div>
</div>
</div>
</section>
<section class="card p-6 mt-6">
<button onclick="toggleDiagExt()" class="w-full flex items-center justify-between text-left">
<div>
<h2 class="text-lg font-semibold" data-i18n="mcp_diag_title">Diagnostics extensions</h2>
<p class="text-xs text-stone-500 mt-1" data-i18n="mcp_diag_help">Mismatches manifest &harr; log &harr; process pour les Desktop Extensions</p>
</div>
<span id="diag-ext-caret" class="text-stone-400">&#9656;</span>
</button>
<div id="diag-ext-body" class="hidden mt-4">
<div id="diag-ext-content" class="text-sm text-stone-500" data-i18n="mcp_diag_loading">Chargement...</div>
</div>
</section>
</div>
<div data-main-tab="skills" class="hidden">
<section id="skills-cli-banner" class="hidden card p-3 mb-3 border-l-4 border-amber-400"></section>
<section id="skills-health-banner" class="card p-4 mb-4"></section>
<div class="grid grid-cols-1 md:grid-cols-[260px_1fr] gap-4">
<aside class="card p-4 md:sticky md:top-4 md:self-start md:max-h-[calc(100vh-2rem)] md:overflow-y-auto">
<input id="skills-search" type="search" oninput="filterSkills()" data-i18n-placeholder="skills_search_placeholder" placeholder="Rechercher..." class="w-full mb-4 p-2 border border-stone-200 rounded-lg text-sm focus:outline-none focus:border-stone-400"/>
<div id="skills-filters" class="space-y-4 text-sm"></div>
</aside>
<section>
<div id="skills-bulkbar" class="hidden card p-3 mb-3 flex items-center gap-3 bg-stone-900 text-white"></div>
<div id="skills" class="grid grid-cols-1 lg:grid-cols-2 gap-3"></div>
</section>
</div>
</div>
<div data-main-tab="plugins" class="hidden">
<section class="card p-6">
<div class="flex items-baseline justify-between mb-1">
<h2 class="text-lg font-semibold" data-i18n="plugins">Plugins</h2>
<button onclick="openAddPlugin()" class="text-xs text-stone-700 hover:text-stone-900 font-medium" data-i18n="plugin_add_btn">+ Ajouter un plugin (Git)</button>
</div>
<p class="text-xs text-stone-500 mb-3" data-i18n="plugins_help">Plugins Claude Code installés via marketplace</p>
<div class="mb-3 p-3 rounded-lg bg-stone-50 border border-stone-200 text-xs text-stone-600 leading-relaxed">
<strong>&#8505; <span data-i18n="plugins_explainer_title">Pas de Start/Stop/Restart sur les plugins</span></strong>
<p class="mt-1" data-i18n-html="plugins_explainer_body">Un plugin n'est pas un process - c'est un bundle (manifest JSON + fichiers) qui peut <em>contenir</em> des MCPs, skills, commands, hooks. Pour relancer un MCP fourni par un plugin, retrouve-le dans la tab <strong>Serveurs MCP</strong> avec ses boutons Stop/Démarrer/Redémarrer dédiés. La case à cocher ici contrôle uniquement si le plugin est <em>enabled</em> dans <code>~/.claude/settings.json</code>.</p>
</div>
<input id="plugins-search" type="search" oninput="filterPlugins()" data-i18n-placeholder="plugins_search_placeholder" placeholder="Rechercher un plugin..." class="w-full mb-3 p-2 border border-stone-200 rounded-lg text-sm focus:outline-none focus:border-stone-400"/>
<div id="plugins" class="space-y-2 max-h-[700px] overflow-y-auto"></div>
</section>
</div>
<div id="repair-skill-modal" class="hidden fixed inset-0 modal-bg flex items-center justify-center z-50">
<div class="card p-6 w-[640px] max-w-[92vw] max-h-[90vh] overflow-y-auto">
<h3 class="text-lg font-semibold mb-1"><span data-i18n="repair_skill_title">Reparer le skill</span> <span id="repair-skill-name" class="font-mono text-stone-700"></span></h3>
<p class="text-xs text-stone-500 mb-4" data-i18n="repair_skill_help">Une description claire est ce qui permet a Claude de declencher ton skill au bon moment. Sans description, le skill ne sera jamais utilise automatiquement.</p>
<div class="mb-3">
<label class="block text-xs font-semibold uppercase tracking-wide text-stone-600 mb-1" data-i18n="repair_skill_desc_label">Description</label>
<textarea id="repair-skill-desc" class="w-full p-2 border border-stone-300 rounded-lg text-sm font-sans" rows="3" data-i18n-placeholder="repair_skill_desc_placeholder" placeholder="Decris quand Claude doit utiliser ce skill (40-150 chars)"></textarea>
<div class="flex justify-between items-center mt-1 flex-wrap gap-2">
<span id="repair-skill-desc-count" class="text-[10px] text-stone-400 font-mono">0</span>
<div class="flex items-center gap-2">
<div class="inline-flex rounded border border-stone-200 overflow-hidden text-[10px]" title="Langue de la description generee">
<button type="button" id="repair-skill-lang-fr" onclick="setRepairSuggestLang('fr')" class="px-2 py-0.5 text-stone-700 hover:bg-stone-100">FR</button>
<button type="button" id="repair-skill-lang-en" onclick="setRepairSuggestLang('en')" class="px-2 py-0.5 text-stone-700 hover:bg-stone-100 border-l border-stone-200">EN</button>
</div>
<button id="repair-skill-suggest-btn" onclick="suggestSkillDescription()" class="text-xs font-medium text-amber-800 bg-amber-50 hover:bg-amber-100 border border-amber-200 rounded px-2 py-1"><span data-i18n="repair_skill_suggest_btn">Suggerer via Claude Code</span></button>
</div>
</div>
<div id="repair-skill-suggest-meta" class="text-[10px] text-stone-500 mt-1 hidden"></div>
</div>
<details class="mb-3">
<summary class="text-xs font-semibold text-stone-600 cursor-pointer mb-1" data-i18n="repair_skill_preview_label">Apercu actuel du SKILL.md</summary>
<pre id="repair-skill-preview" class="text-[11px] bg-stone-50 border border-stone-200 rounded p-2 max-h-48 overflow-auto whitespace-pre-wrap font-mono mt-2"></pre>
</details>
<div class="flex gap-2 justify-end">
<button onclick="closeRepairSkill()" class="px-4 py-2 text-sm rounded-lg border border-stone-200 hover:bg-stone-50" data-i18n="btn_cancel">Annuler</button>
<button id="repair-skill-save-btn" onclick="saveRepairSkill()" class="px-4 py-2 text-sm rounded-lg bg-stone-900 hover:bg-stone-800 text-white font-medium" data-i18n="btn_save">Sauvegarder</button>
</div>
</div>
</div>
<div id="add-plugin-modal" class="hidden fixed inset-0 modal-bg flex items-center justify-center z-50">
<div class="card p-6 w-[480px] max-w-[92vw]">
<h3 class="text-lg font-semibold mb-1" data-i18n="plugin_add_modal_title">Ajouter un plugin via Git</h3>
<p class="text-xs text-stone-500 mb-4" data-i18n="plugin_add_modal_help">Le repo sera cloné dans ~/.claude/plugins/cache/manual/, plugin.json sera lu pour le nom et la version, puis enregistré comme &lt;name&gt;@manual et activé.</p>
<input id="add-plugin-url" type="text" class="w-full p-3 border border-stone-200 rounded-lg text-sm mb-3" placeholder="https://github.com/.../mon-plugin.git" autocomplete="off"/>
<div class="flex gap-2 justify-end">
<button onclick="closeAddPlugin()" class="px-4 py-2 text-sm rounded-lg border border-stone-200 hover:bg-stone-50" data-i18n="btn_cancel">Annuler</button>
<button onclick="confirmAddPlugin()" class="px-4 py-2 text-sm rounded-lg bg-stone-900 hover:bg-stone-800 text-white font-medium" data-i18n="btn_clone_install">Cloner et installer</button>
</div>
</div>
</div>
<div data-main-tab="commands" class="hidden">
<section class="card p-6">
<h2 class="text-lg font-semibold mb-1" data-i18n="commands">Commands</h2>
<p class="text-xs text-stone-500 mb-4" data-i18n="commands_help">Commands utilisateur (~/.claude/commands/) et fournies par les plugins actifs</p>
<div id="commands" class="space-y-2 max-h-[700px] overflow-y-auto"></div>
</section>
</div>
<div data-main-tab="watchdog" class="hidden">
<section class="card p-6 mb-6">
<h2 class="text-lg font-semibold mb-1" data-i18n="watchdog_tab_title">Watchdog</h2>
<p class="text-xs text-stone-500 mb-4" data-i18n="watchdog_tab_help">Surveillance Claude Desktop, MCPs et Desktop Commander. Tous les declenchements sont logges dans le journal en bas.</p>
<div class="mb-4 p-3 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-800"><strong>&#9888;</strong> <span data-i18n="watchdog_dc_explainer">Le mode auto-remediation DC tente d'abord un toggle settings (sans kill PID), puis te demande l'autorisation de redemarrer Claude Desktop si necessaire. Il ne kille jamais directement de process et ne s'active que si DC est silencieux ET Claude Desktop reste responsive (freeze isole).</span></div>
<div id="watchdog-tab-config" class="space-y-3"></div>
<div class="mt-6 pt-4 border-t border-stone-200">
<div class="flex items-baseline justify-between mb-2 gap-3 flex-wrap">
<div>
<h3 class="text-sm font-semibold text-stone-700" data-i18n="watchdog_nuclear_title">Option nucleaire</h3>
<p class="text-xs text-stone-500 mt-0.5" data-i18n="watchdog_nuclear_help">Redemarre Claude Desktop entier. Ferme toutes les conversations en cours. Garantie de debloquer n'importe quelle extension figee.</p>
</div>
<button data-restart-cd-btn onclick="restartClaudeDesktop()" class="text-xs font-medium text-red-700 bg-red-50 hover:bg-red-100 border border-red-200 rounded-md px-3 py-2 shrink-0"><span class="mr-1">&#128260;</span><span data-i18n="watchdog_nuclear_btn">Redemarrer Claude Desktop</span></button>
</div>
</div>
</section>
<section class="card p-6">
<h3 class="text-base font-semibold mb-3" data-i18n="watchdog_events_title">Journal d'evenements</h3>
<div id="watchdog-tab-events" class="space-y-1 max-h-[500px] overflow-y-auto text-xs font-mono"></div>
</section>
</div>
<div data-main-tab="advanced" class="hidden">
<div class="mb-4 p-3 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-800"><strong>&#9888; <span data-i18n="advanced_warning_title">Section avancée</span></strong> &middot; <span data-i18n="advanced_warning_text">éditer ces fichiers peut casser Claude Code. Backups horodatés créés à chaque sauvegarde.</span></div>
<section class="card p-6">
<div class="flex items-baseline justify-between mb-1">
<h2 class="text-lg font-semibold"><span class="font-mono">CLAUDE.md</span></h2>
<span class="text-xs text-stone-400" data-i18n="claudemd_meta">Lu à chaque session Claude Code</span>
</div>
<p class="text-xs text-stone-500 mb-3" data-i18n="claudemd_help">Préférences globales injectées dans chaque conversation Claude Code (~/.claude/CLAUDE.md)</p>
<textarea id="claudemd-textarea" class="w-full p-3 border border-stone-200 rounded-lg font-mono text-xs h-64 focus:outline-none focus:border-stone-400" spellcheck="false" data-i18n-placeholder="claudemd_placeholder" placeholder="# Mes préférences globales pour Claude Code..."></textarea>
<div class="flex items-center justify-between mt-2">
<span class="text-xs text-stone-400"><span id="claudemd-count">0</span> <span data-i18n="characters">caractères</span></span>
<button onclick="saveClaudeMd()" class="px-4 py-2 text-sm rounded-lg bg-stone-900 hover:bg-stone-800 text-white font-medium" data-i18n="btn_save">Sauvegarder</button>
</div>
</section>
<section class="card p-6 mt-6">
<div class="flex items-baseline justify-between mb-1">
<h2 class="text-lg font-semibold"><span class="font-mono">settings.json</span></h2>
<span class="text-xs text-stone-400" data-i18n="settings_meta">Configuration globale Claude Code (~/.claude/settings.json)</span>
</div>
<p class="text-xs text-stone-500 mb-3" data-i18n="settings_help">Le JSON est validé avant sauvegarde, et un backup horodaté est créé.</p>
<textarea id="settings-textarea" class="w-full p-3 border border-stone-200 rounded-lg font-mono text-xs h-72 focus:outline-none focus:border-stone-400" spellcheck="false" oninput="validateSettingsLive()"></textarea>
<div class="flex items-center justify-between mt-2">
<span id="settings-status" class="text-xs"></span>
<button onclick="saveSettings()" class="px-4 py-2 text-sm rounded-lg bg-stone-900 hover:bg-stone-800 text-white font-medium" data-i18n="btn_save">Sauvegarder</button>
</div>
</section>
<div class="grid grid-cols-1 md:grid-cols-2 gap-6 mt-6">
<section class="card p-6">
<h2 class="text-lg font-semibold mb-1" data-i18n="add_mcp">+ Ajouter un MCP</h2>
<p class="text-xs text-stone-500 mb-4" data-i18n="add_mcp_help">JSON, fichier local ou repo Git</p>
<div class="flex gap-1 mb-4 bg-stone-100 p-1 rounded-lg">
<button class="tab-btn active flex-1 px-2 py-1.5 text-xs rounded-md font-medium" data-tab="mcp-json" onclick="setTab('mcp','json')">JSON</button>
<button class="tab-btn flex-1 px-2 py-1.5 text-xs rounded-md font-medium" data-tab="mcp-file" onclick="setTab('mcp','file')" data-i18n="tab_file">Fichier</button>
<button class="tab-btn flex-1 px-2 py-1.5 text-xs rounded-md font-medium" data-tab="mcp-zip" onclick="setTab('mcp','zip')">ZIP</button>
<button class="tab-btn flex-1 px-2 py-1.5 text-xs rounded-md font-medium" data-tab="mcp-git" onclick="setTab('mcp','git')">Git</button>
</div>
<div data-pane="mcp-json"><textarea id="mcp-json-in" class="w-full p-3 border border-stone-200 rounded-lg font-mono text-xs h-32 focus:outline-none focus:border-stone-400" placeholder='{"my-mcp": {"command": "node", "args": ["/path/server.js"]}}'></textarea>
<button onclick="addMcpJson()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium" data-i18n="btn_add">Ajouter</button></div>
<div data-pane="mcp-file" class="hidden"><input id="mcp-file-in" type="text" class="w-full p-3 border border-stone-200 rounded-lg text-sm" placeholder="/Users/.../config.json"/>
<p class="text-xs text-stone-500 mt-1" data-i18n="mcp_file_help">Chemin absolu d'un .json contenant mcpServers</p>
<button onclick="addMcpFile()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium" data-i18n="btn_import">Importer</button></div>
<div data-pane="mcp-git" class="hidden"><input id="mcp-git-in" type="text" class="w-full p-3 border border-stone-200 rounded-lg text-sm" placeholder="https://github.com/.../mcp.git"/>
<p class="text-xs text-stone-500 mt-1" data-i18n="mcp_git_help">Sera cloné dans ~/.claude/imported-mcps/</p>
<button onclick="addMcpGit()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium" data-i18n="btn_clone_import">Cloner et importer</button></div>
<div data-pane="mcp-zip" class="hidden"><input id="mcp-zip-in" type="file" accept=".zip,application/zip" class="w-full text-xs file:mr-3 file:py-2 file:px-3 file:rounded-md file:border-0 file:text-xs file:font-medium file:bg-stone-100 file:text-stone-700 hover:file:bg-stone-200"/>
<p class="text-xs text-stone-500 mt-1" data-i18n="mcp_zip_help">ZIP contenant un repo MCP. Le nom du fichier devient le nom du dossier extrait.</p>
<button onclick="addMcpZip()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium" data-i18n="btn_import_zip">Importer le ZIP</button></div>
</section>
<section class="card p-6">
<h2 class="text-lg font-semibold mb-1" data-i18n="add_skill">+ Ajouter un Skill</h2>
<p class="text-xs text-stone-500 mb-4" data-i18n="add_skill_help">Dossier local, repo Git, ou markdown</p>
<div class="flex gap-1 mb-4 bg-stone-100 p-1 rounded-lg">
<button class="tab-btn active flex-1 px-2 py-1.5 text-xs rounded-md font-medium" data-tab="sk-folder" onclick="setTab('sk','folder')" data-i18n="tab_folder">Dossier</button>
<button class="tab-btn flex-1 px-2 py-1.5 text-xs rounded-md font-medium" data-tab="sk-zip" onclick="setTab('sk','zip')">ZIP</button>
<button class="tab-btn flex-1 px-2 py-1.5 text-xs rounded-md font-medium" data-tab="sk-git" onclick="setTab('sk','git')">Git</button>
<button class="tab-btn flex-1 px-2 py-1.5 text-xs rounded-md font-medium" data-tab="sk-md" onclick="setTab('sk','md')">Markdown</button>
</div>
<div data-pane="sk-folder"><input id="sk-folder-in" type="text" class="w-full p-3 border border-stone-200 rounded-lg text-sm" placeholder="/Users/.../mon-skill/"/>
<p class="text-xs text-stone-500 mt-1" data-i18n="sk_folder_help">Le dossier doit contenir SKILL.md</p>
<button onclick="addSkillFolder()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium" data-i18n="btn_import">Importer</button></div>
<div data-pane="sk-git" class="hidden"><input id="sk-git-in" type="text" class="w-full p-3 border border-stone-200 rounded-lg text-sm" placeholder="https://github.com/.../skill.git"/>
<p class="text-xs text-stone-500 mt-1" data-i18n="sk_git_help">Le repo doit contenir SKILL.md</p>
<button onclick="addSkillGit()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium" data-i18n="btn_clone_import">Cloner et importer</button></div>
<div data-pane="sk-zip" class="hidden"><input id="sk-zip-in" type="file" accept=".zip,application/zip" class="w-full text-xs file:mr-3 file:py-2 file:px-3 file:rounded-md file:border-0 file:text-xs file:font-medium file:bg-stone-100 file:text-stone-700 hover:file:bg-stone-200"/>
<p class="text-xs text-stone-500 mt-1" data-i18n="sk_zip_help">ZIP doit contenir SKILL.md (à la racine ou dans un sous-dossier).</p>
<button onclick="addSkillZip()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium" data-i18n="btn_import_zip">Importer le ZIP</button></div>
<div data-pane="sk-md" class="hidden"><input id="sk-md-name" type="text" class="w-full p-2 mb-2 border border-stone-200 rounded-lg text-sm" data-i18n-placeholder="sk_md_name_placeholder" placeholder="nom-du-skill"/>
<textarea id="sk-md-content" class="w-full p-3 border border-stone-200 rounded-lg font-mono text-xs h-24" placeholder="---&#10;name: mon-skill&#10;description: ...&#10;---"></textarea>
<button onclick="addSkillMd()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium" data-i18n="btn_create">Créer</button></div>
</section>
</div>
</div>
<p class="text-xs text-stone-400 mt-8 text-center" data-i18n="footer_apply">Après modifications, clique sur « Redémarrer Claude » pour appliquer.</p>
</div>
<div id="mcp-error-modal" class="hidden fixed inset-0 modal-bg flex items-center justify-center z-50">
<div class="card p-6 w-[680px] max-w-[92vw] max-h-[85vh] overflow-y-auto">
<div class="flex items-baseline justify-between mb-3">
<h3 class="text-lg font-semibold"><span data-i18n="mcp_label">MCP</span> <span id="mcp-err-name" class="font-mono"></span> <span data-i18n="mcp_err_suffix">ne démarre pas</span></h3>
<button onclick="closeMcpError()" class="text-stone-400 hover:text-stone-700 text-xl leading-none px-2">&times;</button>
</div>
<div id="mcp-err-known-wrap" class="hidden mb-4 p-3 rounded-lg" style="background:linear-gradient(135deg,#fef3e2,#fde7d3);border:1px solid #f5c084">
<div class="text-xs font-semibold uppercase tracking-wide text-amber-800 mb-1" data-i18n="mcp_err_known">&#9888; Cause probable</div>
<div id="mcp-err-known" class="text-sm text-stone-800 leading-relaxed"></div>
</div>
<div id="mcp-err-unknown-wrap" class="hidden mb-4 p-3 rounded-lg bg-stone-50 border border-stone-200">
<div class="text-xs font-semibold uppercase tracking-wide text-stone-600 mb-1" data-i18n="mcp_err_unknown">Pas de cause spécifique détectée</div>
<div class="text-sm text-stone-700 leading-relaxed" data-i18n="mcp_err_unknown_text">Le log ci-dessous est l'extrait le plus récent. Pour voir l'erreur réelle : clique « Tester ce MCP maintenant » ci-dessous — l'app fait un vrai handshake JSON-RPC.</div>
</div>
<div id="mcp-err-nolog-wrap" class="hidden mb-4 p-3 rounded-lg bg-stone-50 border border-stone-200">
<div class="text-xs font-semibold uppercase tracking-wide text-stone-600 mb-1" data-i18n="mcp_err_nolog">Aucun log</div>
<div class="text-sm text-stone-700 leading-relaxed" data-i18n="mcp_err_nolog_text">Aucun log d'erreur trouvé dans ~/Library/Logs/Claude/. Redémarre Claude Desktop pour déclencher un démarrage du MCP — son log apparaîtra ici si ça échoue.</div>
</div>
<div id="mcp-err-log-section" class="hidden">
<div class="text-xs font-semibold uppercase tracking-wide text-stone-600 mb-1" data-i18n="mcp_err_log_excerpt">Extrait du log</div>
<pre id="mcp-err-content" class="text-xs bg-stone-900 text-stone-100 rounded-lg p-3 overflow-x-auto whitespace-pre-wrap font-mono max-h-72"></pre>
<p class="text-xs text-stone-400 mt-2"><span data-i18n="file_label">Fichier</span> : <span id="mcp-err-log-path" class="font-mono"></span></p>
</div>
<div id="mcp-err-test-section" class="hidden mt-4">
<div class="text-xs font-semibold uppercase tracking-wide text-stone-600 mb-1" data-i18n="mcp_err_test_result">Résultat du test live</div>
<div id="mcp-err-test-summary" class="text-sm mb-2 p-2 rounded-lg"></div>
<details class="text-xs"><summary class="cursor-pointer text-stone-600 hover:text-stone-900 select-none" data-i18n="mcp_err_captures">stdout / stderr captures</summary>
<div class="mt-2"><div class="text-stone-400 text-xs">stderr</div>
<pre id="mcp-err-test-stderr" class="text-xs bg-stone-900 text-stone-100 rounded-lg p-3 overflow-x-auto whitespace-pre-wrap font-mono max-h-48 mt-1"></pre>
<div class="text-stone-400 text-xs mt-2">stdout</div>
<pre id="mcp-err-test-stdout" class="text-xs bg-stone-900 text-stone-100 rounded-lg p-3 overflow-x-auto whitespace-pre-wrap font-mono max-h-48 mt-1"></pre>
</div></details>
</div>
<div id="mcp-err-envfix" class="hidden mt-4 p-3 rounded-lg" style="background:linear-gradient(135deg,#eaf4ec,#d8eadd);border:1px solid #87b89b">
<div class="text-xs font-semibold uppercase tracking-wide text-green-800 mb-1" data-i18n="envfix_title">&#9989; Fix automatique disponible</div>
<div class="text-sm text-stone-800 mb-2"><span data-i18n="envfix_lead_pre">Variable d'environnement</span> <span id="mcp-envfix-var" class="font-mono font-semibold"></span> <span data-i18n="envfix_lead_post">manquante. Renseigne sa valeur ci-dessous, je l'ajouterai à la config Claude Desktop (avec backup).</span></div>
<input id="mcp-envfix-value" type="text" class="w-full p-2 border border-stone-300 rounded-md text-sm mb-2 font-mono" data-i18n-placeholder="envfix_placeholder" placeholder="Valeur de la variable..." autocomplete="off"/>
<button onclick="applyEnvFix()" class="px-4 py-2 text-sm rounded-lg bg-green-700 hover:bg-green-800 text-white font-medium" data-i18n="envfix_save_retest">Enregistrer et re-tester</button>
</div>
<div class="flex gap-2 justify-between mt-4 items-center">
<button id="mcp-err-test-btn" onclick="runMcpTest()" class="px-4 py-2 text-sm rounded-lg bg-amber-600 hover:bg-amber-700 text-white font-medium" data-i18n="btn_test_now">Tester ce MCP maintenant</button>
<div class="flex gap-2">
<button onclick="restartClaude()" class="px-4 py-2 text-sm rounded-lg bg-stone-100 hover:bg-stone-200 text-stone-700 font-medium" data-i18n="restart_claude">Redémarrer Claude</button>
<button onclick="closeMcpError()" class="px-4 py-2 text-sm rounded-lg border border-stone-200 hover:bg-stone-50" data-i18n="btn_close">Fermer</button>
</div>
</div>
</div>
</div>
<div id="cmd-edit-modal" class="hidden fixed inset-0 modal-bg flex items-center justify-center z-50">
<div class="card p-6 w-[720px] max-w-[92vw] max-h-[85vh] overflow-y-auto">
<div class="flex items-baseline justify-between mb-3">
<h3 class="text-lg font-semibold"><span id="cmd-edit-name" class="font-mono"></span></h3>
<button onclick="closeCmdEdit()" class="text-stone-400 hover:text-stone-700 text-xl leading-none px-2">&times;</button>
</div>
<p class="text-xs text-stone-500 mb-3"><span data-i18n="source_label">Source</span> : <span id="cmd-edit-source" class="font-mono"></span></p>
<input id="cmd-edit-name-input" type="hidden" />
<textarea id="cmd-edit-content" class="w-full p-3 border border-stone-200 rounded-lg font-mono text-xs h-72 focus:outline-none focus:border-stone-400" spellcheck="false"></textarea>
<div class="flex gap-2 justify-end mt-3">
<button onclick="closeCmdEdit()" class="px-4 py-2 text-sm rounded-lg border border-stone-200 hover:bg-stone-50" data-i18n="btn_close">Fermer</button>
<button id="cmd-edit-save" onclick="saveCommand()" class="px-4 py-2 text-sm rounded-lg bg-stone-900 hover:bg-stone-800 text-white font-medium" data-i18n="btn_save">Sauvegarder</button>
</div>
</div>
</div>
<div id="preset-modal" class="hidden fixed inset-0 modal-bg flex items-center justify-center z-50">
<div class="card p-6 w-96 max-w-[90vw]">
<h3 class="text-lg font-semibold mb-1" data-i18n="preset_modal_title">Sauvegarder un preset</h3>
<p class="text-xs text-stone-500 mb-4" data-i18n="preset_modal_help">Capture les MCPs actuellement actifs sous un nom.</p>
<input id="preset-name-in" type="text" class="w-full p-3 border border-stone-200 rounded-lg text-sm mb-2" data-i18n-placeholder="preset_name_placeholder" placeholder="Ex: Klide, Audit client..." autocomplete="off"/>
<p id="preset-modal-info" class="text-xs text-stone-500 mb-4"></p>
<div class="flex gap-2 justify-end">
<button onclick="closeSavePreset()" class="px-4 py-2 text-sm rounded-lg border border-stone-200 hover:bg-stone-50" data-i18n="btn_cancel">Annuler</button>
<button onclick="confirmSavePreset()" class="px-4 py-2 text-sm rounded-lg bg-stone-900 hover:bg-stone-800 text-white font-medium" data-i18n="btn_save">Sauvegarder</button>
</div>
</div>
</div>
<script>
const T = {
fr: {
  header_subtitle: "Contrôle de Claude Desktop",
  update_label: "Mise à jour disponible",
  update_label_with_v: "Mise à jour disponible : v",
  restart_self_title: "Redémarrer le serveur Claude Control",
  restart_app_short: "App",
  restart_claude: "Redémarrer Claude",
  mcp_section: "Serveurs MCP",
  mcp_help: "Coché = chargé au démarrage de Claude Desktop",
  presets: "PRESETS",
  preset_save_current: "+ Sauvegarder l'actuel",
  preset_empty: "Aucun preset. Active des MCPs puis sauvegarde.",
  skills: "Skills",
  skills_help: "Coché = disponible pour Claude",
  skills_search_placeholder: "Rechercher un skill (nom ou description)...",
  no_skill: "Aucun skill",
  plugins: "Plugins",
  plugins_meta: "Lecture seule · toggle persistant dans settings.json",
  plugins_help: "Plugins Claude Code installés via marketplace",
  plugins_explainer_title: "Pas de Start/Stop/Restart sur les plugins",
  plugins_explainer_body: "Un plugin n'est pas un process - c'est un bundle (manifest JSON + fichiers) qui peut <em>contenir</em> des MCPs, skills, commands, hooks. Pour relancer un MCP fourni par un plugin, retrouve-le dans la tab <strong>Serveurs MCP</strong> avec ses boutons Stop/Démarrer/Redémarrer dédiés. La case à cocher ici contrôle uniquement si le plugin est <em>enabled</em> dans <code>~/.claude/settings.json</code>.",
  plugins_empty: "Aucun plugin installé.",
  add_mcp: "+ Ajouter un MCP",
  add_mcp_help: "JSON, fichier local ou repo Git",
  add_skill: "+ Ajouter un Skill",
  add_skill_help: "Dossier local, repo Git, ou markdown",
  tab_file: "Fichier",
  tab_folder: "Dossier",
  btn_add: "Ajouter",
  btn_import: "Importer",
  btn_import_zip: "Importer le ZIP",
  btn_clone_import: "Cloner et importer",
  btn_create: "Créer",
  btn_close: "Fermer",
  btn_cancel: "Annuler",
  btn_save: "Sauvegarder",
  mcp_file_help: "Chemin absolu d'un .json contenant mcpServers",
  mcp_git_help: "Sera cloné dans ~/.claude/imported-mcps/",
  mcp_zip_help: "ZIP contenant un repo MCP. Le nom du fichier devient le nom du dossier extrait.",
  sk_folder_help: "Le dossier doit contenir SKILL.md",
  sk_git_help: "Le repo doit contenir SKILL.md",
  sk_zip_help: "ZIP doit contenir SKILL.md (à la racine ou dans un sous-dossier).",
  sk_md_name_placeholder: "nom-du-skill",
  footer_apply: "Après modifications, clique sur « Redémarrer Claude » pour appliquer.",
  no_mcp: "Aucun MCP",
  running_label: "running",
  not_started_label: "pas démarré · pourquoi ?",
  why_title: "Voir l'erreur",
  file_label: "Fichier",
  mcp_label: "MCP",
  mcp_err_suffix: "ne démarre pas",
  mcp_err_known: "⚠ Cause probable",
  mcp_err_unknown: "Pas de cause spécifique détectée",
  mcp_err_unknown_text: "Le log ci-dessous est l'extrait le plus récent. Pour voir l'erreur réelle : clique « Tester ce MCP maintenant » ci-dessous — l'app fait un vrai handshake JSON-RPC.",
  mcp_err_nolog: "Aucun log",
  mcp_err_nolog_text: "Aucun log d'erreur trouvé dans ~/Library/Logs/Claude/. Redémarre Claude Desktop pour déclencher un démarrage du MCP — son log apparaîtra ici si ça échoue.",
  mcp_err_log_excerpt: "Extrait du log",
  mcp_err_test_result: "Résultat du test live",
  mcp_err_captures: "stdout / stderr captures",
  btn_test_now: "Tester ce MCP maintenant",
  btn_testing: "Test en cours (5s max)...",
  btn_retest: "Re-tester",
  envfix_title: "✅ Fix automatique disponible",
  envfix_lead_pre: "Variable d'environnement",
  envfix_lead_post: "manquante. Renseigne sa valeur ci-dessous, je l'ajouterai à la config Claude Desktop (avec backup).",
  envfix_placeholder: "Valeur de la variable...",
  envfix_save_retest: "Enregistrer et re-tester",
  preset_modal_title: "Sauvegarder un preset",
  preset_modal_help: "Capture les MCPs actuellement actifs sous un nom.",
  preset_name_placeholder: "Ex : Klide, Audit client...",
  confirm_restart_claude: "Redémarrer Claude Desktop ?",
  confirm_restart_self: "Redémarrer le serveur Claude Control ?",
  confirm_apply_update: "Mettre à jour Claude Control ? L'app va se relancer toute seule.",
  banner_restarting: "Redémarrage...",
  banner_reconnecting: "Reconnexion...",
  banner_updating: "Mise à jour...",
  banner_uploading: "Upload du ZIP...",
  banner_cloning: "Clonage...",
  banner_test_failed: "Échec du test",
  banner_value_required: "Valeur requise",
  banner_name_required: "Nom requis",
  banner_zip_required: "Choisis un fichier ZIP",
  banner_upload_failed: "Échec upload : ",
  net_error: "Erreur réseau : ",
  no_active_mcp: "Aucun MCP actif à sauvegarder.",
  active_mcps_label: "MCPs actifs",
  badge_handshake_ok: "✅ Serveur MCP sain (handshake OK)",
  badge_handshake_error: "⚠ Handshake en erreur",
  badge_no_response: "⚠ Pas de réponse au handshake",
  badge_exit_zero: "❌ Quitté avec code 0 sans réponse",
  badge_exit_code: "❌ Quitté avec code ",
  badge_unknown: "Inconnu",
  plugin_install_path_missing: "install path manquant",
  plugin_empty: "vide",
  plugin_no_content: "Aucun contenu détecté.",
  empty_capture: "(vide)",
  commands: "Commands",
  commands_help: "Commands utilisateur (~/.claude/commands/) et fournies par les plugins actifs",
  commands_empty: "Aucune command. Crée-en une avec « + » ou installe un plugin qui en fournit.",
  source_label: "Source",
  source_user: "Utilisateur",
  source_plugin: "Plugin",
  toggle_command_title: "Activer / désactiver",
  btn_edit: "Modifier",
  btn_view: "Voir",
  readonly: "lecture seule",
  plugin_readonly_tooltip: "Skill fourni par un plugin Claude Code, géré par son marketplace. Read-only depuis Claude Control. Pour modifier, passe par le plugin source ou crée un skill perso du même nom.",
  plugin_no_description_hint: "Pas de description (skill plugin, non éditable depuis ici).",
  btn_cleanup_orphan_cache: "Nettoyer cache v{v}",
  command_not_found: "Command introuvable",
  claudemd_meta: "Lu à chaque session Claude Code",
  claudemd_help: "Préférences globales injectées dans chaque conversation Claude Code (~/.claude/CLAUDE.md)",
  claudemd_placeholder: "# Mes préférences globales pour Claude Code...",
  characters: "caractères",
  settings_meta: "Configuration globale Claude Code (~/.claude/settings.json)",
  settings_help: "Le JSON est validé avant sauvegarde, et un backup horodaté est créé.",
  json_valid: "JSON valide",
  json_invalid: "JSON invalide",
  overview: "Vue d'ensemble",
  active_preset: "Preset actif",
  presets_label: "preset(s)",
  stat_mcps_running: "MCPs running",
  stat_skills: "Skills prêts",
  stat_plugins: "Plugins actifs",
  stat_commands: "Commands user + plugin",
  issue_mcps_not_running: "MCP(s) actifs mais non démarrés",
  issue_orphans: "version(s) orpheline(s) de plugin",
  issue_duplicates: "doublon(s) skill / plugin",
  issue_skill_no_frontmatter: "skill(s) sans frontmatter",
  health_all_good: "Tout est en ordre.",
  btn_delete: "Supprimer",
  btn_clone_install: "Cloner et installer",
  banner_url_required: "URL requise",
  plugin_add_btn: "+ Ajouter un plugin (Git)",
  plugin_add_modal_title: "Ajouter un plugin via Git",
  plugin_add_modal_help: "Le repo sera cloné dans ~/.claude/plugins/cache/manual/, plugin.json sera lu pour le nom et la version, puis enregistré comme <name>@manual et activé.",
  confirm_delete_skill: "Supprimer le skill « {name} » ?\\n\\n• Le dossier ~/.claude/skills/{name}/ va être effacé\\n• Un backup ZIP est créé dans ~/.claude/backups/claude-control/ — tu peux toujours restaurer\\n• Si tu veux juste désactiver sans effacer, décoche la case à la place",
  confirm_delete_mcp: "Supprimer le MCP « {name} » de ta config ?\\n\\n• L'entrée disparaît de claude_desktop_config.json\\n• Le binaire / package reste sur ton disque\\n• Si tu veux juste désactiver, décoche la case à la place",
  confirm_delete_plugin: "Supprimer le plugin « {name} » ET ses fichiers cache ?\\n\\n• L'entrée disparaît de installed_plugins.json\\n• Le dossier ~/.claude/plugins/cache/.../ est zippé (backup) puis effacé\\n• Annule ici si tu veux garder les fichiers (autre étape ensuite)",
  confirm_delete_plugin_metadata_only: "Supprimer seulement l'enregistrement du plugin « {name} » ?\\n\\n• Le plugin disparaît de installed_plugins.json\\n• Les fichiers cache restent intacts sur ton disque\\n• Tu peux le réenregistrer plus tard sans re-cloner",
  source_user_skills: "Skills utilisateur",
  general_category: "Général",
  auto_cat_hint: "(catégorie auto)",
  top_skills_30d: "Top skills (30 derniers jours)",
  sessions: "sessions",
  files: "fichiers",
  no_skill_usage_yet: "Aucune activation de skill détectée dans tes logs ({n} fichier(s) scanné(s)). Une fois que Claude Code commence à utiliser tes skills, le classement apparaîtra ici.",
  used_x_times: "Utilisé {n} fois ces 30 derniers jours",
  skill_suggestions: "Suggestions d'optimisation",
  fallback_no_usage: "heuristiques uniquement (pas de données d'usage)",
  cleanup_duplicates_btn: "Supprimer les versions utilisateur en doublon",
  confirm_cleanup_duplicates: "Supprimer toutes les versions utilisateur en doublon avec un plugin ?\n\nUn backup zip individuel est cree avant chaque suppression dans ~/.claude/backups/claude-control/. Les versions plugin restent en place et deviennent la source de verite.\n\nCONFIRMER ?",
  banner_cleanup_running: "Nettoyage des doublons en cours...",
  watchdog_label: "Surveillance",
  watchdog_active: "Active &middot; vérification toutes les {n}s",
  watchdog_inactive: "Désactivée",
  watchdog_enable: "Surveiller Claude Desktop",
  watchdog_crash: "Redémarrer si crash",
  watchdog_freeze: "Détecter freeze + redémarrer",
  tab_watchdog: "Watchdog",
  watchdog_tab_title: "Watchdog",
  watchdog_tab_help: "Surveillance Claude Desktop, MCPs et Desktop Commander. Tous les declenchements sont logges dans le journal en bas.",
  watchdog_dc_explainer: "Le mode auto-remediation DC tente d'abord un toggle settings (sans kill PID), puis te demande l'autorisation de redemarrer Claude Desktop si necessaire. Il ne kille jamais directement de process et ne s'active que si DC est silencieux ET Claude Desktop reste responsive (freeze isole).",
  watchdog_nuclear_title: "Option nucleaire",
  watchdog_nuclear_help: "Redemarre Claude Desktop entier. Ferme toutes les conversations en cours. Garantie de debloquer n'importe quelle extension figee.",
  watchdog_nuclear_btn: "Redemarrer Claude Desktop",
  confirm_restart_claude_desktop: "Redemarrer Claude Desktop ?\n\nToutes tes conversations en cours seront fermees. Tous les MCPs et extensions seront reinitialises. Claude Desktop reapparaitra dans 5-10 secondes.\n\nCONFIRMER ?",
  watchdog_dc_auto_label: "Auto-remediation Desktop Commander",
  watchdog_dc_auto_help: "Detecte un freeze isole de DC, tente un toggle settings, puis propose un restart Claude Desktop avec consentement.",
  watchdog_dc_threshold_label: "Seuil d'inactivite log (s)",
  watchdog_dc_verify_label: "Verification post-toggle (s)",
  watchdog_dc_cooldown_label: "Cooldown apres dismiss (s)",
  watchdog_events_title: "Journal d'evenements",
  watchdog_no_events: "Aucun evenement enregistre.",
  watchdog_target_label: "Cible",
  watchdog_custom_target: "Pattern personnalisé",
  watchdog_pattern_placeholder: "ex : desktop-commander, /chemin/vers/binaire, package-name",
  btn_scan: "Scanner",
  scanning: "Scan en cours...",
  scan_no_match: "Aucun process ne contient « {p} » dans son ligne de commande.",
  scan_n_matches: "{n} process trouvé(s) qui contiennent « {p} » :",
  banner_pattern_too_short: "Pattern trop court (>= 2 caractères)",
  ext_badge: "ext",
  mcp_conflict_title: "conflit(s) MCP detecte(s)",
  mcp_conflict_help: "Une Desktop Extension et une entree dans claude_desktop_config.json declarent le meme MCP. Resultat : 2 instances tournent en parallele (cas observe : 16 Helper Nodes au lieu de 8). Recommandation : retirer l'entree manuelle, l'extension reste source de verite (geree via marketplace).",
  mcp_conflict_keep: "À garder (extension)",
  mcp_conflict_remove: "À retirer (config manuelle)",
  mcp_conflict_resolve_btn: "Retirer la config manuelle",
  mcp_conflict_match_name_title: "Match par nom normalise (insensible casse + tirets/espaces)",
  mcp_conflict_match_package_title: "Match par package npm partage (signal fort)",
  mcp_conflict_match_both_title: "Match par nom ET package npm (certitude maximale)",
  confirm_resolve_mcp_conflict: "Retirer l'entree '{classic}' de claude_desktop_config.json ?\\n\\nL'extension '{ext}' reste en place et continue de fournir le MCP. Un backup horodate du config est cree avant la modification.\\n\\nCe nettoyage evite les doublons (2 instances en parallele = concurrence + duplicate tool calls = risque de freeze UI).\\n\\nCONFIRMER ?",
  btn_restart_mcp: "Redémarrer ce MCP (sans toucher à Claude)",
  btn_restart_mcp_short: "Redémarrer",
  btn_stop_mcp_short: "Stopper",
  btn_stop_mcp_title: "Arrêter le process maintenant sans toucher à la config (le checkbox 'actif au prochain démarrage' reste tel quel)",
  btn_start_mcp_short: "Démarrer",
  btn_start_mcp_title: "Démarrer le process maintenant à chaud (toggle config off→on, Claude Desktop respawn via FSEvents, sans redémarrage CD)",
  banner_starting: "Démarrage en cours...",
  btn_delete_mcp_title: "Supprimer ce MCP / cette extension (popup de confirmation)",
  mcp_checkbox_title: "Coché = chargé au prochain démarrage de Claude Desktop. Décoché ne stoppe pas immédiatement le process en cours, utilise Stopper pour ça.",
  confirm_stop_mcp: "Stopper le MCP « {name} » à chaud ?\\n\\n• Le process en cours est tué immédiatement\\n• La config reste intacte (le checkbox ne change pas)\\n• Au prochain redémarrage de Claude Desktop, il sera relancé automatiquement\\n• Pour désactiver durablement, décoche la case en plus\\n\\nCONFIRMER ?",
  confirm_delete_extension: "Supprimer l'extension « {name} » ?\\n\\n• Le dossier d'install est zippé en backup puis supprimé\\n• Le fichier de settings est sauvegardé puis supprimé\\n• L'entrée disparaît de extensions-installations.json\\n\\nNote : si Claude Desktop la ré-installe automatiquement (cas des extensions Anthropic-managed comme PowerPoint, Word, Control Mac), désinstalle-la via Settings → Extensions de Claude Desktop.\\n\\nCONFIRMER ?",
  mcp_col_running: "En cours d'execution",
  mcp_col_stopped: "Inactifs",
  mcp_col_running_empty: "Aucun MCP en cours d'execution",
  mcp_col_stopped_empty: "Tous les MCPs tournent",
  skill_filter_mine: "Mes skills",
  skill_filter_plugins: "Plugins",
  skill_filter_all: "Tous",
  skill_filter_all_cats: "Toutes les catégories",
  category_filter: "Catégorie",
  skills_health_title: "Santé de tes skills",
  skills_health_ready: "prêts à se déclencher",
  skills_health_all_good: "Tous tes skills sont en bonne forme",
  chip_broken_action: "à corriger (sans description)",
  chip_enrich_action: "à enrichir (description courte)",
  chip_duplicate_action: "doublons à nettoyer",
  filter_status: "État",
  filter_status_active: "Actifs",
  filter_status_inactive: "Inactifs",
  filter_quality: "Qualité",
  quality_excellent: "Excellents",
  quality_enrich: "À enrichir",
  quality_broken: "Cassés",
  quality_broken_hint: "Sans description : ne se déclenchera jamais automatiquement.",
  btn_repair_skill: "Reparer",
  repair_skill_title: "Reparer le skill",
  repair_skill_help: "Une description claire est ce qui permet a Claude de declencher ton skill au bon moment. Sans description, le skill ne sera jamais utilise automatiquement.",
  repair_skill_desc_label: "Description",
  repair_skill_desc_placeholder: "Decris quand Claude doit utiliser ce skill (40-150 chars)",
  repair_skill_desc_required: "Description requise",
  repair_skill_suggest_btn: "Suggerer via Claude Code",
  repair_skill_suggesting: "Generation...",
  repair_skill_suggested_via: "Suggere via Claude Code CLI (envoye {n} chars)",
  repair_skill_no_cli: "Claude Code CLI introuvable. Installer avec : npm install -g @anthropic-ai/claude-code",
  skills_cli_banner_title: "Pour generer des descriptions automatiquement",
  skills_cli_banner_body: "Installe le CLI Claude Code pour utiliser le bouton « Suggerer via Claude Code » dans la modal de reparation. Pas de cle API requise, le CLI utilise ton abonnement Claude Code existant.",
  repair_skill_diag_btn: "Diagnostiquer le CLI Claude Code",
  repair_skill_diag_title: "Diagnostic CLI Claude Code",
  repair_skill_not_logged_in_title: "Connexion Claude Code requise",
  repair_skill_not_logged_in_body: "Lance cette commande dans un terminal pour t'authentifier (une seule fois) :",
  btn_copy: "Copier",
  btn_open_terminal: "Ouvrir Terminal",
  copied: "Commande copiee",
  repair_skill_login_pending_hint: "Une fois 'Login successful' affiche dans Terminal, clique ci-dessous pour generer la description :",
  repair_skill_retry_after_login: "Je suis loggé, génère la description",
  repair_skill_preview_label: "Apercu actuel du SKILL.md",
  loading: "Chargement...",
  saving: "Sauvegarde...",
  filter_usage: "Usage (30j)",
  filter_usage_top: "Top 10",
  filter_usage_recent: "Utilisés",
  filter_usage_never: "Jamais utilisés",
  filter_source: "Source",
  filter_all: "Tous",
  toggle_skill_title: "Activer / désactiver le skill",
  select_for_bulk_title: "Sélectionner pour action groupée",
  selected_count: "sélectionné(s)",
  bulk_disable_btn: "Désactiver",
  bulk_delete_btn: "Supprimer",
  confirm_bulk_disable: "Désactiver les {n} skills sélectionnés ?\\n\\nIls passent dans skills-disabled. Toujours réactivables un par un ensuite.\\n\\nCONFIRMER ?",
  confirm_bulk_delete: "Supprimer définitivement les {n} skills sélectionnés ?\\n\\nUn backup zip individuel par skill est créé dans ~/.claude/backups/claude-control/.\\n\\nCONFIRMER ?",
  skills_empty_with_filters: "Aucun skill ne correspond aux filtres actifs.",
  btn_reset_filters: "Réinitialiser les filtres",
  source_badge_user: "perso",
  source_badge_plugin: "plugin",
  confirm_restart_mcp: "Redémarrer le MCP « {name} » ?\\n\\nLe process sera tué puis Claude Desktop le respawn automatiquement (toggle config). Tes conversations Claude restent intactes.",
  claude_running: "Claude tourne",
  claude_stopped: "Claude arrêté",
  tab_overview: "Vue d'ensemble",
  tab_advanced: "Avancé",
  plugins_search_placeholder: "Rechercher un plugin...",
  advanced_warning_title: "Section avancée",
  advanced_warning_text: "éditer ces fichiers peut casser Claude Code. Backups horodatés créés à chaque sauvegarde.",
  mcp_diag_title: "Diagnostics extensions",
  mcp_diag_help: "Mismatches manifest ↔ log ↔ process pour les Desktop Extensions",
  mcp_diag_loading: "Chargement...",
  mcp_diag_all_ok: "Toutes les extensions sont correctement matchées (manifest, log et process).",
  mcp_diag_with_warnings: "{n} extension(s) sur {t} avec avertissements",
  mcp_diag_col_name: "Nom",
  mcp_diag_col_status: "Statut",
  mcp_diag_col_warnings: "Avertissements",
  mcp_diag_status_ok: "OK",
  mcp_diag_status_warn: "À vérifier",
  mcp_diag_status_off: "Désactivée",
  mcp_diag_warn_enabled_but_no_log: "Activée mais aucun fichier log trouvé",
  mcp_diag_warn_log_inactive_5min: "Log inactif depuis plus de 5 min",
  mcp_diag_warn_log_active_no_pid: "Log actif mais aucun PID détecté",
  mcp_diag_warn_display_name_missing: "manifest.display_name absent",
  mcp_diag_warn_manifest_missing: "manifest absent dans extensions-installations.json",
  mcp_diag_warn_version_mismatch: "Version manifest != version racine",
},
en: {
  header_subtitle: "Claude Desktop control",
  update_label: "Update available",
  update_label_with_v: "Update available: v",
  restart_self_title: "Restart the Claude Control server",
  restart_app_short: "App",
  restart_claude: "Restart Claude",
  mcp_section: "MCP servers",
  mcp_help: "Checked = loaded on Claude Desktop start",
  presets: "PRESETS",
  preset_save_current: "+ Save current",
  preset_empty: "No preset. Activate MCPs then save.",
  skills: "Skills",
  skills_help: "Checked = available to Claude",
  skills_search_placeholder: "Search a skill (name or description)...",
  no_skill: "No skill",
  plugins: "Plugins",
  plugins_meta: "Read-only · toggle persistent in settings.json",
  plugins_help: "Claude Code plugins installed via marketplace",
  plugins_explainer_title: "No Start/Stop/Restart on plugins",
  plugins_explainer_body: "A plugin is not a process - it's a bundle (JSON manifest + files) that can <em>contain</em> MCPs, skills, commands, hooks. To restart an MCP provided by a plugin, find it in the <strong>MCP servers</strong> tab with its dedicated Stop/Start/Restart buttons. The checkbox here only controls whether the plugin is <em>enabled</em> in <code>~/.claude/settings.json</code>.",
  plugins_empty: "No plugin installed.",
  add_mcp: "+ Add an MCP",
  add_mcp_help: "JSON, local file or Git repo",
  add_skill: "+ Add a Skill",
  add_skill_help: "Local folder, Git repo, or markdown",
  tab_file: "File",
  tab_folder: "Folder",
  btn_add: "Add",
  btn_import: "Import",
  btn_import_zip: "Import ZIP",
  btn_clone_import: "Clone and import",
  btn_create: "Create",
  btn_close: "Close",
  btn_cancel: "Cancel",
  btn_save: "Save",
  mcp_file_help: "Absolute path to a .json containing mcpServers",
  mcp_git_help: "Will be cloned into ~/.claude/imported-mcps/",
  mcp_zip_help: "ZIP containing an MCP repo. The filename becomes the extracted folder name.",
  sk_folder_help: "Folder must contain SKILL.md",
  sk_git_help: "Repo must contain SKILL.md",
  sk_zip_help: "ZIP must contain SKILL.md (at the root or in a subfolder).",
  sk_md_name_placeholder: "skill-name",
  footer_apply: 'After changes, click "Restart Claude" to apply.',
  no_mcp: "No MCP",
  running_label: "running",
  not_started_label: "not running · why?",
  why_title: "View the error",
  file_label: "File",
  mcp_label: "MCP",
  mcp_err_suffix: "is not running",
  mcp_err_known: "⚠ Probable cause",
  mcp_err_unknown: "No specific cause detected",
  mcp_err_unknown_text: 'The log excerpt below is the most recent. To see the real error: click "Test this MCP now" below — the app runs a real JSON-RPC handshake.',
  mcp_err_nolog: "No log",
  mcp_err_nolog_text: "No error log found in ~/Library/Logs/Claude/. Restart Claude Desktop to trigger an MCP startup — its log will appear here if it fails.",
  mcp_err_log_excerpt: "Log excerpt",
  mcp_err_test_result: "Live test result",
  mcp_err_captures: "stdout / stderr captures",
  btn_test_now: "Test this MCP now",
  btn_testing: "Testing (5s max)...",
  btn_retest: "Re-test",
  envfix_title: "✅ Auto-fix available",
  envfix_lead_pre: "Environment variable",
  envfix_lead_post: "missing. Enter its value below — I'll add it to the Claude Desktop config (with backup).",
  envfix_placeholder: "Variable value...",
  envfix_save_retest: "Save and re-test",
  preset_modal_title: "Save a preset",
  preset_modal_help: "Capture currently active MCPs under a name.",
  preset_name_placeholder: "E.g.: Klide, Client audit...",
  confirm_restart_claude: "Restart Claude Desktop?",
  confirm_restart_self: "Restart the Claude Control server?",
  confirm_apply_update: "Update Claude Control? The app will reload itself.",
  banner_restarting: "Restarting...",
  banner_reconnecting: "Reconnecting...",
  banner_updating: "Updating...",
  banner_uploading: "Uploading ZIP...",
  banner_cloning: "Cloning...",
  banner_test_failed: "Test failed",
  banner_value_required: "Value required",
  banner_name_required: "Name required",
  banner_zip_required: "Pick a ZIP file",
  banner_upload_failed: "Upload failed: ",
  net_error: "Network error: ",
  no_active_mcp: "No active MCP to save.",
  active_mcps_label: "Active MCPs",
  badge_handshake_ok: "✅ Healthy MCP server (handshake OK)",
  badge_handshake_error: "⚠ Handshake error",
  badge_no_response: "⚠ No response to handshake",
  badge_exit_zero: "❌ Exited with code 0 without response",
  badge_exit_code: "❌ Exited with code ",
  badge_unknown: "Unknown",
  plugin_install_path_missing: "install path missing",
  plugin_empty: "empty",
  plugin_no_content: "No content detected.",
  empty_capture: "(empty)",
  commands: "Commands",
  commands_help: "User commands (~/.claude/commands/) and those provided by active plugins",
  commands_empty: 'No command. Create one with "+" or install a plugin that ships them.',
  source_label: "Source",
  source_user: "User",
  source_plugin: "Plugin",
  toggle_command_title: "Enable / disable",
  btn_edit: "Edit",
  btn_view: "View",
  readonly: "read-only",
  plugin_readonly_tooltip: "Skill provided by a Claude Code plugin, managed by its marketplace. Read-only from Claude Control. To modify, go through the plugin source or create a personal skill with the same name.",
  plugin_no_description_hint: "No description (plugin skill, not editable from here).",
  btn_cleanup_orphan_cache: "Clean cache v{v}",
  command_not_found: "Command not found",
  claudemd_meta: "Read at each Claude Code session",
  claudemd_help: "Global preferences injected into every Claude Code conversation (~/.claude/CLAUDE.md)",
  claudemd_placeholder: "# My global preferences for Claude Code...",
  characters: "characters",
  settings_meta: "Global Claude Code config (~/.claude/settings.json)",
  settings_help: "JSON is validated before saving, and a timestamped backup is created.",
  json_valid: "Valid JSON",
  json_invalid: "Invalid JSON",
  overview: "Overview",
  active_preset: "Active preset",
  presets_label: "preset(s)",
  stat_mcps_running: "MCPs running",
  stat_skills: "Skills ready",
  stat_plugins: "Active plugins",
  stat_commands: "Commands user + plugin",
  issue_mcps_not_running: "active MCP(s) not running",
  issue_orphans: "orphan plugin version(s)",
  issue_duplicates: "skill / plugin duplicate(s)",
  issue_skill_no_frontmatter: "skill(s) without frontmatter",
  health_all_good: "All clear.",
  btn_delete: "Delete",
  btn_clone_install: "Clone and install",
  banner_url_required: "URL required",
  plugin_add_btn: "+ Add plugin (Git)",
  plugin_add_modal_title: "Add a plugin via Git",
  plugin_add_modal_help: "The repo will be cloned into ~/.claude/plugins/cache/manual/, plugin.json will be read for name and version, then registered as <name>@manual and enabled.",
  confirm_delete_skill: 'Delete skill "{name}"?\\n\\n• The folder ~/.claude/skills/{name}/ will be removed\\n• A ZIP backup is created in ~/.claude/backups/claude-control/ — you can restore it\\n• To just disable it without removing, uncheck the box instead',
  confirm_delete_mcp: 'Delete MCP "{name}" from your config?\\n\\n• The entry is removed from claude_desktop_config.json\\n• The binary / package stays on your disk\\n• To just disable it, uncheck the box instead',
  confirm_delete_plugin: 'Delete plugin "{name}" AND its cache files?\\n\\n• The entry is removed from installed_plugins.json\\n• The folder under ~/.claude/plugins/cache/.../ is zipped (backup) and removed\\n• Cancel here to keep the files (next step asks)',
  confirm_delete_plugin_metadata_only: 'Delete only the plugin "{name}" entry?\\n\\n• The plugin disappears from installed_plugins.json\\n• Cache files stay on your disk\\n• You can re-register it later without re-cloning',
  source_user_skills: "User skills",
  general_category: "General",
  auto_cat_hint: "(auto-category)",
  top_skills_30d: "Top skills (last 30 days)",
  sessions: "sessions",
  files: "files",
  no_skill_usage_yet: "No skill activations detected in your logs ({n} file(s) scanned). Once Claude Code starts using your skills, the ranking will appear here.",
  used_x_times: "Used {n} times in the last 30 days",
  skill_suggestions: "Optimization suggestions",
  fallback_no_usage: "heuristics only (no usage data)",
  cleanup_duplicates_btn: "Delete duplicate user versions",
  confirm_cleanup_duplicates: "Delete all user-side skill versions duplicated by a plugin?\n\nAn individual zip backup is created before each deletion under ~/.claude/backups/claude-control/. Plugin versions remain and become the source of truth.\n\nCONFIRM?",
  banner_cleanup_running: "Cleaning up duplicates...",
  watchdog_label: "Watchdog",
  watchdog_active: "On &middot; checking every {n}s",
  watchdog_inactive: "Off",
  watchdog_enable: "Watch Claude Desktop",
  watchdog_crash: "Restart on crash",
  watchdog_freeze: "Detect freeze + restart",
  tab_watchdog: "Watchdog",
  watchdog_tab_title: "Watchdog",
  watchdog_tab_help: "Monitors Claude Desktop, MCPs and Desktop Commander. Every trigger is logged in the journal below.",
  watchdog_dc_explainer: "DC auto-remediation first tries a settings toggle (no PID kill), then asks for your consent to restart Claude Desktop if needed. It never kills processes directly and only activates when DC is silent AND Claude Desktop remains responsive (isolated freeze).",
  watchdog_nuclear_title: "Nuclear option",
  watchdog_nuclear_help: "Restarts the whole Claude Desktop. Closes all in-progress conversations. Guaranteed to unfreeze any frozen extension.",
  watchdog_nuclear_btn: "Restart Claude Desktop",
  confirm_restart_claude_desktop: "Restart Claude Desktop?\n\nAll in-progress conversations will be closed. All MCPs and extensions will be reset. Claude Desktop will reappear in 5-10 seconds.\n\nCONFIRM?",
  watchdog_dc_auto_label: "Desktop Commander auto-remediation",
  watchdog_dc_auto_help: "Detects an isolated DC freeze, tries a settings toggle, then prompts for Claude Desktop restart with consent.",
  watchdog_dc_threshold_label: "Log inactivity threshold (s)",
  watchdog_dc_verify_label: "Post-toggle verification (s)",
  watchdog_dc_cooldown_label: "Cooldown after dismiss (s)",
  watchdog_events_title: "Event journal",
  watchdog_no_events: "No event recorded.",
  watchdog_target_label: "Target",
  watchdog_custom_target: "Custom pattern",
  watchdog_pattern_placeholder: "e.g.: desktop-commander, /path/to/binary, package-name",
  btn_scan: "Scan",
  scanning: "Scanning...",
  scan_no_match: 'No process matches "{p}" in its command line.',
  scan_n_matches: '{n} process(es) match "{p}":',
  banner_pattern_too_short: "Pattern too short (>= 2 characters)",
  ext_badge: "ext",
  mcp_conflict_title: "MCP conflict(s) detected",
  mcp_conflict_help: "A Desktop Extension and a claude_desktop_config.json entry declare the same MCP. Result: 2 instances run in parallel (observed: 16 Helper Nodes instead of 8). Recommendation: remove the manual entry; the extension remains source of truth (managed via marketplace).",
  mcp_conflict_keep: "Keep (extension)",
  mcp_conflict_remove: "Remove (manual config)",
  mcp_conflict_resolve_btn: "Remove manual config",
  mcp_conflict_match_name_title: "Match by normalized name (case + dashes/spaces insensitive)",
  mcp_conflict_match_package_title: "Match by shared npm package (strong signal)",
  mcp_conflict_match_both_title: "Match by name AND npm package (maximum certainty)",
  confirm_resolve_mcp_conflict: "Remove '{classic}' from claude_desktop_config.json?\\n\\nExtension '{ext}' stays in place and keeps providing the MCP. A timestamped config backup is created before the change.\\n\\nThis cleanup avoids duplicates (2 parallel instances = concurrency + duplicate tool calls = UI freeze risk).\\n\\nCONFIRM?",
  btn_restart_mcp: "Restart this MCP (without touching Claude)",
  btn_restart_mcp_short: "Restart",
  btn_stop_mcp_short: "Stop",
  btn_stop_mcp_title: "Stop the process now without touching the config (the 'active at next start' checkbox stays the same)",
  btn_start_mcp_short: "Start",
  btn_start_mcp_title: "Start the process now hot (config toggle off→on, Claude Desktop respawns via FSEvents, no CD restart)",
  banner_starting: "Starting...",
  btn_delete_mcp_title: "Delete this MCP / extension (confirmation popup)",
  mcp_checkbox_title: "Checked = loaded at next Claude Desktop start. Unchecking does NOT immediately stop the running process; use Stop for that.",
  confirm_stop_mcp: 'Stop MCP "{name}" hot?\\n\\n• The running process is killed immediately\\n• The config stays intact (checkbox unchanged)\\n• At next Claude Desktop start, it will be respawned automatically\\n• To disable persistently, uncheck the box too\\n\\nCONFIRM?',
  confirm_delete_extension: 'Delete extension "{name}"?\\n\\n• The install dir is zip-backed up then deleted\\n• The settings file is backed up then deleted\\n• The entry is removed from extensions-installations.json\\n\\nNote: if Claude Desktop re-installs it automatically (case of Anthropic-managed extensions like PowerPoint, Word, Control Mac), uninstall it via Settings → Extensions in Claude Desktop.\\n\\nCONFIRM?',
  mcp_col_running: "Running",
  mcp_col_stopped: "Stopped",
  mcp_col_running_empty: "No MCP currently running",
  mcp_col_stopped_empty: "All MCPs are running",
  skill_filter_mine: "My skills",
  skill_filter_plugins: "Plugins",
  skill_filter_all: "All",
  skill_filter_all_cats: "All categories",
  category_filter: "Category",
  skills_health_title: "Skills health",
  skills_health_ready: "ready to trigger",
  skills_health_all_good: "All your skills are in good shape",
  chip_broken_action: "to fix (no description)",
  chip_enrich_action: "to enrich (short description)",
  chip_duplicate_action: "duplicates to clean",
  filter_status: "Status",
  filter_status_active: "Active",
  filter_status_inactive: "Inactive",
  filter_quality: "Quality",
  quality_excellent: "Excellent",
  quality_enrich: "To enrich",
  quality_broken: "Broken",
  quality_broken_hint: "No description: will never auto-trigger.",
  btn_repair_skill: "Repair",
  repair_skill_title: "Repair skill",
  repair_skill_help: "A clear description is what lets Claude trigger your skill at the right moment. Without one, the skill never auto-triggers.",
  repair_skill_desc_label: "Description",
  repair_skill_desc_placeholder: "Describe when Claude should use this skill (40-150 chars)",
  repair_skill_desc_required: "Description required",
  repair_skill_suggest_btn: "Suggest via Claude Code",
  repair_skill_suggesting: "Generating...",
  repair_skill_suggested_via: "Suggested via Claude Code CLI (sent {n} chars)",
  repair_skill_no_cli: "Claude Code CLI not found. Install with: npm install -g @anthropic-ai/claude-code",
  skills_cli_banner_title: "To generate descriptions automatically",
  skills_cli_banner_body: "Install the Claude Code CLI to use the 'Suggest via Claude Code' button in the repair modal. No API key required, the CLI uses your existing Claude Code subscription.",
  repair_skill_diag_btn: "Diagnose Claude Code CLI",
  repair_skill_diag_title: "Claude Code CLI diagnostic",
  repair_skill_not_logged_in_title: "Claude Code login required",
  repair_skill_not_logged_in_body: "Run this command in a terminal to authenticate (one time only):",
  btn_copy: "Copy",
  btn_open_terminal: "Open Terminal",
  copied: "Command copied",
  repair_skill_login_pending_hint: "Once 'Login successful' shows in Terminal, click below to generate the description:",
  repair_skill_retry_after_login: "I'm logged in, generate the description",
  repair_skill_preview_label: "Current SKILL.md preview",
  loading: "Loading...",
  saving: "Saving...",
  filter_usage: "Usage (30d)",
  filter_usage_top: "Top 10",
  filter_usage_recent: "Used",
  filter_usage_never: "Never used",
  filter_source: "Source",
  filter_all: "All",
  toggle_skill_title: "Enable / disable the skill",
  select_for_bulk_title: "Select for bulk action",
  selected_count: "selected",
  bulk_disable_btn: "Disable",
  bulk_delete_btn: "Delete",
  confirm_bulk_disable: "Disable the {n} selected skills?\\n\\nThey move to skills-disabled. Re-enable any one of them later.\\n\\nCONFIRM?",
  confirm_bulk_delete: "Permanently delete the {n} selected skills?\\n\\nAn individual zip backup per skill is created under ~/.claude/backups/claude-control/.\\n\\nCONFIRM?",
  skills_empty_with_filters: "No skill matches the active filters.",
  btn_reset_filters: "Reset filters",
  source_badge_user: "yours",
  source_badge_plugin: "plugin",
  confirm_restart_mcp: 'Restart MCP "{name}"?\\n\\nThe process will be killed and Claude Desktop will respawn it automatically (config toggle). Your Claude conversations stay intact.',
  claude_running: "Claude running",
  claude_stopped: "Claude stopped",
  tab_overview: "Overview",
  tab_advanced: "Advanced",
  plugins_search_placeholder: "Search a plugin...",
  advanced_warning_title: "Advanced section",
  advanced_warning_text: "editing these files can break Claude Code. Timestamped backups are created on every save.",
  mcp_diag_title: "Extension diagnostics",
  mcp_diag_help: "manifest ↔ log ↔ process mismatches for Desktop Extensions",
  mcp_diag_loading: "Loading...",
  mcp_diag_all_ok: "All extensions are matched correctly (manifest, log and process).",
  mcp_diag_with_warnings: "{n} extension(s) out of {t} with warnings",
  mcp_diag_col_name: "Name",
  mcp_diag_col_status: "Status",
  mcp_diag_col_warnings: "Warnings",
  mcp_diag_status_ok: "OK",
  mcp_diag_status_warn: "Check",
  mcp_diag_status_off: "Disabled",
  mcp_diag_warn_enabled_but_no_log: "Enabled but no log file found",
  mcp_diag_warn_log_inactive_5min: "Log inactive for over 5 min",
  mcp_diag_warn_log_active_no_pid: "Log active but no PID detected",
  mcp_diag_warn_display_name_missing: "manifest.display_name missing",
  mcp_diag_warn_manifest_missing: "manifest missing in extensions-installations.json",
  mcp_diag_warn_version_mismatch: "Manifest version != root version",
},
};
let CURRENT_LANG = (localStorage.getItem('cc-lang') || 'fr');
function tr(key, params){
  let s = (T[CURRENT_LANG] && T[CURRENT_LANG][key]) || (T.fr && T.fr[key]) || key;
  if(params){for(const k in params){s = s.split('{'+k+'}').join(params[k]);}}
  return s;
}
function applyLang(lang){
  if(lang !== 'fr' && lang !== 'en') lang = 'fr';
  CURRENT_LANG = lang;
  localStorage.setItem('cc-lang', lang);
  document.documentElement.lang = lang;
  document.querySelectorAll('[data-i18n]').forEach(el=>{const k=el.getAttribute('data-i18n'); if(T[lang][k]!==undefined) el.textContent = T[lang][k];});
  // v1.9.1 - data-i18n-html : pour les strings qui contiennent du HTML inline
  // (ex. <strong>, <em>, <code> dans les explainers). Utilise innerHTML au
  // lieu de textContent. A reserver aux strings de notre code (pas user input).
  document.querySelectorAll('[data-i18n-html]').forEach(el=>{const k=el.getAttribute('data-i18n-html'); if(T[lang][k]!==undefined) el.innerHTML = T[lang][k];});
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el=>{const k=el.getAttribute('data-i18n-placeholder'); if(T[lang][k]!==undefined) el.placeholder = T[lang][k];});
  document.querySelectorAll('[data-i18n-title]').forEach(el=>{const k=el.getAttribute('data-i18n-title'); if(T[lang][k]!==undefined) el.title = T[lang][k];});
  ['fr','en'].forEach(l=>{const b=document.getElementById('lang-'+l); if(b){b.classList.toggle('active', l===lang);}});
  if(typeof loadState==='function') loadState();
  if(typeof loadPlugins==='function') loadPlugins();
  if(typeof loadPresets==='function') loadPresets();
  if(typeof loadCommands==='function') loadCommands();
  if(typeof loadOverview==='function') loadOverview();
}
function setLang(lang){applyLang(lang);}
let CURRENT_MAIN_TAB = (localStorage.getItem('cc-main-tab') || 'overview');
function setMainTab(tab){
  if(!tab) tab = 'overview';
  CURRENT_MAIN_TAB = tab;
  localStorage.setItem('cc-main-tab', tab);
  document.querySelectorAll('[data-main-tab]').forEach(el=>{
    if(el.tagName === 'BUTTON'){
      const active = el.getAttribute('data-main-tab') === tab;
      el.classList.toggle('bg-white', active);
      el.classList.toggle('shadow-sm', active);
      el.classList.toggle('text-stone-900', active);
      el.classList.toggle('text-stone-600', !active);
    } else {
      el.classList.toggle('hidden', el.getAttribute('data-main-tab') !== tab);
    }
  });
  // Trigger any tab-specific reload that needs fresh data
  if(tab === 'commands' && typeof loadCommands === 'function') loadCommands();
  if(tab === 'advanced' && typeof loadClaudeMd === 'function'){loadClaudeMd();loadSettings();}
}
function filterPlugins(){
  const q = (document.getElementById('plugins-search')?.value || '').trim().toLowerCase();
  document.querySelectorAll('#plugins [data-plugin-name]').forEach(el=>{
    const n = (el.getAttribute('data-plugin-name')||'').toLowerCase();
    el.classList.toggle('hidden', q && !n.includes(q));
  });
}
let CURRENT_STATE = {mcps:[], skills:[]};
async function loadState(){
  const s = await (await fetch('/api/state')).json();
  CURRENT_STATE = s;
  // v1.7.1 - rendu en 2 colonnes : running a gauche, inactifs a droite.
  // Chaque ligne expose un bouton "Redemarrer" texte+icone (visible sans hover)
  // qui appelle restart_mcp/restart_extension cote backend (kill PID + toggle
  // settings -> Claude Desktop respawn via FSEvents, sans le redemarrer).
  // v1.7.6 - 3 actions explicites par MCP : Stop (kill PID a chaud sans
  // toucher au config), Redemarrer (kill + toggle config), Supprimer
  // (avec confirm popup detaille). La checkbox a une nouvelle semantique
  // verbalisee dans son title : 'Coche = charge au prochain demarrage de
  // Claude Desktop' (persistance config), distinct du Stop a chaud.
  function _renderMcpRow(m){
    const isExt = m.type === 'extension';
    const extBadge = isExt ? `<span class="text-[10px] font-mono text-amber-800 bg-amber-50 border border-amber-200 px-1.5 py-0.5 rounded" title="Desktop Extension">${tr('ext_badge')}</span>` : '';
    const versionBadge = isExt && m.version ? `<span class="text-[10px] text-stone-400 font-mono">v${escAttr(m.version)}</span>` : '';
    const toggleFn = isExt ? `toggleExtension('${m.name}', this.checked)` : `toggleMcp('${m.name}')`;
    // v1.7.9 - 'pas demarre · pourquoi ?' compacte en simple '?' amber
    // (le pill complet faisait wrap sur 3 lignes en layout 2-colonnes).
    const whyBtn = (m.active && !m.running && !isExt)
      ? `<button type="button" onclick="event.preventDefault();event.stopPropagation();showMcpError('${m.name}')" class="text-[11px] font-bold text-amber-800 bg-amber-50 hover:bg-amber-100 border border-amber-200 rounded-full w-5 h-5 inline-flex items-center justify-center shrink-0" title="${tr('not_started_label')} - ${tr('why_title')}">?</button>`
      : '';
    // v1.7.9 - boutons icon-only (avec title tooltip) : libere ~200px par ligne
    // pour que les noms ne soient plus tronques a 1-2 lettres en layout
    // 2-colonnes. Sémantique des icônes universelle (▶ ⏹ ↻ 🗑) + tooltip clair.
    const iconBtnCls = (theme) => `inline-flex items-center justify-center w-7 h-7 text-sm rounded-md border shrink-0 ${theme}`;
    let runCtlBtn = '';
    if (m.running){
      runCtlBtn = `<button type="button" onclick="event.preventDefault();event.stopPropagation();stopMcp('${m.name}')" title="${tr('btn_stop_mcp_short')} - ${tr('btn_stop_mcp_title')}" class="${iconBtnCls('text-stone-700 bg-stone-50 border-stone-200 hover:bg-stone-100')}" aria-label="${tr('btn_stop_mcp_short')}">&#9209;</button>`;
    } else if (m.active){
      runCtlBtn = `<button type="button" onclick="event.preventDefault();event.stopPropagation();startMcp('${m.name}')" title="${tr('btn_start_mcp_short')} - ${tr('btn_start_mcp_title')}" class="${iconBtnCls('text-green-800 bg-green-50 border-green-200 hover:bg-green-100')}" aria-label="${tr('btn_start_mcp_short')}">&#9654;</button>`;
    }
    const restartBtn = `<button type="button" onclick="event.preventDefault();event.stopPropagation();restartMcp('${m.name}')" title="${tr('btn_restart_mcp_short')} - ${tr('btn_restart_mcp')}" class="${iconBtnCls('text-amber-800 bg-amber-50 border-amber-200 hover:bg-amber-100')}" aria-label="${tr('btn_restart_mcp_short')}">&#x21bb;</button>`;
    const deleteFn = isExt ? `deleteExtension('${m.name}')` : `deleteMcp('${m.name}')`;
    const deleteBtn = `<button type="button" onclick="event.preventDefault();event.stopPropagation();${deleteFn}" title="${tr('btn_delete')} - ${tr('btn_delete_mcp_title')}" class="${iconBtnCls('text-red-700 bg-red-50 border-red-200 hover:bg-red-100')}" aria-label="${tr('btn_delete')}">&#128465;</button>`;
    const checkboxTitle = tr('mcp_checkbox_title');
    return `<label class="flex items-center justify-between gap-3 p-3 rounded-lg hover:bg-stone-50 cursor-pointer border ${m.active?'border-stone-200':'border-stone-100 opacity-60'}"><div class="flex items-center gap-2 flex-1 min-w-0"><input type="checkbox" ${m.active?'checked':''} onchange="${toggleFn}" title="${checkboxTitle}" class="w-5 h-5 rounded accent-green-700 shrink-0"><span class="font-medium truncate">${m.name}</span>${extBadge}${versionBadge}${whyBtn}</div><div class="flex items-center gap-1.5 shrink-0">${runCtlBtn}${restartBtn}${deleteBtn}</div></label>`;
  }
  const running = s.mcps.filter(m=>m.running);
  const stopped = s.mcps.filter(m=>!m.running);
  const elRun = document.getElementById('mcps-running');
  const elStop = document.getElementById('mcps-stopped');
  const elRunCnt = document.getElementById('mcps-running-count');
  const elStopCnt = document.getElementById('mcps-stopped-count');
  if(elRun){
    elRun.innerHTML = running.length === 0
      ? `<p class="text-stone-400 text-sm italic px-3">${tr('mcp_col_running_empty')}</p>`
      : running.map(_renderMcpRow).join('');
  }
  if(elStop){
    elStop.innerHTML = stopped.length === 0
      ? `<p class="text-stone-400 text-sm italic px-3">${tr('mcp_col_stopped_empty')}</p>`
      : stopped.map(_renderMcpRow).join('');
  }
  if(elRunCnt) elRunCnt.textContent = String(running.length);
  if(elStopCnt) elStopCnt.textContent = String(stopped.length);
  document.getElementById('skills').innerHTML = renderSkills(s.skills);
  filterSkills();
  loadMcpConflicts();
}

// v1.8.1 - bandeau de conflits MCP en haut de l'onglet Serveurs MCP.
// Detecte les doublons entre Desktop Extensions et entrees classic dans
// claude_desktop_config.json (apprentissage 2026-05-06 : DC tournait en
// double, 16 Helper Nodes au lieu de 8). Action : retirer l'entree
// classic, l'extension reste source de verite.
async function loadMcpConflicts(){
  const banner = document.getElementById('mcp-conflicts-banner');
  if(!banner) return;
  try {
    const r = await fetch('/api/mcp-conflicts');
    if(!r.ok){ banner.classList.add('hidden'); return; }
    const d = await r.json();
    const conflicts = d.conflicts || [];
    if(conflicts.length === 0){ banner.classList.add('hidden'); banner.innerHTML = ''; return; }
    banner.classList.remove('hidden');
    const rows = conflicts.map(c => {
      const matchBadge = {
        both: `<span class="text-[10px] bg-red-100 text-red-700 px-1.5 py-0.5 rounded font-mono" title="${tr('mcp_conflict_match_both_title')}">match: name + package</span>`,
        package: `<span class="text-[10px] bg-amber-100 text-amber-800 px-1.5 py-0.5 rounded font-mono" title="${tr('mcp_conflict_match_package_title')}">match: package</span>`,
        name: `<span class="text-[10px] bg-stone-100 text-stone-700 px-1.5 py-0.5 rounded font-mono" title="${tr('mcp_conflict_match_name_title')}">match: name</span>`,
      }[c.match_type] || '';
      const pkgs = (c.matched_packages && c.matched_packages.length)
        ? `<div class="text-[11px] text-stone-500 font-mono mt-0.5">${escAttr(c.matched_packages.join(', '))}</div>`
        : '';
      return `<div class="p-3 bg-white rounded border border-red-200">
        <div class="flex items-baseline justify-between flex-wrap gap-2 mb-2">
          <div class="font-semibold text-sm">${escAttr(c.extension_name)} <span class="text-stone-400 font-normal">↔</span> <span class="font-mono">${escAttr(c.classic_name)}</span> ${matchBadge}</div>
          <button onclick="resolveMcpConflict('${escAttr(c.classic_name)}', '${escAttr(c.extension_name)}')" class="text-xs font-medium text-white bg-red-700 hover:bg-red-800 rounded-md px-3 py-1.5">${tr('mcp_conflict_resolve_btn')}</button>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-2 text-xs">
          <div class="p-2 bg-green-50 rounded border border-green-100">
            <div class="text-[10px] uppercase tracking-wide text-green-700 font-semibold mb-1">${tr('mcp_conflict_keep')}</div>
            <div class="font-medium">${escAttr(c.extension_name)} <span class="text-[10px] bg-amber-100 text-amber-800 px-1 rounded">${tr('ext_badge')}</span></div>
            <div class="text-[10px] text-stone-500 font-mono break-all">${escAttr(c.extension_id)}</div>
          </div>
          <div class="p-2 bg-red-50 rounded border border-red-100">
            <div class="text-[10px] uppercase tracking-wide text-red-700 font-semibold mb-1">${tr('mcp_conflict_remove')}</div>
            <div class="font-medium font-mono">${escAttr(c.classic_name)}</div>
            <div class="text-[10px] text-stone-500 font-mono break-all">${escAttr(c.classic_command)} ${escAttr((c.classic_args || []).join(' '))}</div>
            ${pkgs}
          </div>
        </div>
      </div>`;
    }).join('');
    banner.innerHTML = `<div class="card p-4 border-l-4 border-red-500 bg-red-50/30">
      <div class="flex items-center gap-2 mb-2">
        <span class="text-lg">&#9888;</span>
        <h3 class="text-base font-semibold text-red-800">${conflicts.length} ${tr('mcp_conflict_title')}</h3>
      </div>
      <p class="text-xs text-stone-600 mb-3">${tr('mcp_conflict_help')}</p>
      <div class="space-y-2">${rows}</div>
    </div>`;
  } catch(e){ console.error(e); }
}

async function resolveMcpConflict(classicName, extName){
  const msg = tr('confirm_resolve_mcp_conflict')
    .split('{classic}').join(classicName)
    .split('{ext}').join(extName);
  if(!confirm(msg)) return;
  const j = await api('/api/resolve-mcp-conflict', {name: classicName, action: 'remove_classic'});
  banner(j.success ? 'green' : 'red', j.message);
  if(j.success){ loadState(); }
}

// v1.7.3 - Restart Claude Desktop nucleaire, deplace de l'ancienne carte
// 'Action rapide' (supprimee) vers la tab Watchdog ou il a sa place logique
// a cote de l'auto-remediation DC. Confirme avant action puis appelle
// /api/restart-claude-desktop. Fonction generique : pas de reference a un
// bouton precis, le caller fournit son propre 'data-restart-cd-btn'.
async function restartClaudeDesktop(){
  const btn = document.querySelector('[data-restart-cd-btn]');
  if(!confirm(tr('confirm_restart_claude_desktop'))) return;
  if(btn){ btn.disabled = true; btn.classList.add('opacity-70'); }
  try {
    const r = await fetch('/api/restart-claude-desktop', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
    });
    const j = await r.json();
    banner(j.success ? 'green' : 'red', j.message || (j.success ? 'OK' : 'Echec'));
  } catch(e) {
    banner('red', 'Erreur reseau : ' + e.message);
  } finally {
    if(btn){ btn.disabled = false; btn.classList.remove('opacity-70'); }
  }
}

function escAttr(s){return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;');}
let SKILL_USAGE = {};
let SKILL_SOURCE_FILTER = (localStorage.getItem('cc-skill-src') || 'all');
let SKILL_CAT_FILTER = (localStorage.getItem('cc-skill-cat') || '');
// v1.10.0 - re-rendu synchrone depuis CURRENT_STATE quand un filtre change.
// Avant, on appelait loadState() qui refetch /api/state (1-2s sur grosses
// configs). Resultat : delai apparent quand on clique sur un filtre, qui
// donnait l'impression d'un bug. Maintenant : changement filtre = re-render
// instantane depuis le cache, et un loadState() en background pour
// rafraichir les counts d'usage etc.
function _rerenderSkillsFromCache(){
  if(!CURRENT_STATE || !CURRENT_STATE.skills) return;
  document.getElementById('skills').innerHTML = renderSkills(CURRENT_STATE.skills);
  filterSkills();
}
function setSkillSourceFilter(src){
  SKILL_SOURCE_FILTER = src;
  localStorage.setItem('cc-skill-src', src);
  if(typeof SKILL_SELECTED !== 'undefined') SKILL_SELECTED.clear();
  _rerenderSkillsFromCache();
}
function setSkillCatFilter(cat){
  SKILL_CAT_FILTER = cat;
  localStorage.setItem('cc-skill-cat', cat);
  if(typeof SKILL_SELECTED !== 'undefined') SKILL_SELECTED.clear();
  _rerenderSkillsFromCache();
}
// v1.7.7 - refactor complet 3 zones (health banner + sidebar filters + cards
// grid). Filtres orthogonaux : status, qualite, usage, source, categorie.
// Tout le state est persiste dans localStorage pour survivre aux reloads.
let SKILL_STATUS_FILTER  = (localStorage.getItem('cc-skill-status') || 'all');     // all|active|inactive
let SKILL_QUALITY_FILTER = (localStorage.getItem('cc-skill-quality') || 'all');    // all|excellent|enrich|broken
let SKILL_USAGE_FILTER   = (localStorage.getItem('cc-skill-usage') || 'all');      // all|top|recent|never
let SKILL_SELECTED       = new Set();
function setSkillStatusFilter(v){ SKILL_STATUS_FILTER=v; localStorage.setItem('cc-skill-status', v); SKILL_SELECTED.clear(); _rerenderSkillsFromCache(); }
function setSkillQualityFilter(v){ SKILL_QUALITY_FILTER=v; localStorage.setItem('cc-skill-quality', v); SKILL_SELECTED.clear(); _rerenderSkillsFromCache(); }
function setSkillUsageFilter(v){ SKILL_USAGE_FILTER=v; localStorage.setItem('cc-skill-usage', v); SKILL_SELECTED.clear(); _rerenderSkillsFromCache(); }

// v1.10.1 - reset all filtres en un clic. Bug observe : utilisateur
// accumule des filtres incompatibles et se retrouve avec 0 resultat,
// sans porte de sortie evidente.
function resetSkillFilters(){
  SKILL_STATUS_FILTER = 'all';
  SKILL_QUALITY_FILTER = 'all';
  SKILL_USAGE_FILTER = 'all';
  SKILL_SOURCE_FILTER = 'all';
  SKILL_CAT_FILTER = '';
  localStorage.setItem('cc-skill-status', 'all');
  localStorage.setItem('cc-skill-quality', 'all');
  localStorage.setItem('cc-skill-usage', 'all');
  localStorage.setItem('cc-skill-src', 'all');
  localStorage.setItem('cc-skill-cat', '');
  SKILL_SELECTED.clear();
  const search = document.getElementById('skills-search');
  if(search) search.value = '';
  _rerenderSkillsFromCache();
}
function _hasActiveSkillFilters(){
  return SKILL_STATUS_FILTER !== 'all'
    || SKILL_QUALITY_FILTER !== 'all'
    || SKILL_USAGE_FILTER !== 'all'
    || SKILL_SOURCE_FILTER !== 'all'
    || (SKILL_CAT_FILTER && SKILL_CAT_FILTER !== '');
}
function _skillCat(sk){ return sk.category || sk.auto_category || tr('general_category'); }
function _qualityClass(q){
  if(q==='excellent') return {bg:'bg-green-50', text:'text-green-700', border:'border-green-200', dot:'bg-green-500', label:tr('quality_excellent')};
  if(q==='enrich')    return {bg:'bg-amber-50', text:'text-amber-700', border:'border-amber-200', dot:'bg-amber-500', label:tr('quality_enrich')};
  return                     {bg:'bg-red-50',   text:'text-red-700',   border:'border-red-200',   dot:'bg-red-500',   label:tr('quality_broken')};
}
function _applySkillFilters(skills){
  let f = skills;
  if(SKILL_STATUS_FILTER==='active')   f = f.filter(s=>s.active);
  if(SKILL_STATUS_FILTER==='inactive') f = f.filter(s=>!s.active);
  if(SKILL_QUALITY_FILTER!=='all'){
    f = f.filter(s=>s.quality===SKILL_QUALITY_FILTER);
    // v1.9.8 - 'broken' / 'enrich' = filtres actionables, donc user-only.
    // Coherent avec les counts du health banner et de la sidebar.
    // 'excellent' garde tous les skills (info quality globale).
    if(SKILL_QUALITY_FILTER==='broken' || SKILL_QUALITY_FILTER==='enrich'){
      f = f.filter(s=>s.source==='user');
    }
  }
  if(SKILL_SOURCE_FILTER==='user')     f = f.filter(s=>s.source==='user');
  else if(SKILL_SOURCE_FILTER==='plugin') f = f.filter(s=>s.source!=='user');
  if(SKILL_CAT_FILTER)                 f = f.filter(s=>_skillCat(s)===SKILL_CAT_FILTER);
  if(SKILL_USAGE_FILTER==='never')     f = f.filter(s=>(s.usage_count||0)===0);
  else if(SKILL_USAGE_FILTER==='recent') f = f.filter(s=>(s.usage_count||0)>0);
  else if(SKILL_USAGE_FILTER==='top'){
    const top = [...skills].sort((a,b)=>(b.usage_count||0)-(a.usage_count||0)).slice(0,10).map(s=>s.name);
    const set = new Set(top);
    f = f.filter(s=>set.has(s.name));
  }
  return f;
}
function _renderHealthBanner(skills){
  const total = skills.length;
  // v1.9.8 - les counts 'broken' et 'enrich' du health banner ne comptent
  // QUE les skills user. Les skills plugin sont read-only depuis Claude
  // Control (cf. tooltip cadenas), donc les inclure dans 'a corriger' /
  // 'a enrichir' creait du bruit non actionable. Le ratio global et le
  // total restent sur tous les skills (info utile).
  const excellent = skills.filter(s=>s.quality==='excellent').length;
  const userSkills = skills.filter(s=>s.source==='user');
  const enrich = userSkills.filter(s=>s.quality==='enrich').length;
  const broken = userSkills.filter(s=>s.quality==='broken').length;
  const ratio = total>0 ? Math.round(excellent/total*100) : 0;
  const barColor = ratio>=80 ? 'bg-green-500' : (ratio>=50 ? 'bg-amber-500' : 'bg-red-500');
  // Compte des doublons user/plugin (action via cleanupDuplicateUserSkills)
  const userNames = new Set(skills.filter(s=>s.source==='user').map(s=>s.name));
  const pluginNames = new Set(skills.filter(s=>s.source!=='user').map(s=>s.name));
  let dups = 0; userNames.forEach(n=>{ if(pluginNames.has(n)) dups++; });
  const chipBroken = broken>0 ? `<button onclick="setSkillQualityFilter('broken')" class="text-xs px-2.5 py-1 rounded-full bg-red-50 text-red-700 border border-red-200 hover:bg-red-100">${broken} ${tr('chip_broken_action')}</button>` : '';
  const chipEnrich = enrich>0 ? `<button onclick="setSkillQualityFilter('enrich')" class="text-xs px-2.5 py-1 rounded-full bg-amber-50 text-amber-700 border border-amber-200 hover:bg-amber-100">${enrich} ${tr('chip_enrich_action')}</button>` : '';
  const chipDup = dups>0 ? `<button onclick="cleanupDuplicateUserSkills()" class="text-xs px-2.5 py-1 rounded-full bg-stone-100 text-stone-700 border border-stone-200 hover:bg-stone-200">${dups} ${tr('chip_duplicate_action')}</button>` : '';
  const allOk = (broken+enrich+dups)===0 ? `<span class="text-xs text-green-700">${tr('skills_health_all_good')}</span>` : '';
  const banner = document.getElementById('skills-health-banner');
  if(!banner) return;
  banner.innerHTML = `
    <div class="flex items-baseline justify-between mb-2 flex-wrap gap-2">
      <h2 class="text-lg font-semibold">${tr('skills_health_title')}</h2>
      <span class="text-xs text-stone-500">${excellent}/${total} ${tr('skills_health_ready')}</span>
    </div>
    <div class="w-full h-2 rounded-full bg-stone-100 overflow-hidden mb-3"><div class="h-full ${barColor}" style="width:${ratio}%"></div></div>
    <div class="flex flex-wrap gap-2">${chipBroken}${chipEnrich}${chipDup}${allOk}</div>
  `;
}
// v1.10.0 - applique tous les filtres SAUF celui specifie. Permet aux
// counts de la sidebar de refleter le nombre reel de skills qu'on verrait
// en cliquant sur l'option (= passe tous les autres filtres + cette
// option). Avant, les counts etaient absolus, donc 'Build/Deploy 5'
// alors qu'avec Mes skills actif on n'en voyait que 2 cards. Confusion
// utilisateur.
function _filterSkillsExcept(skills, exclude){
  let f = skills;
  if(exclude !== 'status'){
    if(SKILL_STATUS_FILTER==='active')   f = f.filter(s=>s.active);
    if(SKILL_STATUS_FILTER==='inactive') f = f.filter(s=>!s.active);
  }
  if(exclude !== 'quality'){
    if(SKILL_QUALITY_FILTER!=='all'){
      f = f.filter(s=>s.quality===SKILL_QUALITY_FILTER);
      if(SKILL_QUALITY_FILTER==='broken' || SKILL_QUALITY_FILTER==='enrich'){
        f = f.filter(s=>s.source==='user');
      }
    }
  }
  if(exclude !== 'source'){
    if(SKILL_SOURCE_FILTER==='user')     f = f.filter(s=>s.source==='user');
    else if(SKILL_SOURCE_FILTER==='plugin') f = f.filter(s=>s.source!=='user');
  }
  if(exclude !== 'cat'){
    if(SKILL_CAT_FILTER)                 f = f.filter(s=>_skillCat(s)===SKILL_CAT_FILTER);
  }
  if(exclude !== 'usage'){
    if(SKILL_USAGE_FILTER==='never')     f = f.filter(s=>(s.usage_count||0)===0);
    else if(SKILL_USAGE_FILTER==='recent') f = f.filter(s=>(s.usage_count||0)>0);
    else if(SKILL_USAGE_FILTER==='top'){
      const top = [...skills].sort((a,b)=>(b.usage_count||0)-(a.usage_count||0)).slice(0,10).map(s=>s.name);
      const set = new Set(top);
      f = f.filter(s=>set.has(s.name));
    }
  }
  return f;
}
function _renderFiltersSidebar(skills){
  const el = document.getElementById('skills-filters');
  if(!el) return;
  // v1.10.0 - on calcule un set partiel (tous les filtres SAUF celui
  // qu'on rend) pour chaque groupe. Les counts dans le groupe reflectent
  // ainsi le nombre de skills qu'on verrait reellement en cliquant.
  const baseStatus = _filterSkillsExcept(skills, 'status');
  const baseQuality = _filterSkillsExcept(skills, 'quality');
  const baseSource = _filterSkillsExcept(skills, 'source');
  const baseCat = _filterSkillsExcept(skills, 'cat');
  const baseUsage = _filterSkillsExcept(skills, 'usage');

  const totalAll = skills.length;
  const sActive = baseStatus.filter(s=>s.active).length;
  const sInactive = baseStatus.filter(s=>!s.active).length;
  const sStatusAll = baseStatus.length;

  // Pour quality : 'broken'/'enrich' sont user-only (cf v1.9.8). 'excellent'
  // garde tous les skills. 'all' = total apres autres filtres.
  const userInQual = baseQuality.filter(s=>s.source==='user');
  const qExc = baseQuality.filter(s=>s.quality==='excellent').length;
  const qEnr = userInQual.filter(s=>s.quality==='enrich').length;
  const qBro = userInQual.filter(s=>s.quality==='broken').length;
  const qAll = baseQuality.length;

  const usageRecentCount = baseUsage.filter(s=>(s.usage_count||0)>0).length;
  const usageNever = baseUsage.filter(s=>(s.usage_count||0)===0).length;
  const usageAll = baseUsage.length;
  // 'top' : top 10 du set TOUT (pas du filtre), c'est conceptuellement le
  // top 10 absolu. On compte combien de ceux-la passent les autres filtres.
  const top10Names = new Set([...skills].sort((a,b)=>(b.usage_count||0)-(a.usage_count||0)).slice(0,10).map(s=>s.name));
  const usageTopCount = baseUsage.filter(s=>top10Names.has(s.name)).length;

  const userCount = baseSource.filter(s=>s.source==='user').length;
  const pluginCount = baseSource.filter(s=>s.source!=='user').length;
  const sourceAll = baseSource.length;

  const allCats = {};
  baseCat.forEach(s=>{ const c=_skillCat(s); allCats[c]=(allCats[c]||0)+1; });
  const catEntries = Object.entries(allCats).sort((a,b)=>{
    if(a[0]===tr('general_category')) return 1;
    if(b[0]===tr('general_category')) return -1;
    return a[0].localeCompare(b[0]);
  });
  const catAll = baseCat.length;

  // Version dimmed pour les counts a 0 (categorie ou option non actionable
  // dans le contexte courant). Pas masque pour ne pas surprendre l'utilisateur,
  // juste grise.
  const radio = (group, val, label, count, cur, fn) => {
    const isActive = cur===val;
    const isZero = count === 0 && !isActive;
    return `<button onclick="${fn}('${val}')" class="w-full flex items-center justify-between text-left px-2 py-1 rounded ${isActive?'bg-stone-900 text-white':'hover:bg-stone-50'} ${isZero?'opacity-40':''}"><span class="${isZero?'':''}">${escAttr(label)}</span><span class="text-xs ${isActive?'opacity-80':'text-stone-400'}">${count}</span></button>`;
  };
  // v1.10.1 - lien 'Reinitialiser' visible seulement quand au moins un
  // filtre est actif. Permet de revenir a 'tout afficher' en un clic
  // sans devoir cliquer sur chaque categorie de filtre individuellement.
  const resetLink = _hasActiveSkillFilters()
    ? `<button onclick="resetSkillFilters()" class="w-full text-left text-[11px] text-stone-500 hover:text-stone-900 underline px-2 py-1 mb-2">&#x21bb; ${tr('btn_reset_filters')}</button>`
    : '';
  el.innerHTML = `
    ${resetLink}
    <div>
      <div class="text-[10px] uppercase tracking-wide font-semibold text-stone-500 mb-1.5">${tr('filter_status')}</div>
      ${radio('status','all',tr('filter_all'),sStatusAll,SKILL_STATUS_FILTER,'setSkillStatusFilter')}
      ${radio('status','active',tr('filter_status_active'),sActive,SKILL_STATUS_FILTER,'setSkillStatusFilter')}
      ${radio('status','inactive',tr('filter_status_inactive'),sInactive,SKILL_STATUS_FILTER,'setSkillStatusFilter')}
    </div>
    <div>
      <div class="text-[10px] uppercase tracking-wide font-semibold text-stone-500 mb-1.5">${tr('filter_quality')}</div>
      ${radio('quality','all',tr('filter_all'),qAll,SKILL_QUALITY_FILTER,'setSkillQualityFilter')}
      ${radio('quality','excellent',tr('quality_excellent'),qExc,SKILL_QUALITY_FILTER,'setSkillQualityFilter')}
      ${radio('quality','enrich',tr('quality_enrich'),qEnr,SKILL_QUALITY_FILTER,'setSkillQualityFilter')}
      ${radio('quality','broken',tr('quality_broken'),qBro,SKILL_QUALITY_FILTER,'setSkillQualityFilter')}
    </div>
    <div>
      <div class="text-[10px] uppercase tracking-wide font-semibold text-stone-500 mb-1.5">${tr('filter_usage')}</div>
      ${radio('usage','all',tr('filter_all'),usageAll,SKILL_USAGE_FILTER,'setSkillUsageFilter')}
      ${radio('usage','top',tr('filter_usage_top'),usageTopCount,SKILL_USAGE_FILTER,'setSkillUsageFilter')}
      ${radio('usage','recent',tr('filter_usage_recent'),usageRecentCount,SKILL_USAGE_FILTER,'setSkillUsageFilter')}
      ${radio('usage','never',tr('filter_usage_never'),usageNever,SKILL_USAGE_FILTER,'setSkillUsageFilter')}
    </div>
    <div>
      <div class="text-[10px] uppercase tracking-wide font-semibold text-stone-500 mb-1.5">${tr('filter_source')}</div>
      ${radio('source','all',tr('filter_all'),sourceAll,SKILL_SOURCE_FILTER,'setSkillSourceFilter')}
      ${radio('source','user',tr('skill_filter_mine'),userCount,SKILL_SOURCE_FILTER,'setSkillSourceFilter')}
      ${radio('source','plugin',tr('skill_filter_plugins'),pluginCount,SKILL_SOURCE_FILTER,'setSkillSourceFilter')}
    </div>
    <div>
      <div class="text-[10px] uppercase tracking-wide font-semibold text-stone-500 mb-1.5">${tr('category_filter')}</div>
      <button onclick="setSkillCatFilter('')" class="w-full flex items-center justify-between text-left px-2 py-1 rounded ${!SKILL_CAT_FILTER?'bg-stone-900 text-white':'hover:bg-stone-50'}"><span>${tr('skill_filter_all_cats')}</span><span class="text-xs ${!SKILL_CAT_FILTER?'opacity-80':'text-stone-400'}">${catAll}</span></button>
      ${catEntries.map(([c,n])=>{
        const isActive = SKILL_CAT_FILTER===c;
        const isZero = n === 0 && !isActive;
        return `<button onclick="setSkillCatFilter('${escAttr(c)}')" class="w-full flex items-center justify-between text-left px-2 py-1 rounded ${isActive?'bg-stone-900 text-white':'hover:bg-stone-50'} ${isZero?'opacity-40':''}"><span class="truncate">${escAttr(c)}</span><span class="text-xs ${isActive?'opacity-80':'text-stone-400'} ml-2 shrink-0">${n}</span></button>`;
      }).join('')}
    </div>
  `;
}
function _renderSkillCard(sk){
  const q = _qualityClass(sk.quality);
  const isPlugin = sk.source !== 'user';
  // v1.9.8 - les plugin skills sont read-only (managed par leur marketplace).
  // Border neutre gris au lieu de la couleur quality (rouge/ambre/vert) pour
  // signaler visuellement qu'ils ne sont pas dans le bucket actionable.
  // Le dot quality reste pour l'info mais discret.
  const borderClass = isPlugin ? 'border-stone-300' : q.border;
  const sourceBadge = sk.source==='user'
    ? `<span class="text-[10px] bg-green-50 text-green-700 px-1.5 py-0.5 rounded">${tr('source_badge_user')}</span>`
    : `<span class="text-[10px] bg-stone-100 text-stone-600 px-1.5 py-0.5 rounded" title="${escAttr(sk.source)} - ${tr('plugin_readonly_tooltip')}">${tr('source_badge_plugin')}</span>`;
  const usageBadge = (sk.usage_count||0)>0
    ? `<span class="text-[10px] bg-green-50 text-green-700 px-1.5 py-0.5 rounded font-mono" title="${tr('used_x_times').split('{n}').join(sk.usage_count)}">${sk.usage_count}&times;</span>`
    : '';
  const cat = _skillCat(sk);
  const catBadge = `<span class="text-[10px] bg-stone-100 text-stone-500 px-1.5 py-0.5 rounded">${escAttr(cat)}</span>`;
  const desc = (sk.description || '').trim();
  // v1.9.8 - hint "sans description" en rouge SEULEMENT pour user skills
  // (actionable). Pour plugin skills sans description, hint neutre.
  const descHtml = desc
    ? `<p class="text-xs text-stone-600 mt-2 line-clamp-2">${escAttr(desc)}</p>`
    : (isPlugin
        ? `<p class="text-xs italic text-stone-400 mt-2">${tr('plugin_no_description_hint')}</p>`
        : `<p class="text-xs italic text-red-700 mt-2">${tr('quality_broken_hint')}</p>`);
  const editable = !isPlugin;
  const checkbox = editable
    ? `<input type="checkbox" ${sk.active?'checked':''} onchange="event.stopPropagation();toggleSkill('${escAttr(sk.name)}')" class="w-4 h-4 rounded accent-green-700 shrink-0" title="${tr('toggle_skill_title')}">`
    : `<span class="w-4 h-4 inline-flex items-center justify-center text-stone-400 shrink-0" title="${tr('plugin_readonly_tooltip')}">&#128274;</span>`;
  const deleteBtn = editable
    ? `<button type="button" onclick="event.stopPropagation();deleteSkill('${escAttr(sk.name)}')" class="text-[11px] text-stone-400 hover:text-red-700 hover:underline">${tr('btn_delete')}</button>`
    : '';
  // v1.9.0 - bouton 'Reparer' sur skills broken/enrich, uniquement sur les
  // skills user editables (les plugins ont leur propre source de verite).
  const repairBtn = (editable && (sk.quality === 'broken' || sk.quality === 'enrich'))
    ? `<button type="button" onclick="event.stopPropagation();openRepairSkill('${escAttr(sk.name)}')" class="text-[11px] font-medium text-amber-800 bg-amber-50 hover:bg-amber-100 border border-amber-200 rounded px-2 py-0.5">${tr('btn_repair_skill')}</button>`
    : '';
  const checked = SKILL_SELECTED.has(sk.name) ? 'checked' : '';
  // v1.9.8 - bulk select checkbox masque pour les plugin skills (pas
  // d'action bulk possible dessus, et c'est cohérent avec leur read-only).
  const selectCheckbox = editable
    ? `<input type="checkbox" ${checked} onchange="event.stopPropagation();toggleSkillSelect('${escAttr(sk.name)}', this.checked)" class="w-3.5 h-3.5 rounded accent-stone-700 shrink-0" title="${tr('select_for_bulk_title')}">`
    : `<span class="w-3.5 h-3.5 shrink-0"></span>`;
  // v1.9.8 - dot quality discret (gris) pour plugin skills, pour ne pas
  // suggerer une action.
  const dotClass = isPlugin ? 'bg-stone-300' : q.dot;
  return `<div data-skill data-search="${escAttr((sk.name+' '+desc).toLowerCase())}" class="card p-3 border-l-4 ${borderClass} ${sk.active?'':'opacity-60'}${isPlugin?' bg-stone-50/50':''}">
    <div class="flex items-start gap-2">
      ${selectCheckbox}
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-2 mb-0.5">
          <span class="inline-block w-1.5 h-1.5 rounded-full ${dotClass}" title="${escAttr(q.label)}"></span>
          <span class="font-semibold text-sm truncate">${escAttr(sk.name)}</span>
        </div>
        <div class="flex flex-wrap items-center gap-1">${catBadge}${sourceBadge}${usageBadge}</div>
        ${descHtml}
      </div>
      <div class="flex flex-col items-end gap-1 shrink-0">${checkbox}${repairBtn}${deleteBtn}</div>
    </div>
  </div>`;
}
function _renderBulkBar(){
  const bar = document.getElementById('skills-bulkbar');
  if(!bar) return;
  const n = SKILL_SELECTED.size;
  if(n===0){ bar.classList.add('hidden'); return; }
  bar.classList.remove('hidden');
  bar.innerHTML = `<span class="text-xs">${n} ${tr('selected_count')}</span>
    <button onclick="bulkDisableSelectedSkills()" class="text-xs px-3 py-1 rounded bg-stone-700 hover:bg-stone-600">${tr('bulk_disable_btn')}</button>
    <button onclick="bulkDeleteSelectedSkills()" class="text-xs px-3 py-1 rounded bg-red-700 hover:bg-red-600">${tr('bulk_delete_btn')}</button>
    <button onclick="SKILL_SELECTED.clear();loadState();" class="text-xs px-3 py-1 rounded bg-stone-600 hover:bg-stone-500 ml-auto">${tr('btn_cancel')}</button>`;
}
function toggleSkillSelect(name, on){
  if(on) SKILL_SELECTED.add(name); else SKILL_SELECTED.delete(name);
  _renderBulkBar();
}
async function bulkDisableSelectedSkills(){
  const names = Array.from(SKILL_SELECTED);
  if(names.length===0) return;
  if(!confirm(tr('confirm_bulk_disable').split('{n}').join(names.length))) return;
  for(const n of names){ await api('/api/toggle-skill', {name:n}); }
  SKILL_SELECTED.clear(); loadState();
}
async function bulkDeleteSelectedSkills(){
  const names = Array.from(SKILL_SELECTED);
  if(names.length===0) return;
  if(!confirm(tr('confirm_bulk_delete').split('{n}').join(names.length))) return;
  for(const n of names){ await api('/api/delete-skill', {name:n}); }
  SKILL_SELECTED.clear(); loadState(); loadOverview();
}
// v1.9.3 - banner top Skills si CLI absent ET il y a des skills broken/enrich
// (cas ou on aurait besoin de generer des descriptions). Cache le banner
// si CLI dispo OU pas de skill a reparer.
async function _renderCliInstallBanner(skills){
  const banner = document.getElementById('skills-cli-banner');
  if(!banner) return;
  const needs = (skills || []).some(s => s.quality === 'broken' || s.quality === 'enrich');
  if(!needs){ banner.classList.add('hidden'); return; }
  let available = false;
  try {
    const r = await fetch('/api/suggest-source-status');
    if(r.ok){ const d = await r.json(); available = !!d.available; }
  } catch(e){ /* noop */ }
  if(available){ banner.classList.add('hidden'); return; }
  banner.classList.remove('hidden');
  banner.innerHTML = `
    <div class="flex items-start gap-3">
      <span class="text-amber-600 text-lg shrink-0">&#128161;</span>
      <div class="flex-1 text-xs text-stone-700 leading-relaxed">
        <strong>${tr('skills_cli_banner_title')}</strong><br>
        ${tr('skills_cli_banner_body')}
        <code class="text-[11px] bg-stone-100 px-1.5 py-0.5 rounded font-mono mt-1 inline-block">npm install -g @anthropic-ai/claude-code</code>
      </div>
    </div>
  `;
}
function renderSkills(skills){
  _renderHealthBanner(skills);
  _renderCliInstallBanner(skills);
  _renderFiltersSidebar(skills);
  if(!skills || skills.length===0) return `<p class="text-stone-400 text-sm col-span-full">${tr('no_skill')}</p>`;
  const filtered = _applySkillFilters(skills);
  if(filtered.length === 0) return `<div class="col-span-full p-6 text-center">
    <p class="text-stone-500 italic mb-3">${tr('skills_empty_with_filters')}</p>
    <button onclick="resetSkillFilters()" class="text-xs font-medium text-white bg-stone-900 hover:bg-stone-800 rounded-md px-4 py-2">&#x21bb; ${tr('btn_reset_filters')}</button>
  </div>`;
  return filtered.map(_renderSkillCard).join('');
}
function filterSkills(){
  const q = (document.getElementById('skills-search').value || '').trim().toLowerCase();
  const root = document.getElementById('skills');
  if(!root) return;
  root.querySelectorAll('[data-skill]').forEach(el=>{
    const match = !q || (el.getAttribute('data-search')||'').includes(q);
    el.classList.toggle('hidden', !match);
  });
}
function pluginContentBadge(c){
  const parts = [];
  if(c.skills_count) parts.push(c.skills_count + ' skill' + (c.skills_count>1?'s':''));
  if(c.mcp_count) parts.push(c.mcp_count + ' MCP' + (c.mcp_count>1?'s':''));
  if(c.commands_count) parts.push(c.commands_count + ' command' + (c.commands_count>1?'s':''));
  if(c.hooks_count) parts.push(c.hooks_count + ' hook' + (c.hooks_count>1?'s':''));
  if(c.missing) parts.push(tr('plugin_install_path_missing'));
  return parts.length ? parts.join(' &middot; ') : tr('plugin_empty');
}
function pluginDetailHtml(c){
  const sections = [];
  if(c.skills && c.skills.length) sections.push(`<div><span class="text-xs font-semibold uppercase tracking-wide text-stone-600">Skills</span><div class="mt-1 flex flex-wrap gap-1.5">${c.skills.map(s=>`<span class="text-xs bg-stone-100 px-2 py-0.5 rounded">${escAttr(s)}</span>`).join('')}</div></div>`);
  if(c.mcps && c.mcps.length) sections.push(`<div><span class="text-xs font-semibold uppercase tracking-wide text-stone-600">MCPs</span><div class="mt-1 flex flex-wrap gap-1.5">${c.mcps.map(s=>`<span class="text-xs bg-stone-100 px-2 py-0.5 rounded">${escAttr(s)}</span>`).join('')}</div></div>`);
  if(c.commands && c.commands.length) sections.push(`<div><span class="text-xs font-semibold uppercase tracking-wide text-stone-600">Commands</span><div class="mt-1 flex flex-wrap gap-1.5">${c.commands.map(s=>`<span class="text-xs bg-stone-100 px-2 py-0.5 rounded">/${escAttr(s)}</span>`).join('')}</div></div>`);
  return sections.length ? `<div class="mt-3 pt-3 border-t border-stone-100 space-y-3">${sections.join('')}</div>` : `<div class="mt-3 pt-3 border-t border-stone-100 text-xs text-stone-400">${tr('plugin_no_content')}</div>`;
}
async function loadPlugins(){
  try{
    const j = await (await fetch('/api/plugins')).json();
    const plugins = j.plugins || [];
    const list = document.getElementById('plugins');
    if(plugins.length===0){list.innerHTML = `<p class="text-xs text-stone-400">${tr('plugins_empty')}</p>`;return;}
    list.innerHTML = plugins.map(p=>{
      const fn = escAttr(p.full_name);
      const opacity = p.enabled ? '' : 'opacity-60';
      // v1.9.9 - badge orphan UX clarifie. Avant : 'orphan: vX.Y.Z' suggerait
      // un warning passif. Maintenant : icone poubelle + label 'Nettoyer
      // cache vX.Y.Z' rend l'action explicite. Tooltip detaille pour rassurer
      // (le plugin actif n'est pas touche, juste l'ancienne version).
      const orphans = (p.extra_versions||[]).map(v=>{
        const tooltip = (CURRENT_LANG === 'en'
          ? `Click to delete only the orphan cache directory v${v} of "${fn}". The active plugin v${p.version||'?'} is NOT touched.`
          : `Cliquer pour supprimer UNIQUEMENT le dossier de cache orphelin v${v} de « ${fn} ». Le plugin actif v${p.version||'?'} N'EST PAS touche.`);
        return `<button onclick="event.stopPropagation();cleanupOrphan('${fn}','${escAttr(v)}','${escAttr(p.version||'?')}')" class="text-xs px-2 py-0.5 rounded-full font-medium update-badge text-white inline-flex items-center gap-1" title="${escAttr(tooltip)}">&#128465; ${tr('btn_cleanup_orphan_cache').split('{v}').join(escAttr(v))}</button>`;
      }).join(' ');
      return `<div data-plugin-name="${escAttr(p.name)} ${escAttr(p.marketplace||'')} ${escAttr(p.full_name)}" class="group border ${p.enabled?'border-stone-200':'border-stone-100'} rounded-lg p-3 ${opacity}">
<div class="flex items-center gap-3">
<input type="checkbox" ${p.enabled?'checked':''} onchange="togglePlugin('${fn}')" class="w-5 h-5 rounded accent-green-700 shrink-0">
<button onclick="togglePluginDetail('${fn}')" class="flex-1 text-left min-w-0">
<div class="flex items-baseline gap-2 flex-wrap">
<span class="font-medium">${escAttr(p.name)}</span>
<span class="text-xs text-stone-400 font-mono">v${escAttr(p.version||'?')}</span>
<span class="text-xs text-stone-500">${escAttr(p.marketplace||'')}</span>
${orphans}
</div>
<div class="text-xs text-stone-500 mt-0.5">${pluginContentBadge(p.contents||{})}</div>
</button>
<button type="button" onclick="event.stopPropagation();deletePlugin('${fn}')" class="text-xs text-stone-500 hover:text-red-700 hover:underline px-2 py-1 shrink-0">${tr('btn_delete')}</button>
</div>
<div id="pl-detail-${fn}" class="hidden">${pluginDetailHtml(p.contents||{})}</div>
</div>`;
    }).join('');
  }catch(e){console.error(e);}
}
function togglePluginDetail(fn){
  const el = document.getElementById('pl-detail-'+fn);
  if(el) el.classList.toggle('hidden');
}
async function togglePlugin(fn){
  const j = await api('/api/toggle-plugin',{name:fn});
  banner(j.success?'green':'red',j.message);
  loadPlugins();
}
async function deleteSkill(name){
  if(!confirm(tr('confirm_delete_skill').split('{name}').join(name)))return;
  const j = await api('/api/delete-skill', {name:name});
  banner(j.success?'green':'red', j.message);
  if(j.success){loadState();loadOverview();}
}

// v1.9.0 - Modal de reparation des skills (broken/enrich quality).
// Lit le contenu actuel du SKILL.md, expose un textarea pour la
// description, propose un bouton "Suggerer via API Anthropic" qui appelle
// _call_anthropic_messages cote backend (Haiku 4.5, opt-in via env
// ANTHROPIC_API_KEY ou ~/.claude/claude-control-anthropic-key).
let CURRENT_REPAIR_SKILL = null;
let REPAIR_SUGGEST_LANG = null;  // v1.9.6 - null = follow CURRENT_LANG, sinon override
function setRepairSuggestLang(lang){
  REPAIR_SUGGEST_LANG = lang;
  _updateRepairLangToggleUI();
}
function _updateRepairLangToggleUI(){
  const active = REPAIR_SUGGEST_LANG || CURRENT_LANG || 'fr';
  ['fr','en'].forEach(l=>{
    const btn = document.getElementById('repair-skill-lang-'+l);
    if(!btn) return;
    if(l === active){
      btn.classList.add('bg-stone-900','text-white');
      btn.classList.remove('text-stone-700','hover:bg-stone-100');
    } else {
      btn.classList.remove('bg-stone-900','text-white');
      btn.classList.add('text-stone-700','hover:bg-stone-100');
    }
  });
}
async function openRepairSkill(name){
  CURRENT_REPAIR_SKILL = name;
  document.getElementById('repair-skill-name').textContent = name;
  document.getElementById('repair-skill-desc').value = '';
  document.getElementById('repair-skill-desc-count').textContent = '0';
  document.getElementById('repair-skill-suggest-meta').classList.add('hidden');
  _updateRepairLangToggleUI();
  document.getElementById('repair-skill-preview').textContent = tr('loading') || 'Loading...';
  document.getElementById('repair-skill-modal').classList.remove('hidden');
  // Set up live counter
  const ta = document.getElementById('repair-skill-desc');
  ta.oninput = () => {
    document.getElementById('repair-skill-desc-count').textContent = String(ta.value.length);
  };
  ta.focus();
  // v1.9.3 - Disable suggest button if Claude Code CLI not available
  try {
    const ks = await (await fetch('/api/suggest-source-status')).json();
    const btn = document.getElementById('repair-skill-suggest-btn');
    if (!ks.available) {
      btn.disabled = true;
      btn.classList.add('opacity-50', 'cursor-not-allowed');
      btn.title = tr('repair_skill_no_cli');
    } else {
      btn.disabled = false;
      btn.classList.remove('opacity-50', 'cursor-not-allowed');
      btn.title = '';
    }
  } catch(e){ /* noop */ }
  // Load current SKILL.md content
  try {
    const r = await fetch('/api/skill-content/' + encodeURIComponent(name));
    if (!r.ok) {
      document.getElementById('repair-skill-preview').textContent = '(' + r.status + ')';
      return;
    }
    const d = await r.json();
    const desc = (d.meta && d.meta.description) || '';
    if (desc) {
      ta.value = desc;
      document.getElementById('repair-skill-desc-count').textContent = String(desc.length);
    }
    document.getElementById('repair-skill-preview').textContent = d.content || '(empty SKILL.md)';
  } catch(e){
    document.getElementById('repair-skill-preview').textContent = '(error: ' + e + ')';
  }
}
function closeRepairSkill(){
  document.getElementById('repair-skill-modal').classList.add('hidden');
  CURRENT_REPAIR_SKILL = null;
}
async function openTerminalForLogin(){
  // v1.9.5 - ouvre Terminal.app en pre-tapant 'claude /login' via le
  // backend. v1.9.6 : apres ouverture, on remplace la zone meta par
  // un appel a l'action 'Une fois loggé, clique pour reessayer' avec
  // un gros bouton vert. Plus de devinette pour l'utilisateur.
  const j = await api('/api/open-terminal-claude-login', {});
  banner(j.success ? 'green' : 'red', j.message);
  if(!j.success) return;
  const meta = document.getElementById('repair-skill-suggest-meta');
  meta.classList.remove('hidden');
  meta.className = 'text-xs mt-2';
  meta.innerHTML = `
    <div class="p-2 rounded bg-amber-50 border border-amber-200">
      <p class="text-stone-700 mb-2">${tr('repair_skill_login_pending_hint')}</p>
      <button onclick="suggestSkillDescription()" class="text-xs font-medium text-white bg-green-700 hover:bg-green-800 rounded px-3 py-1.5">
        &#10003; ${tr('repair_skill_retry_after_login')}
      </button>
    </div>
  `;
}
async function diagClaudeCli(){
  // v1.9.4 - diag du CLI Claude Code, expose path + version + sanity check
  // pour aider l'utilisateur a comprendre pourquoi le CLI echoue.
  try {
    const r = await fetch('/api/claude-cli-diagnose');
    const d = await r.json();
    const lines = [
      'available: ' + (d.available ? 'YES' : 'NO'),
      'path: ' + (d.path || '(not in PATH)'),
      'version: ' + (d.version || '(unknown)'),
    ];
    if (d.error) lines.push('error: ' + d.error);
    alert(tr('repair_skill_diag_title') + '\n\n' + lines.join('\n'));
  } catch(e){
    alert('Diag failed: ' + e.message);
  }
}
async function suggestSkillDescription(){
  if (!CURRENT_REPAIR_SKILL) return;
  const btn = document.getElementById('repair-skill-suggest-btn');
  if (btn.disabled) return;
  const orig = btn.innerHTML;
  const meta = document.getElementById('repair-skill-suggest-meta');
  // v1.9.2 - status persistant dans la modal (le banner global se dismiss
  // trop vite et l'utilisateur croit que rien ne s'est passe). On affiche
  // dans le meta : success + model+chars OU error en rouge persistant.
  function setMeta(html, color){
    meta.innerHTML = html;
    meta.className = 'text-[10px] mt-1 ' + (color === 'red' ? 'text-red-700' : (color === 'green' ? 'text-green-700' : 'text-stone-500'));
    meta.classList.remove('hidden');
  }
  btn.disabled = true;
  btn.innerHTML = '<span class="inline-block animate-spin">&#x21bb;</span> ' + tr('repair_skill_suggesting');
  setMeta(tr('repair_skill_suggesting'), 'gray');
  try {
    // v1.9.6 - lang : par defaut langue UI, override via REPAIR_SUGGEST_LANG si toggle utilise
    const lang = (typeof REPAIR_SUGGEST_LANG !== 'undefined' && REPAIR_SUGGEST_LANG) || CURRENT_LANG || 'fr';
    const r = await fetch('/api/suggest-skill-description', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: CURRENT_REPAIR_SKILL, lang: lang}),
    });
    const j = await r.json();
    console.log('[repair] suggest response:', j);  // debug visibility
    if (!j.success) {
      const msg = j.error || j.message || 'Suggestion failed (no message)';
      // v1.9.5 - cas specifique 'cli_not_logged_in' : UI dediee avec
      // commande copiable + bouton open-terminal au lieu d'un dump
      // technique. Plus actionnable pour l'utilisateur.
      if (j.error_code === 'cli_not_logged_in') {
        const fixCmd = j.fix_command || 'claude /login';
        setMeta(
          '<strong>&#128274; ' + tr('repair_skill_not_logged_in_title') + '</strong>'
          + '<p class="mt-1">' + tr('repair_skill_not_logged_in_body') + '</p>'
          + '<div class="mt-2 flex items-center gap-2">'
          + '<code class="text-[11px] bg-stone-900 text-stone-100 px-2 py-1 rounded font-mono">' + escAttr(fixCmd) + '</code>'
          + '<button onclick="navigator.clipboard.writeText(\'' + escAttr(fixCmd) + '\').then(()=>banner(\'green\', \'' + escAttr(tr('copied')) + '\'))" '
          + 'class="text-[10px] underline text-stone-600 hover:text-stone-900">'
          + tr('btn_copy') + '</button>'
          + '<button onclick="openTerminalForLogin()" '
          + 'class="text-[10px] underline text-stone-600 hover:text-stone-900">'
          + tr('btn_open_terminal') + '</button>'
          + '</div>',
          'red'
        );
        banner('red', tr('repair_skill_not_logged_in_title'));
      } else {
        // v1.9.4 - quand le CLI echoue (autre cause), on affiche un bouton
        // diag pour l'utilisateur (path + version + sanity check).
        setMeta(
          '<strong>&#9888; ' + escAttr(msg) + '</strong>'
          + '<div class="mt-2"><button onclick="diagClaudeCli()" '
          + 'class="text-[10px] underline text-stone-600 hover:text-stone-900">'
          + tr('repair_skill_diag_btn') + '</button></div>',
          'red'
        );
        banner('red', msg);
      }
    } else {
      const suggestion = (j.suggestion || '').trim();
      if (!suggestion) {
        setMeta('<strong>&#9888; API returned empty suggestion (response: ' + escAttr(JSON.stringify(j).substring(0, 200)) + ')</strong>', 'red');
        banner('red', 'API returned empty suggestion');
        return;
      }
      const ta = document.getElementById('repair-skill-desc');
      ta.value = suggestion;
      document.getElementById('repair-skill-desc-count').textContent = String(ta.value.length);
      setMeta(tr('repair_skill_suggested_via').split('{n}').join(j.chars_sent || 0), 'green');
      ta.focus();
    }
  } catch(e){
    console.error('[repair] suggest error:', e);
    const msg = 'Erreur reseau : ' + e.message;
    setMeta('<strong>&#9888; ' + escAttr(msg) + '</strong>', 'red');
    banner('red', msg);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}
async function saveRepairSkill(){
  if (!CURRENT_REPAIR_SKILL) return;
  const desc = document.getElementById('repair-skill-desc').value.trim();
  if (!desc) { banner('red', tr('repair_skill_desc_required')); return; }
  const btn = document.getElementById('repair-skill-save-btn');
  // v1.9.7 - feedback visuel pendant la sauvegarde. Sans label change,
  // le delai (write file + zip backup + reload state) faisait croire
  // a un bug parce que le bouton restait fige.
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="inline-block animate-spin">&#x21bb;</span> ' + tr('saving');
  try {
    const j = await api('/api/repair-skill', {name: CURRENT_REPAIR_SKILL, description: desc});
    banner(j.success ? 'green' : 'red', j.message);
    if (j.success) {
      closeRepairSkill();
      loadState();
      loadOverview();
    }
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}
async function restartMcp(name){
  if(!confirm(tr('confirm_restart_mcp').split('{name}').join(name)))return;
  banner('blue', tr('banner_restarting'));
  const j = await api('/api/restart-mcp', {name:name});
  banner(j.success?'green':'red', j.message);
  if(j.success){loadState();}
}
async function stopMcp(name){
  if(!confirm(tr('confirm_stop_mcp').split('{name}').join(name)))return;
  const j = await api('/api/stop-mcp', {name:name});
  banner(j.success?'green':'red', j.message);
  if(j.success){loadState();}
}
async function startMcp(name){
  banner('blue', tr('banner_starting'));
  const j = await api('/api/start-mcp', {name:name});
  banner(j.success?'green':'red', j.message);
  if(j.success){setTimeout(loadState, 2000);}
}
async function deleteMcp(name){
  if(!confirm(tr('confirm_delete_mcp').split('{name}').join(name)))return;
  const j = await api('/api/delete-mcp', {name:name});
  banner(j.success?'green':'red', j.message);
  if(j.success){loadState();loadOverview();loadPresets();}
}
async function deleteExtension(name){
  if(!confirm(tr('confirm_delete_extension').split('{name}').join(name)))return;
  const j = await api('/api/delete-extension', {name:name});
  banner(j.success?'green':'red', j.message);
  if(j.success){loadState();loadOverview();}
}
async function deletePlugin(fn){
  const msg = tr('confirm_delete_plugin').split('{name}').join(fn);
  const yesFiles = confirm(msg);
  if(!yesFiles && !confirm(tr('confirm_delete_plugin_metadata_only').split('{name}').join(fn))) return;
  const j = await api('/api/delete-plugin', {name:fn, delete_files: yesFiles});
  banner(j.success?'green':'red', j.message);
  if(j.success){loadPlugins();loadOverview();loadCommands();}
}
function openAddPlugin(){
  document.getElementById('add-plugin-url').value = '';
  document.getElementById('add-plugin-modal').classList.remove('hidden');
  setTimeout(()=>document.getElementById('add-plugin-url').focus(), 50);
}
function closeAddPlugin(){document.getElementById('add-plugin-modal').classList.add('hidden');}
async function confirmAddPlugin(){
  const url = document.getElementById('add-plugin-url').value.trim();
  if(!url){banner('red', tr('banner_url_required'));return;}
  banner('blue', tr('banner_cloning'));
  const j = await api('/api/add-plugin-git', {url:url});
  banner(j.success?'green':'red', j.message);
  if(j.success){closeAddPlugin();loadPlugins();loadOverview();loadCommands();}
}
async function cleanupOrphan(fn, version, activeVersion){
  // v1.9.9 - confirm popup detaille pour rassurer l'utilisateur. Mention
  // explicite : le plugin actif (activeVersion) reste intact, seul le
  // dossier de cache orphelin (version) est supprime + backup ZIP.
  const msg = CURRENT_LANG === 'en'
    ? `Delete the orphan cache directory v${version} of "${fn}"?\n\n` +
      `[OK]   Cache directory v${version} -> backup ZIP + deletion\n` +
      `[NOT TOUCHED]   Active plugin v${activeVersion || '?'} stays running\n` +
      `[NOT TOUCHED]   Plugin metadata, settings, all skills/MCPs/commands\n\n` +
      `Backup location: ~/.claude/backups/claude-control/orphan-plugins/\n\n` +
      `CONFIRM?`
    : `Supprimer le dossier de cache orphelin v${version} de « ${fn} » ?\n\n` +
      `[OUI]   Dossier de cache v${version} -> backup ZIP + suppression\n` +
      `[INTACT]   Plugin actif v${activeVersion || '?'} continue de tourner\n` +
      `[INTACT]   Metadonnees, settings, skills/MCPs/commands du plugin\n\n` +
      `Backup : ~/.claude/backups/claude-control/orphan-plugins/\n\n` +
      `CONFIRMER ?`;
  if(!confirm(msg))return;
  const j = await api('/api/plugin-cleanup',{name:fn, version:version});
  banner(j.success?'green':'red',j.message);
  if(j.success){loadPlugins();}
}
function statBox(value, label, color){
  return `<div class="p-3 rounded-lg border ${color}"><div class="text-2xl font-semibold leading-none mb-1">${value}</div><div class="text-xs text-stone-500">${label}</div></div>`;
}
async function loadWatchdog(){
  try{
    const r = await fetch('/api/watchdog');
    if(!r.ok) return;
    const d = await r.json();
    const el = document.getElementById('watchdog-widget');
    if(!el) return;
    const cfg = d.config || {};
    const running = !!d.claude_running;
    const targetLabel = d.target_label || cfg.target || 'Claude Desktop';
    const statusBadge = running
      ? `<span class="text-xs text-green-700 bg-green-50 px-2 py-0.5 rounded-full running-dot">${escAttr(targetLabel)} &middot; ${tr('claude_running')}</span>`
      : `<span class="text-xs text-stone-700 bg-stone-100 px-2 py-0.5 rounded-full">${escAttr(targetLabel)} &middot; ${tr('claude_stopped')}</span>`;
    const events = (d.events || []).slice(0,3).map(ev=>`<div class="text-[10px] text-stone-500"><span class="font-mono">${escAttr(ev.ts.slice(11,19))}</span> &middot; ${escAttr(ev.action)} &middot; ${escAttr(ev.detail||'')}</div>`).join('');
    const targets = (d.available_targets || ['claude_desktop']);
    const targetOptions = targets.map(t=>{
      const label = t === 'claude_desktop' ? 'Claude Desktop' : (t === 'custom' ? tr('watchdog_custom_target') : t);
      return `<option value="${escAttr(t)}" ${cfg.target===t?'selected':''}>${escAttr(label)}</option>`;
    }).join('');
    const isCustom = cfg.target === 'custom';
    const customRow = isCustom ? `<div class="mt-2 flex flex-wrap items-center gap-2 text-xs">
<input id="watchdog-pattern" type="text" value="${escAttr(cfg.target_pattern||'')}" placeholder="${tr('watchdog_pattern_placeholder')}" class="flex-1 min-w-[200px] px-2 py-1 border border-stone-200 rounded bg-white"/>
<button onclick="saveWatchdogPattern()" class="px-3 py-1 rounded bg-stone-900 hover:bg-stone-800 text-white font-medium">${tr('btn_save')}</button>
<button onclick="testWatchdogPattern()" class="px-3 py-1 rounded border border-stone-200 hover:bg-white font-medium">${tr('btn_scan')}</button>
</div><div id="watchdog-scan-result" class="mt-2 text-xs"></div>` : '';
    el.innerHTML = `<div class="p-3 rounded-lg border border-stone-200 bg-stone-50">
<div class="flex items-baseline justify-between mb-2 flex-wrap gap-2">
<div class="flex items-center gap-2">
<span class="text-xs font-semibold uppercase tracking-wide text-stone-600">${tr('watchdog_label')}</span>
${statusBadge}
</div>
<span class="text-xs text-stone-400">${cfg.enabled ? tr('watchdog_active').split('{n}').join(cfg.interval_seconds) : tr('watchdog_inactive')}</span>
</div>
<div class="flex flex-wrap items-center gap-3 text-xs">
<label class="flex items-center gap-2 cursor-pointer"><input type="checkbox" ${cfg.enabled?'checked':''} onchange="updateWatchdog({enabled:this.checked})" class="w-4 h-4 rounded accent-green-700"><span>${tr('watchdog_enable')}</span></label>
<label class="flex items-center gap-2 cursor-pointer ${cfg.enabled?'':'opacity-50'}"><span>${tr('watchdog_target_label')}:</span>
<select onchange="updateWatchdog({target:this.value})" ${cfg.enabled?'':'disabled'} class="text-xs border border-stone-200 rounded px-2 py-1 bg-white">${targetOptions}</select>
</label>
<label class="flex items-center gap-2 cursor-pointer ${cfg.enabled?'':'opacity-50'}"><input type="checkbox" ${cfg.auto_restart_on_crash?'checked':''} ${cfg.enabled?'':'disabled'} onchange="updateWatchdog({auto_restart_on_crash:this.checked})" class="w-4 h-4 rounded accent-green-700"><span>${tr('watchdog_crash')}</span></label>
<label class="flex items-center gap-2 cursor-pointer ${cfg.enabled?'':'opacity-50'}"><input type="checkbox" ${cfg.freeze_detection?'checked':''} ${cfg.enabled?'':'disabled'} onchange="updateWatchdog({freeze_detection:this.checked})" class="w-4 h-4 rounded accent-green-700"><span>${tr('watchdog_freeze')}</span></label>
</div>
${customRow}
${events ? `<div class="mt-2 pt-2 border-t border-stone-200 space-y-0.5">${events}</div>` : ''}
</div>`;
  }catch(e){console.error(e);}
}
async function updateWatchdog(updates){
  const j = await api('/api/watchdog-config', updates);
  if(!j.success){banner('red', j.message || 'erreur');return;}
  loadWatchdog();
  loadWatchdogTab();
}
function _watchdogEventColor(action){
  const a = String(action || '');
  if(a.indexOf('error') >= 0 || a.indexOf('failed') >= 0) return 'text-red-700';
  if(a.indexOf('detected') >= 0 || a.indexOf('attempted') >= 0 || a.indexOf('restart') >= 0) return 'text-amber-700';
  if(a.indexOf('success') >= 0 || a.indexOf('user_chose_restart') >= 0) return 'text-green-700';
  if(a.indexOf('skipped') >= 0 || a.indexOf('dismissed') >= 0) return 'text-stone-500';
  return 'text-stone-700';
}
async function loadWatchdogTab(){
  const cfgEl = document.getElementById('watchdog-tab-config');
  const evEl = document.getElementById('watchdog-tab-events');
  if(!cfgEl || !evEl) return;
  try{
    const r = await fetch('/api/watchdog');
    if(!r.ok){evEl.innerHTML = `<div class="text-red-700">HTTP ${r.status}</div>`;return;}
    const d = await r.json();
    const cfg = d.config || {};
    cfgEl.innerHTML = `
<label class="flex items-center justify-between gap-3 p-3 rounded-lg border border-stone-200 cursor-pointer hover:bg-stone-50">
  <div>
    <div class="font-medium text-sm">${tr('watchdog_dc_auto_label')}</div>
    <div class="text-xs text-stone-500 mt-0.5">${tr('watchdog_dc_auto_help')}</div>
  </div>
  <input type="checkbox" ${cfg.dc_auto_remediation?'checked':''} onchange="updateWatchdog({dc_auto_remediation:this.checked})" class="w-5 h-5 rounded accent-green-700 shrink-0"/>
</label>
<div class="grid grid-cols-1 md:grid-cols-3 gap-2 text-xs ${cfg.dc_auto_remediation?'':'opacity-60'}">
  <label class="block"><span class="block text-stone-500 mb-1">${tr('watchdog_dc_threshold_label')}</span>
    <input type="number" min="60" value="${cfg.dc_inactivity_threshold_seconds||120}" ${cfg.dc_auto_remediation?'':'disabled'} onchange="updateWatchdog({dc_inactivity_threshold_seconds:parseInt(this.value)||120})" class="w-full px-2 py-1 border border-stone-200 rounded"/>
  </label>
  <label class="block"><span class="block text-stone-500 mb-1">${tr('watchdog_dc_verify_label')}</span>
    <input type="number" min="10" value="${cfg.dc_verify_after_toggle_seconds||30}" ${cfg.dc_auto_remediation?'':'disabled'} onchange="updateWatchdog({dc_verify_after_toggle_seconds:parseInt(this.value)||30})" class="w-full px-2 py-1 border border-stone-200 rounded"/>
  </label>
  <label class="block"><span class="block text-stone-500 mb-1">${tr('watchdog_dc_cooldown_label')}</span>
    <input type="number" min="120" value="${cfg.dc_cooldown_after_dismiss_seconds||300}" ${cfg.dc_auto_remediation?'':'disabled'} onchange="updateWatchdog({dc_cooldown_after_dismiss_seconds:parseInt(this.value)||300})" class="w-full px-2 py-1 border border-stone-200 rounded"/>
  </label>
</div>`;
    const events = (d.events || []).slice(0, 50);
    if(events.length === 0){
      evEl.innerHTML = `<div class="text-stone-400 italic font-sans">${tr('watchdog_no_events')}</div>`;
    } else {
      evEl.innerHTML = events.map(ev=>{
        const color = _watchdogEventColor(ev.action);
        return `<div class="flex gap-2 py-0.5 border-b border-stone-100"><span class="text-stone-400 shrink-0">${escAttr((ev.ts||'').slice(11,19))}</span><span class="${color} shrink-0 font-semibold">${escAttr(ev.action||'')}</span><span class="text-stone-600 truncate" title="${escAttr(ev.detail||'')}">${escAttr(ev.detail||'')}</span></div>`;
      }).join('');
    }
  }catch(e){console.error(e); evEl.innerHTML = `<div class="text-red-700 font-sans">${escAttr(String(e))}</div>`;}
}
async function saveWatchdogPattern(){
  const p = document.getElementById('watchdog-pattern').value.trim();
  await updateWatchdog({target_pattern: p});
}
async function testWatchdogPattern(){
  const p = document.getElementById('watchdog-pattern').value.trim();
  const out = document.getElementById('watchdog-scan-result');
  if(!p || p.length < 2){out.innerHTML = `<span class="text-red-700">${tr('banner_pattern_too_short')}</span>`;return;}
  out.innerHTML = `<span class="text-stone-500">${tr('scanning')}</span>`;
  const j = await api('/api/scan-process', {pattern:p});
  if(j.error){out.innerHTML = `<span class="text-red-700">${escAttr(j.error)}</span>`;return;}
  if(!j.matches || j.matches.length===0){
    out.innerHTML = `<div class="text-stone-600 italic">${tr('scan_no_match').split('{p}').join(escAttr(p))}</div>`;
    return;
  }
  out.innerHTML = `<div class="mb-1 text-stone-700">${tr('scan_n_matches').split('{n}').join(j.matches.length).split('{p}').join(escAttr(p))}</div>` +
    j.matches.slice(0,8).map(m=>`<div class="font-mono text-[11px] text-stone-500 truncate" title="${escAttr(m.cmd)}"><span class="text-stone-700 font-semibold mr-2">${m.pid}</span>${escAttr(m.cmd.substring(0,180))}</div>`).join('');
}
function toggleDiagExt(){
  const body = document.getElementById('diag-ext-body');
  const caret = document.getElementById('diag-ext-caret');
  if(!body || !caret) return;
  const opening = body.classList.contains('hidden');
  body.classList.toggle('hidden');
  caret.innerHTML = opening ? '&#9662;' : '&#9656;';
  if(opening) loadDiagExt();
}
async function loadDiagExt(){
  const out = document.getElementById('diag-ext-content');
  if(!out) return;
  out.innerHTML = `<div class="text-stone-500">${tr('mcp_diag_loading')}</div>`;
  try{
    const r = await fetch('/api/diagnose-extensions');
    if(!r.ok){ out.innerHTML = `<div class="text-red-700">HTTP ${r.status}</div>`; return; }
    const d = await r.json();
    const exts = d.extensions || [];
    const summary = d.summary || {total:0, with_warnings:0};
    if(exts.length === 0){
      out.innerHTML = `<div class="text-stone-500">${tr('mcp_diag_all_ok')}</div>`;
      return;
    }
    if(summary.with_warnings === 0){
      out.innerHTML = `<div class="p-3 rounded-lg bg-green-50 border border-green-200 text-sm text-green-800">${tr('mcp_diag_all_ok')}</div>`;
      return;
    }
    const head = `<div class="text-xs text-stone-600 mb-3">${tr('mcp_diag_with_warnings').split('{n}').join(summary.with_warnings).split('{t}').join(summary.total)}</div>`;
    const rows = exts.map(e=>{
      const status = !e.enabled
        ? `<span class="text-xs text-stone-500 bg-stone-100 px-2 py-0.5 rounded-full">${tr('mcp_diag_status_off')}</span>`
        : (e.warnings.length === 0
            ? `<span class="text-xs text-green-700 bg-green-50 px-2 py-0.5 rounded-full">${tr('mcp_diag_status_ok')}</span>`
            : `<span class="text-xs text-amber-700 bg-amber-50 px-2 py-0.5 rounded-full">${tr('mcp_diag_status_warn')}</span>`);
      const warns = e.warnings.length === 0
        ? '<span class="text-stone-300">&mdash;</span>'
        : e.warnings.map(w=>`<div class="text-xs text-amber-800">${tr('mcp_diag_warn_'+w) || w}</div>`).join('');
      // v1.8.3 Bug E - extension cochee mais jamais demarree : on affiche
      // sous les warnings les lignes pertinentes de main.log si scan a
      // trouve quelque chose (allowlist, error, blocked, ...).
      const hints = (e.main_log_hints && e.main_log_hints.length)
        ? `<div class="mt-1 p-2 bg-red-50 border border-red-200 rounded text-[11px] font-mono text-red-800 leading-tight">${e.main_log_hints.map(l=>`<div class="truncate" title="${escAttr(l)}">${escAttr(l)}</div>`).join('')}</div>`
        : '';
      const meta = `<div class="text-[10px] text-stone-400 font-mono mt-0.5">${escAttr(e.id)}${e.log_match_method && e.log_match_method !== 'none' ? ' &middot; '+escAttr(e.log_match_method) : ''}${e.pid_method && e.pid_method !== 'none' ? ' &middot; '+escAttr(e.pid_method) : ''}</div>`;
      return `<tr class="border-t border-stone-100"><td class="py-2 pr-3 align-top"><div class="text-sm font-medium text-stone-800">${escAttr(e.name)}</div>${meta}</td><td class="py-2 pr-3 align-top">${status}</td><td class="py-2 align-top">${warns}${hints}</td></tr>`;
    }).join('');
    out.innerHTML = head + `<table class="w-full text-sm"><thead><tr class="text-xs uppercase tracking-wide text-stone-500"><th class="text-left pb-2 pr-3">${tr('mcp_diag_col_name')}</th><th class="text-left pb-2 pr-3">${tr('mcp_diag_col_status')}</th><th class="text-left pb-2">${tr('mcp_diag_col_warnings')}</th></tr></thead><tbody>${rows}</tbody></table>`;
  } catch(e){
    out.innerHTML = `<div class="text-red-700">${escAttr(String(e))}</div>`;
  }
}
async function loadSkillSuggestions(){
  try{
    const r = await fetch('/api/skill-suggestions');
    if(!r.ok) return;
    const d = await r.json();
    const el = document.getElementById('overview-suggestions');
    if(!el) return;
    const sugs = d.suggestions || [];
    if(sugs.length===0){el.innerHTML = '';return;}
    const msgKey = CURRENT_LANG === 'en' ? 'message_en' : 'message_fr';
    el.innerHTML = `<div class="text-xs font-semibold uppercase tracking-wide text-stone-600 mb-1">${tr('skill_suggestions')}${d.fallback ? ' <span class="text-stone-400 font-normal normal-case">&middot; '+tr('fallback_no_usage')+'</span>' : ''}</div><div class="space-y-1.5">` +
      sugs.map(s=>{
        const color = s.severity === 'warn' ? 'bg-amber-50 border-amber-200 text-amber-800' : 'bg-stone-50 border-stone-200 text-stone-700';
        const icon = s.severity === 'warn' ? '&#9888;' : '&#8505;';
        const items = (s.items || []).slice(0,8).map(n=>`<span class="text-[10px] font-mono bg-white px-1.5 py-0.5 rounded border border-stone-200">${escAttr(n)}</span>`).join(' ');
        // v1.7.4 - bouton d'action quand le suggestion est actionable cote backend.
        // Pour l'instant : kind in {duplicate, duplicate_many} -> bulk delete des
        // versions utilisateur en doublon avec un plugin (backup zip individuel).
        let actionBtn = '';
        if (s.kind === 'duplicate' || s.kind === 'duplicate_many'){
          const n = (s.items || []).length;
          actionBtn = `<div class="mt-2"><button onclick="cleanupDuplicateUserSkills()" class="inline-flex items-center gap-1 text-xs font-medium text-amber-800 bg-white border border-amber-300 hover:bg-amber-100 rounded-md px-2.5 py-1">&#x1f9f9; ${tr('cleanup_duplicates_btn')}</button></div>`;
        }
        return `<div class="text-xs p-2 rounded border ${color}"><div class="flex gap-2 items-start"><span class="shrink-0">${icon}</span><div class="flex-1"><div>${escAttr(s[msgKey] || '')}</div>${items ? '<div class="mt-1 flex flex-wrap gap-1">'+items+'</div>' : ''}${actionBtn}</div></div></div>`;
      }).join('') + '</div>';
  }catch(e){console.error(e);}
}
async function cleanupDuplicateUserSkills(){
  if(!confirm(tr('confirm_cleanup_duplicates'))) return;
  banner('blue', tr('banner_cleanup_running'));
  const j = await api('/api/delete-user-skill-duplicates', {});
  banner(j.success ? 'green' : 'red', j.message || (j.success ? 'OK' : 'Echec'));
  if(j.success){ loadOverview(); loadSkillSuggestions(); loadState(); }
}
async function loadOverview(){
  try{
    const r = await fetch('/api/overview');
    if(!r.ok) return;
    const o = await r.json();
    const s = o.stats || {};
    const h = o.health || {};
    SKILL_USAGE = {};
    (o.top_skills || []).forEach(t=>{SKILL_USAGE[t.name] = t.count;});
    const stats = document.getElementById('overview-stats');
    if(stats){
      stats.innerHTML =
        statBox(`${s.mcps_running}/${s.mcps_active}`, tr('stat_mcps_running'), s.mcps_failing>0?'border-amber-200 bg-amber-50':'border-green-200 bg-green-50') +
        // v1.10.2 - 'Skills prets' (quality excellent) au lieu de 'Skills
        // actifs' (toutes les skills enabled). Coherent avec le Health
        // banner de la tab Skills. Border ambre si quelques broken/enrich
        // a corriger.
        statBox(`${s.skills_excellent||0}/${s.skills_total}`, tr('stat_skills'),
                ((s.skills_broken||0)+(s.skills_enrich||0)) > 0
                  ? 'border-amber-200 bg-amber-50'
                  : 'border-green-200 bg-green-50') +
        statBox(`${s.plugins_enabled}/${s.plugins_total}`, tr('stat_plugins'), 'border-stone-200 bg-stone-50') +
        statBox(`${s.commands_user}+${s.commands_total - s.commands_user}`, tr('stat_commands'), 'border-stone-200 bg-stone-50');
    }
    const presetEl = document.getElementById('overview-preset');
    if(presetEl){presetEl.textContent = o.active_preset ? `${tr('active_preset')} : ${o.active_preset}` : (o.presets_count>0 ? `${o.presets_count} ${tr('presets_label')}` : '');}
    const topEl = document.getElementById('overview-top-skills');
    if(topEl){
      const top = o.top_skills || [];
      const meta = o.usage_meta || {};
      if(top.length){
        const items = top.map(t=>`<button onclick="document.getElementById('skills-search').value='${escAttr(t.name)}';filterSkills();document.getElementById('skills').scrollIntoView({behavior:'smooth'});" class="text-xs px-2 py-0.5 rounded-full bg-green-50 text-green-800 border border-green-200 hover:bg-green-100 font-mono">${escAttr(t.name)} <span class="text-green-700 font-semibold">${t.count}×</span></button>`).join(' ');
        topEl.innerHTML = `<div class="text-xs font-semibold uppercase tracking-wide text-stone-600 mb-1">${tr('top_skills_30d')} <span class="text-stone-400 font-normal normal-case">&middot; ${meta.sessions||0} ${tr('sessions')} / ${meta.files_scanned||0} ${tr('files')}</span></div><div class="flex flex-wrap gap-1.5">${items}</div>`;
      }else if(meta.ok){
        topEl.innerHTML = `<div class="text-xs text-stone-400">${tr('no_skill_usage_yet').split('{n}').join(meta.files_scanned||0)}</div>`;
      }else{
        topEl.innerHTML = '';
      }
    }
    loadSkillSuggestions();
    const healthEl = document.getElementById('overview-health');
    if(healthEl){
      const issues = [];
      if(h.mcps_failing && h.mcps_failing.length){
        issues.push(`<div class="flex items-center gap-2 text-xs p-2 rounded bg-amber-50 border border-amber-200 text-amber-800"><span>&#9888;</span><span><strong>${h.mcps_failing.length}</strong> ${tr('issue_mcps_not_running')} : ${h.mcps_failing.map(n=>`<button onclick="showMcpError('${escAttr(n)}')" class="underline hover:no-underline font-mono">${escAttr(n)}</button>`).join(', ')}</span></div>`);
      }
      if(h.plugin_orphans && h.plugin_orphans.length){
        issues.push(`<div class="flex items-center gap-2 text-xs p-2 rounded text-white" style="background:linear-gradient(135deg,#D97757,#C15F3C)"><span>&#9888;</span><span><strong>${h.plugin_orphans.length}</strong> ${tr('issue_orphans')} : ${h.plugin_orphans.map(o=>`${escAttr(o.plugin)} v${escAttr(o.version)}`).join(', ')}</span></div>`);
      }
      // v1.7.5 - duplicate_names retire de health (etait du bruit sans action).
      // Les doublons sont maintenant exclusivement remontes via les suggestions
      // skill (kind:duplicate / duplicate_many) qui portent le bouton de
      // cleanup. Idem skill_issues, redondant avec le suggestion no_description.
      healthEl.innerHTML = issues.length ? `<div class="space-y-2">${issues.join('')}</div>` : `<div class="text-xs text-green-700 bg-green-50 border border-green-200 rounded p-2 flex items-center gap-2"><span>&#9989;</span><span>${tr('health_all_good')}</span></div>`;
    }
  }catch(e){console.error(e);}
}
async function loadSettings(){
  try{
    const r = await fetch('/api/settings');
    if(!r.ok) return;
    const d = await r.json();
    const ta = document.getElementById('settings-textarea');
    if(!ta) return;
    ta.value = d.content || '{}';
    validateSettingsLive();
  }catch(e){console.error(e);}
}
function validateSettingsLive(){
  const ta = document.getElementById('settings-textarea');
  const status = document.getElementById('settings-status');
  if(!ta || !status) return;
  if(!ta.value.trim()){status.textContent = ''; ta.classList.remove('border-red-300'); return;}
  try{JSON.parse(ta.value); status.textContent = '✓ ' + tr('json_valid'); status.className='text-xs text-green-700'; ta.classList.remove('border-red-300');}
  catch(e){status.textContent = '✗ ' + e.message; status.className='text-xs text-red-700'; ta.classList.add('border-red-300');}
}
async function saveSettings(){
  const ta = document.getElementById('settings-textarea');
  try{JSON.parse(ta.value);}catch(e){banner('red', tr('json_invalid')+' : '+e.message); return;}
  const j = await api('/api/save-settings', {content: ta.value});
  banner(j.success?'green':'red', j.message);
  if(j.success){loadPlugins();}
}
async function loadClaudeMd(){
  try{
    const r = await fetch('/api/claude-md');
    if(!r.ok) return;
    const d = await r.json();
    const ta = document.getElementById('claudemd-textarea');
    if(!ta) return;
    ta.value = d.content || '';
    document.getElementById('claudemd-count').textContent = (d.content || '').length;
    ta.oninput = ()=>{document.getElementById('claudemd-count').textContent = ta.value.length;};
  }catch(e){console.error(e);}
}
async function saveClaudeMd(){
  const ta = document.getElementById('claudemd-textarea');
  const j = await api('/api/save-claude-md', {content: ta.value});
  banner(j.success?'green':'red', j.message);
}
async function loadCommands(){
  try{
    const j = await (await fetch('/api/commands')).json();
    const commands = j.commands || [];
    const list = document.getElementById('commands');
    if(!list) return;
    if(commands.length===0){list.innerHTML = `<p class="text-xs text-stone-400">${tr('commands_empty')}</p>`;return;}
    list.innerHTML = commands.map(c=>{
      const ne = escAttr(c.name);
      const sourceLabel = c.source==='user' ? tr('source_user') : c.source.replace(/^plugin:/, tr('source_plugin')+' ');
      const actions = c.editable
        ? `<input type="checkbox" ${c.active?'checked':''} onchange="toggleCommand('${ne}')" class="w-5 h-5 rounded accent-green-700" title="${tr('toggle_command_title')}"><button onclick="editCommand('${ne}','${escAttr(c.source)}')" class="text-xs text-stone-700 hover:text-stone-900 font-medium">${tr('btn_edit')}</button>`
        : `<button onclick="editCommand('${ne}','${escAttr(c.source)}')" class="text-xs text-stone-500 hover:text-stone-700 font-medium">${tr('btn_view')}</button>`;
      const opacity = c.active ? '' : 'opacity-60';
      return `<div class="flex items-center justify-between gap-3 p-3 rounded-lg border border-stone-200 ${opacity}">
<div class="flex flex-col flex-1 min-w-0">
<div class="flex items-baseline gap-2 flex-wrap">
<span class="font-medium text-sm">/${ne}</span>
<span class="text-xs text-stone-400">${escAttr(sourceLabel)}</span>
${c.editable ? '' : `<span class="text-xs text-stone-400" data-i18n="readonly">${tr('readonly')}</span>`}
</div>
</div>
<div class="flex items-center gap-2 shrink-0">${actions}</div>
</div>`;
    }).join('');
  }catch(e){console.error(e);}
}
async function toggleCommand(name){
  const j = await api('/api/toggle-command', {name:name});
  banner(j.success?'green':'red', j.message);
  if(j.success) loadCommands();
}
async function editCommand(name, source){
  try{
    const r = await fetch('/api/command/' + encodeURIComponent(name) + '?source=' + encodeURIComponent(source));
    if(!r.ok){banner('red', tr('command_not_found'));return;}
    const d = await r.json();
    document.getElementById('cmd-edit-name').textContent = '/' + d.name;
    document.getElementById('cmd-edit-source').textContent = d.source==='user' ? tr('source_user') : d.source.replace(/^plugin:/, tr('source_plugin')+' ');
    document.getElementById('cmd-edit-content').value = d.content || '';
    document.getElementById('cmd-edit-content').readOnly = !d.editable;
    document.getElementById('cmd-edit-save').classList.toggle('hidden', !d.editable);
    document.getElementById('cmd-edit-name-input').value = d.name;
    document.getElementById('cmd-edit-modal').classList.remove('hidden');
  }catch(e){banner('red', tr('net_error') + e);}
}
function closeCmdEdit(){document.getElementById('cmd-edit-modal').classList.add('hidden');}
async function saveCommand(){
  const name = document.getElementById('cmd-edit-name-input').value.trim();
  const content = document.getElementById('cmd-edit-content').value;
  if(!name){banner('red', tr('banner_name_required'));return;}
  const j = await api('/api/save-command', {name:name, content:content});
  banner(j.success?'green':'red', j.message);
  if(j.success){closeCmdEdit();loadCommands();}
}
async function loadPresets(){
  try{
    const j = await (await fetch('/api/presets')).json();
    const presets = j.presets || {};
    const names = Object.keys(presets).sort();
    const list = document.getElementById('presets-list');
    if(names.length===0){
      list.innerHTML = `<p class="text-xs text-stone-400">${tr('preset_empty')}</p>`;
      return;
    }
    const deleteTitle = CURRENT_LANG === 'en' ? 'Delete' : 'Supprimer';
    list.innerHTML = names.map(n=>{
      const count = (presets[n]||[]).length;
      const ne = escAttr(n);
      return `<div class="flex items-center justify-between gap-2 p-2 bg-white border border-stone-200 rounded-md"><div class="flex-1 min-w-0"><div class="text-sm font-medium truncate">${ne}</div><div class="text-xs text-stone-500">${count} MCP${count>1?'s':''}</div></div><button onclick="applyPreset('${ne}')" class="text-xs px-2.5 py-1 rounded-md bg-stone-900 hover:bg-stone-800 text-white font-medium">Apply</button><button onclick="deletePreset('${ne}')" title="${deleteTitle}" class="text-stone-400 hover:text-red-600 text-lg leading-none px-1">&times;</button></div>`;
    }).join('');
  }catch(e){}
}
async function checkUpdate(){
  try{
    const u = await (await fetch('/api/check-update')).json();
    document.getElementById('version').textContent = 'v' + (u.local || '?');
    if(u.update_available){
      document.getElementById('update-banner').classList.remove('hidden');
      document.getElementById('update-text').textContent = tr('update_label_with_v') + u.latest;
    }
  }catch(e){}
}
function setTab(g, sub){
  const p = g==='mcp' ? 'mcp' : 'sk';
  document.querySelectorAll(`[data-tab^="${p}-"]`).forEach(b => b.classList.toggle('active', b.dataset.tab === `${p}-${sub}`));
  document.querySelectorAll(`[data-pane^="${p}-"]`).forEach(pn => pn.classList.toggle('hidden', pn.dataset.pane !== `${p}-${sub}`));
}
async function api(path, body){
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body||{})});
  return r.json();
}
async function toggleMcp(n){const j=await api('/api/toggle-mcp',{name:n});banner(j.success?'green':'red',j.message);loadState();}
async function toggleExtension(name, enabled){const j=await api('/api/toggle-extension',{name:name, enabled:enabled});banner(j.success?'green':'red',j.message);loadState();}
let CURRENT_MCP_ERR = null;
async function showMcpError(name){
  CURRENT_MCP_ERR = name;
  document.getElementById('mcp-err-name').textContent = name;
  ['mcp-err-known-wrap','mcp-err-unknown-wrap','mcp-err-nolog-wrap','mcp-err-log-section','mcp-err-test-section','mcp-err-envfix'].forEach(id=>document.getElementById(id).classList.add('hidden'));
  document.getElementById('mcp-err-test-btn').disabled = false;
  document.getElementById('mcp-err-test-btn').textContent = tr('btn_test_now');
  document.getElementById('mcp-error-modal').classList.remove('hidden');
  try{
    const r = await fetch('/api/mcp-error/' + encodeURIComponent(name) + '?lang=' + CURRENT_LANG);
    const d = await r.json();
    if(d.error){
      document.getElementById('mcp-err-content').textContent = d.error;
      document.getElementById('mcp-err-log-path').textContent = d.log_path || '';
      document.getElementById('mcp-err-log-section').classList.remove('hidden');
    }
    if(d.suggestion){
      document.getElementById('mcp-err-known').textContent = d.suggestion;
      document.getElementById('mcp-err-known-wrap').classList.remove('hidden');
    }else if(d.error){
      document.getElementById('mcp-err-unknown-wrap').classList.remove('hidden');
    }else{
      document.getElementById('mcp-err-nolog-wrap').classList.remove('hidden');
    }
  }catch(e){
    document.getElementById('mcp-err-content').textContent = tr('net_error') + e;
    document.getElementById('mcp-err-log-section').classList.remove('hidden');
  }
}
function closeMcpError(){document.getElementById('mcp-error-modal').classList.add('hidden');CURRENT_MCP_ERR=null;}
async function runMcpTest(){
  if(!CURRENT_MCP_ERR) return;
  const btn = document.getElementById('mcp-err-test-btn');
  btn.disabled = true; btn.textContent = tr('btn_testing');
  document.getElementById('mcp-err-envfix').classList.add('hidden');
  try{
    const r = await fetch('/api/mcp-test', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name: CURRENT_MCP_ERR, lang: CURRENT_LANG})});
    const d = await r.json();
    if(!d.success){banner('red', d.message || tr('banner_test_failed'));return;}
    const summary = document.getElementById('mcp-err-test-summary');
    let badge, color;
    if(d.kind === 'handshake_ok'){badge=tr('badge_handshake_ok'); color='bg-green-50 text-green-800 border border-green-200';}
    else if(d.kind === 'handshake_error'){badge=tr('badge_handshake_error'); color='bg-amber-50 text-amber-800 border border-amber-200';}
    else if(d.kind === 'no_response'){badge=tr('badge_no_response'); color='bg-amber-50 text-amber-800 border border-amber-200';}
    else if(d.exit_code !== null && d.exit_code !== 0){badge=tr('badge_exit_code') + d.exit_code; color='bg-red-50 text-red-800 border border-red-200';}
    else if(d.exit_code === 0){badge=tr('badge_exit_zero'); color='bg-red-50 text-red-800 border border-red-200';}
    else {badge=tr('badge_unknown'); color='bg-stone-50 text-stone-700 border border-stone-200';}
    summary.className = 'text-sm mb-2 p-2 rounded-lg ' + color;
    summary.innerHTML = `<div class="font-semibold mb-1">${badge}</div><div>${escAttr(d.suggestion||'')}</div>`;
    document.getElementById('mcp-err-test-stderr').textContent = d.stderr || tr('empty_capture');
    document.getElementById('mcp-err-test-stdout').textContent = d.stdout || tr('empty_capture');
    document.getElementById('mcp-err-test-section').classList.remove('hidden');
    if(d.missing_env_var){
      document.getElementById('mcp-envfix-var').textContent = d.missing_env_var;
      document.getElementById('mcp-envfix-value').value = '';
      document.getElementById('mcp-err-envfix').classList.remove('hidden');
      setTimeout(()=>document.getElementById('mcp-envfix-value').focus(), 50);
    }
  }catch(e){banner('red', tr('net_error') + e);}
  finally{btn.disabled=false; btn.textContent=tr('btn_retest');}
}
async function applyEnvFix(){
  if(!CURRENT_MCP_ERR) return;
  const v = document.getElementById('mcp-envfix-var').textContent;
  const val = document.getElementById('mcp-envfix-value').value;
  if(!val){banner('red', tr('banner_value_required'));return;}
  const j = await api('/api/mcp-set-env', {name: CURRENT_MCP_ERR, var: v, value: val});
  banner(j.success?'green':'red', j.message);
  if(j.success){
    document.getElementById('mcp-err-envfix').classList.add('hidden');
    runMcpTest();
  }
}
async function toggleSkill(n){const j=await api('/api/toggle-skill',{name:n});banner(j.success?'green':'red',j.message);loadState();}
async function restartClaude(){if(!confirm(tr('confirm_restart_claude')))return;banner('blue', tr('banner_restarting'));const j=await api('/api/restart-claude');banner(j.success?'green':'red',j.message);setTimeout(loadState,4000);}
async function restartSelf(){if(!confirm(tr('confirm_restart_self')))return;banner('blue', tr('banner_restarting'));try{await api('/api/restart-self');}catch(e){}setTimeout(()=>{banner('green', tr('banner_reconnecting'));location.reload();}, 1500);}
async function applyUpdate(){if(!confirm(tr('confirm_apply_update')))return;banner('blue', tr('banner_updating'));try{const j=await api('/api/apply-update');if(!j.success){banner('red',j.message);return;}banner('green',j.message);}catch(e){}setTimeout(()=>{banner('blue', tr('banner_reconnecting'));location.reload();}, 2500);}
async function addMcpJson(){const v=document.getElementById('mcp-json-in').value.trim();if(!v)return;const j=await api('/api/import-mcp-json',{json:v});banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('mcp-json-in').value='';loadState();}}
async function addMcpFile(){const v=document.getElementById('mcp-file-in').value.trim();if(!v)return;const j=await api('/api/import-mcp-file',{path:v});banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('mcp-file-in').value='';loadState();}}
async function addMcpGit(){const v=document.getElementById('mcp-git-in').value.trim();if(!v)return;banner('blue', tr('banner_cloning'));const j=await api('/api/import-mcp-git',{url:v});banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('mcp-git-in').value='';loadState();}}
async function addSkillFolder(){const v=document.getElementById('sk-folder-in').value.trim();if(!v)return;const j=await api('/api/import-skill-folder',{path:v});banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('sk-folder-in').value='';loadState();}}
async function addSkillGit(){const v=document.getElementById('sk-git-in').value.trim();if(!v)return;banner('blue', tr('banner_cloning'));const j=await api('/api/import-skill-git',{url:v});banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('sk-git-in').value='';loadState();}}
async function addSkillMd(){const n=document.getElementById('sk-md-name').value.trim();const c=document.getElementById('sk-md-content').value;if(!n||!c)return;const j=await api('/api/import-skill-markdown',{name:n,content:c});banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('sk-md-name').value='';document.getElementById('sk-md-content').value='';loadState();}}
async function uploadZip(path, inputId){
  const inp = document.getElementById(inputId);
  const f = inp.files && inp.files[0];
  if(!f){banner('red', tr('banner_zip_required'));return null;}
  banner('blue', tr('banner_uploading'));
  try{
    const r = await fetch(path, {method:'POST', headers:{'X-Filename': encodeURIComponent(f.name)}, body: f});
    return await r.json();
  }catch(e){return {success:false, message: tr('banner_upload_failed') + e};}
}
async function addMcpZip(){const j=await uploadZip('/api/import-mcp-zip','mcp-zip-in');if(!j)return;banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('mcp-zip-in').value='';loadState();}}
async function addSkillZip(){const j=await uploadZip('/api/import-skill-zip','sk-zip-in');if(!j)return;banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('sk-zip-in').value='';loadState();}}
function openSavePreset(){
  const active = (CURRENT_STATE.mcps||[]).filter(m=>m.active).map(m=>m.name);
  document.getElementById('preset-modal-info').textContent = active.length===0 ? tr('no_active_mcp') : `${tr('active_mcps_label')} (${active.length}) : ${active.join(', ')}`;
  document.getElementById('preset-name-in').value='';
  document.getElementById('preset-modal').classList.remove('hidden');
  setTimeout(()=>document.getElementById('preset-name-in').focus(),50);
}
function closeSavePreset(){document.getElementById('preset-modal').classList.add('hidden');}
async function confirmSavePreset(){
  const name = document.getElementById('preset-name-in').value.trim();
  if(!name){banner('red', tr('banner_name_required'));return;}
  const active = (CURRENT_STATE.mcps||[]).filter(m=>m.active).map(m=>m.name);
  const j = await api('/api/preset-save',{name:name, mcps:active});
  banner(j.success?'green':'red', j.message);
  if(j.success){closeSavePreset();loadPresets();}
}
async function applyPreset(name){
  const msg = CURRENT_LANG === 'en'
    ? `Apply preset "${name}"?\n\nMCPs not listed will be disabled.`
    : `Appliquer le preset « ${name} » ?\n\nLes MCPs non listés seront désactivés.`;
  if(!confirm(msg))return;
  const j = await api('/api/preset-apply',{name:name});
  banner(j.success?'green':'red', j.message);
  if(j.success){loadState();}
}
async function deletePreset(name){
  const msg = CURRENT_LANG === 'en' ? `Delete preset "${name}"?` : `Supprimer le preset « ${name} » ?`;
  if(!confirm(msg))return;
  const j = await api('/api/preset-delete',{name:name});
  banner(j.success?'green':'red', j.message);
  if(j.success){loadPresets();}
}
document.addEventListener('keydown', e=>{
  if(e.key==='Escape'){closeSavePreset();closeMcpError();closeAddPlugin();closeCmdEdit();}
  if(e.key==='Enter' && !document.getElementById('preset-modal').classList.contains('hidden') && document.activeElement.id==='preset-name-in'){e.preventDefault();confirmSavePreset();}
});
function banner(c,m){const b=document.getElementById('banner');const cls={green:'bg-green-50 text-green-800 border-green-200',red:'bg-red-50 text-red-800 border-red-200',blue:'bg-blue-50 text-blue-800 border-blue-200'};b.className='mb-4 p-3 rounded-lg text-sm border '+cls[c];b.textContent=m;b.classList.remove('hidden');setTimeout(()=>b.classList.add('hidden'),4500);}
applyLang(CURRENT_LANG);setMainTab(CURRENT_MAIN_TAB);loadOverview();loadState();loadPresets();loadPlugins();loadCommands();loadClaudeMd();loadSettings();loadWatchdog();loadWatchdogTab();checkUpdate();setInterval(loadOverview,10000);setInterval(loadState,5000);setInterval(loadPlugins,15000);setInterval(loadCommands,30000);setInterval(loadWatchdog,10000);setInterval(loadWatchdogTab,10000);setInterval(checkUpdate,3600000);
</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/favicon.ico":
            # v1.7.2 - fallback pour Safari et navigateurs qui ignorent le
            # <link rel="icon"> et bypass-fetch /favicon.ico. On sert le meme
            # SVG (Content-Type image/svg+xml) ; les navigateurs modernes
            # acceptent ca, ICO strict est rare et ne justifie pas un binaire
            # commit dans le repo (philosophie stdlib-only).
            body = FAVICON_SVG.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            self._json(get_state())
        elif path == "/api/check-update":
            self._json(check_update())
        elif path == "/api/presets":
            self._json({"presets": list_presets()})
        elif path == "/api/plugins":
            self._json({"plugins": list_plugins()})
        elif path.startswith("/api/plugin-detail/"):
            full_name = unquote(path[len("/api/plugin-detail/"):])
            ok, payload = get_plugin_detail(full_name)
            if ok:
                self._json(payload)
            else:
                self._json({"error": payload}, status=404)
        elif path.startswith("/api/mcp-error/"):
            name = unquote(path[len("/api/mcp-error/"):])
            qs = parse_qs(urlparse(self.path).query)
            lang = (qs.get("lang", ["fr"])[0] or "fr").lower()
            if lang not in ("fr", "en"): lang = "fr"
            self._json(read_mcp_error(name, lang))
        elif path == "/api/commands":
            self._json({"commands": list_commands()})
        elif path == "/api/claude-md":
            self._json(read_claude_md())
        elif path == "/api/settings":
            self._json(read_settings_raw())
        elif path == "/api/overview":
            self._json(get_overview())
        elif path == "/api/skill-suggestions":
            self._json(skill_optimization_suggestions())
        elif path.startswith("/api/skill-content/"):
            # v1.9.0 - lecture du contenu SKILL.md pour la modal de reparation
            sk_name = unquote(path[len("/api/skill-content/"):])
            ok, payload = _read_skill_content(sk_name)
            if ok:
                self._json(payload)
            else:
                self._json({"error": payload}, status=404)
        elif path == "/api/suggest-source-status":
            # v1.9.3 - savoir si la source de suggestion est dispo (Claude CLI)
            cli = _claude_cli_path()
            self._json({"available": bool(cli), "source": "claude_cli" if cli else None,
                        "path": cli or None})
        elif path == "/api/claude-cli-diagnose":
            # v1.9.4 - diag complet (path + version + sanity check) pour
            # debug quand le CLI exit avec une erreur opaque.
            self._json(_diagnose_claude_cli())
        elif path == "/api/watchdog":
            self._json(get_watchdog_status())
        elif path == "/api/diagnose-extensions":
            self._json(diagnose_extensions())
        elif path == "/api/mcp-conflicts":
            self._json({"conflicts": _detect_mcp_conflicts()})
        elif path == "/api/dc-status":
            self._json(dc_status() or {"installed": False})
        elif path.startswith("/api/command/"):
            qs = parse_qs(urlparse(self.path).query)
            source = qs.get("source", ["user"])[0]
            name = unquote(path[len("/api/command/"):])
            ok, payload = get_command(name, source)
            if ok:
                self._json(payload)
            else:
                self._json({"error": payload}, status=404)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        if path in ("/api/import-skill-zip", "/api/import-mcp-zip"):
            if length > MAX_ZIP_SIZE:
                self._json({"success": False, "message": f"Trop volumineux (max {MAX_ZIP_SIZE // 1024 // 1024} Mo)"}); return
            try:
                blob = self.rfile.read(length) if length else b""
                filename = unquote(self.headers.get("X-Filename", ""))
                fn = import_skill_zip if path == "/api/import-skill-zip" else import_mcp_zip
                ok, msg = fn(blob, filename)
            except Exception as e:
                ok, msg = False, f"Erreur serveur : {e}"
            self._json({"success": ok, "message": msg}); return
        try:
            data = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            data = {}
        routes = {
            "/api/toggle-mcp": lambda: toggle_mcp(data.get("name", "")),
            "/api/toggle-skill": lambda: toggle_skill(data.get("name", "")),
            "/api/restart-claude": lambda: restart_claude(),
            "/api/restart-self": lambda: restart_self(),
            "/api/apply-update": lambda: _apply_update_then_restart(),
            "/api/import-mcp-json": lambda: import_mcp_json(data.get("json", "")),
            "/api/import-mcp-file": lambda: import_mcp_file(data.get("path", "")),
            "/api/import-mcp-git": lambda: import_mcp_git(data.get("url", "")),
            "/api/import-skill-folder": lambda: import_skill_folder(data.get("path", "")),
            "/api/import-skill-git": lambda: import_skill_git(data.get("url", "")),
            "/api/import-skill-markdown": lambda: import_skill_markdown(data.get("name", ""), data.get("content", "")),
            "/api/preset-save": lambda: save_preset(data.get("name", ""), data.get("mcps", [])),
            "/api/preset-apply": lambda: apply_preset(data.get("name", "")),
            "/api/preset-delete": lambda: delete_preset(data.get("name", "")),
            "/api/toggle-plugin": lambda: toggle_plugin(data.get("name", "")),
            "/api/plugin-cleanup": lambda: cleanup_plugin_orphan(data.get("name", ""), data.get("version", "")),
            "/api/mcp-test": lambda: test_mcp(data.get("name", ""), data.get("lang", "fr")),
            "/api/mcp-set-env": lambda: set_mcp_env(data.get("name", ""), data.get("var", ""), data.get("value", "")),
            "/api/toggle-command": lambda: toggle_command(data.get("name", "")),
            "/api/save-command": lambda: save_command(data.get("name", ""), data.get("content", "")),
            "/api/save-claude-md": lambda: save_claude_md(data.get("content", "")),
            "/api/save-settings": lambda: save_settings(data.get("content", "")),
            "/api/delete-skill": lambda: delete_skill(data.get("name", "")),
            "/api/repair-skill": lambda: repair_skill(data.get("name", ""), data.get("description"), data.get("name_override")),
            "/api/suggest-skill-description": lambda: suggest_skill_description(data.get("name", ""), lang=data.get("lang")),
            "/api/delete-user-skill-duplicates": lambda: delete_user_skill_duplicates(),
            "/api/delete-mcp": lambda: delete_mcp(data.get("name", "")),
            "/api/delete-extension": lambda: delete_extension(data.get("name", "")),
            "/api/resolve-mcp-conflict": lambda: resolve_mcp_conflict(data.get("name", ""), data.get("action", "remove_classic")),
            "/api/restart-mcp": lambda: restart_mcp(data.get("name", "")),
            "/api/stop-mcp": lambda: stop_mcp(data.get("name", "")),
            "/api/start-mcp": lambda: start_mcp(data.get("name", "")),
            "/api/restart-claude-desktop": lambda: restart_claude_desktop(),
            "/api/open-terminal-claude-login": lambda: open_terminal_claude_login(),
            "/api/toggle-extension": lambda: toggle_extension(data.get("name", ""), data.get("enabled")),
            "/api/delete-plugin": lambda: delete_plugin(data.get("name", ""), bool(data.get("delete_files", False))),
            "/api/add-plugin-git": lambda: add_plugin_from_git(data.get("url", "")),
            "/api/watchdog-config": lambda: save_watchdog_config(data),
            "/api/scan-process": lambda: (True, scan_processes(data.get("pattern", ""))),
        }
        if path in routes:
            try:
                ok, msg = routes[path]()
            except Exception as e:
                ok, msg = False, f"Erreur serveur : {e}"
            if isinstance(msg, dict):
                self._json({"success": ok, **msg})
            else:
                self._json({"success": ok, "message": msg})
        else:
            self.send_response(404); self.end_headers()

    def log_message(self, *args):
        return


def _show_dialog(message):
    """Show a macOS dialog via osascript. Best-effort, no-op elsewhere."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display dialog "{message}" buttons {{"OK"}} with icon stop with title "Claude Control"'],
            check=False, timeout=10,
        )
    except Exception:
        pass


def _stay_alive_for_app():
    """When the .app launcher's python would otherwise exit, sleep instead so
    macOS does not show the 'L'application n'est plus ouverte' dialog."""
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main():
    SKILLS_DISABLED_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    start_watchdog()
    print(f"\n  Claude Control v{get_local_version()} - http://localhost:{PORT}")
    print(f"  Cmd+C pour arreter\n")
    socketserver.TCPServer.allow_reuse_address = True
    try:
        server = socketserver.TCPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        if e.errno in (48, 98, 10048):
            _log(f"port {PORT} already in use, opening browser to existing instance")
            print(f"  Port {PORT} deja utilise. Ouverture du navigateur...")
            webbrowser.open(f"http://localhost:{PORT}")
            _stay_alive_for_app()
            return
        raise
    webbrowser.open(f"http://localhost:{PORT}")
    try:
        with server:
            server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Au revoir.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        tb = traceback.format_exc()
        _log("startup crash:\n" + tb)
        _show_dialog(
            f"Claude Control a crashe au demarrage.\\n\\n"
            f"Log : ~/Library/Logs/claude-control.log\\n\\n"
            f"Premiere ligne : {tb.splitlines()[-1][:120]}"
        )
        _stay_alive_for_app()
