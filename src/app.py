#!/usr/bin/env python3
"""Claude Control - app locale pour gerer MCPs et Skills de Claude Desktop."""
import http.server, json, socketserver, subprocess, time, webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

PORT = 8765
HOME = Path.home()
CONFIG_PATH = HOME / "Library/Application Support/Claude/claude_desktop_config.json"
SKILLS_DIR = HOME / ".claude/skills"
SKILLS_DISABLED_DIR = HOME / ".claude/skills-disabled"
BACKUP_DIR = HOME / ".claude/backups/claude-control"

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
    return {
        "mcps": [{"name": n, "active": True, "running": n in running} for n in sorted(active.keys())]
              + [{"name": n, "active": False, "running": False} for n in sorted(disabled.keys())],
        "skills": [{"name": n, "active": True} for n in active_skills]
                + [{"name": n, "active": False} for n in disabled_skills],
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

HTML = r"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"><title>Claude Control</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
body{background:linear-gradient(180deg,#fafaf9 0%,#f5f5f4 100%);}
.card{background:white;border:1px solid #e7e5e4;border-radius:12px;}
.running-dot{animation:pulse 2s infinite;}
@keyframes pulse{0%,100%{opacity:1;}50%{opacity:0.5;}}
</style></head><body class="min-h-screen text-stone-900">
<div class="max-w-5xl mx-auto px-6 py-10">
<header class="flex justify-between items-center mb-8">
<div><h1 class="text-3xl font-semibold">Claude Control</h1>
<p class="text-sm text-stone-500 mt-1">Controle de Claude Desktop</p></div>
<button onclick="restartClaude()" class="bg-stone-900 hover:bg-stone-800 text-white px-5 py-2.5 rounded-lg font-medium flex items-center gap-2">
<span>↻</span><span>Redemarrer Claude</span></button></header>
<div id="banner" class="hidden mb-4 p-3 rounded-lg text-sm border"></div>
<div class="grid grid-cols-1 md:grid-cols-2 gap-6">
<section class="card p-6">
<h2 class="text-lg font-semibold mb-1">Serveurs MCP</h2>
<p class="text-xs text-stone-500 mb-4">Coche = charge au demarrage de Claude Desktop</p>
<div id="mcps" class="space-y-2"></div></section>
<section class="card p-6">
<h2 class="text-lg font-semibold mb-1">Skills</h2>
<p class="text-xs text-stone-500 mb-4">Coche = disponible pour Claude</p>
<div id="skills" class="space-y-2 max-h-[600px] overflow-y-auto"></div></section></div>
<p class="text-xs text-stone-400 mt-8 text-center">Apres modifications, clique sur "Redemarrer Claude" pour appliquer.</p>
</div><script>
async function loadState(){
  const r=await fetch('/api/state');const s=await r.json();
  const mcps=document.getElementById('mcps');
  mcps.innerHTML=s.mcps.length===0?'<p class="text-stone-400 text-sm">Aucun MCP configure</p>':s.mcps.map(m=>`
    <label class="flex items-center justify-between gap-3 p-3 rounded-lg hover:bg-stone-50 cursor-pointer border ${m.active?'border-stone-200':'border-stone-100 opacity-60'}">
    <div class="flex items-center gap-3 flex-1">
    <input type="checkbox" ${m.active?'checked':''} onchange="toggleMcp('${m.name}')" class="w-5 h-5 rounded accent-green-600">
    <span class="font-medium">${m.name}</span>
    ${m.running?'<span class="text-xs text-green-700 bg-green-50 px-2 py-0.5 rounded-full running-dot">● running</span>':(m.active?'<span class="text-xs text-amber-700 bg-amber-50 px-2 py-0.5 rounded-full">○ pas demarre</span>':'')}
    </div></label>`).join('');
  const skills=document.getElementById('skills');
  skills.innerHTML=s.skills.length===0?'<p class="text-stone-400 text-sm">Aucun skill</p>':s.skills.map(sk=>`
    <label class="flex items-center gap-3 p-3 rounded-lg hover:bg-stone-50 cursor-pointer border ${sk.active?'border-stone-200':'border-stone-100 opacity-60'}">
    <input type="checkbox" ${sk.active?'checked':''} onchange="toggleSkill('${sk.name}')" class="w-5 h-5 rounded accent-green-600">
    <span class="font-medium text-sm">${sk.name}</span></label>`).join('');
}
async function toggleMcp(n){const r=await fetch('/api/toggle-mcp',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})});const j=await r.json();banner(j.success?'green':'red',j.message);loadState();}
async function toggleSkill(n){const r=await fetch('/api/toggle-skill',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})});const j=await r.json();banner(j.success?'green':'red',j.message);loadState();}
async function restartClaude(){if(!confirm('Redemarrer Claude Desktop ? Toutes les conversations en cours seront fermees.'))return;banner('blue','Redemarrage...');const r=await fetch('/api/restart-claude',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const j=await r.json();banner(j.success?'green':'red',j.message);setTimeout(loadState,4000);}
function banner(c,m){const b=document.getElementById('banner');const cls={green:'bg-green-50 text-green-800 border-green-200',red:'bg-red-50 text-red-800 border-red-200',blue:'bg-blue-50 text-blue-800 border-blue-200'};b.className=`mb-4 p-3 rounded-lg text-sm border ${cls[c]}`;b.textContent=m;b.classList.remove('hidden');setTimeout(()=>b.classList.add('hidden'),4000);}
loadState();setInterval(loadState,5000);
</script></body></html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
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
            self._send_json(get_state())
        else:
            self.send_response(404); self.end_headers()
    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            data = {}
        if path == "/api/toggle-mcp":
            ok, msg = toggle_mcp(data.get("name", ""))
        elif path == "/api/toggle-skill":
            ok, msg = toggle_skill(data.get("name", ""))
        elif path == "/api/restart-claude":
            ok, msg = restart_claude()
        else:
            self.send_response(404); self.end_headers(); return
        self._send_json({"success": ok, "message": msg})
    def log_message(self, *args):
        return

def main():
    SKILLS_DISABLED_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n  Claude Control - http://localhost:{PORT}\n  Cmd+C pour arreter\n")
    webbrowser.open(f"http://localhost:{PORT}")
    try:
        with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Au revoir.")
    except OSError as e:
        if e.errno == 48:
            print(f"\n  Port {PORT} deja utilise. App deja ouverte ?")
            webbrowser.open(f"http://localhost:{PORT}")
        else:
            raise

if __name__ == "__main__":
    main()


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
    return {
        "mcps": [{"name": n, "active": True, "running": n in running} for n in sorted(active.keys())]
              + [{"name": n, "active": False, "running": False} for n in sorted(disabled.keys())],
        "skills": [{"name": n, "active": True} for n in active_skills]
                + [{"name": n, "active": False} for n in disabled_skills],
    }


def toggle_mcp(name):
    config = load_config()
    active = config.setdefault("mcpServers", {})
    disabled = config.setdefault("_disabledMcps", {})
    if name in active:
        disabled[name] = active.pop(name); msg = f"MCP '{name}' desactive"
    elif name in disabled:
        active[name] = disabled.pop(name); msg = f"MCP '{name}' active"
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
        if target.exists(): return False, f"Conflit: {target}"
        src_a.rename(target); return True, f"Skill '{name}' desactive"
    elif src_d.exists():
        target = SKILLS_DIR / name
        if target.exists(): return False, f"Conflit: {target}"
        src_d.rename(target); return True, f"Skill '{name}' active"
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
    if not p.exists(): return False, f"Fichier introuvable : {p}"
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


# === AUTO-UPDATE (interroge GitHub releases) ===

VERSION_FILE = HOME / "dev/claude-control/version.txt"
# GITHUB_REPO sera defini lors du premier deploiement, ex: "sekoia-ca/claude-control"
GITHUB_REPO_FILE = HOME / "dev/claude-control/.github-repo"


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
    # Copie le nouveau app.py vers l'emplacement d'execution
    src = repo_dir / "src/app.py"
    dst = HOME / "Applications/claude-control/app.py"
    if src.exists():
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            return False, f"Echec copie : {e}"
    return True, "Mis a jour. Quitte et relance Claude Control pour appliquer."


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
</style></head><body class="min-h-screen text-stone-900">
<div class="max-w-5xl mx-auto px-6 py-10">
<header class="flex justify-between items-start mb-8 gap-4">
<div><h1 class="text-3xl font-semibold">Claude Control</h1>
<p class="text-sm text-stone-500 mt-1">Sekoia &middot; Controle de Claude Desktop &middot; <span id="version" class="font-mono">v?</span></p>
<div id="update-banner" class="hidden mt-3"><button onclick="applyUpdate()" class="update-badge text-white text-xs px-3 py-1.5 rounded-full font-medium hover:opacity-90"><span id="update-text">Update disponible</span></button></div>
</div>
<button onclick="restartClaude()" class="bg-stone-900 hover:bg-stone-800 text-white px-5 py-2.5 rounded-lg font-medium flex items-center gap-2 shrink-0">
<span>&#x21bb;</span><span>Redemarrer Claude</span></button></header>
<div id="banner" class="hidden mb-4 p-3 rounded-lg text-sm border"></div>
<div class="grid grid-cols-1 md:grid-cols-2 gap-6">
<section class="card p-6"><h2 class="text-lg font-semibold mb-1">Serveurs MCP</h2>
<p class="text-xs text-stone-500 mb-4">Coche = charge au demarrage de Claude Desktop</p>
<div id="mcps" class="space-y-2"></div></section>
<section class="card p-6"><h2 class="text-lg font-semibold mb-1">Skills</h2>
<p class="text-xs text-stone-500 mb-4">Coche = disponible pour Claude</p>
<div id="skills" class="space-y-2 max-h-[500px] overflow-y-auto"></div></section>
</div>
<div class="grid grid-cols-1 md:grid-cols-2 gap-6 mt-6">
<section class="card p-6">
<h2 class="text-lg font-semibold mb-1">+ Ajouter un MCP</h2>
<p class="text-xs text-stone-500 mb-4">JSON, fichier local ou repo Git</p>
<div class="flex gap-1 mb-4 bg-stone-100 p-1 rounded-lg">
<button class="tab-btn active flex-1 px-3 py-1.5 text-xs rounded-md font-medium" data-tab="mcp-json" onclick="setTab('mcp','json')">JSON</button>
<button class="tab-btn flex-1 px-3 py-1.5 text-xs rounded-md font-medium" data-tab="mcp-file" onclick="setTab('mcp','file')">Fichier</button>
<button class="tab-btn flex-1 px-3 py-1.5 text-xs rounded-md font-medium" data-tab="mcp-git" onclick="setTab('mcp','git')">Git</button>
</div>
<div data-pane="mcp-json"><textarea id="mcp-json-in" class="w-full p-3 border border-stone-200 rounded-lg font-mono text-xs h-32 focus:outline-none focus:border-stone-400" placeholder='{"my-mcp": {"command": "node", "args": ["/path/server.js"]}}'></textarea>
<button onclick="addMcpJson()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium">Ajouter</button></div>
<div data-pane="mcp-file" class="hidden"><input id="mcp-file-in" type="text" class="w-full p-3 border border-stone-200 rounded-lg text-sm" placeholder="/Users/.../config.json"/>
<p class="text-xs text-stone-500 mt-1">Path absolu d'un .json contenant mcpServers</p>
<button onclick="addMcpFile()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium">Importer</button></div>
<div data-pane="mcp-git" class="hidden"><input id="mcp-git-in" type="text" class="w-full p-3 border border-stone-200 rounded-lg text-sm" placeholder="https://github.com/.../mcp.git"/>
<p class="text-xs text-stone-500 mt-1">Sera clone dans ~/.claude/imported-mcps/</p>
<button onclick="addMcpGit()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium">Cloner et importer</button></div>
</section>
<section class="card p-6">
<h2 class="text-lg font-semibold mb-1">+ Ajouter un Skill</h2>
<p class="text-xs text-stone-500 mb-4">Dossier local, repo Git, ou markdown</p>
<div class="flex gap-1 mb-4 bg-stone-100 p-1 rounded-lg">
<button class="tab-btn active flex-1 px-3 py-1.5 text-xs rounded-md font-medium" data-tab="sk-folder" onclick="setTab('sk','folder')">Dossier</button>
<button class="tab-btn flex-1 px-3 py-1.5 text-xs rounded-md font-medium" data-tab="sk-git" onclick="setTab('sk','git')">Git</button>
<button class="tab-btn flex-1 px-3 py-1.5 text-xs rounded-md font-medium" data-tab="sk-md" onclick="setTab('sk','md')">Markdown</button>
</div>
<div data-pane="sk-folder"><input id="sk-folder-in" type="text" class="w-full p-3 border border-stone-200 rounded-lg text-sm" placeholder="/Users/.../mon-skill/"/>
<p class="text-xs text-stone-500 mt-1">Dossier doit contenir SKILL.md</p>
<button onclick="addSkillFolder()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium">Importer</button></div>
<div data-pane="sk-git" class="hidden"><input id="sk-git-in" type="text" class="w-full p-3 border border-stone-200 rounded-lg text-sm" placeholder="https://github.com/.../skill.git"/>
<p class="text-xs text-stone-500 mt-1">Le repo doit contenir SKILL.md</p>
<button onclick="addSkillGit()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium">Cloner et importer</button></div>
<div data-pane="sk-md" class="hidden"><input id="sk-md-name" type="text" class="w-full p-2 mb-2 border border-stone-200 rounded-lg text-sm" placeholder="nom-du-skill"/>
<textarea id="sk-md-content" class="w-full p-3 border border-stone-200 rounded-lg font-mono text-xs h-24" placeholder="---&#10;name: mon-skill&#10;description: ...&#10;---"></textarea>
<button onclick="addSkillMd()" class="mt-2 w-full bg-stone-900 hover:bg-stone-800 text-white py-2 rounded-lg text-sm font-medium">Creer</button></div>
</section>
</div>
<p class="text-xs text-stone-400 mt-8 text-center">Apres modifications, clique sur "Redemarrer Claude" pour appliquer.</p>
</div>
<script>
async function loadState(){
  const s = await (await fetch('/api/state')).json();
  document.getElementById('mcps').innerHTML = s.mcps.length===0 ? '<p class="text-stone-400 text-sm">Aucun MCP</p>' : s.mcps.map(m=>`<label class="flex items-center justify-between gap-3 p-3 rounded-lg hover:bg-stone-50 cursor-pointer border ${m.active?'border-stone-200':'border-stone-100 opacity-60'}"><div class="flex items-center gap-3 flex-1"><input type="checkbox" ${m.active?'checked':''} onchange="toggleMcp('${m.name}')" class="w-5 h-5 rounded accent-green-700"><span class="font-medium">${m.name}</span>${m.running?'<span class="text-xs text-green-700 bg-green-50 px-2 py-0.5 rounded-full running-dot">running</span>':(m.active?'<span class="text-xs text-amber-700 bg-amber-50 px-2 py-0.5 rounded-full">pas demarre</span>':'')}</div></label>`).join('');
  document.getElementById('skills').innerHTML = s.skills.length===0 ? '<p class="text-stone-400 text-sm">Aucun skill</p>' : s.skills.map(sk=>`<label class="flex items-center gap-3 p-3 rounded-lg hover:bg-stone-50 cursor-pointer border ${sk.active?'border-stone-200':'border-stone-100 opacity-60'}"><input type="checkbox" ${sk.active?'checked':''} onchange="toggleSkill('${sk.name}')" class="w-5 h-5 rounded accent-green-700"><span class="font-medium text-sm">${sk.name}</span></label>`).join('');
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
async function applyUpdate(){if(!confirm('Mettre a jour Claude Control ? Tu devras quitter et relancer l\'app.'))return;banner('blue','Mise a jour...');const j=await api('/api/apply-update');banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('update-banner').classList.add('hidden');}}
async function addMcpJson(){const v=document.getElementById('mcp-json-in').value.trim();if(!v)return;const j=await api('/api/import-mcp-json',{json:v});banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('mcp-json-in').value='';loadState();}}
async function addMcpFile(){const v=document.getElementById('mcp-file-in').value.trim();if(!v)return;const j=await api('/api/import-mcp-file',{path:v});banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('mcp-file-in').value='';loadState();}}
async function addMcpGit(){const v=document.getElementById('mcp-git-in').value.trim();if(!v)return;banner('blue','Clonage...');const j=await api('/api/import-mcp-git',{url:v});banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('mcp-git-in').value='';loadState();}}
async function addSkillFolder(){const v=document.getElementById('sk-folder-in').value.trim();if(!v)return;const j=await api('/api/import-skill-folder',{path:v});banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('sk-folder-in').value='';loadState();}}
async function addSkillGit(){const v=document.getElementById('sk-git-in').value.trim();if(!v)return;banner('blue','Clonage...');const j=await api('/api/import-skill-git',{url:v});banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('sk-git-in').value='';loadState();}}
async function addSkillMd(){const n=document.getElementById('sk-md-name').value.trim();const c=document.getElementById('sk-md-content').value;if(!n||!c)return;const j=await api('/api/import-skill-markdown',{name:n,content:c});banner(j.success?'green':'red',j.message);if(j.success){document.getElementById('sk-md-name').value='';document.getElementById('sk-md-content').value='';loadState();}}
function banner(c,m){const b=document.getElementById('banner');const cls={green:'bg-green-50 text-green-800 border-green-200',red:'bg-red-50 text-red-800 border-red-200',blue:'bg-blue-50 text-blue-800 border-blue-200'};b.className='mb-4 p-3 rounded-lg text-sm border '+cls[c];b.textContent=m;b.classList.remove('hidden');setTimeout(()=>b.classList.add('hidden'),4500);}
loadState();checkUpdate();setInterval(loadState,5000);setInterval(checkUpdate,3600000);
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
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            data = {}
        routes = {
            "/api/toggle-mcp": lambda: toggle_mcp(data.get("name", "")),
            "/api/toggle-skill": lambda: toggle_skill(data.get("name", "")),
            "/api/restart-claude": lambda: restart_claude(),
            "/api/apply-update": lambda: apply_update(),
            "/api/import-mcp-json": lambda: import_mcp_json(data.get("json", "")),
            "/api/import-mcp-file": lambda: import_mcp_file(data.get("path", "")),
            "/api/import-mcp-git": lambda: import_mcp_git(data.get("url", "")),
            "/api/import-skill-folder": lambda: import_skill_folder(data.get("path", "")),
            "/api/import-skill-git": lambda: import_skill_git(data.get("url", "")),
            "/api/import-skill-markdown": lambda: import_skill_markdown(data.get("name", ""), data.get("content", "")),
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


def main():
    SKILLS_DISABLED_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n  Claude Control v{get_local_version()} - http://localhost:{PORT}")
    print(f"  Cmd+C pour arreter\n")
    webbrowser.open(f"http://localhost:{PORT}")
    try:
        with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Au revoir.")
    except OSError as e:
        if e.errno == 48:
            print(f"\n  Port {PORT} deja utilise.")
            webbrowser.open(f"http://localhost:{PORT}")
        else:
            raise


if __name__ == "__main__":
    main()
