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

v1.14.12 - le repli descend une ECHELLE (cf. _CLI_RUNGS) au lieu de tout
jeter d'un coup : d'abord seules les options non essentielles sont
retirees (--strict-mcp-config est conserve, sinon l'appel de secours
recharge la config MCP complete de l'utilisateur), l'appel nu reste le
filet final. Les tests de ce fichier sont mis a jour en consequence ;
leur intention d'origine est inchangee.

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

    def _stall_until(self, answer_at):
        """Cale les (answer_at - 1) premiers essais, repond au suivant."""
        def behaviour(n, cmd, kw):
            if n < answer_at:
                raise subprocess.TimeoutExpired("claude", kw["timeout"])
            return _Done(stdout="pong")

        self._run_with(behaviour)

    def test_timeout_falls_back_and_succeeds(self):
        self._stall_until(2)
        self.assertEqual(app._call_claude_cli("x"), "pong")
        self.assertEqual(len(self.calls), 2)

    def test_first_fallback_keeps_the_mcp_neutralisation(self):
        """v1.14.12 - le premier barreau de repli conserve
        --strict-mcp-config : sans lui, l'appel de secours recharge la
        config MCP complete de l'utilisateur -- la lenteur exacte que
        l'option existe pour eviter."""
        self._stall_until(2)
        app._call_claude_cli("x")
        self.assertIn("--strict-mcp-config", self.calls[1]["cmd"])
        self.assertNotIn("--no-session-persistence", self.calls[1]["cmd"])
        self.assertNotIn("--disable-slash-commands", self.calls[1]["cmd"])

    def test_final_fallback_drops_the_optional_options(self):
        self._stall_until(3)
        app._call_claude_cli("x")
        for group in app._CLI_OPTIONAL_FLAGS:
            for member in group:
                self.assertNotIn(member, self.calls[2]["cmd"])

    def test_fallback_keeps_the_core_options(self):
        """Sans -p ni --output-format, la reponse n'est plus exploitable."""
        self._stall_until(3)
        app._call_claude_cli("x")
        for call in self.calls[1:]:
            self.assertIn("-p", call["cmd"])
            self.assertEqual(
                call["cmd"][call["cmd"].index("--output-format") + 1],
                "text")

    def test_fallback_still_sends_the_prompt_through_stdin(self):
        self._stall_until(3)
        app._call_claude_cli("---\nfrontmatter")
        self.assertEqual(self.calls[1]["kw"]["input"], "---\nfrontmatter")
        self.assertEqual(self.calls[2]["kw"]["input"], "---\nfrontmatter")

    def test_the_proven_call_gets_the_budget(self):
        """v1.14.7 - budgets inverses. Les appels optimises ne sont que des
        optimisations et n'ont droit qu'a un essai court chacun ; l'appel
        nu est la seule forme prouvee et recoit tout le budget. Avant,
        l'app misait 120 s sur la variante incertaine et n'en laissait que
        60 a celle qui marche."""
        self._stall_until(3)
        app._call_claude_cli("x", timeout=120)
        self.assertEqual(self.calls[0]["kw"]["timeout"],
                         app.CLI_FAST_PATH_TIMEOUT)
        self.assertEqual(self.calls[1]["kw"]["timeout"],
                         app.CLI_FAST_PATH_TIMEOUT)
        self.assertEqual(self.calls[2]["kw"]["timeout"], 120)

    def test_bare_attempt_carries_the_instruction_in_the_prompt(self):
        """--system-prompt a ete ajoute par le meme commit que --safe-mode et
        n'a jamais ete valide sur le CLI concerne : l'appel nu ne doit pas en
        heriter. La consigne repasse par stdin, seul canal prouve."""
        self._stall_until(3)
        app._call_claude_cli("CORPS", system_prompt="SOIS BREF")
        self.assertIn("--system-prompt", self.calls[1]["cmd"])
        self.assertNotIn("--system-prompt", self.calls[2]["cmd"])
        self.assertIn("SOIS BREF", self.calls[2]["kw"]["input"])
        self.assertIn("CORPS", self.calls[2]["kw"]["input"])

    def test_fallback_is_recorded(self):
        self._stall_until(2)
        app._call_claude_cli("x")
        self.assertTrue(app._CLI_FALLBACK["used"])
        self.assertEqual(app._CLI_FALLBACK["rung"], 1)

    def test_no_fallback_when_the_first_attempt_works(self):
        self._run_with(lambda n, cmd, kw: _Done(stdout="ok"))
        app._call_claude_cli("x")
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(app._CLI_FALLBACK["used"])

    def test_all_stalling_reports_the_total_wait(self):
        """Annoncer le budget d'un seul essai sous-declarerait ce que
        l'utilisateur a reellement patiente."""
        def behaviour(n, cmd, kw):
            raise subprocess.TimeoutExpired("claude", kw["timeout"])

        self._run_with(behaviour)
        with self.assertRaises(app.ClaudeCliTimeout) as ctx:
            app._call_claude_cli("x", timeout=120)
        self.assertEqual(ctx.exception.timeout,
                         2 * app.CLI_FAST_PATH_TIMEOUT + 120)

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


class FastPathIsNotRetriedForeverTests(unittest.TestCase):
    """v1.14.8 - Sur une machine ou une option bloque, chaque generation
    perdait 30 s a rejouer un appel dont l'echec est deja connu : 40 s au
    lieu de 10 par skill, et une reparation en lot qui dure des heures.

    v1.14.12 - meme garantie sur l'echelle a trois barreaux : le barreau qui
    a cale n'est jamais rejoue, les appels suivants partent du barreau qui a
    repondu. Ici le pire cas : c'est --strict-mcp-config lui-meme qui bloque,
    donc les barreaux 0 ET 1 calent et seul l'appel nu repond."""

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
            optimise = "--strict-mcp-config" in cmd
            if optimise:
                raise subprocess.TimeoutExpired("claude", kw["timeout"])
            return _Done(stdout="pong")

        app.subprocess.run = fake

    def tearDown(self):
        app.subprocess.run = self._orig_run
        app._claude_cli_path = self._orig_path
        app._SHELL_ENV_CACHE.clear()
        app._SHELL_ENV_CACHE.update(self._shell)
        app._CLI_FALLBACK.update({"used": False, "rung": 0})

    def test_first_call_pays_the_stalling_rungs_once(self):
        self.assertEqual(app._call_claude_cli("x"), "pong")
        self.assertEqual(len(self.calls), 3)

    def test_next_calls_go_straight_to_the_proven_one(self):
        app._call_claude_cli("x")
        self.calls.clear()
        self.assertEqual(app._call_claude_cli("y"), "pong")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("--strict-mcp-config", self.calls[0]["cmd"])

    def test_a_bulk_repair_does_not_pay_it_every_time(self):
        for _ in range(5):
            app._call_claude_cli("x")
        stalled = [c for c in self.calls if "--strict-mcp-config" in c["cmd"]]
        self.assertEqual(len(stalled), 2)

    def test_a_toxic_secondary_option_keeps_mcp_off(self):
        """v1.14.12 - si l'option toxique n'est PAS --strict-mcp-config, le
        repli s'arrete au barreau intermediaire : les MCP restent coupes et
        la reparation reste rapide."""
        def fake(cmd, **kw):
            self.calls.append({"cmd": cmd, "kw": kw})
            if "--disable-slash-commands" in cmd:
                raise subprocess.TimeoutExpired("claude", kw["timeout"])
            return _Done(stdout="pong")

        app.subprocess.run = fake
        self.assertEqual(app._call_claude_cli("x"), "pong")
        self.assertEqual(len(self.calls), 2)
        self.assertIn("--strict-mcp-config", self.calls[1]["cmd"])
        self.calls.clear()
        app._call_claude_cli("y")
        self.assertEqual(len(self.calls), 1)
        self.assertIn("--strict-mcp-config", self.calls[0]["cmd"])

    def test_the_proven_call_is_not_replayed_twice_when_it_stalls(self):
        """Sinon l'utilisateur attendrait deux fois le budget complet."""
        app._CLI_FALLBACK.update({"used": True, "rung": 2})

        def always_stall(cmd, **kw):
            self.calls.append({"cmd": cmd, "kw": kw})
            raise subprocess.TimeoutExpired("claude", kw["timeout"])

        app.subprocess.run = always_stall
        with self.assertRaises(app.ClaudeCliTimeout):
            app._call_claude_cli("x", timeout=60)
        self.assertEqual(len(self.calls), 1)


class NestedSessionEnvIsStrippedTests(unittest.TestCase):
    """v1.14.9 - Depuis l'app, api.anthropic.com repond (HTTP 401) et le
    binaire est sain, mais meme l'appel NU cale -- alors qu'il repond dans le
    terminal de l'utilisateur. Il ne reste que ce que l'app transmet.

    Si Claude Control a ete demarre depuis une session Claude Code, son
    environnement porte CLAUDECODE=1 et les CLAUDE_CODE_* de cette session.
    Le CLI enfant en herite et ne se comporte plus comme un appel autonome.
    Un terminal ordinaire ne les a pas.
    """

    def setUp(self):
        self._shell = dict(app._SHELL_ENV_CACHE)
        app._SHELL_ENV_CACHE.update({"done": True, "env": {}})

    def tearDown(self):
        app._SHELL_ENV_CACHE.clear()
        app._SHELL_ENV_CACHE.update(self._shell)

    def _env(self, os_env):
        with patch.object(app.os, "environ", dict(os_env)):
            return app._cli_env()

    def test_claudecode_marker_is_removed(self):
        self.assertNotIn("CLAUDECODE",
                         self._env({"PATH": "/usr/bin", "CLAUDECODE": "1"}))

    def test_claude_code_prefixed_vars_are_removed(self):
        env = self._env({"PATH": "/usr/bin",
                         "CLAUDE_CODE_ENTRYPOINT": "cli",
                         "CLAUDE_CODE_SSE_PORT": "1234"})
        self.assertEqual([k for k in env if k.startswith("CLAUDE_CODE_")], [])

    def test_unrelated_variables_survive(self):
        env = self._env({"PATH": "/usr/bin", "HOME": "/Users/x",
                         "CLAUDECODE": "1"})
        self.assertEqual(env["HOME"], "/Users/x")
        self.assertIn("/usr/bin", env["PATH"])

    def test_anthropic_credentials_are_not_stripped(self):
        """Elles servent a l'authentification : les retirer casserait
        l'appel au lieu de le reparer."""
        env = self._env({"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-x"})
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-x")


class StableWorkdirTests(unittest.TestCase):
    """Chaque appel tournait dans un dossier temporaire NEUF, sous
    /var/folders. Le CLI traite le repertoire courant comme le projet
    courant : un chemin jamais vu a chaque appel."""

    def test_workdir_is_stable_across_calls(self):
        self.assertEqual(app._cli_workdir(), app._cli_workdir())

    def test_workdir_is_not_a_throwaway_temp_dir(self):
        import tempfile
        self.assertFalse(
            app._cli_workdir().startswith(tempfile.gettempdir()),
            "le cwd ne doit plus etre un dossier temporaire jetable")

    def test_workdir_exists_after_the_call(self):
        self.assertTrue(Path(app._cli_workdir()).is_dir())

    def test_probe_and_production_share_it(self):
        """Une sonde qui tourne ailleurs que la production ne prouve rien."""
        calls = []

        def fake(cmd, **kw):
            calls.append(kw.get("cwd"))
            return _Done(stdout="pong")

        orig_run, orig_path = app.subprocess.run, app._claude_cli_path
        shell = dict(app._SHELL_ENV_CACHE)
        app._SHELL_ENV_CACHE.update({"done": True, "env": {}})
        app.subprocess.run = fake
        app._claude_cli_path = lambda: "/bin/claude"
        fallback = dict(app._CLI_FALLBACK)
        app._CLI_FALLBACK.update({"used": False, "rung": 0})
        try:
            app._call_claude_cli("x")
            app._cli_probe(())
        finally:
            app.subprocess.run, app._claude_cli_path = orig_run, orig_path
            app._CLI_FALLBACK.update(fallback)
            app._SHELL_ENV_CACHE.clear()
            app._SHELL_ENV_CACHE.update(shell)
        self.assertEqual(len(set(calls)), 1, calls)
