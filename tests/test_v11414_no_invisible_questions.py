"""v1.14.14 - Tests : plus aucune question invisible du CLI.

La capture d'ecran decisive (v1.14.13 en place) : les TROIS barreaux de
l'echelle calent, zero octet ecrit, pendant que les sondes node et jeton
passent. Ce qui gele sans rien ecrire ET sans etre l'auth ni le binaire,
c'est une question interactive posee via /dev/tty -- invisible depuis un
process sans terminal. Et l'archeologie git le corrobore : l'epoque ou "ca
marchait" (<= v1.13.4) n'imposait aucun cwd au CLI, qui tournait dans un
dossier deja approuve ; depuis v1.14.1/v1.14.9 il tourne dans un dossier
cree par l'app que personne n'a jamais approuve -- et la question de
confiance ("Do you trust the files in this folder?") a deja ete observee
mot pour mot dans une sortie partielle de l'ere v1.14.3.

Trois verrous :
1. _pretrust_cli_workdir : l'entree que le CLI ecrit quand on repond oui
   est posee dans ~/.claude.json AVANT le premier appel.
2. start_new_session=True sur tous les spawns claude : plus de terminal de
   controle, donc un /dev/tty impossible -- une question devient une erreur
   visible au lieu d'un gel.
3. Le diagnostic est loggue et copiable en un clic (bouton "Copier le
   rapport") : les donnees voyagent enfin depuis la machine concernee.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class _Done:
    def __init__(self, stdout="pong", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class PretrustTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._cfg = app.CLI_CONFIG_PATH
        self._wd = app.CLI_WORKDIR
        app.CLI_CONFIG_PATH = self.tmp / "claude.json"
        app.CLI_WORKDIR = self.tmp / "workdir"
        app._CLI_WORKDIR_TRUSTED["done"] = False

    def tearDown(self):
        app.CLI_CONFIG_PATH = self._cfg
        app.CLI_WORKDIR = self._wd
        # cf. tests/__init__.py : hors de ces tests, le pretrust reste desarme
        app._CLI_WORKDIR_TRUSTED["done"] = True

    def test_missing_config_means_no_write(self):
        """CLI jamais lance : on ne cree pas son fichier d'etat a sa place."""
        app._pretrust_cli_workdir()
        self.assertFalse(app.CLI_CONFIG_PATH.exists())

    def test_entry_is_added_and_the_rest_is_preserved(self):
        app.CLI_CONFIG_PATH.write_text(json.dumps(
            {"hasCompletedOnboarding": True,
             "projects": {"/autre/projet": {"allowedTools": ["Bash"]}}}))
        app._pretrust_cli_workdir()
        data = json.loads(app.CLI_CONFIG_PATH.read_text())
        entry = data["projects"][str(app.CLI_WORKDIR)]
        self.assertTrue(entry["hasTrustDialogAccepted"])
        self.assertTrue(entry["hasCompletedProjectOnboarding"])
        self.assertTrue(data["hasCompletedOnboarding"])
        self.assertEqual(data["projects"]["/autre/projet"]["allowedTools"],
                         ["Bash"])

    def test_already_trusted_means_zero_rewrite(self):
        """Pas de course avec les ecritures du CLI : si l'entree est la, on
        ne reecrit rien."""
        app.CLI_WORKDIR.mkdir(parents=True)
        content = json.dumps({"projects": {
            str(app.CLI_WORKDIR): {"hasTrustDialogAccepted": True},
        }, "marqueur": "intact"})
        app.CLI_CONFIG_PATH.write_text(content)
        app._pretrust_cli_workdir()
        self.assertEqual(app.CLI_CONFIG_PATH.read_text(), content)

    def test_malformed_config_is_left_alone(self):
        app.CLI_CONFIG_PATH.write_text("{pas du json")
        app._pretrust_cli_workdir()
        self.assertEqual(app.CLI_CONFIG_PATH.read_text(), "{pas du json")

    def test_runs_once_per_process(self):
        app.CLI_CONFIG_PATH.write_text("{}")
        app._pretrust_cli_workdir()
        first = app.CLI_CONFIG_PATH.read_text()
        app.CLI_CONFIG_PATH.write_text("{}")
        app._pretrust_cli_workdir()  # memo arme : plus aucune ecriture
        self.assertEqual(app.CLI_CONFIG_PATH.read_text(), "{}")
        self.assertIn("hasTrustDialogAccepted", first)


class NoControllingTtyTests(unittest.TestCase):
    """Un CLI qui veut poser une question ne doit avoir aucun /dev/tty ou la
    poser : l'echec immediat vaut mieux que le gel eternel."""

    def setUp(self):
        self.calls = []
        self._orig_run = app.subprocess.run
        self._orig_path = app._claude_cli_path
        app._claude_cli_path = lambda: "/bin/claude"
        self._shell = dict(app._SHELL_ENV_CACHE)
        app._SHELL_ENV_CACHE.update({"done": True, "env": {}})
        app._CLI_FALLBACK.update({"used": False, "rung": 0})

        def fake(cmd, **kw):
            self.calls.append({"cmd": cmd, "kw": kw})
            return _Done()

        app.subprocess.run = fake

    def tearDown(self):
        app.subprocess.run = self._orig_run
        app._claude_cli_path = self._orig_path
        app._SHELL_ENV_CACHE.clear()
        app._SHELL_ENV_CACHE.update(self._shell)
        app._CLI_FALLBACK.update({"used": False, "rung": 0})

    def test_production_call_detaches_from_the_tty(self):
        app._call_claude_cli("x")
        self.assertIs(self.calls[0]["kw"].get("start_new_session"), True)

    def test_probe_detaches_from_the_tty(self):
        app._cli_probe(())
        self.assertIs(self.calls[0]["kw"].get("start_new_session"), True)

    def test_debug_probe_detaches_from_the_tty(self):
        app._cli_debug_tail(timeout=5)
        self.assertIs(self.calls[0]["kw"].get("start_new_session"), True)


class DiagReportTests(unittest.TestCase):
    def test_diagnosis_carries_the_app_version_and_is_logged(self):
        logged = []
        with patch.object(app, "_claude_cli_path", lambda: None), \
                patch.object(app, "_log", logged.append):
            out = app._diagnose_claude_cli()
        self.assertIn("app_version", out)
        self.assertTrue(any(str(m).startswith("cli-diagnose:") for m in logged))

    def test_log_line_is_bounded(self):
        """Le rapport part dans le log fichier : jamais plus de ~2 Ko."""
        logged = []
        big = {"debug_tail": "x" * 10_000}
        with patch.object(app, "_diagnose_claude_cli_stages", lambda: dict(big)), \
                patch.object(app, "_log", logged.append):
            app._diagnose_claude_cli()
        line = [m for m in logged if str(m).startswith("cli-diagnose:")][0]
        self.assertLess(len(line), 2200)


class WiringTests(unittest.TestCase):
    def test_ui_has_the_copy_button(self):
        self.assertIn("copyCliDiagReport", app.HTML)
        self.assertIn("LAST_CLI_DIAG_REPORT", app.HTML)
        self.assertEqual(app.HTML.count("cli_diag_copy:"), 2)  # fr + en

    def test_every_claude_spawn_is_detached(self):
        # _attempt (production), _cli_probe, _cli_debug_tail, --version.
        src = Path(app.__file__).read_text()
        self.assertGreaterEqual(src.count("start_new_session=True"), 4)


if __name__ == "__main__":
    unittest.main()
