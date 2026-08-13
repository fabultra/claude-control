"""v1.14.12 - Tests : le meme claude que le terminal, l'echelle de repli,
et les gardes de chemins.

Trois volets, issus de l'audit v1.14.12 :

1. FIDELITE AU TERMINAL. "Ca marche dans mon terminal" designe un binaire
   precis : celui que le PATH du shell de connexion resout. _claude_cli_path
   le cherchait dans une liste devinee (brew avant nvm, tri nvm reste
   ALPHABETIQUE ici alors que v1.14.11 l'avait corrige dans _cli_env) et
   pouvait donc executer un autre claude -- plus vieux, voire installe sous
   un node 9 x86_64 qui attend Rosetta sans un octet de sortie : le
   symptome exact du "CLI timeout". Desormais : PATH du process, puis PATH
   du shell de connexion, puis les candidats devines (tri numerique). Et
   _cli_env insere les entrees du shell avant les suppositions, pour que le
   `#!/usr/bin/env node` du CLI resolve le meme node que le terminal.

2. ECHELLE DE REPLI. Le repli v1.14.6 jetait toutes les options d'un coup :
   l'appel nu recharge la config MCP complete de l'utilisateur -- la
   lenteur exacte que --strict-mcp-config evite, payee a chaque skill d'un
   lot. Le barreau intermediaire garde les MCP coupes. Et une option
   devenue inconnue du CLI (exit `unknown option`, pas un timeout)
   emprunte la meme echelle au lieu de tuer la reparation.

3. GARDES DE CHEMINS. toggle_skill ne validait pas son nom (deplacement de
   dossier arbitraire via ../) ; import_mcp_git derivait un nom de l'URL
   sans validation, et `https://x/..` faisait un rmtree de ~/.claude
   entier. _save_settings refuse desormais d'ecraser un settings.json
   illisible (toggle_plugin le remplacait par {enabledPlugins} seul).
"""
import shutil as real_shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402

# Reference capturee AVANT tout patch : app.shutil et ce module sont le MEME
# objet module, donc patcher app.shutil.which patche aussi real_shutil.which.
_REAL_WHICH = real_shutil.which


class _Done:
    def __init__(self, stdout="pong", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _make_exec(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


class CliPathPrefersTheTerminalsClaudeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._home = app.HOME
        app.HOME = self.tmp / "home"
        self._shell = dict(app._SHELL_ENV_CACHE)
        app._SHELL_ENV_CACHE.clear()
        app._SHELL_ENV_CACHE.update({"done": True, "env": {}})

    def tearDown(self):
        app.HOME = self._home
        app._SHELL_ENV_CACHE.clear()
        app._SHELL_ENV_CACHE.update(self._shell)

    def _finder_which(self):
        """Simule le lancement Finder : le PATH du process ne connait pas
        claude, mais which reste reel des qu'un path= explicite est fourni."""
        def fake(cmd, path=None):
            if path is None:
                return None
            return _REAL_WHICH(cmd, path=path)
        return fake

    def test_process_path_still_wins(self):
        with patch.object(app.shutil, "which",
                          lambda cmd, path=None: "/process/bin/claude"):
            self.assertEqual(app._claude_cli_path(), "/process/bin/claude")

    def test_shell_path_claude_beats_the_guesses(self):
        """Le claude du terminal de l'utilisateur, pas un equivalent devine."""
        shell_bin = self.tmp / "shellbin"
        _make_exec(shell_bin / "claude")
        # Un candidat devine existe AUSSI : il ne doit pas gagner.
        _make_exec(app.HOME / ".npm-global/bin/claude")
        app._SHELL_ENV_CACHE["env"] = {"PATH": str(shell_bin)}
        with patch.object(app.shutil, "which", self._finder_which()):
            self.assertEqual(app._claude_cli_path(), str(shell_bin / "claude"))

    def test_guesses_still_work_when_the_shell_is_mute(self):
        _make_exec(app.HOME / ".npm-global/bin/claude")
        with patch.object(app.shutil, "which", lambda cmd, path=None: None):
            self.assertEqual(app._claude_cli_path(),
                             str(app.HOME / ".npm-global/bin/claude"))

    def test_nvm_guesses_are_scanned_newest_first(self):
        """Le defaut exact de v1.14.11, sur l'autre moitie du chemin : le tri
        alphabetique classait v9 avant v22 et choisissait le claude installe
        sous le node le plus ancien."""
        _make_exec(app.HOME / ".nvm/versions/node/v9.11.2/bin/claude")
        _make_exec(app.HOME / ".nvm/versions/node/v22.1.0/bin/claude")
        with patch.object(app.shutil, "which", lambda cmd, path=None: None):
            found = app._claude_cli_path()
        self.assertTrue(found.endswith("v22.1.0/bin/claude"), found)

    def test_not_found_anywhere_returns_none(self):
        with patch.object(app.shutil, "which", lambda cmd, path=None: None):
            self.assertIsNone(app._claude_cli_path())


class CliEnvMergesTheShellPathTests(unittest.TestCase):
    def setUp(self):
        self._shell = dict(app._SHELL_ENV_CACHE)
        app._SHELL_ENV_CACHE.clear()
        app._SHELL_ENV_CACHE.update({"done": True, "env": {}})

    def tearDown(self):
        app._SHELL_ENV_CACHE.clear()
        app._SHELL_ENV_CACHE.update(self._shell)

    def _entries(self, process_path, shell_path=None):
        if shell_path is not None:
            app._SHELL_ENV_CACHE["env"] = {"PATH": shell_path}
        with patch.object(app.os, "environ", {"PATH": process_path}):
            return app._cli_env()["PATH"].split(":")

    def test_shell_entries_come_after_the_process_path(self):
        entries = self._entries("/usr/bin", "/shell/a:/shell/b")
        self.assertEqual(entries[0], "/usr/bin")
        self.assertEqual(entries[1:3], ["/shell/a", "/shell/b"])

    def test_shell_entries_come_before_the_guesses(self):
        entries = self._entries("/usr/bin", "/shell/a")
        self.assertLess(entries.index("/shell/a"),
                        entries.index("/opt/homebrew/bin"))

    def test_shell_order_is_preserved(self):
        """C'est l'ordre du PATH du terminal qui choisit le node execute par
        le shebang du CLI : le bousculer reintroduirait la panne."""
        entries = self._entries("/usr/bin", "/shell/z:/shell/a:/shell/m")
        i = entries.index("/shell/z")
        self.assertEqual(entries[i:i + 3], ["/shell/z", "/shell/a", "/shell/m"])

    def test_no_duplicates_between_process_and_shell(self):
        entries = self._entries("/usr/bin:/shell/a", "/shell/a:/shell/b")
        self.assertEqual(entries.count("/shell/a"), 1)
        # et la premiere occurrence garde sa position (celle du process)
        self.assertEqual(entries[1], "/shell/a")


class UnknownOptionDescendsTheLadderTests(unittest.TestCase):
    """Une option que le CLI ne connait plus est la meme panne qu'une option
    qui bloque : elle doit emprunter la meme echelle de repli."""

    def setUp(self):
        self.calls = []
        self._orig_run = app.subprocess.run
        self._orig_path = app._claude_cli_path
        app._claude_cli_path = lambda: "/bin/claude"
        self._shell = dict(app._SHELL_ENV_CACHE)
        app._SHELL_ENV_CACHE.update({"done": True, "env": {}})
        app._CLI_FALLBACK.update({"used": False, "rung": 0})

    def tearDown(self):
        app.subprocess.run = self._orig_run
        app._claude_cli_path = self._orig_path
        app._SHELL_ENV_CACHE.clear()
        app._SHELL_ENV_CACHE.update(self._shell)
        app._CLI_FALLBACK.update({"used": False, "rung": 0})

    def _run_with(self, behaviour):
        def fake(cmd, **kw):
            self.calls.append({"cmd": cmd, "kw": kw})
            return behaviour(len(self.calls), cmd, kw)
        app.subprocess.run = fake

    def test_unknown_option_falls_back_and_succeeds(self):
        self._run_with(lambda n, cmd, kw: _Done(
            stdout="", stderr="error: unknown option '--disable-slash-commands'",
            returncode=1) if n == 1 else _Done(stdout="pong"))
        self.assertEqual(app._call_claude_cli("x"), "pong")
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(app._CLI_FALLBACK["used"])

    def test_the_failing_rung_is_not_replayed(self):
        # Le stub rejette la COMMANDE qui porte le flag toxique, quel que
        # soit le rang de l'appel : c'est le comportement d'un vrai CLI.
        self._run_with(lambda n, cmd, kw: _Done(
            stdout="", stderr="error: unknown option '--no-session-persistence'",
            returncode=1) if "--no-session-persistence" in cmd
            else _Done(stdout="pong"))
        app._call_claude_cli("x")
        self.assertEqual(len(self.calls), 2)
        self.calls.clear()
        app._call_claude_cli("y")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("--no-session-persistence", self.calls[0]["cmd"])

    def test_unknown_core_option_names_the_real_cause(self):
        """Apres l'echelle, un `unknown option` restant vise les options de
        BASE : le message doit orienter vers la mise a jour du CLI, plus
        accuser le prompt (il passe par stdin depuis v1.14.1)."""
        self._run_with(lambda n, cmd, kw: _Done(
            stdout="", stderr="error: unknown option '--output-format'",
            returncode=1))
        with self.assertRaises(RuntimeError) as ctx:
            app._call_claude_cli("x")
        self.assertEqual(len(self.calls), 3)  # les trois barreaux
        self.assertIn("npm install", str(ctx.exception))
        self.assertNotIn("prompt", str(ctx.exception).lower())

    def test_not_logged_in_does_not_descend(self):
        """Pas une panne d'option : redescendre l'echelle ne changerait rien
        et retarderait le verdict."""
        self._run_with(lambda n, cmd, kw: _Done(
            stdout="Not logged in. Please run /login", returncode=1))
        with self.assertRaises(app.ClaudeCliNotLoggedIn):
            app._call_claude_cli("x")
        self.assertEqual(len(self.calls), 1)


class ProbeMessagesAreCurrentTests(unittest.TestCase):
    def test_empty_output_hint_does_not_cite_safe_mode(self):
        """Le message conseillait `claude -p --safe-mode` : l'option retiree
        en v1.14.6 parce qu'elle GELE le CLI. Suivre le conseil bloquait le
        terminal de l'utilisateur."""
        with patch.object(app, "_call_claude_cli", lambda *a, **k: "   "):
            ok, _elapsed, msg = app._bulk_repair_probe()
        self.assertFalse(ok)
        self.assertNotIn("--safe-mode", msg)
        self.assertIn("--output-format text", msg)

    def test_timeout_message_reports_the_total_wait(self):
        """L'exception porte l'attente TOTALE (tous les barreaux) : le
        message du preflight doit citer ce chiffre, pas le budget d'un seul
        appel."""
        def stall(*a, **k):
            raise app.ClaudeCliTimeout(150, "")
        with patch.object(app, "_call_claude_cli", stall):
            ok, _elapsed, msg = app._bulk_repair_probe()
        self.assertFalse(ok)
        self.assertIn("150", msg)


class ToggleSkillValidatesItsNameTests(unittest.TestCase):
    """toggle_skill etait la seule fonction de la famille sans garde : un
    nom '../X' suivi de .rename() deplacait n'importe quel dossier."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._dirs = (app.SKILLS_DIR, app.SKILLS_DISABLED_DIR)
        app.SKILLS_DIR = self.tmp / "skills"
        app.SKILLS_DISABLED_DIR = self.tmp / "skills-disabled"

    def tearDown(self):
        app.SKILLS_DIR, app.SKILLS_DISABLED_DIR = self._dirs

    def test_traversal_names_are_rejected(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        ok, msg = app.toggle_skill("../outside")
        self.assertFalse(ok)
        self.assertIn("invalide", msg)
        self.assertTrue(outside.is_dir(), "le dossier ne doit pas bouger")

    def test_absolute_and_empty_names_are_rejected(self):
        for bad in ("/etc", "", "..", ".hidden", "a\\b"):
            with self.subTest(name=bad):
                self.assertFalse(app.toggle_skill(bad)[0])

    def test_normal_names_still_toggle(self):
        (app.SKILLS_DIR / "demo").mkdir(parents=True)
        ok, _ = app.toggle_skill("demo")
        self.assertTrue(ok)
        self.assertTrue((app.SKILLS_DISABLED_DIR / "demo").is_dir())


class GitImportNamesAreGuardedTests(unittest.TestCase):
    """`https://x/..` donnait name='..', donc target=~/.claude, et le rmtree
    du 'clone existant' EFFACAIT tout ~/.claude avant meme le clone."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._imported = app.IMPORTED_REPOS_DIR
        self._skills = app.SKILLS_DIR
        app.IMPORTED_REPOS_DIR = self.tmp / "claude" / "imported-mcps"
        app.SKILLS_DIR = self.tmp / "claude" / "skills"
        self._orig_run = app.subprocess.run

        def forbidden(*a, **kw):
            raise AssertionError("git ne doit pas etre invoque sur un nom rejete")
        app.subprocess.run = forbidden

    def tearDown(self):
        app.IMPORTED_REPOS_DIR = self._imported
        app.SKILLS_DIR = self._skills
        app.subprocess.run = self._orig_run

    def test_dotdot_url_does_not_wipe_the_parent(self):
        marker = self.tmp / "claude" / "marker.txt"
        marker.parent.mkdir(parents=True)
        marker.write_text("still here")
        ok, _msg = app.import_mcp_git("https://x.example/..")
        self.assertFalse(ok)
        self.assertTrue(marker.exists(), "~/.claude a ete touche")

    def test_bare_dotgit_url_is_rejected(self):
        """`https://x/.git` : le suffixe .git est retire, le nom devient
        vide, et target pointait sur imported-mcps/ lui-meme -- rase par le
        rmtree du 'clone existant'."""
        app.IMPORTED_REPOS_DIR.mkdir(parents=True)
        keep = app.IMPORTED_REPOS_DIR / "autre-repo"
        keep.mkdir()
        ok, _msg = app.import_mcp_git("https://x.example/.git")
        self.assertFalse(ok)
        self.assertTrue(keep.is_dir(), "imported-mcps/ a ete rase")

    def test_skill_git_import_has_the_same_guard(self):
        ok, _msg = app.import_skill_git("https://x.example/..")
        self.assertFalse(ok)


class SettingsAreNotClobberedTests(unittest.TestCase):
    """Un settings.json casse etait lu comme {} puis reecrit avec la seule
    cle enabledPlugins : hooks, permissions et env perdus en cochant une
    case."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._settings = app.SETTINGS_FILE
        self._backup = app.BACKUP_DIR
        app.SETTINGS_FILE = self.tmp / "settings.json"
        app.BACKUP_DIR = self.tmp / "backups"

    def tearDown(self):
        app.SETTINGS_FILE = self._settings
        app.BACKUP_DIR = self._backup

    def test_corrupt_settings_refuse_the_overwrite(self):
        app.SETTINGS_FILE.write_text('{"hooks": {...pas du json')
        with self.assertRaises(RuntimeError) as ctx:
            app._save_settings({"enabledPlugins": {}})
        self.assertIn("settings.json", str(ctx.exception))
        self.assertEqual(app.SETTINGS_FILE.read_text(),
                         '{"hooks": {...pas du json')

    def test_valid_settings_still_save_with_a_backup(self):
        app.SETTINGS_FILE.write_text('{"hooks": {"a": 1}}')
        app._save_settings({"hooks": {"a": 1}, "enabledPlugins": {"p": True}})
        self.assertIn("enabledPlugins", app.SETTINGS_FILE.read_text())
        backups = list(app.BACKUP_DIR.glob("settings.json.backup.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), '{"hooks": {"a": 1}}')

    def test_missing_file_saves_without_backup(self):
        app._save_settings({"enabledPlugins": {}})
        self.assertTrue(app.SETTINGS_FILE.exists())


class UiRegressionGuardsTests(unittest.TestCase):
    """Verifications de source sur la constante HTML, dans l'esprit de
    test_ui_bundle_integrity : le correctif escJsAttr (audit v1.13.4,
    constat 9) n'avait ete applique qu'au renderer MCP."""

    def test_cli_diagnose_is_called_via_post(self):
        self.assertIn("api('/api/claude-cli-diagnose'", app.HTML)
        self.assertNotIn("fetch('/api/claude-cli-diagnose')", app.HTML)

    def test_js_positions_use_escjsattr(self):
        for needle in (
            "togglePlugin('${fnJs}')",
            "deletePlugin('${fnJs}')",
            "toggleSkill('${escJsAttr(sk.name)}')",
            "deleteSkill('${escJsAttr(sk.name)}')",
            "openRepairSkill('${escJsAttr(sk.name)}')",
            "toggleCommand('${neJs}')",
            "applyPreset('${neJs}')",
            "deletePreset('${neJs}')",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, app.HTML)

    def test_diagnose_route_is_not_a_get(self):
        # La route GET a ete retiree ; la route POST existe.
        import inspect
        src = inspect.getsource(app.Handler.do_GET)
        self.assertNotIn("claude-cli-diagnose", src)
        src_post = inspect.getsource(app.Handler.do_POST)
        self.assertIn("claude-cli-diagnose", src_post)


if __name__ == "__main__":
    unittest.main()
