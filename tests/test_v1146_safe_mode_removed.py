"""v1.14.6 - Tests : `--safe-mode` retire, et repli automatique.

Cause racine enfin identifiee, par bissection sur la machine concernee
(CLI 2.1.173) :

    echo ping | claude -p --output-format text                -> repond
    echo ping | claude -p --safe-mode --output-format text    -> ne rend
                                                                jamais la main

`--safe-mode` avait ete ajoute en v1.14.1 pour une seule raison utile : ne
pas demarrer les vingt serveurs MCP de l'utilisateur avant de generer une
phrase de description. Il est remplace par `--strict-mcp-config` avec une
config vide, qui fait ce travail sans toucher au reste du demarrage.

Le second volet compte autant que le premier. Pendant cinq semaines, une
option toxique a rendu la fonctionnalite morte, sans recours : l'app
attendait 120 s puis affichait "le CLI n'a pas repondu". Les options
optionnelles sont des optimisations, pas des conditions de fonctionnement --
quand l'appel complet cale, l'app retente desormais sans elles.

Le repli est refuse au diagnostic : une sonde qui se rattrape toute seule
mesurerait autre chose que l'appel de production, et ne prouverait plus rien.
"""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class _Done:
    def __init__(self, stdout="une description", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class SafeModeIsGoneTests(unittest.TestCase):
    def test_not_in_production_command(self):
        self.assertNotIn("--safe-mode", app._cli_cmd("/bin/claude"))

    def test_not_in_any_optional_group(self):
        for group in app._CLI_OPTIONAL_FLAGS:
            self.assertNotIn("--safe-mode", group)

    def test_mcp_servers_are_still_disabled(self):
        """Le but de --safe-mode reste atteint : aucun serveur MCP charge."""
        cmd = app._cli_cmd("/bin/claude")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1],
                         '{"mcpServers":{}}')


class FallbackTests(unittest.TestCase):
    """Une option toxique ne doit plus pouvoir tuer la fonctionnalite."""

    def setUp(self):
        self.calls = []
        self._orig_run = app.subprocess.run
        self._orig_path = app._claude_cli_path
        app._claude_cli_path = lambda: "/bin/claude"
        self._shell = dict(app._SHELL_ENV_CACHE)
        app._SHELL_ENV_CACHE.update({"done": True, "env": {}})
        app._CLI_FALLBACK["used"] = False

    def tearDown(self):
        app.subprocess.run = self._orig_run
        app._claude_cli_path = self._orig_path
        app._SHELL_ENV_CACHE.clear()
        app._SHELL_ENV_CACHE.update(self._shell)
        app._CLI_FALLBACK["used"] = False

    def _run_with(self, behaviour):
        def fake(cmd, **kw):
            self.calls.append({"cmd": cmd, "kw": kw})
            return behaviour(len(self.calls), cmd, kw)

        app.subprocess.run = fake

    def _stall_then_answer(self):
        def behaviour(n, cmd, kw):
            if n == 1:
                raise subprocess.TimeoutExpired("claude", kw["timeout"])
            return _Done(stdout="pong")

        self._run_with(behaviour)

    def test_timeout_falls_back_and_succeeds(self):
        self._stall_then_answer()
        self.assertEqual(app._call_claude_cli("x"), "pong")
        self.assertEqual(len(self.calls), 2)

    def test_fallback_drops_the_optional_options(self):
        self._stall_then_answer()
        app._call_claude_cli("x")
        for group in app._CLI_OPTIONAL_FLAGS:
            for member in group:
                self.assertNotIn(member, self.calls[1]["cmd"])

    def test_fallback_keeps_the_core_options(self):
        """Sans -p ni --output-format, la reponse n'est plus exploitable."""
        self._stall_then_answer()
        app._call_claude_cli("x")
        self.assertIn("-p", self.calls[1]["cmd"])
        self.assertEqual(
            self.calls[1]["cmd"][self.calls[1]["cmd"].index("--output-format") + 1],
            "text")

    def test_fallback_still_sends_the_prompt_through_stdin(self):
        self._stall_then_answer()
        app._call_claude_cli("---\nfrontmatter")
        self.assertEqual(self.calls[1]["kw"]["input"], "---\nfrontmatter")

    def test_fallback_budget_is_smaller_than_the_first_attempt(self):
        """Sinon un CLI totalement mort ferait attendre 240 s."""
        self._stall_then_answer()
        app._call_claude_cli("x", timeout=120)
        self.assertLess(self.calls[1]["kw"]["timeout"],
                        self.calls[0]["kw"]["timeout"])

    def test_fallback_is_recorded(self):
        self._stall_then_answer()
        app._call_claude_cli("x")
        self.assertTrue(app._CLI_FALLBACK["used"])

    def test_no_fallback_when_the_first_attempt_works(self):
        self._run_with(lambda n, cmd, kw: _Done(stdout="ok"))
        app._call_claude_cli("x")
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(app._CLI_FALLBACK["used"])

    def test_both_stalling_reports_the_first_budget(self):
        """L'utilisateur a attendu 120 s puis 60 s. Annoncer "60 s"
        sous-declarerait l'attente et designerait l'appel nu, alors que
        c'est l'appel complet qui a echoue d'abord."""
        def behaviour(n, cmd, kw):
            raise subprocess.TimeoutExpired("claude", kw["timeout"])

        self._run_with(behaviour)
        with self.assertRaises(app.ClaudeCliTimeout) as ctx:
            app._call_claude_cli("x", timeout=120)
        self.assertEqual(ctx.exception.timeout, 120)
        self.assertIn("120", str(ctx.exception))

    def test_partial_output_of_the_first_attempt_survives(self):
        def behaviour(n, cmd, kw):
            raise subprocess.TimeoutExpired(
                "claude", kw["timeout"],
                output=b"invite" if n == 1 else b"")

        self._run_with(behaviour)
        with self.assertRaises(app.ClaudeCliTimeout) as ctx:
            app._call_claude_cli("x")
        self.assertIn("invite", ctx.exception.partial)


class DiagnosticDoesNotFallBackTests(unittest.TestCase):
    """Une sonde qui se rattrape ne mesure plus l'appel de production."""

    def setUp(self):
        self.calls = []
        self._orig_run = app.subprocess.run
        self._orig_path = app._claude_cli_path
        app._claude_cli_path = lambda: "/bin/claude"
        self._shell = dict(app._SHELL_ENV_CACHE)
        app._SHELL_ENV_CACHE.update({"done": True, "env": {}})

    def tearDown(self):
        app.subprocess.run = self._orig_run
        app._claude_cli_path = self._orig_path
        app._SHELL_ENV_CACHE.clear()
        app._SHELL_ENV_CACHE.update(self._shell)

    def test_explicit_opt_out_raises_without_retrying(self):
        def fake(cmd, **kw):
            self.calls.append(cmd)
            raise subprocess.TimeoutExpired("claude", kw["timeout"])

        app.subprocess.run = fake
        with self.assertRaises(app.ClaudeCliTimeout):
            app._call_claude_cli("x", allow_fallback=False)
        self.assertEqual(len(self.calls), 1)

    def test_the_ping_opts_out(self):
        seen = {}

        def fake_call(prompt, **kw):
            seen.update(kw)
            raise app.ClaudeCliTimeout(60, "")

        with patch.object(app, "_claude_cli_path", lambda: "/bin/claude"), \
                patch.object(app.subprocess, "run",
                             lambda *a, **kw: _Done(stdout="2.1.173")), \
                patch.object(app, "_call_claude_cli", fake_call), \
                patch.object(app, "_check_api_reachable",
                             lambda **k: (True, "HTTP 401")), \
                patch.object(app, "_cli_probe",
                             lambda o, **k: {"ok": False, "seconds": 1}):
            app._diagnose_claude_cli()
        self.assertIs(seen.get("allow_fallback"), False)


if __name__ == "__main__":
    unittest.main()
