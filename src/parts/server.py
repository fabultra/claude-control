

# v1.14.0 - routes dont le corps fait un cycle lire-modifier-ecrire sur un
# fichier de config. Elles sont serialisees par _CONFIG_LOCK : sans ca, deux
# requetes concurrentes (le serveur est multi-thread) lisent la meme version
# et la derniere ecriture ecrase l'autre. Les routes lentes qui ne touchent
# pas la config (mcp-test, suggest-skill-description, pick-folder) sont
# volontairement hors de cette liste pour ne pas bloquer les autres.
_CONFIG_MUTATING_ROUTES = {
    "/api/toggle-mcp", "/api/delete-mcp", "/api/delete-extension",
    "/api/toggle-extension", "/api/resolve-mcp-conflict", "/api/mcp-set-env",
    "/api/import-mcp-json", "/api/import-mcp-file", "/api/import-mcp-git",
    "/api/preset-save", "/api/preset-apply", "/api/preset-delete",
    "/api/toggle-plugin", "/api/delete-plugin", "/api/bridge-plugin-mcp",
    "/api/add-plugin-git", "/api/plugin-cleanup", "/api/plugin-cleanup-all",
    "/api/save-settings",
    "/api/watchdog-config", "/api/save-claude-md", "/api/save-command",
    "/api/toggle-skill", "/api/toggle-command", "/api/delete-skill",
    "/api/repair-skill", "/api/delete-user-skill-duplicates",
    "/api/repair-all-skills", "/api/repair-all-cancel",
    "/api/package-plugin-skill",
}

# Routes qui recoivent un binaire brut (upload ZIP) : pas de Content-Type JSON.
_RAW_BODY_ROUTES = {"/api/import-skill-zip", "/api/import-mcp-zip"}

# v1.14.17 - routes qui changent l'etat SANS toucher un fichier de config
# (kill/start de process) : elles doivent aussi invalider le cache court de
# get_state.
_STATE_AFFECTING_ROUTES = {
    "/api/restart-mcp", "/api/stop-mcp", "/api/start-mcp",
    "/api/restart-claude", "/api/restart-claude-desktop",
    "/api/restart-extension",
}

_ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}", f"[::1]:{PORT}"}
_ALLOWED_ORIGINS = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}",
                    f"http://[::1]:{PORT}"}


class Handler(http.server.BaseHTTPRequestHandler):
    def _guard_request(self):
        """v1.14.0 - Defense CSRF / DNS rebinding.

        Le serveur n'ecoute que sur 127.0.0.1, mais ca ne protege de rien
        contre un site tiers ouvert dans le meme navigateur : do_POST parsait
        le corps SANS regarder le Content-Type, donc un simple

            <form action="http://localhost:8765/api/delete-mcp"
                  method="POST" enctype="text/plain">

        passait (text/plain est une "simple request" : pas de preflight CORS)
        et declenchait delete-mcp, apply-update ou restart-self. L'attaquant
        ne lit pas la reponse, mais l'action a lieu.

        Trois controles : Host attendu (bloque le DNS rebinding), Origin
        attendu quand il est present, et Content-Type JSON sur les routes
        JSON. Retourne (ok, message).
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in _ALLOWED_HOSTS:
            return False, f"Host non autorise : {host or '(absent)'}"
        origin = self.headers.get("Origin")
        if origin is not None and origin.strip().lower() not in _ALLOWED_ORIGINS:
            return False, f"Origin non autorise : {origin}"
        # v1.15.0 - quatrieme verrou : Fetch Metadata. Les GET "simples"
        # (<img src=...>, cf. le poll UI) ne portent jamais d'Origin, donc
        # la garde ci-dessus les laissait passer depuis n'importe quel site.
        # Les navigateurs modernes annoncent la provenance de CHAQUE requete
        # via Sec-Fetch-Site : une requete declaree cross-site est refusee.
        # Seule exception : la navigation HAUT NIVEAU (cliquer un lien vers
        # http://localhost:8765 depuis une page web) -- GET + mode navigate
        # + dest document ; un iframe cross-site (dest=iframe) reste refuse.
        # Header absent (curl, vieux navigateurs) : on n'exige rien.
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site == "cross-site":
            mode = (self.headers.get("Sec-Fetch-Mode") or "").strip().lower()
            dest = (self.headers.get("Sec-Fetch-Dest") or "").strip().lower()
            if not (self.command in ("GET", "HEAD") and mode == "navigate"
                    and dest == "document"):
                return False, "Requete cross-site refusee (Sec-Fetch-Site)"
        return True, ""

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        ok, why = self._guard_request()
        if not ok:
            self._json({"error": why}, status=403); return
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
        elif path == "/api/repair-all-status":
            self._json(bulk_repair_status())
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
        ok, why = self._guard_request()
        if not ok:
            self._json({"success": False, "message": why}, status=403); return
        # v1.15.0 - langue de la requete (en-tete pose par api() cote JS),
        # rangee dans un threading.local : le serveur est thread-par-requete,
        # donc _srv() peut la lire depuis n'importe quelle fonction appelee
        # par cette route, sans changer aucune signature.
        _REQ_LANG.lang = _norm_lang(self.headers.get("X-CC-Lang"))
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            self._json({"success": False, "message": "Content-Length invalide"}, status=400); return
        if length < 0:
            self._json({"success": False, "message": "Content-Length invalide"}, status=400); return
        if path not in _RAW_BODY_ROUTES:
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._json({"success": False,
                            "message": "Content-Type application/json requis"}, status=415); return
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
            # v1.14.20 - toutes les versions orphelines en un clic.
            "/api/plugin-cleanup-all": lambda: cleanup_all_plugin_orphans(),
            "/api/mcp-test": lambda: test_mcp(data.get("name", ""), data.get("lang", "fr")),
            "/api/mcp-set-env": lambda: set_mcp_env(data.get("name", ""), data.get("var", ""), data.get("value", "")),
            "/api/toggle-command": lambda: toggle_command(data.get("name", "")),
            "/api/save-command": lambda: save_command(data.get("name", ""), data.get("content", "")),
            "/api/save-claude-md": lambda: save_claude_md(data.get("content", "")),
            "/api/save-settings": lambda: save_settings(data.get("content", "")),
            "/api/delete-skill": lambda: delete_skill(data.get("name", "")),
            "/api/repair-skill": lambda: repair_skill(data.get("name", ""), data.get("description"), data.get("name_override")),
            "/api/repair-all-skills": lambda: start_bulk_repair(
                data.get("lang"), bool(data.get("include_synced", False))),
            # v1.14.13 - meme reparation, executee dans Terminal.app : le
            # chemin qui fonctionne par construction quand le subprocess cale.
            "/api/repair-all-terminal": lambda: start_terminal_repair(
                data.get("lang"), bool(data.get("include_synced", False)),
                data.get("names")),
            "/api/repair-all-cancel": lambda: cancel_bulk_repair(),
            "/api/repair-all-dismiss": lambda: dismiss_bulk_repair(),
            "/api/suggest-skill-description": lambda: suggest_skill_description(data.get("name", ""), lang=data.get("lang")),
            # v1.14.12 - POST et non GET : le diagnostic lance des subprocess
            # et paie un aller-retour API reel. En GET, une page tierce
            # pouvait le declencher en boucle (<img src> n'envoie pas
            # d'Origin, et la garde ne refuse que les Origin presents).
            "/api/claude-cli-diagnose": lambda: (True, _diagnose_claude_cli()),
            # v1.14.16 - mise a jour du CLI en un clic (installeur officiel,
            # domaine epingle). Demande utilisateur : "l'app devrait checker
            # la derniere version du CLI et l'installer".
            "/api/cli-update": lambda: update_claude_cli(),
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
            "/api/bridge-plugin-mcp": lambda: bridge_plugin_mcp_to_desktop(data.get("plugin", ""), data.get("mcp", "")),
            "/api/package-plugin-skill": lambda: package_plugin_skill_for_desktop(data.get("plugin", ""), data.get("skill", "")),
            "/api/reveal-path": lambda: reveal_path_in_finder(data.get("path", "")),
            "/api/pick-folder": lambda: pick_native_folder(data.get("prompt", "Choisis le dossier du skill")),
            "/api/watchdog-config": lambda: save_watchdog_config(data),
            "/api/scan-process": lambda: (True, scan_processes(data.get("pattern", ""))),
        }
        if path in routes:
            try:
                if path in _CONFIG_MUTATING_ROUTES:
                    with _CONFIG_LOCK:
                        ok, msg = routes[path]()
                else:
                    ok, msg = routes[path]()
            except Exception as e:
                ok, msg = False, f"Erreur serveur : {e}"
            # v1.14.17 - toute action qui peut changer l'etat invalide le
            # cache court de get_state : le loadState() qui suit un toggle
            # doit voir le resultat du toggle, pas la photo d'avant.
            if path in _CONFIG_MUTATING_ROUTES or path in _STATE_AFFECTING_ROUTES:
                _invalidate_state_cache()
            if isinstance(msg, dict):
                self._json({"success": ok, **msg})
            else:
                self._json({"success": ok, "message": msg})
        else:
            self.send_response(404); self.end_headers()

    def log_message(self, *args):
        return


# ─── v1.15.0 - Retention des backups ────────────────────────────────────────
# Chaque action destructrice laisse un filet de securite (zip/json dans
# BACKUP_DIR ou ORPHAN_BACKUP_DIR) et chaque reparation via le Terminal un
# dossier de run dans TERMINAL_REPAIR_DIR. Rien n'etait jamais purge : des
# mois d'usage accumulaient des centaines d'archives. Politique : supprimer
# ce qui a plus de BACKUP_RETENTION_DAYS jours en conservant TOUJOURS les
# BACKUP_RETENTION_MIN_KEEP entrees les plus recentes de chaque racine -- un
# utilisateur inactif six mois garde ses derniers filets. La purge n'est
# armee QUE par main(), comme _CLI_AUTO_HEAL : une suite de tests qui
# oublierait de rediriger les chemins ne peut pas vider les vrais backups.
BACKUP_RETENTION_DAYS = 30
BACKUP_RETENTION_MIN_KEEP = 10
_RETENTION_STATE = {"armed": False}


def _purge_dir_entries(root, kind, cutoff_ts, min_keep):
    """Purge les enfants DIRECTS de `root` plus vieux que cutoff_ts.

    kind "files" : fichiers seulement -- les sous-dossiers (orphan-plugins/
    sous BACKUP_DIR) sont des racines a part entiere, jamais des entrees.
    kind "dirs" : dossiers de run seulement. Un symlink est detache
    (unlink), jamais suivi ni parcouru. Retourne (supprimes, gardes,
    erreurs).
    """
    root = Path(root)
    if not root.is_dir():
        return 0, 0, 0
    entries = []
    for child in root.iterdir():
        try:
            real_dir = child.is_dir() and not child.is_symlink()
            if kind == "files" and real_dir:
                continue
            if kind == "dirs" and not (real_dir or child.is_symlink()):
                continue
            entries.append((child.lstat().st_mtime, child))
        except OSError:
            continue
    entries.sort(key=lambda t: t[0], reverse=True)  # plus recents d'abord
    deleted = errors = 0
    for i, (mtime, child) in enumerate(entries):
        if i < min_keep or mtime >= cutoff_ts:
            continue
        try:
            if child.is_symlink() or not child.is_dir():
                child.unlink()
            else:
                shutil.rmtree(child)
            deleted += 1
        except OSError:
            errors += 1
    return deleted, len(entries) - deleted, errors


def purge_old_backups():
    """Applique la retention aux trois racines de l'app.

    Les chemins sont les constantes du module, jamais une entree de requete.
    Sans armement par main(), ne touche a rien -- invariante de
    construction, cf. le commentaire de _RETENTION_STATE.
    """
    if not _RETENTION_STATE["armed"]:
        return 0, 0
    cutoff = time.time() - BACKUP_RETENTION_DAYS * 86400
    total_deleted = total_errors = 0
    for root, kind in ((BACKUP_DIR, "files"),
                       (ORPHAN_BACKUP_DIR, "files"),
                       (TERMINAL_REPAIR_DIR, "dirs")):
        deleted, kept, errors = _purge_dir_entries(
            root, kind, cutoff, BACKUP_RETENTION_MIN_KEEP)
        total_deleted += deleted
        total_errors += errors
        if deleted or errors:
            _log(f"retention {root}: {deleted} purge(s), {kept} garde(s), "
                 f"{errors} erreur(s)")
    return total_deleted, total_errors


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
    # v1.15.0 - SKILLS_DIR aussi : c'etait le mkdir de _compute_state (GET)
    # qui le creait en douce, retire depuis.
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    SKILLS_DISABLED_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    # v1.14.18 - l'auto-reparation du CLI n'est armee QUE dans le vrai
    # serveur (cf. _CLI_AUTO_HEAL).
    _CLI_AUTO_HEAL["armed"] = True
    # v1.14.19 - idem pour la demande de reconnexion, et l'app VERIFIE la
    # session au demarrage : si elle est expiree, elle le demande tout de
    # suite au lieu de laisser l'utilisateur decouvrir un echec plus tard.
    _RELOGIN_DIALOG["armed"] = True

    def _startup_session_check():
        time.sleep(5)
        try:
            session = _check_cli_session()
            if session["status"] == "expired":
                _ask_relogin_dialog(session["detail"])
        except Exception as e:
            _log(f"startup session check: {type(e).__name__}: {e}")

    threading.Thread(target=_startup_session_check,
                     name="claude-control-session-check",
                     daemon=True).start()
    # v1.15.0 - retention des backups : armee ici seulement, puis purge en
    # fond au demarrage et une fois par jour tant que l'app tourne.
    _RETENTION_STATE["armed"] = True

    def _retention_loop():
        delay = 15  # laisser le demarrage respirer
        while True:
            time.sleep(delay)
            delay = 86400
            try:
                purge_old_backups()
            except Exception as e:
                _log(f"retention: {type(e).__name__}: {e}")

    threading.Thread(target=_retention_loop,
                     name="claude-control-backup-retention",
                     daemon=True).start()
    start_watchdog()
    print(f"\n  Claude Control v{get_local_version()} - http://localhost:{PORT}")
    print(f"  Cmd+C pour arreter\n")
    socketserver.TCPServer.allow_reuse_address = True
    # v1.12.1 - serveur multi-thread pour eviter que les setInterval JS
    # (loadState 5s, loadOverview 10s, loadPlugins 15s, etc.) bloquent en
    # cascade les actions utilisateur. Avant : un POST /api/package-plugin-skill
    # pouvait attendre 1-2s qu'un loadPlugins en cours finisse, donnant une
    # impression de lag tres marquee.
    class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True
    try:
        server = ThreadingTCPServer(("127.0.0.1", PORT), Handler)
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
