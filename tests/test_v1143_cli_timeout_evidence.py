"""v1.14.3 - Tests de non-regression : rendre diagnosticable le CLI qui
"ne repond pas en 120 s".

Avant, cette panne etait un cul-de-sac. Le message disait a l'utilisateur
d'aller taper une commande dans un terminal, et l'app :

- jetait la sortie partielle du CLI (subprocess la fournit pourtant dans
  TimeoutExpired.stdout) -- soit la seule trace de CE QUE le CLI faisait
  quand il a cale ;
- proposait un bouton "Diagnostiquer le CLI" qui lancait `claude --version`,
  un check du binaire local qui ne parle jamais a l'API : il repondait
  "available: YES" pendant que le vrai appel bloquait indefiniment.
"""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class PartialOutputIsKeptTests(unittest.TestCase):
    """La sortie partielle est la preuve. On la perdait."""

    def setUp(self):
        self._orig = app._claude_cli_path
        app._claude_cli_path = lambda: "/usr/local/bin/claude"
        # v1.14.8 - l'echec de l'appel optimise est memorise pour la duree du
        # process : sans remise a zero, un test precedent ferait sauter
        # l'essai court et ces assertions mesureraient un autre chemin.
        self._fallback = dict(app._CLI_FALLBACK)
        app._CLI_FALLBACK.update({"used": False, "rung": 0})

    def tearDown(self):
        app._claude_cli_path = self._orig
        app._CLI_FALLBACK.update(self._fallback)

    def _timeout_with(self, stdout=None, stderr=None):
        def boom(cmd, **kw):
            raise subprocess.TimeoutExpired("claude", 120, output=stdout,
                                            stderr=stderr)
        with patch.object(app.subprocess, "run", boom):
            with self.assertRaises(app.ClaudeCliTimeout) as ctx:
                app._call_claude_cli("x")
        return ctx.exception

    def test_partial_stdout_reaches_the_message(self):
        """Le cas qui debloque le diagnostic : le CLI a commence a ecrire
        (une invite interactive, une erreur reseau) puis a cale."""
        e = self._timeout_with(stdout=b"Do you trust the files in this folder?")
        self.assertIn("Do you trust the files", e.partial)
        self.assertIn("Do you trust the files", str(e))

    def test_partial_stderr_also_kept(self):
        e = self._timeout_with(stderr=b"proxy: 407 authentication required")
        self.assertIn("407", e.partial)

    def test_bytes_are_decoded_not_repr(self):
        """subprocess rend des bytes meme en text=True : sans decodage,
        l'utilisateur lisait b'...' dans l'UI."""
        e = self._timeout_with(stdout=b"invite accentu\xc3\xa9e")
        self.assertNotIn("b'", str(e))
        self.assertIn("accentuée", e.partial)

    def test_silence_is_reported_as_silence(self):
        """Aucune sortie du tout est une information differente, pas un
        message vide : le CLI n'a jamais demarre."""
        e = self._timeout_with()
        self.assertEqual(e.partial, "")
        self.assertIn("rien écrit", str(e))

    def test_timeout_value_is_in_the_message(self):
        """v1.14.7 - quand tous les essais calent, le message annonce
        l'attente TOTALE (les barreaux de l'echelle), pas le budget d'un
        seul. v1.14.12 : l'echelle compte trois barreaux."""
        e = self._timeout_with()
        self.assertEqual(e.timeout, 2 * app.CLI_FAST_PATH_TIMEOUT + 120)
        self.assertIn(str(2 * app.CLI_FAST_PATH_TIMEOUT + 120), str(e))


class SuggestSurfacesTimeoutTests(unittest.TestCase):
    """suggest_skill_description doit rendre un payload exploitable par
    l'UI (error_code + partial), pas une phrase figee renvoyant au terminal.
    """

    def setUp(self):
        self._orig = app._claude_cli_path
        app._claude_cli_path = lambda: "/usr/local/bin/claude"
        self._find = app._find_skill_dir
        app._find_skill_dir = lambda n: Path("/nonexistent-but-truthy")

    def tearDown(self):
        app._claude_cli_path = self._orig
        app._find_skill_dir = self._find

    def test_timeout_payload_carries_code_and_partial(self):
        with patch.object(app, "_call_claude_cli",
                          side_effect=app.ClaudeCliTimeout(120, "Select login method")):
            ok, payload = app.suggest_skill_description("demo")
        self.assertFalse(ok)
        self.assertEqual(payload["error_code"], "cli_timeout")
        self.assertIn("Select login method", payload["partial"])

    def test_timeout_no_longer_tells_user_to_open_a_terminal(self):
        """L'app diagnostique elle-meme desormais."""
        with patch.object(app, "_call_claude_cli",
                          side_effect=app.ClaudeCliTimeout(120, "")):
            ok, payload = app.suggest_skill_description("demo")
        self.assertNotIn("terminal", payload["error"].lower())


class DiagnoseDoesARealCallTests(unittest.TestCase):
    """Le diagnostic doit reproduire la panne, donc faire un vrai
    aller-retour avec les memes flags -- pas seulement `claude --version`.
    """

    def setUp(self):
        self._orig = app._claude_cli_path
        app._claude_cli_path = lambda: "/usr/local/bin/claude"
        # v1.14.8 - l'echec de l'appel optimise est memorise pour la duree du
        # process : sans remise a zero, un test precedent ferait sauter
        # l'essai court et ces assertions mesureraient un autre chemin.
        self._fallback = dict(app._CLI_FALLBACK)
        app._CLI_FALLBACK.update({"used": False, "rung": 0})

    def tearDown(self):
        app._claude_cli_path = self._orig
        app._CLI_FALLBACK.update(self._fallback)

    def _diag(self, ping):
        def fake_version(cmd, **kw):
            return type("R", (), {"stdout": "2.0.1", "stderr": "", "returncode": 0})()
        with patch.object(app.subprocess, "run", fake_version):
            with patch.object(app, "_call_claude_cli", **ping):
                return app._diagnose_claude_cli()

    def test_healthy_cli_reports_ping_ok_with_timing(self):
        d = self._diag({"return_value": "pong"})
        self.assertTrue(d["ping_ok"])
        self.assertIsNotNone(d["ping_seconds"])
        self.assertEqual(d["stage"], "ping")

    def test_hanging_cli_is_caught_even_though_version_works(self):
        """LE cas qui rendait l'ancien diag inutile : binaire sain,
        appel reel bloque. Avant : 'available: YES', rien d'autre."""
        d = self._diag({"side_effect": app.ClaudeCliTimeout(60, "Select login method")})
        self.assertTrue(d["available"])       # --version passe
        self.assertFalse(d["ping_ok"])        # ...mais l'appel reel cale
        self.assertIn("Select login method", d["partial"])

    def test_diag_uses_the_same_env_as_the_real_call(self):
        """Un diagnostic qui teste un autre PATH que le vrai appel ne
        prouve rien."""
        seen = {}

        def fake_version(cmd, **kw):
            seen["env"] = kw.get("env")
            return type("R", (), {"stdout": "2.0.1", "stderr": "", "returncode": 0})()

        with patch.object(app.subprocess, "run", fake_version):
            with patch.object(app, "_call_claude_cli", return_value="pong"):
                app._diagnose_claude_cli()
        self.assertIsNotNone(seen["env"], "le diag doit passer l'env augmente")
        self.assertEqual(seen["env"]["PATH"], app._cli_env()["PATH"])

    def test_missing_binary_stops_before_pinging(self):
        app._claude_cli_path = lambda: None
        called = []
        with patch.object(app, "_call_claude_cli", side_effect=lambda *a, **k: called.append(1)):
            d = app._diagnose_claude_cli()
        self.assertEqual(d["stage"], "path")
        self.assertEqual(called, [], "inutile de pinger sans binaire")


class ProbeMessageTests(unittest.TestCase):
    """Le preflight du lot doit lui aussi porter la preuve."""

    def test_probe_timeout_message_includes_partial(self):
        with patch.object(app, "_call_claude_cli",
                          side_effect=app.ClaudeCliTimeout(90, "Do you trust this folder?")):
            ok, _, err = app._bulk_repair_probe()
        self.assertFalse(ok)
        self.assertIn("Do you trust this folder?", err)

    def test_probe_silent_timeout_says_so(self):
        with patch.object(app, "_call_claude_cli",
                          side_effect=app.ClaudeCliTimeout(90, "")):
            ok, _, err = app._bulk_repair_probe()
        self.assertIn("rien écrit", err)


if __name__ == "__main__":
    unittest.main()
