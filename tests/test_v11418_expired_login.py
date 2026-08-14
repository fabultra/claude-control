"""v1.14.18 - Tests : une session expiree n'est plus jamais un "timeout".

Denouement de l'enquete, ecrit par le CLI lui-meme dans le journal --debug
enfin capture (grace au start_new_session de v1.14.14) :

    Anthropic profile login expired · Re-authenticate your Anthropic profile

Le CLI 2.1.173 ne sortait pas en erreur sur une session OAuth expiree : il
tentait de poser la question de re-authentification sur un tty que personne
ne voyait -- des semaines de gels muets. Et la sonde jeton disait "ok"
parce qu'elle teste l'EXISTENCE du jeton dans le Keychain, pas sa validite.

Trois verrous :
1. Un gel dont la sortie partielle porte un marqueur d'authentification
   leve ClaudeCliNotLoggedIn -> l'UX de reconnexion existante (claude
   /login + bouton "Ouvrir le Terminal"), pas l'echelle de repli.
2. Le diagnostic route le journal --debug "login expired" vers le verdict
   reconnexion, pas vers "binaire perime".
3. L'auto-reparation du CLI (mise a jour automatique au timeout) n'est
   armee QUE par main() : la suite de tests ne peut jamais declencher un
   vrai installeur -- invariante de construction.
"""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402

EXPIRED = b"Anthropic profile login expired \xc2\xb7 Re-authenticate your Anthropic profile"


class _Done:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class MarkersTests(unittest.TestCase):
    def test_all_known_forms_are_recognised(self):
        for text in ("Not logged in. Please run /login",
                     "Anthropic profile login expired",
                     "Re-authenticate your Anthropic profile",
                     "please log in again"):
            with self.subTest(text=text):
                self.assertTrue(app._looks_not_logged_in(text))

    def test_ordinary_output_is_not_flagged(self):
        for text in ("", None, "pong", "loading MCP servers",
                     "error: unknown option '--x'"):
            with self.subTest(text=text):
                self.assertFalse(app._looks_not_logged_in(text))


class FrozenExpiredLoginTests(unittest.TestCase):
    """Le cas reel : le CLI GELE (timeout subprocess) en ayant ecrit le
    message d'expiration -- il ne sort pas en erreur."""

    def setUp(self):
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

    def _hang_with_expired(self, cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 30),
                                        output=EXPIRED)

    def test_timeout_with_expired_message_is_not_logged_in(self):
        app.subprocess.run = self._hang_with_expired
        with self.assertRaises(app.ClaudeCliNotLoggedIn) as ctx:
            app._call_claude_cli("x")
        self.assertIn("claude /login", str(ctx.exception))
        self.assertIn("expirée", str(ctx.exception))

    def test_no_ladder_descent_on_expired_login(self):
        """Redescendre les barreaux ne reconnecte personne : un seul essai."""
        calls = []

        def hang(cmd, **kw):
            calls.append(cmd)
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 30),
                                            output=EXPIRED)

        app.subprocess.run = hang
        with self.assertRaises(app.ClaudeCliNotLoggedIn):
            app._call_claude_cli("x")
        self.assertEqual(len(calls), 1)

    def test_suggest_routes_to_the_login_ux(self):
        app.subprocess.run = self._hang_with_expired
        with patch.object(app, "_find_skill_dir",
                          lambda n: Path("/nonexistent-but-truthy")):
            ok, payload = app.suggest_skill_description("demo")
        self.assertFalse(ok)
        self.assertEqual(payload["error_code"], "cli_not_logged_in")
        self.assertEqual(payload["fix_command"], "claude /login")

    def test_plain_silent_timeout_still_times_out(self):
        """Sans marqueur, le comportement timeout + echelle est inchange."""
        def hang(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 30))

        app.subprocess.run = hang
        with self.assertRaises(app.ClaudeCliTimeout):
            app._call_claude_cli("x", timeout=30)


class DiagnoseExpiredTests(unittest.TestCase):
    def test_debug_tail_with_expired_login_sets_the_verdict(self):
        with patch.object(app, "_claude_cli_path", lambda: "/bin/claude"), \
                patch.object(app.subprocess, "run",
                             lambda cmd, **kw: _Done(stdout="2.1.173")), \
                patch.object(app, "_check_cli_node",
                             lambda: {"node_path": "/x/node",
                                      "node_version": "v25",
                                      "node_warning": None}), \
                patch.object(app, "_check_cli_credentials",
                             lambda **kw: ("ok", "jeton accessible")), \
                patch.object(app, "_call_claude_cli",
                             lambda *a, **kw: (_ for _ in ()).throw(
                                 app.ClaudeCliTimeout(60, ""))), \
                patch.object(app, "_check_api_reachable",
                             lambda **kw: (True, "HTTP 401")), \
                patch.object(app, "_check_cli_freshness", lambda **kw: "2.1.232"), \
                patch.object(app, "_cli_probe",
                             lambda o, **kw: {"ok": False, "seconds": 45}), \
                patch.object(app, "_cli_debug_tail",
                             lambda **kw: "Anthropic profile login expired · Re-authenticate"):
            out = app._diagnose_claude_cli()
        self.assertTrue(out["not_logged_in"])
        self.assertIn("claude /login", out["error"])

    def test_keychain_ok_no_longer_overclaims(self):
        self.assertIn("existence, pas validit",
                      app.Path(app.__file__).read_text())


class AutoHealSafetyTests(unittest.TestCase):
    def test_disarmed_by_default_for_tests(self):
        """La suite ne peut jamais declencher un vrai installeur."""
        self.assertFalse(app._CLI_AUTO_HEAL["armed"])
        healed, detail = app._cli_auto_heal_after_timeout()
        self.assertFalse(healed)
        self.assertIsNone(detail)

    def test_armed_heal_requires_the_stale_signature(self):
        state = dict(app._CLI_AUTO_HEAL)
        app._CLI_AUTO_HEAL.update({"armed": True, "attempted": False})
        try:
            with patch.object(app, "_check_cli_freshness", lambda **kw: "2.1.232"), \
                    patch.object(app, "_claude_cli_path", lambda: "/bin/claude"), \
                    patch.object(app.subprocess, "run",
                                 lambda cmd, **kw: _Done(stdout="2.1.232 (Claude Code)")), \
                    patch.object(app, "update_claude_cli",
                                 lambda: (_ for _ in ()).throw(
                                     AssertionError("pas de mise a jour sans signature"))):
                healed, _d = app._cli_auto_heal_after_timeout()
            self.assertFalse(healed)
        finally:
            app._CLI_AUTO_HEAL.update(state)

    def test_armed_heal_updates_once_on_stale_binary(self):
        state = dict(app._CLI_AUTO_HEAL)
        app._CLI_AUTO_HEAL.update({"armed": True, "attempted": False})
        updates = []
        try:
            with patch.object(app, "_check_cli_freshness", lambda **kw: "2.1.232"), \
                    patch.object(app, "_claude_cli_path", lambda: "/bin/claude"), \
                    patch.object(app.subprocess, "run",
                                 lambda cmd, **kw: _Done(stdout="2.1.173 (Claude Code)")), \
                    patch.object(app, "update_claude_cli",
                                 lambda: updates.append(1) or (True, "maj ok")):
                healed1, d1 = app._cli_auto_heal_after_timeout()
                healed2, d2 = app._cli_auto_heal_after_timeout()
            self.assertTrue(healed1)
            self.assertEqual(d1, "maj ok")
            self.assertFalse(healed2)
            self.assertEqual(updates, [1])
        finally:
            app._CLI_AUTO_HEAL.update(state)

    def test_main_arms_it(self):
        import inspect
        self.assertIn('_CLI_AUTO_HEAL["armed"] = True',
                      inspect.getsource(app.main))


if __name__ == "__main__":
    unittest.main()
