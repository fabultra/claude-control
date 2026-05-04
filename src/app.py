#!/usr/bin/env python3
"""Claude Control - app locale pour gerer MCPs et Skills de Claude Desktop."""
import http.server, io, json, os, re, shutil, socketserver, subprocess, sys, tempfile, threading, time, traceback, webbrowser, zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

MAX_ZIP_SIZE = 50 * 1024 * 1024  # 50 Mo

PORT = 8765
HOME = Path.home()
CONFIG_PATH = HOME / "Library/Application Support/Claude/claude_desktop_config.json"
SKILLS_DIR = HOME / ".claude/skills"
SKILLS_DISABLED_DIR = HOME / ".claude/skills-disabled"
BACKUP_DIR = HOME / ".claude/backups/claude-control"
IMPORTED_REPOS_DIR = HOME / ".claude/imported-mcps"
PRESETS_FILE = HOME / ".claude/claude-control-presets.json"

PLUGINS_DIR = HOME / ".claude/plugins"
INSTALLED_PLUGINS_FILE = PLUGINS_DIR / "installed_plugins.json"
KNOWN_MARKETPLACES_FILE = PLUGINS_DIR / "known_marketplaces.json"
SETTINGS_FILE = HOME / ".claude/settings.json"
ORPHAN_BACKUP_DIR = BACKUP_DIR / "orphan-plugins"

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


def restart_claude():
    subprocess.run(["pkill", "-9", "-f", "Claude"], check=False)
    time.sleep(2.5)
    subprocess.run(["open", "-a", "Claude"], check=False)
    return True, "Claude Desktop redemarre"


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
</style></head><body class="min-h-screen text-stone-900">
<div class="max-w-5xl mx-auto px-6 py-10">
<header class="flex justify-between items-start mb-8 gap-4">
<div><h1 class="text-3xl font-semibold">Claude Control</h1>
<p class="text-sm text-stone-500 mt-1">Sekoia &middot; Controle de Claude Desktop &middot; <span id="version" class="font-mono">v?</span></p>
<div id="update-banner" class="hidden mt-3"><button onclick="applyUpdate()" class="update-badge text-white text-xs px-3 py-1.5 rounded-full font-medium hover:opacity-90"><span id="update-text">Update disponible</span></button></div>
</div>
<div class="flex gap-2 shrink-0">
<button onclick="restartSelf()" class="bg-stone-100 hover:bg-stone-200 text-stone-700 px-3 py-2.5 rounded-lg text-sm font-medium" title="Redemarrer le serveur Claude Control">&#x21bb; App</button>
<button onclick="restartClaude()" class="bg-stone-900 hover:bg-stone-800 text-white px-5 py-2.5 rounded-lg font-medium flex items-center gap-2">
<span>&#x21bb;</span><span>Redemarrer Claude</span></button>
</div></header>
<div id="banner" class="hidden mb-4 p-3 rounded-lg text-sm border"></div>
<div class="grid grid-cols-1 md:grid-cols-2 gap-6">
<section class="card p-6"><h2 class="text-lg font-semibold mb-1">Serveurs MCP</h2>
<p class="text-xs text-stone-500 mb-4">Coche = charge au demarrage de Claude Desktop</p>
<div class="mb-4 p-3 bg-stone-50 rounded-lg border border-stone-200">
<div class="flex items-center justify-between mb-2">
<span class="text-xs font-semibold uppercase tracking-wide text-stone-600">Presets</span>
<button onclick="openSavePreset()" class="text-xs text-stone-700 hover:text-stone-900 font-medium">+ Sauvegarder l'actuel</button>
</div>
<div id="presets-list" class="space-y-1.5"></div>
</div>
<div id="mcps" class="space-y-2"></div></section>
<section class="card p-6"><h2 class="text-lg font-semibold mb-1">Skills</h2>
<p class="text-xs text-stone-500 mb-4">Coche = disponible pour Claude</p>
<input id="skills-search" type="search" oninput="filterSkills()" placeholder="Rechercher un skill (nom ou description)..." class="w-full mb-3 p-2 border border-stone-200 rounded-lg text-sm focus:outline-none focus:border-stone-400"/>
<div id="skills" class="space-y-2 max-h-[500px] overflow-y-auto"></div></section>
</div>
<section class="card p-6 mt-6">
<div class="flex items-baseline justify-between mb-1">
<h2 class="text-lg font-semibold">Plugins</h2>
<span class="text-xs text-stone-400">Lecture seule &middot; toggle persistant dans settings.json</span>
</div>
<p class="text-xs text-stone-500 mb-4">Plugins Claude Code installes via marketplace</p>
<div id="plugins" class="space-y-2"></div>
</section>
<div class="grid grid-cols-1 md:grid-cols-2 gap-6 mt-6">
<section class="card p-6">
<h2 class="text-lg font-semibold mb-1">+ Ajouter un MCP</h2>
<p class="text-xs text-stone-500 mb-4">JSON, fichier local ou repo Git</p>
<div class="flex gap-1 mb-4 bg-stone-100 p-1 rounded-lg">
<button class="tab-btn active flex-1 px-2 py-1.5 text-xs rounded-md font-medium" data-tab="mcp-json" onclick="setTab('mcp','json')">JSON</button>
<button class="tab-btn flex-1 px-2 py-1.5 text-xs rounded-md font-medium" data-tab="mcp-file" onclick="setTab('mcp','file')">Fichier</button>
<button class="tab-btn flex-1 px-2 py-1.5 text-xs rounded-md font-medium" data-tab="mcp-zip" onclick="setTab('mcp','zip')">ZIP</button>
<button class="tab-btn flex-1 px-2 py-1.5 text-xs rounded-md font-medium" data-tab="mcp-git" onclick="setTab('mcp','git')">Git</button>
</div>
<div data-pane="mcp-json"><textarea id="mcp-json-in" class="w-full p-3 border border-stone-200 rounded-lg font-mono text-xs h-32 focus:outline-none focus:border-stone-400" placeholder='{"my-mcp": {"command": "node", "args": ["/path/server.js"]}}'></textarea>
<button onclick="addMcpJson()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium">Ajouter</button></div>
<div data-pane="mcp-file" class="hidden"><input id="mcp-file-in" type="text" class="w-full p-3 border border-stone-200 rounded-lg text-sm" placeholder="/Users/.../config.json"/>
<p class="text-xs text-stone-500 mt-1">Path absolu d'un .json contenant mcpServers</p>
<button onclick="addMcpFile()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium">Importer</button></div>
<div data-pane="mcp-git" class="hidden"><input id="mcp-git-in" type="text" class="w-full p-3 border border-stone-200 rounded-lg text-sm" placeholder="https://github.com/.../mcp.git"/>
<p class="text-xs text-stone-500 mt-1">Sera clone dans ~/.claude/imported-mcps/</p>
<button onclick="addMcpGit()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium">Cloner et importer</button></div>
<div data-pane="mcp-zip" class="hidden"><input id="mcp-zip-in" type="file" accept=".zip,application/zip" class="w-full text-xs file:mr-3 file:py-2 file:px-3 file:rounded-md file:border-0 file:text-xs file:font-medium file:bg-stone-100 file:text-stone-700 hover:file:bg-stone-200"/>
<p class="text-xs text-stone-500 mt-1">ZIP contenant un repo MCP. Le nom du fichier devient le nom du dossier extrait.</p>
<button onclick="addMcpZip()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium">Importer le ZIP</button></div>
</section>
<section class="card p-6">
<h2 class="text-lg font-semibold mb-1">+ Ajouter un Skill</h2>
<p class="text-xs text-stone-500 mb-4">Dossier local, repo Git, ou markdown</p>
<div class="flex gap-1 mb-4 bg-stone-100 p-1 rounded-lg">
<button class="tab-btn active flex-1 px-2 py-1.5 text-xs rounded-md font-medium" data-tab="sk-folder" onclick="setTab('sk','folder')">Dossier</button>
<button class="tab-btn flex-1 px-2 py-1.5 text-xs rounded-md font-medium" data-tab="sk-zip" onclick="setTab('sk','zip')">ZIP</button>
<button class="tab-btn flex-1 px-2 py-1.5 text-xs rounded-md font-medium" data-tab="sk-git" onclick="setTab('sk','git')">Git</button>
<button class="tab-btn flex-1 px-2 py-1.5 text-xs rounded-md font-medium" data-tab="sk-md" onclick="setTab('sk','md')">Markdown</button>
</div>
<div data-pane="sk-folder"><input id="sk-folder-in" type="text" class="w-full p-3 border border-stone-200 rounded-lg text-sm" placeholder="/Users/.../mon-skill/"/>
<p class="text-xs text-stone-500 mt-1">Dossier doit contenir SKILL.md</p>
<button onclick="addSkillFolder()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium">Importer</button></div>
<div data-pane="sk-git" class="hidden"><input id="sk-git-in" type="text" class="w-full p-3 border border-stone-200 rounded-lg text-sm" placeholder="https://github.com/.../skill.git"/>
<p class="text-xs text-stone-500 mt-1">Le repo doit contenir SKILL.md</p>
<button onclick="addSkillGit()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium">Cloner et importer</button></div>
<div data-pane="sk-zip" class="hidden"><input id="sk-zip-in" type="file" accept=".zip,application/zip" class="w-full text-xs file:mr-3 file:py-2 file:px-3 file:rounded-md file:border-0 file:text-xs file:font-medium file:bg-stone-100 file:text-stone-700 hover:file:bg-stone-200"/>
<p class="text-xs text-stone-500 mt-1">ZIP doit contenir SKILL.md (a la racine ou dans un sous-dossier).</p>
<button onclick="addSkillZip()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium">Importer le ZIP</button></div>
<div data-pane="sk-md" class="hidden"><input id="sk-md-name" type="text" class="w-full p-2 mb-2 border border-stone-200 rounded-lg text-sm" placeholder="nom-du-skill"/>
<textarea id="sk-md-content" class="w-full p-3 border border-stone-200 rounded-lg font-mono text-xs h-24" placeholder="---&#10;name: mon-skill&#10;description: ...&#10;---"></textarea>
<button onclick="addSkillMd()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium">Creer</button></div>
</section>
</div>
<p class="text-xs text-stone-400 mt-8 text-center">Apres modifications, clique sur "Redemarrer Claude" pour appliquer.</p>
</div>
<div id="preset-modal" class="hidden fixed inset-0 modal-bg flex items-center justify-center z-50">
<div class="card p-6 w-96 max-w-[90vw]">
<h3 class="text-lg font-semibold mb-1">Sauvegarder un preset</h3>
<p class="text-xs text-stone-500 mb-4">Capture les MCPs actuellement actifs sous un nom.</p>
<input id="preset-name-in" type="text" class="w-full p-3 border border-stone-200 rounded-lg text-sm mb-2" placeholder="Ex: Klide, Audit client..." autocomplete="off"/>
<p id="preset-modal-info" class="text-xs text-stone-500 mb-4"></p>
<div class="flex gap-2 justify-end">
<button onclick="closeSavePreset()" class="px-4 py-2 text-sm rounded-lg border border-stone-200 hover:bg-stone-50">Annuler</button>
<button onclick="confirmSavePreset()" class="px-4 py-2 text-sm rounded-lg bg-stone-900 hover:bg-stone-800 text-white font-medium">Sauvegarder</button>
</div>
</div>
</div>
<script>
let CURRENT_STATE = {mcps:[], skills:[]};
async function loadState(){
  const s = await (await fetch('/api/state')).json();
  CURRENT_STATE = s;
  document.getElementById('mcps').innerHTML = s.mcps.length===0 ? '<p class="text-stone-400 text-sm">Aucun MCP</p>' : s.mcps.map(m=>`<label class="flex items-center justify-between gap-3 p-3 rounded-lg hover:bg-stone-50 cursor-pointer border ${m.active?'border-stone-200':'border-stone-100 opacity-60'}"><div class="flex items-center gap-3 flex-1"><input type="checkbox" ${m.active?'checked':''} onchange="toggleMcp('${m.name}')" class="w-5 h-5 rounded accent-green-700"><span class="font-medium">${m.name}</span>${m.running?'<span class="text-xs text-green-700 bg-green-50 px-2 py-0.5 rounded-full running-dot">running</span>':(m.active?'<span class="text-xs text-amber-700 bg-amber-50 px-2 py-0.5 rounded-full">pas demarre</span>':'')}</div></label>`).join('');
  document.getElementById('skills').innerHTML = renderSkills(s.skills);
  filterSkills();
}
function escAttr(s){return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;');}
function renderSkills(skills){
  if(!skills || skills.length===0) return '<p class="text-stone-400 text-sm">Aucun skill</p>';
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
  if(c.missing) parts.push('install path manquant');
  return parts.length ? parts.join(' &middot; ') : 'vide';
}
function pluginDetailHtml(c){
  const sections = [];
  if(c.skills && c.skills.length) sections.push(`<div><span class="text-xs font-semibold uppercase tracking-wide text-stone-600">Skills</span><div class="mt-1 flex flex-wrap gap-1.5">${c.skills.map(s=>`<span class="text-xs bg-stone-100 px-2 py-0.5 rounded">${escAttr(s)}</span>`).join('')}</div></div>`);
  if(c.mcps && c.mcps.length) sections.push(`<div><span class="text-xs font-semibold uppercase tracking-wide text-stone-600">MCPs</span><div class="mt-1 flex flex-wrap gap-1.5">${c.mcps.map(s=>`<span class="text-xs bg-stone-100 px-2 py-0.5 rounded">${escAttr(s)}</span>`).join('')}</div></div>`);
  if(c.commands && c.commands.length) sections.push(`<div><span class="text-xs font-semibold uppercase tracking-wide text-stone-600">Commands</span><div class="mt-1 flex flex-wrap gap-1.5">${c.commands.map(s=>`<span class="text-xs bg-stone-100 px-2 py-0.5 rounded">/${escAttr(s)}</span>`).join('')}</div></div>`);
  return sections.length ? `<div class="mt-3 pt-3 border-t border-stone-100 space-y-3">${sections.join('')}</div>` : '<div class="mt-3 pt-3 border-t border-stone-100 text-xs text-stone-400">Aucun contenu detecte.</div>';
}
async function loadPlugins(){
  try{
    const j = await (await fetch('/api/plugins')).json();
    const plugins = j.plugins || [];
    const list = document.getElementById('plugins');
    if(plugins.length===0){list.innerHTML = '<p class="text-xs text-stone-400">Aucun plugin installe.</p>';return;}
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
  if(!confirm(`Supprimer la version orpheline ${version} de "${fn}" ?\n\nUn backup ZIP sera cree dans ~/.claude/backups/claude-control/orphan-plugins/`))return;
  const j = await api('/api/plugin-cleanup',{name:fn, version:version});
  banner(j.success?'green':'red',j.message);
  if(j.success){loadPlugins();}
}
async function loadPresets(){
  try{
    const j = await (await fetch('/api/presets')).json();
    const presets = j.presets || {};
    const names = Object.keys(presets).sort();
    const list = document.getElementById('presets-list');
    if(names.length===0){
      list.innerHTML = '<p class="text-xs text-stone-400">Aucun preset. Active des MCPs puis sauvegarde.</p>';
      return;
    }
    list.innerHTML = names.map(n=>{
      const count = (presets[n]||[]).length;
      const ne = escAttr(n);
      return `<div class="flex items-center justify-between gap-2 p-2 bg-white border border-stone-200 rounded-md"><div class="flex-1 min-w-0"><div class="text-sm font-medium truncate">${ne}</div><div class="text-xs text-stone-500">${count} MCP${count>1?'s':''}</div></div><button onclick="applyPreset('${ne}')" class="text-xs px-2.5 py-1 rounded-md bg-stone-900 hover:bg-stone-800 text-white font-medium">Apply</button><button onclick="deletePreset('${ne}')" title="Supprimer" class="text-stone-400 hover:text-red-600 text-lg leading-none px-1">&times;</button></div>`;
    }).join('');
  }catch(e){}
}
async function checkUpdate(){
  try{
    const u = await (await fetch('/api/check-update')).json();
    document.getElementById('version').textContent = 'v' + (u.local || '?');
    if(u.update_available){
      document.getElementById('update-banner').classList.remove('hidden');
      document.getElementById('update-text').textContent = `Update disponible: v${u.latest}`;
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
async function toggleSkill(n){const j=await api('/api/toggle-skill',{name:n});banner(j.success?'green':'red',j.message);loadState();}
async function restartClaude(){if(!confirm('Redemarrer Claude Desktop ?'))return;banner('blue','Redemarrage...');const j=await api('/api/restart-claude');banner(j.success?'green':'red',j.message);setTimeout(loadState,4000);}
async function restartSelf(){if(!confirm('Redemarrer le serveur Claude Control ?'))return;banner('blue','Redemarrage...');try{await api('/api/restart-self');}catch(e){}setTimeout(()=>{banner('green','Reconnexion...');location.reload();}, 1500);}
async function applyUpdate(){if(!confirm('Mettre a jour Claude Control ? L\'app va se relancer toute seule.'))return;banner('blue','Mise a jour...');try{const j=await api('/api/apply-update');if(!j.success){banner('red',j.message);return;}banner('green',j.message);}catch(e){}setTimeout(()=>{banner('blue','Reconnexion...');location.reload();}, 2500);}
async function addMcpJson(){const v=document.getElementById('mcp-json-in').value.trim();if(!v)return;const j=await api('/api/import-mcp-json',{json:v});banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('mcp-json-in').value='';loadState();}}
async function addMcpFile(){const v=document.getElementById('mcp-file-in').value.trim();if(!v)return;const j=await api('/api/import-mcp-file',{path:v});banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('mcp-file-in').value='';loadState();}}
async function addMcpGit(){const v=document.getElementById('mcp-git-in').value.trim();if(!v)return;banner('blue','Clonage...');const j=await api('/api/import-mcp-git',{url:v});banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('mcp-git-in').value='';loadState();}}
async function addSkillFolder(){const v=document.getElementById('sk-folder-in').value.trim();if(!v)return;const j=await api('/api/import-skill-folder',{path:v});banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('sk-folder-in').value='';loadState();}}
async function addSkillGit(){const v=document.getElementById('sk-git-in').value.trim();if(!v)return;banner('blue','Clonage...');const j=await api('/api/import-skill-git',{url:v});banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('sk-git-in').value='';loadState();}}
async function addSkillMd(){const n=document.getElementById('sk-md-name').value.trim();const c=document.getElementById('sk-md-content').value;if(!n||!c)return;const j=await api('/api/import-skill-markdown',{name:n,content:c});banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('sk-md-name').value='';document.getElementById('sk-md-content').value='';loadState();}}
async function uploadZip(path, inputId){
  const inp = document.getElementById(inputId);
  const f = inp.files && inp.files[0];
  if(!f){banner('red','Choisis un fichier ZIP');return null;}
  banner('blue','Upload du ZIP...');
  try{
    const r = await fetch(path, {method:'POST', headers:{'X-Filename': encodeURIComponent(f.name)}, body: f});
    return await r.json();
  }catch(e){return {success:false, message:'Echec upload : '+e};}
}
async function addMcpZip(){const j=await uploadZip('/api/import-mcp-zip','mcp-zip-in');if(!j)return;banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('mcp-zip-in').value='';loadState();}}
async function addSkillZip(){const j=await uploadZip('/api/import-skill-zip','sk-zip-in');if(!j)return;banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('sk-zip-in').value='';loadState();}}
function openSavePreset(){
  const active = (CURRENT_STATE.mcps||[]).filter(m=>m.active).map(m=>m.name);
  document.getElementById('preset-modal-info').textContent = active.length===0 ? 'Aucun MCP actif a sauvegarder.' : `MCPs actifs (${active.length}) : ${active.join(', ')}`;
  document.getElementById('preset-name-in').value='';
  document.getElementById('preset-modal').classList.remove('hidden');
  setTimeout(()=>document.getElementById('preset-name-in').focus(),50);
}
function closeSavePreset(){document.getElementById('preset-modal').classList.add('hidden');}
async function confirmSavePreset(){
  const name = document.getElementById('preset-name-in').value.trim();
  if(!name){banner('red','Nom requis');return;}
  const active = (CURRENT_STATE.mcps||[]).filter(m=>m.active).map(m=>m.name);
  const j = await api('/api/preset-save',{name:name, mcps:active});
  banner(j.success?'green':'red', j.message);
  if(j.success){closeSavePreset();loadPresets();}
}
async function applyPreset(name){
  if(!confirm(`Appliquer le preset "${name}" ?\n\nLes MCPs non listes seront desactives.`))return;
  const j = await api('/api/preset-apply',{name:name});
  banner(j.success?'green':'red', j.message);
  if(j.success){loadState();}
}
async function deletePreset(name){
  if(!confirm(`Supprimer le preset "${name}" ?`))return;
  const j = await api('/api/preset-delete',{name:name});
  banner(j.success?'green':'red', j.message);
  if(j.success){loadPresets();}
}
document.addEventListener('keydown', e=>{
  if(e.key==='Escape') closeSavePreset();
  if(e.key==='Enter' && !document.getElementById('preset-modal').classList.contains('hidden') && document.activeElement.id==='preset-name-in'){e.preventDefault();confirmSavePreset();}
});
function banner(c,m){const b=document.getElementById('banner');const cls={green:'bg-green-50 text-green-800 border-green-200',red:'bg-red-50 text-red-800 border-red-200',blue:'bg-blue-50 text-blue-800 border-blue-200'};b.className='mb-4 p-3 rounded-lg text-sm border '+cls[c];b.textContent=m;b.classList.remove('hidden');setTimeout(()=>b.classList.add('hidden'),4500);}
loadState();loadPresets();loadPlugins();checkUpdate();setInterval(loadState,5000);setInterval(loadPlugins,15000);setInterval(checkUpdate,3600000);
</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def _json(self, data, status=200):
        body = json.dumps(data).encode()
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
        }
        if path in routes:
            try:
                ok, msg = routes[path]()
            except Exception as e:
                ok, msg = False, f"Erreur serveur : {e}"
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
