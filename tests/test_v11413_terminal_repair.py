"""v1.14.13 - Tests : le diagnostic decisif et la reparation via le Terminal.

Huit versions (v1.14.3 -> v1.14.12) ont corrige des differences reelles entre
le subprocess de l'app et le terminal de l'utilisateur -- et le symptome
"CLI timeout" a survecu a toutes. Cette version change la strategie :

1. LE DIAGNOSTIC TESTE LES DEUX MAILLONS JAMAIS TESTES. Un gel sans un seul
   octet ecrit, reseau sain, binaire sain, ne laisse que deux suspects :
   le node qui execute le script CLI (x86 sous Rosetta = attente infinie) et
   l'acces au jeton Keychain depuis l'app (ACL liee au binaire ; une demande
   d'autorisation invisible = gel). Deux sondes courtes tranchent AVANT le
   ping de 60 s, et une sonde `--debug` finale fait nommer par le CLI
   lui-meme l'etape ou il s'arrete.

2. LA REPARATION VIA LE TERMINAL FONCTIONNE PAR CONSTRUCTION. Plutot que de
   reconstruire l'environnement du terminal, on execute les appels DEDANS :
   Terminal.app lance un script genere (boucle triviale d'appels nus), l'app
   suit les out-*.txt et applique les descriptions avec son code teste
   (memes prompts via _suggestion_prompts, meme nettoyage via
   _clean_suggestion, meme repair_skill).
"""
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class _Done:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


GOOD_SUGGESTION = (
    "Analyse les exports CRM et audite la qualite des donnees : completude, "
    "doublons, coherence. Declencheurs : audit de donnees, nettoyage CRM, "
    "deduplication, data quality, profilage."
)


def _reset_bulk_state():
    with app._BULK_REPAIR_LOCK:
        app._BULK_REPAIR.update({
            "running": False, "total": 0, "done": 0, "current": None,
            "results": [], "started_at": None, "finished_at": None,
            "cancelled": False, "lang": None, "phase": "idle",
            "aborted": False, "abort_reason": None, "probe_seconds": None,
            "started_monotonic": None, "mode": "cli",
        })


class CredentialProbeTests(unittest.TestCase):
    """L'acces au jeton est le maillon qu'aucune version n'avait teste."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._home = app.HOME
        app.HOME = self.tmp

    def tearDown(self):
        app.HOME = self._home

    def _darwin(self):
        return patch.object(app.sys, "platform", "darwin")

    def test_credentials_file_short_circuits_the_keychain(self):
        (self.tmp / ".claude").mkdir(parents=True)
        (self.tmp / ".claude/.credentials.json").write_text("{}")

        def forbidden(*a, **kw):
            raise AssertionError("le Keychain ne doit pas etre interroge")

        with patch.object(app.subprocess, "run", forbidden), \
                patch.dict(app.os.environ, {}, clear=False):
            app.os.environ.pop("CLAUDE_CONFIG_DIR", None)
            status, _detail = app._check_cli_credentials()
        self.assertEqual(status, "file")

    def test_keychain_timeout_is_the_verdict(self):
        def hang(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 6))

        with self._darwin(), patch.object(app.subprocess, "run", hang):
            status, detail = app._check_cli_credentials()
        self.assertEqual(status, "blocked")
        self.assertIn("Keychain", detail)

    def test_readable_token_is_ok_and_never_leaks(self):
        secret = "sk-ant-oat-SECRET-VALUE"
        with self._darwin(), patch.object(
                app.subprocess, "run",
                lambda cmd, **kw: _Done(stdout=secret, returncode=0)):
            status, detail = app._check_cli_credentials()
        self.assertEqual(status, "ok")
        self.assertNotIn(secret, detail)
        self.assertNotIn("SECRET", detail)

    def test_missing_item_is_a_neutral_finding(self):
        with self._darwin(), patch.object(
                app.subprocess, "run",
                lambda cmd, **kw: _Done(stderr="not found", returncode=44)):
            status, detail = app._check_cli_credentials()
        self.assertEqual(status, "missing")
        self.assertNotIn("cassee", detail)

    def test_non_darwin_without_file_is_skipped(self):
        status, _ = app._check_cli_credentials()
        self.assertEqual(status, "skipped")


class NodeProbeTests(unittest.TestCase):
    def setUp(self):
        self._shell = dict(app._SHELL_ENV_CACHE)
        app._SHELL_ENV_CACHE.update({"done": True, "env": {}})

    def tearDown(self):
        app._SHELL_ENV_CACHE.clear()
        app._SHELL_ENV_CACHE.update(self._shell)

    def test_missing_node_is_named(self):
        with patch.object(app.shutil, "which", lambda *a, **kw: None):
            out = app._check_cli_node()
        self.assertIsNone(out["node_path"])
        self.assertIn("aucun `node`", out["node_warning"])

    def test_version_is_reported(self):
        with patch.object(app.shutil, "which",
                          lambda *a, **kw: "/x/bin/node"), \
                patch.object(app.subprocess, "run",
                             lambda cmd, **kw: _Done(stdout="v22.1.0\n")):
            out = app._check_cli_node()
        self.assertEqual(out["node_path"], "/x/bin/node")
        self.assertEqual(out["node_version"], "v22.1.0")

    def test_hanging_node_is_the_verdict(self):
        """Un `node --version` qui ne repond pas EST le gel Rosetta."""
        def hang(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 6))

        with patch.object(app.shutil, "which",
                          lambda *a, **kw: "/x/bin/node"), \
                patch.object(app.subprocess, "run", hang):
            out = app._check_cli_node()
        self.assertIn("ne repond pas", out["node_warning"])


class DiagnoseShortCircuitsTests(unittest.TestCase):
    """Un verdict acquis en quelques secondes ne doit pas faire patienter
    les 60 s d'un ping voue a caler sur la meme cause."""

    def setUp(self):
        self._path = app._claude_cli_path
        app._claude_cli_path = lambda: "/bin/claude"
        self._shell = dict(app._SHELL_ENV_CACHE)
        app._SHELL_ENV_CACHE.update({"done": True, "env": {}})

    def tearDown(self):
        app._claude_cli_path = self._path
        app._SHELL_ENV_CACHE.clear()
        app._SHELL_ENV_CACHE.update(self._shell)

    def _no_ping(self):
        def boom(*a, **kw):
            raise AssertionError("le ping ne doit pas tourner")
        return patch.object(app, "_call_claude_cli", boom)

    def test_blocked_credentials_end_the_diagnosis(self):
        with patch.object(app.subprocess, "run",
                          lambda cmd, **kw: _Done(stdout="2.3.0")), \
                patch.object(app, "_check_cli_node",
                             lambda: {"node_path": "/x/node",
                                      "node_version": "v22",
                                      "node_warning": None}), \
                patch.object(app, "_check_cli_credentials",
                             lambda **kw: ("blocked", "le Keychain n'a pas rendu le jeton")), \
                self._no_ping():
            out = app._diagnose_claude_cli()
        self.assertEqual(out["stage"], "creds")
        self.assertEqual(out["creds_status"], "blocked")
        self.assertIn("Keychain", out["error"])

    def test_hanging_node_ends_the_diagnosis(self):
        with patch.object(app.subprocess, "run",
                          lambda cmd, **kw: _Done(stdout="2.3.0")), \
                patch.object(app, "_check_cli_node",
                             lambda: {"node_path": "/x/node",
                                      "node_version": None,
                                      "node_warning": "`node --version` ne repond pas en 6 s"}), \
                self._no_ping():
            out = app._diagnose_claude_cli()
        self.assertEqual(out["stage"], "node")
        self.assertIn("ne repond pas", out["error"])


class TerminalRunBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._dirs = (app.SKILLS_DIR, app.SKILLS_DISABLED_DIR,
                      app.TERMINAL_REPAIR_DIR)
        app.SKILLS_DIR = self.tmp / "skills"
        app.SKILLS_DISABLED_DIR = self.tmp / "skills-disabled"
        app.TERMINAL_REPAIR_DIR = self.tmp / "runs"
        for name in ("alpha", "beta"):
            d = app.SKILLS_DIR / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: courte\n---\nCorps de {name}.")
        self.targets = [{"name": "alpha", "description": "courte"},
                        {"name": "beta", "description": "courte"}]

    def tearDown(self):
        (app.SKILLS_DIR, app.SKILLS_DISABLED_DIR,
         app.TERMINAL_REPAIR_DIR) = self._dirs

    def test_run_layout(self):
        run_dir, script = app._build_terminal_repair_run(self.targets, "fr")
        self.assertTrue((run_dir / "plan.json").exists())
        self.assertTrue((run_dir / "prompt-000.txt").exists())
        self.assertTrue((run_dir / "prompt-001.txt").exists())
        self.assertTrue(script.exists())
        self.assertTrue(app.os.access(script, app.os.X_OK))

    def test_prompts_are_the_production_prompts(self):
        run_dir, _ = app._build_terminal_repair_run(self.targets, "fr")
        payload = (run_dir / "prompt-000.txt").read_text()
        self.assertIn(app._SUGGEST_SYSTEM_PROMPT[:60], payload)
        self.assertIn("Write the description in French", payload)
        self.assertIn("Corps de alpha.", payload)

    def test_script_uses_the_bare_proven_call(self):
        _, script = app._build_terminal_repair_run(self.targets, None)
        body = script.read_text()
        self.assertIn("claude -p --output-format text", body)
        for optional in ("--strict-mcp-config", "--no-session-persistence",
                         "--disable-slash-commands", "--safe-mode",
                         "--system-prompt"):
            self.assertNotIn(optional, body)

    def test_outputs_are_atomic_and_the_run_signals_its_end(self):
        _, script = app._build_terminal_repair_run(self.targets, None)
        body = script.read_text()
        self.assertIn('.out-000.tmp', body)
        self.assertIn('mv "$DIR/.out-000.tmp" "$DIR/out-000.txt"', body)
        self.assertIn('touch "$DIR/DONE"', body)

    def test_hostile_display_name_cannot_escape_the_script(self):
        label = app._shell_display_label('x"; rm -rf $HOME; echo "')
        self.assertNotIn('"', label)
        self.assertNotIn(";", label)
        self.assertNotIn("$", label)


class TerminalWatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._dirs = (app.SKILLS_DIR, app.SKILLS_DISABLED_DIR,
                      app.TERMINAL_REPAIR_DIR)
        app.SKILLS_DIR = self.tmp / "skills"
        app.SKILLS_DISABLED_DIR = self.tmp / "skills-disabled"
        app.TERMINAL_REPAIR_DIR = self.tmp / "runs"
        for name in ("alpha", "beta"):
            d = app.SKILLS_DIR / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: courte\n---\nCorps.")
        self.targets = [{"name": "alpha", "description": "courte"},
                        {"name": "beta", "description": "courte"}]
        self.repaired = []
        self._repair = app.repair_skill
        app.repair_skill = lambda name, description=None, **kw: (
            self.repaired.append((name, description)) or (True, "ok"))
        self._notify = app._notify
        app._notify = lambda *a, **kw: None
        _reset_bulk_state()
        with app._BULK_REPAIR_LOCK:
            app._BULK_REPAIR.update({"running": True, "phase": "running",
                                     "total": 2, "mode": "terminal",
                                     "started_monotonic": time.monotonic()})

    def tearDown(self):
        (app.SKILLS_DIR, app.SKILLS_DISABLED_DIR,
         app.TERMINAL_REPAIR_DIR) = self._dirs
        app.repair_skill = self._repair
        app._notify = self._notify
        _reset_bulk_state()

    def test_results_are_applied_and_failures_carry_stderr(self):
        run_dir, _ = app._build_terminal_repair_run(self.targets, None)
        (run_dir / "out-000.txt").write_text(GOOD_SUGGESTION)
        (run_dir / "err-001.txt").write_text("Error: something exploded\n")
        (run_dir / "DONE").touch()
        app._terminal_repair_watcher(run_dir, self.targets, None)
        self.assertEqual([n for n, _ in self.repaired], ["alpha"])
        self.assertIn("Declencheurs", self.repaired[0][1])
        snap = app.bulk_repair_status()
        self.assertEqual(snap["ok_count"], 1)
        self.assertEqual(snap["failed_count"], 1)
        failed = [r for r in snap["results"] if not r["ok"]][0]
        self.assertEqual(failed["name"], "beta")
        self.assertIn("exploded", failed["error"])
        self.assertEqual(snap["outcome"], "partial")

    def test_cancel_stops_without_applying_more(self):
        run_dir, _ = app._build_terminal_repair_run(self.targets, None)
        with app._BULK_REPAIR_LOCK:
            app._BULK_REPAIR["cancelled"] = True
        app._terminal_repair_watcher(run_dir, self.targets, None)
        self.assertEqual(self.repaired, [])
        snap = app.bulk_repair_status()
        self.assertFalse(snap["running"])
        self.assertEqual(snap["phase"], "finished")
        # Zero resultat + annule = "idle" pour l'UI (meme semantique que le
        # chemin CLI) : le panneau se ferme, rien n'a ete touche.
        self.assertEqual(snap["outcome"], "idle")

    def test_silence_aborts_with_a_useful_message(self):
        run_dir, _ = app._build_terminal_repair_run(self.targets, None)
        with patch.object(app, "TERMINAL_REPAIR_STALL_SECONDS", 0):
            app._terminal_repair_watcher(run_dir, self.targets, None)
        snap = app.bulk_repair_status()
        self.assertEqual(snap["outcome"], "aborted")
        self.assertIn("Terminal", snap["abort_reason"])


class TerminalStartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._dirs = (app.SKILLS_DIR, app.SKILLS_DISABLED_DIR,
                      app.TERMINAL_REPAIR_DIR)
        app.SKILLS_DIR = self.tmp / "skills"
        app.SKILLS_DISABLED_DIR = self.tmp / "skills-disabled"
        app.TERMINAL_REPAIR_DIR = self.tmp / "runs"
        for name in ("alpha", "beta"):
            d = app.SKILLS_DIR / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: courte\n---\nCorps.")
        self.osascript_calls = []
        self._run = app.subprocess.run

        def fake_run(cmd, **kw):
            self.osascript_calls.append(cmd)
            return _Done(returncode=0)

        app.subprocess.run = fake_run
        self._watcher = app._terminal_repair_watcher
        self.watched = []
        app._terminal_repair_watcher = lambda *a, **kw: self.watched.append(a)
        _reset_bulk_state()

    def tearDown(self):
        (app.SKILLS_DIR, app.SKILLS_DISABLED_DIR,
         app.TERMINAL_REPAIR_DIR) = self._dirs
        app.subprocess.run = self._run
        app._terminal_repair_watcher = self._watcher
        _reset_bulk_state()

    def test_start_opens_terminal_and_tracks_state(self):
        ok, payload = app.start_terminal_repair(lang="fr")
        self.assertTrue(ok, payload)
        self.assertTrue(payload["started"])
        self.assertEqual(payload["total"], 2)
        osa = " ".join(self.osascript_calls[0])
        self.assertIn("Terminal", osa)
        self.assertIn("run.sh", osa)
        snap = app.bulk_repair_status()
        self.assertTrue(snap["running"])
        self.assertEqual(snap["mode"], "terminal")

    def test_names_filter_restricts_the_batch(self):
        ok, payload = app.start_terminal_repair(names=["beta"])
        self.assertTrue(ok, payload)
        self.assertEqual(payload["total"], 1)

    def test_refused_while_a_repair_runs(self):
        with app._BULK_REPAIR_LOCK:
            app._BULK_REPAIR["running"] = True
        ok, _msg = app.start_terminal_repair()
        self.assertFalse(ok)

    def test_osascript_failure_does_not_leave_a_running_state(self):
        app.subprocess.run = lambda cmd, **kw: _Done(
            stderr="Terminal got an error", returncode=1)
        ok, msg = app.start_terminal_repair()
        self.assertFalse(ok)
        self.assertIn("Terminal", msg)
        self.assertFalse(app.bulk_repair_status()["running"])


class WiringTests(unittest.TestCase):
    """Verifications de source, dans l'esprit de test_ui_bundle_integrity."""

    def test_route_is_registered_as_post(self):
        import inspect
        src_post = inspect.getsource(app.Handler.do_POST)
        self.assertIn("repair-all-terminal", src_post)

    def test_ui_offers_the_terminal_path(self):
        self.assertIn("startTerminalRepair", app.HTML)
        self.assertEqual(app.HTML.count("bulk_terminal_btn:"), 2)  # fr + en
        self.assertIn("confirm_terminal_repair", app.HTML)

    def test_diag_ui_shows_the_new_probes(self):
        for needle in ("d.node_path", "d.creds_status", "d.debug_tail"):
            self.assertIn(needle, app.HTML)

    def test_suggest_still_uses_the_shared_helpers(self):
        """La modal et le Terminal doivent rester le MEME pipeline."""
        import inspect
        src = inspect.getsource(app.suggest_skill_description)
        self.assertIn("_suggestion_prompts", src)
        self.assertIn("_clean_suggestion", src)


if __name__ == "__main__":
    unittest.main()
