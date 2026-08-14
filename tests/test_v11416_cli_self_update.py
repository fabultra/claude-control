"""v1.14.16 - Tests : mise a jour du CLI en un clic depuis l'app.

Demande utilisateur directe ("l'app devrait checker la derniere version du
CLI et l'installer"), apres le cas reel : binaire fige a 2.1.173, public a
2.1.232, updater interne coince, et une commande de terminal comme seul
remede -- contraire a la promesse de l'app. Le bouton du diagnostic execute
l'installeur OFFICIEL Anthropic (domaine epingle, jamais une URL de
requete), verifie la version obtenue, et rearme l'app (echelle de repli en
haut, capture du shell invalidee).
"""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class _Done:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class SelfUpdateTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.version_replies = ["2.1.173 (Claude Code)",
                                "2.1.232 (Claude Code)"]
        self._version_i = 0
        self.installer_result = _Done(stdout="installed", returncode=0)
        self._orig_run = app.subprocess.run
        self._orig_path = app._claude_cli_path
        app._claude_cli_path = lambda: "/x/bin/claude"
        self._shell = dict(app._SHELL_ENV_CACHE)
        app._SHELL_ENV_CACHE.update({"done": True, "env": {}})
        self._fallback = dict(app._CLI_FALLBACK)
        app._CLI_FALLBACK.update({"used": True, "rung": 2})

        def fake(cmd, **kw):
            self.calls.append({"cmd": cmd, "kw": kw})
            if "--version" in cmd:
                reply = self.version_replies[
                    min(self._version_i, len(self.version_replies) - 1)]
                self._version_i += 1
                return _Done(stdout=reply)
            if len(cmd) >= 2 and cmd[1] == "-c":
                return self.installer_result
            return _Done(stdout="PATH=/usr/bin")  # capture d'env shell

        app.subprocess.run = fake

    def tearDown(self):
        app.subprocess.run = self._orig_run
        app._claude_cli_path = self._orig_path
        app._SHELL_ENV_CACHE.clear()
        app._SHELL_ENV_CACHE.update(self._shell)
        app._CLI_FALLBACK.update(self._fallback)

    def _installer_call(self):
        return next(c for c in self.calls
                    if len(c["cmd"]) >= 2 and c["cmd"][1] == "-c")

    def test_success_reports_both_versions(self):
        ok, msg = app.update_claude_cli()
        self.assertTrue(ok, msg)
        self.assertIn("2.1.173", msg)
        self.assertIn("2.1.232", msg)

    def test_only_the_pinned_official_installer_runs(self):
        app.update_claude_cli()
        call = self._installer_call()
        self.assertIn("claude.ai/install.sh", call["cmd"][2])
        self.assertIs(call["kw"].get("start_new_session"), True)

    def test_success_rearms_the_fallback_ladder(self):
        """Nouveau binaire : les options optimisees meritent un nouvel
        essai au lieu de rester sur le barreau memorise de l'ancien."""
        app.update_claude_cli()
        self.assertEqual(app._CLI_FALLBACK["rung"], 0)
        self.assertFalse(app._CLI_FALLBACK["used"])

    def test_installer_failure_is_reported_and_changes_nothing(self):
        self.installer_result = _Done(stderr="curl: (6) no DNS", returncode=1)
        ok, msg = app.update_claude_cli()
        self.assertFalse(ok)
        self.assertIn("echoue", msg)
        self.assertEqual(app._CLI_FALLBACK["rung"], 2)

    def test_installer_timeout_names_the_manual_command(self):
        def fake(cmd, **kw):
            self.calls.append({"cmd": cmd, "kw": kw})
            if "--version" in cmd:
                return _Done(stdout="2.1.173")
            if len(cmd) >= 2 and cmd[1] == "-c":
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 300))
            return _Done(stdout="")

        app.subprocess.run = fake
        ok, msg = app.update_claude_cli()
        self.assertFalse(ok)
        self.assertIn("install.sh", msg)

    def test_unchanged_version_is_a_failure_with_a_lead(self):
        """Installeur OK mais meme version = un autre binaire masque le
        nouveau : le message donne la piste (`which claude`)."""
        self.version_replies = ["2.1.173 (Claude Code)",
                                "2.1.173 (Claude Code)"]
        ok, msg = app.update_claude_cli()
        self.assertFalse(ok)
        self.assertIn("n'a pas bouge", msg)
        self.assertIn("which claude", msg)


class WiringTests(unittest.TestCase):
    def test_route_is_registered_as_post(self):
        import inspect
        self.assertIn("/api/cli-update",
                      inspect.getsource(app.Handler.do_POST))

    def test_ui_offers_the_update_and_names_what_is_untouched(self):
        self.assertIn("updateClaudeCli", app.HTML)
        self.assertEqual(app.HTML.count("confirm_cli_update:"), 2)  # fr + en
        self.assertEqual(app.HTML.count("cli_update_btn:"), 2)
        # La confusion Claude Desktop / CLI est documentee : la confirmation
        # doit nommer ce qui n'est PAS touche.
        self.assertIn("NE TOUCHE PAS Claude Desktop", app.HTML)
        self.assertIn("Does NOT touch Claude Desktop", app.HTML)


if __name__ == "__main__":
    unittest.main()
