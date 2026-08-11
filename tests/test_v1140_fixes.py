"""v1.14.0 - Tests de non-regression pour les correctifs de l'audit.

Un test par defaut corrige, nomme d'apres le symptome qu'il empeche de
revenir. Chaque classe documente le comportement casse d'avant.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class OsaQuoteTests(unittest.TestCase):
    """Avant : _notify echappait les guillemets AVANT les antislashs, donc
    `"` devenait `\\\\"` -> antislash echappe suivi d'un guillemet nu, qui
    ferme la chaine AppleScript. Comme AppleScript expose `do shell script`,
    un nom de MCP issu d'un import hostile devenait une execution de commande.
    """

    def test_quote_is_escaped_once(self):
        self.assertEqual(app._osa_quote('He said "hi"', 100), 'He said \\"hi\\"')

    def test_backslash_escaped_before_quote(self):
        # Un antislash seul doit etre double, sans toucher au reste.
        self.assertEqual(app._osa_quote(r"a\b", 100), r"a\\b")

    def test_backslash_then_quote_stays_balanced(self):
        out = app._osa_quote(r'a\"b', 100)
        # Chaque " du resultat doit etre precede d'un nombre IMPAIR
        # d'antislashs (donc echappe). Sinon la chaine AppleScript se ferme.
        for i, ch in enumerate(out):
            if ch == '"':
                n = 0
                j = i - 1
                while j >= 0 and out[j] == "\\":
                    n += 1
                    j -= 1
                self.assertEqual(n % 2, 1, f"guillemet non echappe a l'index {i} dans {out!r}")

    def test_truncation_happens_before_escaping(self):
        # Tronquer APRES echappement pourrait couper au milieu d'une sequence
        # et laisser un antislash orphelin en fin de chaine.
        out = app._osa_quote("\\" * 50, 10)
        self.assertEqual(out, "\\" * 20)  # 10 antislashs, chacun double

    def test_newlines_neutralised(self):
        self.assertNotIn("\n", app._osa_quote("a\nb", 100))


class MatchTokensTests(unittest.TestCase):
    """Avant : get_running_mcps reposait sur 6 mots-cles codes en dur. Tout
    MCP absent de cette table avait running=False a vie et atterrissait dans
    la colonne 'Inactifs' meme quand il tournait -- symptome 'je ne vois pas
    tous mes MCPs'.
    """

    def test_generic_launchers_are_not_tokens(self):
        toks = app._mcp_match_tokens("x", {"command": "npx", "args": ["-y", "node"]})
        for generic in ("npx", "node", "-y"):
            self.assertNotIn(generic, toks)

    def test_package_name_is_a_token(self):
        toks = app._mcp_match_tokens(
            "mailchimp", {"command": "npx", "args": ["-y", "@mailchimp/mcp-server"]})
        self.assertIn("@mailchimp/mcp-server", toks)

    def test_script_path_and_basename_are_tokens(self):
        toks = app._mcp_match_tokens("geo", {"command": "python3",
                                             "args": ["/opt/sekoia/geo_server.py"]})
        self.assertIn("/opt/sekoia/geo_server.py", toks)
        self.assertIn("geo_server.py", toks)

    def test_generic_script_basename_excluded(self):
        # index.js seul matcherait la moitie des process node de la machine.
        toks = app._mcp_match_tokens("x", {"command": "node",
                                           "args": ["/srv/thing/index.js"]})
        self.assertNotIn("index.js", toks)

    def test_short_name_is_not_a_token(self):
        # Un nom de 2-3 lettres matcherait trop de lignes de ps.
        self.assertEqual(app._mcp_match_tokens("db", {}), set())


def _ps_line(pid, command):
    """Fabrique une ligne au format `ps auxww` :
    USER PID %CPU %MEM VSZ RSS TT STAT STARTED TIME COMMAND"""
    return f"fab {pid} 0.0 0.1 400000 30000 ?? S 10:00AM 0:01.23 {command}"


class RunningDetectionTests(unittest.TestCase):
    """Verifie que la detection 'running' derive bien de la config et non
    d'une table figee."""

    def setUp(self):
        self._orig = app._ps_snapshot

    def tearDown(self):
        app._ps_snapshot = self._orig

    def _with_ps(self, commands):
        lines = [_ps_line(4000 + i, c) for i, c in enumerate(commands)]
        app._ps_snapshot = lambda force=False: lines

    def test_arbitrary_mcp_is_detected(self):
        # Ce MCP n'existait dans AUCUNE table codee en dur.
        self._with_ps([
            "node /Users/fab/.npm/_npx/abc/node_modules/@acme/weird-mcp/dist/cli.js",
        ])
        cfg = {"mcpServers": {"weird": {"command": "npx", "args": ["-y", "@acme/weird-mcp"]}}}
        self.assertEqual(app.get_running_mcps(cfg), {"weird"})

    def test_absent_mcp_is_not_detected(self):
        self._with_ps(["node /somewhere/else/index.js"])
        cfg = {"mcpServers": {"weird": {"command": "npx", "args": ["-y", "@acme/weird-mcp"]}}}
        self.assertEqual(app.get_running_mcps(cfg), set())

    def test_generic_launcher_does_not_produce_false_positive(self):
        # Un process node quelconque ne doit pas rendre "running" un MCP
        # dont la seule empreinte serait 'node'.
        self._with_ps(["node /unrelated/app.js"])
        cfg = {"mcpServers": {"db": {"command": "node"}}}
        self.assertEqual(app.get_running_mcps(cfg), set())

    def test_own_process_line_is_ignored(self):
        my = os.getpid()
        app._ps_snapshot = lambda force=False: [
            _ps_line(my, "python3 /Users/fab/Applications/claude-control/app.py")]
        cfg = {"mcpServers": {"claude-control": {"command": "python3",
                                                 "args": ["/Users/fab/Applications/claude-control/app.py"]}}}
        self.assertEqual(app.get_running_mcps(cfg), set())

    def test_shell_mentioning_the_name_is_not_running(self):
        """Un `bash -c` qui porte le nom du MCP dans son argv (ou un grep, un
        editeur) ne doit PAS le faire passer pour actif."""
        self._with_ps([
            "/bin/bash -c echo demarrage de @acme/weird-mcp && sleep 1",
            "grep -r @acme/weird-mcp /Users/fab/dev",
            "tail -f /Users/fab/Library/Logs/Claude/mcp-server-weird.log",
        ])
        cfg = {"mcpServers": {"weird": {"command": "npx", "args": ["-y", "@acme/weird-mcp"]}}}
        self.assertEqual(app.get_running_mcps(cfg), set())

    def test_one_mcp_does_not_mark_another_as_running(self):
        """Cross-contamination : @linear/mcp-server et @mailchimp/mcp-server
        partagent le basename 'mcp-server'. Si linear tourne, mailchimp ne
        doit pas etre signale actif."""
        self._with_ps(["node /Users/fab/.npm/node_modules/@linear/mcp-server/dist/cli.js"])
        cfg = {"mcpServers": {
            "linear": {"command": "npx", "args": ["-y", "@linear/mcp-server"]},
            "mailchimp": {"command": "npx", "args": ["-y", "@mailchimp/mcp-server"]},
        }}
        self.assertEqual(app.get_running_mcps(cfg), {"linear"})

    def test_standalone_binary_mcp_is_detected(self):
        # Un MCP compile (Go/Rust) n'est lance par aucun runtime connu.
        self._with_ps(["/usr/local/bin/klide-mcp-daemon --stdio"])
        cfg = {"mcpServers": {"klide": {"command": "/usr/local/bin/klide-mcp-daemon",
                                        "args": ["--stdio"]}}}
        self.assertEqual(app.get_running_mcps(cfg), {"klide"})

    def test_malformed_ps_lines_are_skipped(self):
        app._ps_snapshot = lambda force=False: ["", "garbage", "fab 1 2"]
        cfg = {"mcpServers": {"weird": {"command": "npx", "args": ["-y", "@acme/weird-mcp"]}}}
        self.assertEqual(app.get_running_mcps(cfg), set())


class AtomicWriteTests(unittest.TestCase):
    """Avant : tous les save_* ouvraient la cible en 'w', ce qui la tronque
    immediatement. Un dump interrompu (ou un pkill entre-temps) laissait
    claude_desktop_config.json tronque -> Claude Desktop ne chargeait plus
    aucun MCP.
    """

    def test_write_replaces_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "conf.json"
            app._atomic_write_json(target, {"a": 1})
            self.assertEqual(json.loads(target.read_text()), {"a": 1})
            app._atomic_write_json(target, {"b": 2})
            self.assertEqual(json.loads(target.read_text()), {"b": 2})

    def test_failed_write_leaves_original_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "conf.json"
            target.write_text('{"original": true}')

            class Unserialisable:
                pass

            with self.assertRaises(TypeError):
                app._atomic_write_json(target, {"bad": Unserialisable()})
            # Le fichier d'origine ne doit pas avoir ete touche.
            self.assertEqual(json.loads(target.read_text()), {"original": True})

    def test_no_temp_file_left_behind_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "conf.json"
            target.write_text("{}")

            class Unserialisable:
                pass

            with self.assertRaises(TypeError):
                app._atomic_write_json(target, {"bad": Unserialisable()})
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name != "conf.json"]
            self.assertEqual(leftovers, [])

    def test_utf8_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "conf.json"
            app._atomic_write_json(target, {"nom": "Montréal — café"})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                             {"nom": "Montréal — café"})


class LoadConfigSafeTests(unittest.TestCase):
    """Avant : un claude_desktop_config.json malforme faisait lever json.load
    -> 500 sur /api/state -> le JS ne catchait rien -> la liste MCP gardait
    son dernier rendu SANS message.
    """

    def setUp(self):
        self._orig = app.CONFIG_PATH
        self._tmp = tempfile.TemporaryDirectory()
        app.CONFIG_PATH = Path(self._tmp.name) / "claude_desktop_config.json"

    def tearDown(self):
        app.CONFIG_PATH = self._orig
        self._tmp.cleanup()

    def test_malformed_config_returns_error_not_raise(self):
        app.CONFIG_PATH.write_text('{"mcpServers": {"a": {}},}')  # virgule en trop
        cfg, err = app.load_config_safe()
        self.assertIsNotNone(err)
        self.assertIn("claude_desktop_config.json", err)
        self.assertEqual(cfg, {"mcpServers": {}, "_disabledMcps": {}})

    def test_valid_config_has_no_error(self):
        app.CONFIG_PATH.write_text('{"mcpServers": {"a": {"command": "x"}}}')
        cfg, err = app.load_config_safe()
        self.assertIsNone(err)
        self.assertEqual(list(cfg["mcpServers"]), ["a"])

    def test_bom_is_tolerated(self):
        app.CONFIG_PATH.write_bytes(b'\xef\xbb\xbf{"mcpServers": {}}')
        cfg, err = app.load_config_safe()
        self.assertIsNone(err)


class PathTraversalTests(unittest.TestCase):
    """Avant : `name` venait d'une requete HTTP et etait interpole dans un
    chemin ou un glob sans validation."""

    def test_safe_component_rejects_traversal(self):
        for bad in ("../x", "a/b", "a\\b", "..", ".hidden", ""):
            self.assertFalse(app._safe_component(bad), bad)

    def test_safe_component_accepts_normal_names(self):
        for good in ("deploy", "my-command", "my_command", "_archived-thing"):
            self.assertTrue(app._safe_component(good), good)

    def test_get_command_rejects_traversal(self):
        ok, msg = app.get_command("../../../../etc/passwd", "user")
        self.assertFalse(ok)
        self.assertIn("invalide", msg.lower())

    def test_read_mcp_error_rejects_traversal_name(self):
        # Ne doit pas sortir de CLAUDE_LOGS_DIR ni lever.
        out = app.read_mcp_error("../../../../etc/hosts")
        self.assertIsInstance(out, dict)
        self.assertIsNone(out.get("error"))


class SkillUsageCacheTests(unittest.TestCase):
    """Avant : aucun cache. get_state (poll 5 s) et get_overview (poll 10 s,
    et qui l'appelait DEUX fois) relançaient un scan complet de
    ~/.claude/projects -- 24 scans par minute, plusieurs Go a chaque fois.
    """

    def setUp(self):
        self._orig = app._compute_skill_usage
        app._SKILL_USAGE_CACHE.update({"ts": 0.0, "days": None, "data": None})
        self.calls = []

    def tearDown(self):
        app._compute_skill_usage = self._orig
        app._SKILL_USAGE_CACHE.update({"ts": 0.0, "days": None, "data": None})

    def test_second_call_is_served_from_cache(self):
        app._compute_skill_usage = lambda days=30: (self.calls.append(days) or
                                                    {"ok": True, "counts": {}, "ranked": []})
        app.get_skill_usage(days=30)
        app.get_skill_usage(days=30)
        app.get_skill_usage(days=30)
        self.assertEqual(len(self.calls), 1, "le scan doit etre fait une seule fois")

    def test_force_bypasses_cache(self):
        app._compute_skill_usage = lambda days=30: (self.calls.append(days) or
                                                    {"ok": True, "counts": {}, "ranked": []})
        app.get_skill_usage(days=30)
        app.get_skill_usage(days=30, force=True)
        self.assertEqual(len(self.calls), 2)

    def test_expired_cache_recomputes(self):
        app._compute_skill_usage = lambda days=30: (self.calls.append(days) or
                                                    {"ok": True, "counts": {}, "ranked": []})
        app.get_skill_usage(days=30)
        app._SKILL_USAGE_CACHE["ts"] = time.time() - app._SKILL_USAGE_TTL - 1
        app.get_skill_usage(days=30)
        self.assertEqual(len(self.calls), 2)

    def test_concurrent_calls_compute_once(self):
        """Single-flight : 8 threads sur un cache froid = 1 seul scan."""
        def slow(days=30):
            self.calls.append(days)
            time.sleep(0.15)
            return {"ok": True, "counts": {}, "ranked": []}

        app._compute_skill_usage = slow
        threads = [threading.Thread(target=lambda: app.get_skill_usage(days=30))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.calls), 1,
                         "les threads concurrents doivent partager un seul scan")


class ExtensionRunningCacheTests(unittest.TestCase):
    """Avant : get_state appelait _extension_pids pour CHAQUE extension a
    chaque requete (jusqu'a 3 pgrep + 3 lsof + un ps par PID chacune)."""

    def setUp(self):
        self._orig = app._compute_extension_running_map
        app._EXT_RUNNING_CACHE.update({"ts": 0.0, "data": {}})
        self.calls = []

    def tearDown(self):
        app._compute_extension_running_map = self._orig
        app._EXT_RUNNING_CACHE.update({"ts": 0.0, "data": {}})

    def test_repeated_calls_hit_cache(self):
        exts = [{"id": "a.b", "name": "Thing"}]
        app._compute_extension_running_map = lambda e: (self.calls.append(1) or {"a.b": True})
        for _ in range(5):
            self.assertEqual(app._extension_running_map(exts), {"a.b": True})
        self.assertEqual(len(self.calls), 1)

    def test_empty_extension_list_short_circuits(self):
        app._compute_extension_running_map = lambda e: (self.calls.append(1) or {})
        self.assertEqual(app._extension_running_map([]), {})
        self.assertEqual(len(self.calls), 0)


class CliMcpSourceTests(unittest.TestCase):
    """Avant : les MCPs ajoutes via `claude mcp add` (~/.claude.json)
    n'apparaissaient nulle part -- seul claude_desktop_config.json etait lu.
    """

    def setUp(self):
        self._orig_path = app.CLI_CONFIG_PATH
        self._orig_ps = app._ps_snapshot
        app._ps_snapshot = lambda force=False: []
        self._tmp = tempfile.TemporaryDirectory()
        app.CLI_CONFIG_PATH = Path(self._tmp.name) / ".claude.json"

    def tearDown(self):
        app.CLI_CONFIG_PATH = self._orig_path
        app._ps_snapshot = self._orig_ps
        self._tmp.cleanup()

    def test_missing_file_returns_empty(self):
        self.assertEqual(app._list_cli_mcps(), [])

    def test_malformed_file_returns_empty(self):
        app.CLI_CONFIG_PATH.write_text("{not json")
        self.assertEqual(app._list_cli_mcps(), [])

    def test_user_scope_servers_are_listed(self):
        app.CLI_CONFIG_PATH.write_text(json.dumps({
            "mcpServers": {"linear": {"command": "npx", "args": ["-y", "@linear/mcp"]}}
        }))
        out = app._list_cli_mcps()
        self.assertEqual([m["name"] for m in out], ["linear"])
        self.assertEqual(out[0]["type"], "cli")
        self.assertFalse(out[0]["editable"], "un MCP CLI ne se pilote pas depuis cette app")

    def test_project_scope_servers_are_listed(self):
        app.CLI_CONFIG_PATH.write_text(json.dumps({
            "projects": {"/Users/fab/dev/klide": {
                "mcpServers": {"klide-db": {"command": "uvx", "args": ["klide-mcp"]}}}}
        }))
        out = app._list_cli_mcps()
        self.assertEqual([m["name"] for m in out], ["klide-db"])
        self.assertTrue(out[0]["source"].startswith("cli:"))

    def test_user_scope_wins_over_project_scope(self):
        app.CLI_CONFIG_PATH.write_text(json.dumps({
            "mcpServers": {"dup": {"command": "a-server"}},
            "projects": {"/p": {"mcpServers": {"dup": {"command": "b-server"}}}},
        }))
        out = app._list_cli_mcps()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "cli")


class RestartClaudeAnchorTests(unittest.TestCase):
    """Avant : restart_claude faisait `pkill -9 -f Claude`, un motif non
    ancre qui matche la ligne de commande complete de TOUS les process de
    l'utilisateur -- dont le subprocess `claude -p <contenu SKILL.md>` lance
    par _call_claude_cli, et toute session Claude Code CLI.
    """

    def setUp(self):
        self._orig_run = app.subprocess.run
        self._orig_notify = app._notify
        self._orig_sleep = app.time.sleep
        self.cmds = []
        app.subprocess.run = lambda cmd, **kw: self.cmds.append(cmd) or type(
            "R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        app._notify = lambda *a, **kw: None
        app.time.sleep = lambda s: None

    def tearDown(self):
        app.subprocess.run = self._orig_run
        app._notify = self._orig_notify
        app.time.sleep = self._orig_sleep

    def test_restart_claude_uses_anchored_pattern(self):
        app.restart_claude()
        pkills = [c for c in self.cmds if c and c[0] == "pkill"]
        self.assertTrue(pkills, "restart_claude doit tuer Claude Desktop")
        for c in pkills:
            self.assertIn(app.CLAUDE_DESKTOP_BIN, c)
            self.assertNotIn("Claude", c[-1:] if c[-1] != app.CLAUDE_DESKTOP_BIN else [])

    def test_pattern_is_a_full_binary_path(self):
        self.assertTrue(app.CLAUDE_DESKTOP_BIN.startswith("/Applications/"))
        self.assertTrue(app.CLAUDE_DESKTOP_BIN.endswith("/Claude"))

    def test_restart_claude_desktop_uses_same_anchor(self):
        app.restart_claude_desktop()
        pkills = [c for c in self.cmds if c and c[0] == "pkill"]
        self.assertTrue(pkills)
        self.assertIn(app.CLAUDE_DESKTOP_BIN, pkills[0])


class RequestGuardTests(unittest.TestCase):
    """Avant : do_POST parsait le corps SANS regarder le Content-Type, et
    aucun controle d'Origin/Host. Un formulaire enctype="text/plain" (simple
    request, pas de preflight CORS) depuis n'importe quel site declenchait
    /api/delete-mcp ou /api/restart-self.
    """

    def _guard(self, headers):
        h = dict(headers)
        fake = app.Handler.__new__(app.Handler)
        fake.headers = h
        return app.Handler._guard_request(fake)

    def test_expected_host_passes(self):
        ok, _ = self._guard({"Host": f"localhost:{app.PORT}"})
        self.assertTrue(ok)

    def test_loopback_ip_host_passes(self):
        ok, _ = self._guard({"Host": f"127.0.0.1:{app.PORT}"})
        self.assertTrue(ok)

    def test_foreign_host_rejected(self):
        ok, why = self._guard({"Host": "evil.com"})
        self.assertFalse(ok)
        self.assertIn("Host", why)

    def test_missing_host_rejected(self):
        ok, _ = self._guard({})
        self.assertFalse(ok)

    def test_foreign_origin_rejected(self):
        ok, why = self._guard({"Host": f"localhost:{app.PORT}",
                               "Origin": "https://evil.com"})
        self.assertFalse(ok)
        self.assertIn("Origin", why)

    def test_same_origin_passes(self):
        ok, _ = self._guard({"Host": f"localhost:{app.PORT}",
                             "Origin": f"http://localhost:{app.PORT}"})
        self.assertTrue(ok)

    def test_json_routes_are_not_raw_body_routes(self):
        # Les uploads ZIP sont les seules routes exemptees du Content-Type JSON.
        self.assertEqual(app._RAW_BODY_ROUTES,
                         {"/api/import-skill-zip", "/api/import-mcp-zip"})

    def test_destructive_routes_take_the_config_lock(self):
        for route in ("/api/toggle-mcp", "/api/delete-mcp", "/api/save-settings",
                      "/api/preset-apply", "/api/toggle-plugin"):
            self.assertIn(route, app._CONFIG_MUTATING_ROUTES)

    def test_slow_non_config_routes_stay_out_of_the_lock(self):
        # Sinon un pick-folder (timeout 300 s) bloquerait tous les autres POST.
        for route in ("/api/mcp-test", "/api/suggest-skill-description",
                      "/api/pick-folder"):
            self.assertNotIn(route, app._CONFIG_MUTATING_ROUTES)


class LogTailBoundedTests(unittest.TestCase):
    """Avant : read_mcp_error et _mcp_log_says_frozen faisaient read_text()
    sur des logs de plusieurs centaines de Mo pour n'en lire que la queue,
    alors que _read_log_tail (seek borne) existait deja.
    """

    def setUp(self):
        self._orig_dir = app.CLAUDE_LOGS_DIR
        self._tmp = tempfile.TemporaryDirectory()
        app.CLAUDE_LOGS_DIR = Path(self._tmp.name)

    def tearDown(self):
        app.CLAUDE_LOGS_DIR = self._orig_dir
        self._tmp.cleanup()

    def test_read_mcp_error_does_not_load_whole_file(self):
        log = app.CLAUDE_LOGS_DIR / "mcp-server-big.log"
        filler = "ligne de bruit sans interet\n" * 200_000      # ~5 Mo
        log.write_text(filler + "FATAL error: boom\n")
        reads = []
        orig = Path.read_text

        def spy(self_path, *a, **kw):
            reads.append(str(self_path))
            return orig(self_path, *a, **kw)

        Path.read_text = spy
        try:
            out = app.read_mcp_error("big")
        finally:
            Path.read_text = orig
        self.assertIsNotNone(out.get("error"))
        self.assertIn("boom", out["error"])
        self.assertNotIn(str(log), reads,
                         "le log ne doit pas etre charge entier via read_text")

    def test_frozen_detection_reads_only_the_tail(self):
        log = app.CLAUDE_LOGS_DIR / "mcp-server-frozen.log"
        log.write_text("bruit\n" * 100_000 + "transport closed unexpectedly\n")
        self.assertTrue(app._mcp_log_says_frozen("frozen", within_seconds=60))

    def test_marker_far_from_the_tail_is_not_matched(self):
        log = app.CLAUDE_LOGS_DIR / "mcp-server-old.log"
        log.write_text("transport closed unexpectedly\n" + "bruit\n" * 100_000)
        self.assertFalse(app._mcp_log_says_frozen("old", within_seconds=60))


if __name__ == "__main__":
    unittest.main()
