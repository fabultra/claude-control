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


def _log(msg):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
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


def read_skill_meta(skill_dir):
    """Lit le frontmatter YAML d'un SKILL.md et retourne {category, description}."""
    meta = {"category": None, "description": None}
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
    return meta


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
    def _skill_entry(name, base, active):
        meta = read_skill_meta(base / name)
        return {"name": name, "active": active, "category": meta["category"], "description": meta["description"]}
    return {
        "mcps": [{"name": n, "active": True, "running": n in running} for n in sorted(active.keys())]
              + [{"name": n, "active": False, "running": False} for n in sorted(disabled.keys())],
        "skills": [_skill_entry(n, SKILLS_DIR, True) for n in active_skills]
                + [_skill_entry(n, SKILLS_DISABLED_DIR, False) for n in disabled_skills],
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


HTML = r"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"><title>Claude Control</title>
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
<div class="grid grid-cols-1 md:grid-cols-2 gap-6">
<section class="card p-6"><h2 class="text-lg font-semibold mb-1" data-i18n="mcp_section">Serveurs MCP</h2>
<p class="text-xs text-stone-500 mb-4" data-i18n="mcp_help">Coché = chargé au démarrage de Claude Desktop</p>
<div class="mb-4 p-3 bg-stone-50 rounded-lg border border-stone-200">
<div class="flex items-center justify-between mb-2">
<span class="text-xs font-semibold uppercase tracking-wide text-stone-600" data-i18n="presets">PRESETS</span>
<button onclick="openSavePreset()" class="text-xs text-stone-700 hover:text-stone-900 font-medium" data-i18n="preset_save_current">+ Sauvegarder l'actuel</button>
</div>
<div id="presets-list" class="space-y-1.5"></div>
</div>
<div id="mcps" class="space-y-2"></div></section>
<section class="card p-6"><h2 class="text-lg font-semibold mb-1" data-i18n="skills">Skills</h2>
<p class="text-xs text-stone-500 mb-4" data-i18n="skills_help">Coché = disponible pour Claude</p>
<input id="skills-search" type="search" oninput="filterSkills()" data-i18n-placeholder="skills_search_placeholder" placeholder="Rechercher un skill (nom ou description)..." class="w-full mb-3 p-2 border border-stone-200 rounded-lg text-sm focus:outline-none focus:border-stone-400"/>
<div id="skills" class="space-y-2 max-h-[500px] overflow-y-auto"></div></section>
</div>
<section class="card p-6 mt-6">
<div class="flex items-baseline justify-between mb-1">
<h2 class="text-lg font-semibold" data-i18n="plugins">Plugins</h2>
<span class="text-xs text-stone-400" data-i18n="plugins_meta">Lecture seule &middot; toggle persistant dans settings.json</span>
</div>
<p class="text-xs text-stone-500 mb-4" data-i18n="plugins_help">Plugins Claude Code installés via marketplace</p>
<div id="plugins" class="space-y-2"></div>
</section>
<section class="card p-6 mt-6">
<h2 class="text-lg font-semibold mb-1" data-i18n="commands">Commands</h2>
<p class="text-xs text-stone-500 mb-4" data-i18n="commands_help">Commands utilisateur (~/.claude/commands/) et fournies par les plugins actifs</p>
<div id="commands" class="space-y-2 max-h-[500px] overflow-y-auto"></div>
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
  command_not_found: "Command introuvable",
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
  command_not_found: "Command not found",
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
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el=>{const k=el.getAttribute('data-i18n-placeholder'); if(T[lang][k]!==undefined) el.placeholder = T[lang][k];});
  document.querySelectorAll('[data-i18n-title]').forEach(el=>{const k=el.getAttribute('data-i18n-title'); if(T[lang][k]!==undefined) el.title = T[lang][k];});
  ['fr','en'].forEach(l=>{const b=document.getElementById('lang-'+l); if(b){b.classList.toggle('active', l===lang);}});
  if(typeof loadState==='function') loadState();
  if(typeof loadPlugins==='function') loadPlugins();
  if(typeof loadPresets==='function') loadPresets();
  if(typeof loadCommands==='function') loadCommands();
}
function setLang(lang){applyLang(lang);}
let CURRENT_STATE = {mcps:[], skills:[]};
async function loadState(){
  const s = await (await fetch('/api/state')).json();
  CURRENT_STATE = s;
  document.getElementById('mcps').innerHTML = s.mcps.length===0 ? `<p class="text-stone-400 text-sm">${tr('no_mcp')}</p>` : s.mcps.map(m=>`<label class="flex items-center justify-between gap-3 p-3 rounded-lg hover:bg-stone-50 cursor-pointer border ${m.active?'border-stone-200':'border-stone-100 opacity-60'}"><div class="flex items-center gap-3 flex-1"><input type="checkbox" ${m.active?'checked':''} onchange="toggleMcp('${m.name}')" class="w-5 h-5 rounded accent-green-700"><span class="font-medium">${m.name}</span>${m.running?`<span class="text-xs text-green-700 bg-green-50 px-2 py-0.5 rounded-full running-dot">${tr('running_label')}</span>`:(m.active?`<button type="button" onclick="event.preventDefault();event.stopPropagation();showMcpError('${m.name}')" class="text-xs text-amber-700 bg-amber-50 hover:bg-amber-100 px-2 py-0.5 rounded-full cursor-pointer" title="${tr('why_title')}">${tr('not_started_label')}</button>`:'')}</div></label>`).join('');
  document.getElementById('skills').innerHTML = renderSkills(s.skills);
  filterSkills();
}
function escAttr(s){return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;');}
function renderSkills(skills){
  if(!skills || skills.length===0) return `<p class="text-stone-400 text-sm">${tr('no_skill')}</p>`;
  const groups = {};
  skills.forEach(sk=>{const cat = sk.category || 'Uncategorized'; (groups[cat] = groups[cat] || []).push(sk);});
  const cats = Object.keys(groups).sort((a,b)=>{if(a==='Uncategorized')return 1; if(b==='Uncategorized')return -1; return a.localeCompare(b);});
  return cats.map(cat=>{
    const items = groups[cat].map(sk=>{
      const desc = sk.description || '';
      const search = (sk.name + ' ' + desc).toLowerCase();
      const descHtml = desc ? `<span class="text-xs text-stone-500 truncate">${escAttr(desc)}</span>` : '';
      return `<label data-skill data-search="${escAttr(search)}" class="flex items-center gap-3 p-2.5 rounded-lg hover:bg-stone-50 cursor-pointer border ${sk.active?'border-stone-200':'border-stone-100 opacity-60'}"><input type="checkbox" ${sk.active?'checked':''} onchange="toggleSkill('${sk.name}')" class="w-5 h-5 rounded accent-green-700 shrink-0"><div class="flex flex-col min-w-0 flex-1"><span class="font-medium text-sm truncate">${escAttr(sk.name)}</span>${descHtml}</div></label>`;
    }).join('');
    return `<details data-skill-cat="${escAttr(cat)}" open class="mb-2"><summary class="cursor-pointer text-xs font-semibold uppercase tracking-wide text-stone-600 mb-1.5 px-1 select-none hover:text-stone-900">${escAttr(cat)} <span class="text-stone-400 font-normal normal-case" data-cat-count>(${groups[cat].length})</span></summary><div class="space-y-1.5 mt-1.5">${items}</div></details>`;
  }).join('');
}
function filterSkills(){
  const q = (document.getElementById('skills-search').value || '').trim().toLowerCase();
  const root = document.getElementById('skills');
  root.querySelectorAll('[data-skill]').forEach(el=>{
    const match = !q || (el.getAttribute('data-search')||'').includes(q);
    el.classList.toggle('hidden', !match);
  });
  root.querySelectorAll('[data-skill-cat]').forEach(d=>{
    const visible = d.querySelectorAll('[data-skill]:not(.hidden)').length;
    d.classList.toggle('hidden', visible===0);
    if(q && visible>0) d.setAttribute('open','');
    const counter = d.querySelector('[data-cat-count]');
    if(counter){
      const total = d.querySelectorAll('[data-skill]').length;
      counter.textContent = q ? `(${visible}/${total})` : `(${total})`;
    }
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
      const orphans = (p.extra_versions||[]).map(v=>`<button onclick="event.stopPropagation();cleanupOrphan('${fn}','${escAttr(v)}')" class="text-xs px-2 py-0.5 rounded-full font-medium update-badge text-white" title="Cliquer pour supprimer ce dossier orphelin">&#9888; orphan: v${escAttr(v)}</button>`).join(' ');
      return `<div class="border ${p.enabled?'border-stone-200':'border-stone-100'} rounded-lg p-3 ${opacity}">
<div class="flex items-center gap-3">
<input type="checkbox" ${p.enabled?'checked':''} onchange="togglePlugin('${fn}')" class="w-5 h-5 rounded accent-green-700 shrink-0">
<button onclick="togglePluginDetail('${fn}')" class="flex-1 text-left">
<div class="flex items-baseline gap-2 flex-wrap">
<span class="font-medium">${escAttr(p.name)}</span>
<span class="text-xs text-stone-400 font-mono">v${escAttr(p.version||'?')}</span>
<span class="text-xs text-stone-500">${escAttr(p.marketplace||'')}</span>
${orphans}
</div>
<div class="text-xs text-stone-500 mt-0.5">${pluginContentBadge(p.contents||{})}</div>
</button>
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
async function cleanupOrphan(fn, version){
  const msg = CURRENT_LANG === 'en'
    ? `Delete orphan version ${version} of "${fn}"?\n\nA ZIP backup will be created in ~/.claude/backups/claude-control/orphan-plugins/`
    : `Supprimer la version orpheline ${version} de « ${fn} » ?\n\nUn backup ZIP sera créé dans ~/.claude/backups/claude-control/orphan-plugins/`;
  if(!confirm(msg))return;
  const j = await api('/api/plugin-cleanup',{name:fn, version:version});
  banner(j.success?'green':'red',j.message);
  if(j.success){loadPlugins();}
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
  if(e.key==='Escape'){closeSavePreset();closeMcpError();}
  if(e.key==='Enter' && !document.getElementById('preset-modal').classList.contains('hidden') && document.activeElement.id==='preset-name-in'){e.preventDefault();confirmSavePreset();}
});
function banner(c,m){const b=document.getElementById('banner');const cls={green:'bg-green-50 text-green-800 border-green-200',red:'bg-red-50 text-red-800 border-red-200',blue:'bg-blue-50 text-blue-800 border-blue-200'};b.className='mb-4 p-3 rounded-lg text-sm border '+cls[c];b.textContent=m;b.classList.remove('hidden');setTimeout(()=>b.classList.add('hidden'),4500);}
applyLang(CURRENT_LANG);loadState();loadPresets();loadPlugins();loadCommands();checkUpdate();setInterval(loadState,5000);setInterval(loadPlugins,15000);setInterval(loadCommands,30000);setInterval(checkUpdate,3600000);
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
