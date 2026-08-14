"""v1.14.15 - Tests : le verdict nomme le binaire perime, chiffres a l'appui.

Deuxieme rapport de la machine de reference, app 1.14.14 en place : jeton ok,
reseau ok, node ok, --debug muet, et le CLI toujours fige a 2.1.173 pendant
que le public est a 2.1.232 -- 59 versions de retard. Deux defauts de
lisibilite restaient :

1. Le bandeau de verdict datait d'avant les sondes v1.14.13 : il concluait
   "il reste l'authentification" en CONTREDISANT la ligne "jeton: ok"
   affichee juste en dessous. Quand le jeton est blanchi, le verdict accuse
   desormais le binaire perime et donne le geste (reinstallation).
2. "version 2.1.173" s'affichait comme un fait sain : sans la derniere
   version publiee en regard, aucune raison de s'en mefier. Le diagnostic
   va la chercher (registre npm, best-effort) et affiche l'ecart.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FreshnessProbeTests(unittest.TestCase):
    def setUp(self):
        self._shell = dict(app._SHELL_ENV_CACHE)
        app._SHELL_ENV_CACHE.update({"done": True, "env": {}})

    def tearDown(self):
        app._SHELL_ENV_CACHE.clear()
        app._SHELL_ENV_CACHE.update(self._shell)

    def test_latest_version_is_read_from_the_registry(self):
        class _Opener:
            def open(self, url, timeout=None):
                assert "claude-code" in url
                return _Resp(b'{"name":"@anthropic-ai/claude-code","version":"2.1.232"}')

        with patch.object(app.urllib.request, "build_opener",
                          lambda *a, **kw: _Opener()):
            self.assertEqual(app._check_cli_freshness(env={}), "2.1.232")

    def test_offline_returns_none_never_raises(self):
        class _Opener:
            def open(self, url, timeout=None):
                raise OSError("no network")

        with patch.object(app.urllib.request, "build_opener",
                          lambda *a, **kw: _Opener()):
            self.assertIsNone(app._check_cli_freshness(env={}))

    def test_garbage_payload_returns_none(self):
        class _Opener:
            def open(self, url, timeout=None):
                return _Resp(b"pas du json")

        with patch.object(app.urllib.request, "build_opener",
                          lambda *a, **kw: _Opener()):
            self.assertIsNone(app._check_cli_freshness(env={}))


class DiagCarriesFreshnessTests(unittest.TestCase):
    def test_field_exists_even_on_early_return(self):
        with patch.object(app, "_claude_cli_path", lambda: None):
            out = app._diagnose_claude_cli()
        self.assertIn("latest_cli_version", out)

    def test_freshness_is_not_fetched_when_the_ping_succeeds(self):
        """Un diagnostic sain ne fait pas d'aller-retour registre inutile."""
        def forbidden(*a, **kw):
            raise AssertionError("le registre ne doit pas etre consulte")

        with patch.object(app, "_claude_cli_path", lambda: "/bin/claude"), \
                patch.object(app.subprocess, "run",
                             lambda cmd, **kw: type("R", (), {
                                 "stdout": "2.1.232", "stderr": "",
                                 "returncode": 0})()), \
                patch.object(app, "_check_cli_node",
                             lambda: {"node_path": "/x/node",
                                      "node_version": "v22",
                                      "node_warning": None}), \
                patch.object(app, "_check_cli_credentials",
                             lambda **kw: ("ok", "jeton accessible")), \
                patch.object(app, "_call_claude_cli", lambda *a, **kw: "pong"), \
                patch.object(app, "_check_cli_freshness", forbidden):
            out = app._diagnose_claude_cli()
        self.assertTrue(out["ping_ok"])
        self.assertIsNone(out["latest_cli_version"])


class VerdictWiringTests(unittest.TestCase):
    def test_stale_binary_verdict_exists_in_both_languages(self):
        self.assertEqual(app.HTML.count("cli_diag_stale_binary:"), 2)

    def test_verdict_checks_the_token_before_blaming_auth(self):
        """L'ancien verdict "authentification" ne sort plus quand la sonde
        jeton dit ok : c'est la contradiction exacte du rapport utilisateur."""
        self.assertIn("d.creds_status === 'ok'", app.HTML)
        self.assertIn("cli_diag_stale_binary", app.HTML)

    def test_facts_show_the_published_version_gap(self):
        self.assertIn("derniere version publiee du CLI", app.HTML)


if __name__ == "__main__":
    unittest.main()
