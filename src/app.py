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
        })
    return {
        "mcps": [{"name": n, "active": True, "running": n in running} for n in sorted(active.keys())]
              + [{"name": n, "active": False, "running": False} for n in sorted(disabled.keys())],
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
    if not name or "/" in name or name.startswith(".") or name.startswith("_"):
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


def restart_mcp(name):
    """Redémarre un MCP sans toucher à Claude Desktop : kill le process puis
    toggle off/on dans claude_desktop_config.json (Claude Desktop surveille ce
    fichier, le toggle déclenche un respawn du MCP par son host MCP)."""
    if not name:
        return False, "Nom MCP requis"
    config = load_config()
    is_active = name in config.get("mcpServers", {})
    is_disabled = name in config.get("_disabledMcps", {})
    if not (is_active or is_disabled):
        return False, f"MCP '{name}' introuvable"
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
        return True, f"MCP '{name}' redémarré (killed {killed}, config togglée)"
    return True, f"MCP '{name}' inactif : {killed} process killed, rien d'autre à faire"


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
_WATCHDOG_MAX_EVENTS = 30


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


def _mcp_pids(name):
    fp = _mcp_process_fingerprint(name)
    if not fp:
        return []
    try:
        r = subprocess.run(["pgrep", "-f", fp], capture_output=True, text=True, timeout=3)
        if r.returncode != 0:
            return []
        return [int(p) for p in r.stdout.split() if p.strip().isdigit()]
    except Exception:
        return []


def _mcp_log_says_frozen(name, within_seconds):
    """Lit mcp-server-<name>.log et regarde si une erreur 'transport closed' /
    'process exiting early' est apparue dans la fenêtre."""
    log = CLAUDE_LOGS_DIR / f"mcp-server-{name}.log"
    if not log.exists():
        return False
    try:
        mtime = log.stat().st_mtime
    except Exception:
        return False
    if mtime < time.time() - within_seconds:
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
    try:
        r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=3)
        if r.returncode != 0:
            return []
        my_pid = os.getpid()
        return [int(p) for p in r.stdout.split() if p.strip().isdigit() and int(p) != my_pid]
    except Exception:
        return []


def get_watchdog_status():
    cfg = load_watchdog_config()
    target = cfg.get("target", "claude_desktop")
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
        events = list(_WATCHDOG_EVENTS[-10:][::-1])
    return {
        "config": cfg,
        "claude_running": len(pids) > 0,
        "claude_pids": pids,
        "target_label": target_label,
        "available_targets": ["claude_desktop"] + _list_known_mcps() + ["custom"],
        "events": events,
    }


def _watchdog_loop():
    while True:
        try:
            cfg = load_watchdog_config()
            interval = cfg["interval_seconds"]
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
                        if cfg["auto_restart_on_crash"] and not _claude_pids():
                            _watchdog_event("start", f"MCP '{target}' down + Claude Desktop down, launching Claude")
                            subprocess.run(["open", "-a", "Claude"], check=False)
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
    user_names = {s["name"] for s in skills if s["source"] == "user"}
    plugin_names = {s["name"] for s in skills if s["source"] != "user"}
    duplicates = sorted(user_names & plugin_names)
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
    skill_issues = []
    for sk in state["skills"]:
        if not sk.get("description") and not sk.get("category"):
            skill_issues.append({"name": sk["name"], "reason": "sans frontmatter"})
    skill_names = {sk["name"] for sk in state["skills"]}
    plugin_skill_names = set()
    for p in plugins:
        for s in (p.get("contents", {}).get("skills") or []):
            plugin_skill_names.add(s)
    duplicates = sorted(skill_names & plugin_skill_names)
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
            "duplicate_names": duplicates,
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
<div id="watchdog-widget" class="mb-4"></div>
<nav id="main-tabs" class="flex gap-1 mb-6 bg-stone-100 p-1 rounded-lg overflow-x-auto">
<button class="main-tab-btn flex-1 min-w-[80px] px-3 py-2 text-sm rounded-md font-medium" data-main-tab="overview" onclick="setMainTab('overview')" data-i18n="tab_overview">Vue d'ensemble</button>
<button class="main-tab-btn flex-1 min-w-[80px] px-3 py-2 text-sm rounded-md font-medium" data-main-tab="mcps" onclick="setMainTab('mcps')" data-i18n="mcp_section">Serveurs MCP</button>
<button class="main-tab-btn flex-1 min-w-[80px] px-3 py-2 text-sm rounded-md font-medium" data-main-tab="skills" onclick="setMainTab('skills')" data-i18n="skills">Skills</button>
<button class="main-tab-btn flex-1 min-w-[80px] px-3 py-2 text-sm rounded-md font-medium" data-main-tab="plugins" onclick="setMainTab('plugins')" data-i18n="plugins">Plugins</button>
<button class="main-tab-btn flex-1 min-w-[80px] px-3 py-2 text-sm rounded-md font-medium" data-main-tab="commands" onclick="setMainTab('commands')" data-i18n="commands">Commands</button>
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
<div id="mcps" class="space-y-2"></div>
</section>
</div>
<div data-main-tab="skills" class="hidden">
<section class="card p-6">
<h2 class="text-lg font-semibold mb-1" data-i18n="skills">Skills</h2>
<p class="text-xs text-stone-500 mb-4" data-i18n="skills_help">Coché = disponible pour Claude</p>
<input id="skills-search" type="search" oninput="filterSkills()" data-i18n-placeholder="skills_search_placeholder" placeholder="Rechercher un skill (nom ou description)..." class="w-full mb-3 p-2 border border-stone-200 rounded-lg text-sm focus:outline-none focus:border-stone-400"/>
<div id="skills" class="space-y-2 max-h-[600px] overflow-y-auto"></div>
</section>
</div>
<div data-main-tab="plugins" class="hidden">
<section class="card p-6">
<div class="flex items-baseline justify-between mb-1">
<h2 class="text-lg font-semibold" data-i18n="plugins">Plugins</h2>
<button onclick="openAddPlugin()" class="text-xs text-stone-700 hover:text-stone-900 font-medium" data-i18n="plugin_add_btn">+ Ajouter un plugin (Git)</button>
</div>
<p class="text-xs text-stone-500 mb-3" data-i18n="plugins_help">Plugins Claude Code installés via marketplace</p>
<input id="plugins-search" type="search" oninput="filterPlugins()" data-i18n-placeholder="plugins_search_placeholder" placeholder="Rechercher un plugin..." class="w-full mb-3 p-2 border border-stone-200 rounded-lg text-sm focus:outline-none focus:border-stone-400"/>
<div id="plugins" class="space-y-2 max-h-[700px] overflow-y-auto"></div>
</section>
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
  stat_skills: "Skills actifs",
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
  watchdog_label: "Surveillance",
  watchdog_active: "Active &middot; vérification toutes les {n}s",
  watchdog_inactive: "Désactivée",
  watchdog_enable: "Surveiller Claude Desktop",
  watchdog_crash: "Redémarrer si crash",
  watchdog_freeze: "Détecter freeze + redémarrer",
  watchdog_target_label: "Cible",
  watchdog_custom_target: "Pattern personnalisé",
  watchdog_pattern_placeholder: "ex : desktop-commander, /chemin/vers/binaire, package-name",
  btn_scan: "Scanner",
  scanning: "Scan en cours...",
  scan_no_match: "Aucun process ne contient « {p} » dans son ligne de commande.",
  scan_n_matches: "{n} process trouvé(s) qui contiennent « {p} » :",
  banner_pattern_too_short: "Pattern trop court (>= 2 caractères)",
  btn_restart_mcp: "Redémarrer ce MCP (sans toucher à Claude)",
  skill_filter_mine: "Mes skills",
  skill_filter_plugins: "Plugins",
  skill_filter_all: "Tous",
  skill_filter_all_cats: "Toutes les catégories",
  category_filter: "Catégorie",
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
  stat_skills: "Active skills",
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
  watchdog_label: "Watchdog",
  watchdog_active: "On &middot; checking every {n}s",
  watchdog_inactive: "Off",
  watchdog_enable: "Watch Claude Desktop",
  watchdog_crash: "Restart on crash",
  watchdog_freeze: "Detect freeze + restart",
  watchdog_target_label: "Target",
  watchdog_custom_target: "Custom pattern",
  watchdog_pattern_placeholder: "e.g.: desktop-commander, /path/to/binary, package-name",
  btn_scan: "Scan",
  scanning: "Scanning...",
  scan_no_match: 'No process matches "{p}" in its command line.',
  scan_n_matches: '{n} process(es) match "{p}":',
  banner_pattern_too_short: "Pattern too short (>= 2 characters)",
  btn_restart_mcp: "Restart this MCP (without touching Claude)",
  skill_filter_mine: "My skills",
  skill_filter_plugins: "Plugins",
  skill_filter_all: "All",
  skill_filter_all_cats: "All categories",
  category_filter: "Category",
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
  document.getElementById('mcps').innerHTML = s.mcps.length===0 ? `<p class="text-stone-400 text-sm">${tr('no_mcp')}</p>` : s.mcps.map(m=>`<label class="group flex items-center justify-between gap-3 p-3 rounded-lg hover:bg-stone-50 cursor-pointer border ${m.active?'border-stone-200':'border-stone-100 opacity-60'}"><div class="flex items-center gap-3 flex-1 min-w-0"><input type="checkbox" ${m.active?'checked':''} onchange="toggleMcp('${m.name}')" class="w-5 h-5 rounded accent-green-700 shrink-0"><span class="font-medium truncate">${m.name}</span>${m.running?`<span class="text-xs text-green-700 bg-green-50 px-2 py-0.5 rounded-full running-dot">${tr('running_label')}</span>`:(m.active?`<button type="button" onclick="event.preventDefault();event.stopPropagation();showMcpError('${m.name}')" class="text-xs text-amber-700 bg-amber-50 hover:bg-amber-100 px-2 py-0.5 rounded-full cursor-pointer" title="${tr('why_title')}">${tr('not_started_label')}</button>`:'')}</div><button type="button" onclick="event.preventDefault();event.stopPropagation();restartMcp('${m.name}')" title="${tr('btn_restart_mcp')}" class="text-stone-400 hover:text-amber-700 hover:bg-amber-50 rounded px-2 py-1 text-sm leading-none shrink-0">&#x21bb;</button><button type="button" onclick="event.preventDefault();event.stopPropagation();deleteMcp('${m.name}')" class="text-xs text-stone-500 hover:text-red-700 hover:underline px-2 py-1 shrink-0">${tr('btn_delete')}</button></label>`).join('');
  document.getElementById('skills').innerHTML = renderSkills(s.skills);
  filterSkills();
}
function escAttr(s){return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;');}
let SKILL_USAGE = {};
let SKILL_SOURCE_FILTER = (localStorage.getItem('cc-skill-src') || 'user');
let SKILL_CAT_FILTER = '';
function setSkillSourceFilter(src){
  SKILL_SOURCE_FILTER = src;
  localStorage.setItem('cc-skill-src', src);
  if(typeof loadState==='function') loadState();
}
function setSkillCatFilter(cat){
  SKILL_CAT_FILTER = cat;
  if(typeof loadState==='function') loadState();
}
function renderSkills(skills){
  if(!skills || skills.length===0) return `<p class="text-stone-400 text-sm">${tr('no_skill')}</p>`;
  const userCount = skills.filter(s=>s.source==='user').length;
  const pluginCount = skills.length - userCount;
  let filtered;
  if(SKILL_SOURCE_FILTER === 'user') filtered = skills.filter(s=>s.source==='user');
  else if(SKILL_SOURCE_FILTER === 'plugin') filtered = skills.filter(s=>s.source!=='user');
  else filtered = skills;
  // Build category list for the dropdown — based on filtered set
  const allCats = new Set();
  filtered.forEach(sk=>{allCats.add(sk.category || sk.auto_category || tr('general_category'));});
  const sortedCats = Array.from(allCats).sort((a,b)=>{
    if(a===tr('general_category')) return 1; if(b===tr('general_category')) return -1;
    return a.localeCompare(b);
  });
  if(SKILL_CAT_FILTER) filtered = filtered.filter(sk=>(sk.category || sk.auto_category || tr('general_category')) === SKILL_CAT_FILTER);
  const cats = SKILL_CAT_FILTER ? [SKILL_CAT_FILTER] : sortedCats;
  // Source filter pills
  const pill = (val, label, count) => `<button onclick="setSkillSourceFilter('${val}')" class="px-3 py-1 text-xs rounded-full font-medium ${SKILL_SOURCE_FILTER===val ? 'bg-stone-900 text-white' : 'bg-stone-100 text-stone-700 hover:bg-stone-200'}">${escAttr(label)} <span class="opacity-70">(${count})</span></button>`;
  const sourceBar = `<div class="flex flex-wrap items-center gap-2 mb-3">${pill('user', tr('skill_filter_mine'), userCount)}${pill('plugin', tr('skill_filter_plugins'), pluginCount)}${pill('all', tr('skill_filter_all'), skills.length)}</div>`;
  // Category dropdown
  const catOptions = `<option value="">${tr('skill_filter_all_cats')}</option>` + sortedCats.map(c=>`<option value="${escAttr(c)}" ${SKILL_CAT_FILTER===c?'selected':''}>${escAttr(c)}</option>`).join('');
  const catBar = `<div class="flex items-center gap-2 mb-4 text-xs"><label class="text-stone-500">${tr('category_filter')}:</label><select onchange="setSkillCatFilter(this.value)" class="border border-stone-200 rounded px-2 py-1 bg-white">${catOptions}</select></div>`;
  if(filtered.length === 0) return sourceBar + catBar + `<p class="text-stone-400 text-sm">${tr('no_skill')}</p>`;
  // Group filtered by category
  const groups = {};
  filtered.forEach(sk=>{
    const cat = sk.category || sk.auto_category || tr('general_category');
    (groups[cat] = groups[cat] || []).push(sk);
  });
  const blocks = cats.filter(c=>groups[c]).map(cat=>{
    const items = groups[cat].map(sk=>{
      const desc = sk.description || '';
      const search = (sk.name + ' ' + desc).toLowerCase();
      const descHtml = desc ? `<span class="text-xs text-stone-500 truncate">${escAttr(desc)}</span>` : '';
      const tagHtml = (sk.tags||[]).slice(0,4).map(t=>`<span class="text-[10px] bg-stone-100 text-stone-600 px-1.5 py-0.5 rounded">${escAttr(t)}</span>`).join('');
      const autoHint = (!sk.category && sk.auto_category) ? `<span class="text-[10px] text-stone-400 italic">${tr('auto_cat_hint')}</span>` : '';
      const sourceBadge = sk.source==='user'
        ? `<span class="text-[10px] bg-green-50 text-green-700 px-1.5 py-0.5 rounded">${tr('source_badge_user')}</span>`
        : `<span class="text-[10px] bg-stone-100 text-stone-600 px-1.5 py-0.5 rounded" title="${escAttr(sk.source)}">${tr('source_badge_plugin')}</span>`;
      const usageCount = SKILL_USAGE[sk.name] || 0;
      const usageBadge = usageCount > 0 ? `<span class="text-[10px] text-stone-500 bg-stone-100 px-1.5 py-0.5 rounded font-mono" title="${tr('used_x_times').split('{n}').join(usageCount)}">${usageCount}×</span>` : '';
      const editable = sk.editable !== false;
      const checkbox = editable
        ? `<input type="checkbox" ${sk.active?'checked':''} onchange="toggleSkill('${sk.name}')" class="w-5 h-5 rounded accent-green-700 shrink-0">`
        : `<span class="w-5 h-5 inline-flex items-center justify-center text-stone-300 shrink-0" title="${tr('readonly')}">&#128274;</span>`;
      const deleteBtn = editable
        ? `<button type="button" onclick="event.preventDefault();event.stopPropagation();deleteSkill('${sk.name}')" class="text-xs text-stone-500 hover:text-red-700 hover:underline px-2 py-1 shrink-0">${tr('btn_delete')}</button>`
        : '';
      return `<label data-skill data-search="${escAttr(search)}" class="flex items-center gap-3 p-2.5 rounded-lg hover:bg-stone-50 cursor-pointer border ${sk.active?'border-stone-200':'border-stone-100 opacity-60'}">${checkbox}<div class="flex flex-col min-w-0 flex-1"><div class="flex items-baseline gap-2 flex-wrap"><span class="font-medium text-sm truncate">${escAttr(sk.name)}</span>${sourceBadge}${usageBadge}${tagHtml}${autoHint}</div>${descHtml}</div>${deleteBtn}</label>`;
    }).join('');
    return `<details data-skill-cat="${escAttr(cat)}" open class="mb-3"><summary class="cursor-pointer text-sm font-semibold text-stone-800 mb-2 px-1 select-none hover:text-stone-900">${escAttr(cat)} <span class="text-stone-400 font-normal text-xs" data-cat-count>(${groups[cat].length})</span></summary><div class="space-y-1.5">${items}</div></details>`;
  }).join('');
  return sourceBar + catBar + blocks;
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
async function restartMcp(name){
  if(!confirm(tr('confirm_restart_mcp').split('{name}').join(name)))return;
  banner('blue', tr('banner_restarting'));
  const j = await api('/api/restart-mcp', {name:name});
  banner(j.success?'green':'red', j.message);
  if(j.success){loadState();}
}
async function deleteMcp(name){
  if(!confirm(tr('confirm_delete_mcp').split('{name}').join(name)))return;
  const j = await api('/api/delete-mcp', {name:name});
  banner(j.success?'green':'red', j.message);
  if(j.success){loadState();loadOverview();loadPresets();}
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
async function cleanupOrphan(fn, version){
  const msg = CURRENT_LANG === 'en'
    ? `Delete orphan version ${version} of "${fn}"?\n\nA ZIP backup will be created in ~/.claude/backups/claude-control/orphan-plugins/`
    : `Supprimer la version orpheline ${version} de « ${fn} » ?\n\nUn backup ZIP sera créé dans ~/.claude/backups/claude-control/orphan-plugins/`;
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
        return `<div class="text-xs p-2 rounded border ${color}"><div class="flex gap-2 items-start"><span class="shrink-0">${icon}</span><div class="flex-1"><div>${escAttr(s[msgKey] || '')}</div>${items ? '<div class="mt-1 flex flex-wrap gap-1">'+items+'</div>' : ''}</div></div></div>`;
      }).join('') + '</div>';
  }catch(e){console.error(e);}
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
        statBox(`${s.skills_active}/${s.skills_total}`, tr('stat_skills'), 'border-stone-200 bg-stone-50') +
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
      if(h.duplicate_names && h.duplicate_names.length){
        issues.push(`<div class="flex items-center gap-2 text-xs p-2 rounded bg-stone-100 border border-stone-200 text-stone-700"><span>&#8505;</span><span><strong>${h.duplicate_names.length}</strong> ${tr('issue_duplicates')} : ${h.duplicate_names.map(n=>`<span class="font-mono">${escAttr(n)}</span>`).join(', ')}</span></div>`);
      }
      if(h.skill_issues && h.skill_issues.length){
        issues.push(`<div class="flex items-center gap-2 text-xs p-2 rounded bg-stone-50 border border-stone-200 text-stone-600"><span>&#8505;</span><span><strong>${h.skill_issues.length}</strong> ${tr('issue_skill_no_frontmatter')} : ${h.skill_issues.map(s=>`<span class="font-mono">${escAttr(s.name)}</span>`).join(', ')}</span></div>`);
      }
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
applyLang(CURRENT_LANG);setMainTab(CURRENT_MAIN_TAB);loadOverview();loadState();loadPresets();loadPlugins();loadCommands();loadClaudeMd();loadSettings();loadWatchdog();checkUpdate();setInterval(loadOverview,10000);setInterval(loadState,5000);setInterval(loadPlugins,15000);setInterval(loadCommands,30000);setInterval(loadWatchdog,10000);setInterval(checkUpdate,3600000);
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
        elif path == "/api/claude-md":
            self._json(read_claude_md())
        elif path == "/api/settings":
            self._json(read_settings_raw())
        elif path == "/api/overview":
            self._json(get_overview())
        elif path == "/api/skill-suggestions":
            self._json(skill_optimization_suggestions())
        elif path == "/api/watchdog":
            self._json(get_watchdog_status())
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
            "/api/delete-mcp": lambda: delete_mcp(data.get("name", "")),
            "/api/restart-mcp": lambda: restart_mcp(data.get("name", "")),
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
