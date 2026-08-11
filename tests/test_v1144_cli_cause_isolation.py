"""v1.14.4 - Tests : isoler la CAUSE du CLI qui ne repond pas.

v1.14.3 avait rendu la panne visible (sortie partielle conservee, ping reel
au lieu d'un `--version` qui ne parle jamais a l'API). Sur la machine
concernee le verdict est tombe : binaire sain, `--version` instantane, ping
mort a 60 s sans un octet ecrit. Vrai, et insuffisant : "il ne repond pas"
est le point de depart du probleme, pas une reponse.

Trois causes restaient possibles, que rien ne separait :

1. la requete ne sort pas de la machine (reseau, proxy, CA absents de
   l'environnement launchd dont l'app herite quand on la lance du Finder) ;
2. elle sort, mais un flag de l'app fait caler le CLI -- le CLI se met a
   jour tout seul, donc un appel qui marchait la veille peut bloquer le
   lendemain sans que rien n'ait bouge cote app ;
3. elle sort, le CLI va bien, mais il n'accede pas a son jeton.

Ces tests verrouillent les sondes qui tranchent, et surtout la propriete
qui les rend valables : elles rejouent l'appel de production a l'identique,
seuls les flags varient. Une sonde qui teste autre chose que le vrai appel
ne prouve rien -- c'est exactement ce qui rendait le diagnostic v1.9.4
inutile.
"""
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpener:
    def __init__(self, behaviour):
        self._b = behaviour

    def open(self, url, timeout=None):
        return self._b()


class CommandBuildingTests(unittest.TestCase):
    """Le refactor qui rend les flags retirables ne doit rien changer a
    l'appel de production."""

    def test_production_flags(self):
        cmd = app._cli_cmd("/bin/claude")
        for flag in ("-p", "--strict-mcp-config", "--no-session-persistence",
                     "--disable-slash-commands"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "text")
        self.assertNotIn("--safe-mode", cmd)

    def test_bare_call_drops_only_the_optional_flags(self):
        cmd = app._cli_cmd("/bin/claude", optional=())
        self.assertIn("-p", cmd)
        self.assertIn("--output-format", cmd)
        for group in app._CLI_OPTIONAL_FLAGS:
            for member in group:
                self.assertNotIn(member, cmd)

    def test_valued_options_keep_their_value(self):
        """Les options sont groupees : une bissection qui separerait
        --mcp-config de sa valeur produirait une commande invalide."""
        for group in app._CLI_OPTIONAL_FLAGS:
            cmd = app._cli_cmd("/bin/claude", optional=(group,))
            if "--mcp-config" in cmd:
                self.assertEqual(cmd[cmd.index("--mcp-config") + 1],
                                 '{"mcpServers":{}}')

    def test_system_prompt_is_a_flag_not_a_prefix(self):
        cmd = app._cli_cmd("/bin/claude", system_prompt="SOIS BREF")
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], "SOIS BREF")

    def test_probe_replays_the_production_call(self):
        """La propriete qui rend le diagnostic valable : meme binaire, meme
        cwd neutre, prompt par stdin -- jamais par argv."""
        calls = []

        def fake_run(cmd, **kw):
            calls.append({"cmd": cmd, "kw": kw})
            return type("R", (), {"stdout": "pong", "stderr": "",
                                  "returncode": 0})()

        with patch.object(app, "_claude_cli_path", lambda: "/bin/claude"), \
                patch.object(app.subprocess, "run", fake_run):
            out = app._cli_probe(())
        self.assertTrue(out["ok"])
        self.assertIn("Reply with the single word: pong",
                      calls[0]["kw"]["input"])
        self.assertIn("claude-control-probe", str(calls[0]["kw"]["cwd"]))
        for arg in calls[0]["cmd"]:
            self.assertNotIn("Reply with the single word", arg)

    def test_bare_probe_carries_no_system_prompt_flag(self):
        """Sinon la sonde "appel nu" heriterait d'une option jamais validee,
        et un diagnostic la designerait a tort comme un probleme d'auth."""
        calls = []

        def fake_run(cmd, **kw):
            calls.append({"cmd": cmd, "kw": kw})
            return type("R", (), {"stdout": "pong", "stderr": "",
                                  "returncode": 0})()

        with patch.object(app, "_claude_cli_path", lambda: "/bin/claude"), \
                patch.object(app.subprocess, "run", fake_run):
            app._cli_probe(())
        self.assertNotIn("--system-prompt", calls[0]["cmd"])
        self.assertIn("one word", calls[0]["kw"]["input"])


class LoginShellEnvTests(unittest.TestCase):
    """Lance depuis Finder, l'app herite d'un environnement launchd minimal.
    Le PATH n'en etait que la moitie visible."""

    def setUp(self):
        self._orig = dict(app._SHELL_ENV_CACHE)
        app._SHELL_ENV_CACHE.update({"done": False, "env": {}})

    def tearDown(self):
        app._SHELL_ENV_CACHE.clear()
        app._SHELL_ENV_CACHE.update(self._orig)

    def test_parses_env_output(self):
        def fake_run(cmd, **kw):
            return type("R", (), {"stdout": "HTTPS_PROXY=http://p:3128\nFOO=bar\n",
                                  "stderr": "", "returncode": 0})()

        with patch.object(app.subprocess, "run", fake_run):
            env = app._login_shell_env()
        self.assertEqual(env["HTTPS_PROXY"], "http://p:3128")

    def test_falls_back_to_non_interactive_shell(self):
        """Un shell interactif peut bloquer sur une invite : on ne remplace
        pas un hang par un autre."""
        seen = []

        def fake_run(cmd, **kw):
            seen.append(cmd[1])
            if cmd[1] == "-lic":
                raise subprocess.TimeoutExpired("zsh", 8)
            return type("R", (), {"stdout": "HTTP_PROXY=http://q\n",
                                  "stderr": "", "returncode": 0})()

        with patch.object(app.subprocess, "run", fake_run):
            env = app._login_shell_env()
        self.assertEqual(seen, ["-lic", "-lc"])
        self.assertEqual(env["HTTP_PROXY"], "http://q")

    def test_total_failure_is_survivable(self):
        def boom(*a, **kw):
            raise OSError("no shell")

        with patch.object(app.subprocess, "run", boom):
            self.assertEqual(app._login_shell_env(), {})

    def test_result_is_cached(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return type("R", (), {"stdout": "A=1\n", "stderr": "",
                                  "returncode": 0})()

        with patch.object(app.subprocess, "run", fake_run):
            app._login_shell_env()
            app._login_shell_env()
        self.assertEqual(len(calls), 1)

    def test_stdin_is_closed(self):
        """Sans ca le shell peut attendre une entree qui ne viendra jamais."""
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(kw)
            return type("R", (), {"stdout": "A=1\n", "stderr": "",
                                  "returncode": 0})()

        with patch.object(app.subprocess, "run", fake_run):
            app._login_shell_env()
        self.assertEqual(seen["stdin"], subprocess.DEVNULL)
        self.assertLessEqual(seen["timeout"], 10)


class CliEnvRecoveryTests(unittest.TestCase):
    def setUp(self):
        self._orig = dict(app._SHELL_ENV_CACHE)

    def tearDown(self):
        app._SHELL_ENV_CACHE.clear()
        app._SHELL_ENV_CACHE.update(self._orig)

    def _with_shell(self, shell_env, os_env):
        app._SHELL_ENV_CACHE.update({"done": True, "env": shell_env})
        with patch.object(app.os, "environ", os_env):
            return app._cli_env()

    def test_missing_proxy_is_recovered(self):
        env = self._with_shell({"HTTPS_PROXY": "http://p:3128"},
                               {"PATH": "/usr/bin"})
        self.assertEqual(env["HTTPS_PROXY"], "http://p:3128")
        self.assertIn("HTTPS_PROXY", app._CLI_ENV_RECOVERED["vars"])

    def test_existing_value_is_never_overwritten(self):
        """L'environnement de l'app fait autorite : on comble un trou, on ne
        redefinit rien."""
        env = self._with_shell({"HTTPS_PROXY": "http://shell"},
                               {"PATH": "/usr/bin", "HTTPS_PROXY": "http://app"})
        self.assertEqual(env["HTTPS_PROXY"], "http://app")
        self.assertNotIn("HTTPS_PROXY", app._CLI_ENV_RECOVERED["vars"])

    def test_unrelated_shell_vars_are_not_imported(self):
        """Liste fermee : importer un shell entier change le comportement du
        subprocess de facons qu'on ne controle plus."""
        env = self._with_shell({"RANDOM_THING": "x"}, {"PATH": "/usr/bin"})
        self.assertNotIn("RANDOM_THING", env)

    def test_path_augmentation_still_happens(self):
        env = self._with_shell({}, {"PATH": "/usr/bin"})
        self.assertIn("/opt/homebrew/bin", env["PATH"])
        self.assertIn("/usr/bin", env["PATH"])


class ApiReachabilityTests(unittest.TestCase):
    """Separe 'la requete ne sort pas' de 'elle sort et le CLI bloque'."""

    def _run(self, behaviour, env=None):
        with patch.object(app.urllib.request, "build_opener",
                          lambda *a, **kw: _FakeOpener(behaviour)):
            return app._check_api_reachable(env=env or {})

    def test_401_counts_as_reachable(self):
        """On n'envoie aucune clef : une reponse d'erreur de l'API prouve
        qu'on l'a jointe. Seule l'absence de reponse est un echec."""
        def boom():
            raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

        ok, detail = self._run(boom)
        self.assertTrue(ok)
        self.assertIn("401", detail)

    def test_success_is_reachable(self):
        ok, detail = self._run(lambda: _Resp())
        self.assertTrue(ok)

    def test_network_failure_is_unreachable(self):
        def boom():
            raise urllib.error.URLError("connection timed out")

        ok, detail = self._run(boom)
        self.assertFalse(ok)
        self.assertIn("timed out", detail)

    def test_proxy_from_env_is_honoured(self):
        """Tester en direct une API que le CLI doit joindre via un proxy
        donnerait un faux 'reseau ok'."""
        seen = {}

        def fake_handler(proxies):
            seen.update(proxies)
            return object()

        with patch.object(app.urllib.request, "ProxyHandler", fake_handler), \
                patch.object(app.urllib.request, "build_opener",
                             lambda *a, **kw: _FakeOpener(lambda: _Resp())):
            app._check_api_reachable(env={"HTTPS_PROXY": "http://p:3128"})
        self.assertEqual(seen["https"], "http://p:3128")


class FlagBlameTests(unittest.TestCase):
    def test_names_the_first_group_that_stalls(self):
        target = app._CLI_OPTIONAL_FLAGS[0]

        def probe(optional, **kw):
            return {"ok": optional != (target,), "seconds": 1}

        with patch.object(app, "_cli_probe", probe):
            self.assertEqual(app._blame_cli_flag(), " ".join(target))

    def test_returns_none_when_each_flag_is_fine_alone(self):
        with patch.object(app, "_cli_probe",
                          lambda optional, **kw: {"ok": True, "seconds": 1}):
            self.assertIsNone(app._blame_cli_flag())

    def test_stops_at_the_first_culprit(self):
        tried = []

        def probe(optional, **kw):
            tried.append(optional[0])
            return {"ok": False}

        with patch.object(app, "_cli_probe", probe):
            app._blame_cli_flag()
        self.assertEqual(len(tried), 1)


class DiagnosticStagesTests(unittest.TestCase):
    """Le diagnostic doit descendre jusqu'a une cause, et ne pas payer les
    sondes quand elles n'apprendraient rien."""

    def setUp(self):
        self._p = patch.object(app, "_claude_cli_path", lambda: "/bin/claude")
        self._p.start()
        self._v = patch.object(
            app.subprocess, "run",
            lambda *a, **kw: type("R", (), {"stdout": "2.1.173", "stderr": "",
                                            "returncode": 0})())
        self._v.start()

    def tearDown(self):
        self._p.stop()
        self._v.stop()

    def _diag(self, ping, **kw):
        with patch.object(app, "_call_claude_cli", ping), \
                patch.object(app, "_check_api_reachable",
                             kw.get("net", lambda **k: (True, "HTTP 401"))), \
                patch.object(app, "_cli_probe",
                             kw.get("probe", lambda o, **k: {"ok": True,
                                                             "seconds": 2.0})), \
                patch.object(app, "_blame_cli_flag",
                             kw.get("blame", lambda: "--safe-mode")):
            return app._diagnose_claude_cli()

    def test_healthy_ping_skips_every_probe(self):
        d = self._diag(lambda *a, **kw: "pong")
        self.assertTrue(d["ping_ok"])
        self.assertIsNone(d["network_ok"])
        self.assertIsNone(d["minimal_ok"])

    def test_stalled_ping_goes_all_the_way_to_the_guilty_flag(self):
        """Le cas observe : ping mort, sans un octet ecrit."""
        def stall(*a, **kw):
            raise app.ClaudeCliTimeout(60, "")

        d = self._diag(stall)
        self.assertTrue(d["network_ok"])
        self.assertTrue(d["minimal_ok"])
        self.assertEqual(d["blamed_flag"], "--safe-mode")
        self.assertEqual(d["stage"], "blame")

    def test_unreachable_network_stops_before_the_expensive_probes(self):
        def stall(*a, **kw):
            raise app.ClaudeCliTimeout(60, "")

        d = self._diag(stall, net=lambda **k: (False, "URLError: timed out"))
        self.assertFalse(d["network_ok"])
        self.assertIsNone(d["minimal_ok"])
        self.assertEqual(d["stage"], "network")

    def test_bare_call_also_stalling_points_at_auth(self):
        def stall(*a, **kw):
            raise app.ClaudeCliTimeout(60, "")

        d = self._diag(stall, probe=lambda o, **k: {"ok": False, "seconds": 45})
        self.assertTrue(d["network_ok"])
        self.assertFalse(d["minimal_ok"])
        self.assertIsNone(d["blamed_flag"])

    def test_not_logged_in_is_a_verdict_on_its_own(self):
        def nope(*a, **kw):
            raise app.ClaudeCliNotLoggedIn("Not logged in")

        d = self._diag(nope)
        self.assertTrue(d["not_logged_in"])
        self.assertIsNone(d["network_ok"])


class VerdictWiringTests(unittest.TestCase):
    """Chaque cause detectee doit avoir une phrase, dans les deux langues.
    Une cle absente afficherait un identifiant brut a l'utilisateur."""

    KEYS = ("cli_diag_net_ko", "cli_diag_flag_blamed", "cli_diag_flag_generic",
            "cli_diag_auth", "cli_diag_not_logged_in")

    def setUp(self):
        self.src = (Path(__file__).resolve().parents[1]
                    / "src" / "app.py").read_text()

    def test_keys_exist_in_both_languages(self):
        for key in self.KEYS:
            self.assertEqual(self.src.count(key + ":"), 2,
                             f"{key} doit exister en fr ET en en")

    def test_verdict_reads_each_key(self):
        for key in self.KEYS:
            self.assertIn(f"tr('{key}')", self.src)

    def test_placeholders_are_substituted(self):
        for key, ph in (("cli_diag_net_ko", "{d}"),
                        ("cli_diag_flag_blamed", "{f}"),
                        ("cli_diag_flag_generic", "{s}")):
            self.assertIn(f"tr('{key}').replace('{ph}'", self.src)


if __name__ == "__main__":
    unittest.main()
