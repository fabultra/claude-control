"""v1.15.0 - Tests : les messages construits cote serveur parlent la langue
de l'utilisateur.

Constat d'audit : l'UI est bilingue depuis longtemps (dicts fr/en cote JS)
mais les messages fabriques COTE SERVEUR -- abandon d'un lot de reparation,
preflight, verdict "session expiree" du diagnostic, compte rendu du
nettoyage des orphelins -- partaient en francais chez les utilisateurs en
anglais. Mecanique : catalogue _SRV_MSG + _srv(key, lang=..., **fmt) ; la
langue vient du parametre lang deja transporte par les routes de
reparation, ou de l'en-tete X-CC-Lang pose par api() et range par requete
dans un threading.local (serveur thread-par-requete). Les jobs de fond
capturent la langue au lancement. Defaut : francais -- tous les tests
existants qui verifiaient des textes francais passent inchanges.
"""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class CatalogueTests(unittest.TestCase):
    def test_every_key_exists_in_both_languages(self):
        self.assertEqual(set(app._SRV_MSG["fr"]), set(app._SRV_MSG["en"]))
        for lang in ("fr", "en"):
            for key, txt in app._SRV_MSG[lang].items():
                self.assertTrue(txt.strip(), f"{lang}:{key} vide")

    def test_norm_lang_is_forgiving(self):
        for raw in ("en", "EN", "en-US", " english "):
            self.assertEqual(app._norm_lang(raw), "en")
        for raw in ("fr", "fr-CA", None, "", "de", "nawak"):
            self.assertEqual(app._norm_lang(raw), "fr")

    def test_srv_picks_language_and_formats(self):
        self.assertEqual(app._srv("bulk_none", lang="fr"),
                         "Aucun skill à réparer")
        self.assertEqual(app._srv("bulk_none", lang="en"),
                         "No skill needs repair")
        self.assertIn("3 skill(s)", app._srv("bulk_preflight", lang="en", n=3))

    def test_missing_placeholder_never_raises(self):
        # Le texte brut vaut mieux qu'une 500.
        out = app._srv("bulk_preflight", lang="fr")  # {n} non fourni
        self.assertIn("{n}", out)

    def test_default_language_is_french(self):
        # Sans lang explicite ni requete en cours : francais, la langue
        # historique -- aucun test existant ne change de comportement.
        if hasattr(app._REQ_LANG, "lang"):
            del app._REQ_LANG.lang
        self.assertEqual(app._srv("bulk_dismissed"), "Compte rendu effacé")

    def test_request_thread_local_wins_when_set(self):
        app._REQ_LANG.lang = "en"
        try:
            self.assertEqual(app._srv("bulk_dismissed"), "Report cleared")
        finally:
            del app._REQ_LANG.lang


class BulkMessagesTests(unittest.TestCase):
    def test_start_bulk_repair_speaks_the_callers_language(self):
        with patch.object(app, "_claude_cli_path", lambda: None):
            ok, msg = app.start_bulk_repair(lang="en")
        self.assertFalse(ok)
        self.assertIn("not found in PATH", msg)
        with patch.object(app, "_claude_cli_path", lambda: None):
            ok, msg = app.start_bulk_repair(lang="fr")
        self.assertIn("introuvable dans le PATH", msg)

    def test_probe_timeout_message_is_translated(self):
        def hang(*a, **kw):
            raise app.ClaudeCliTimeout(60, "partial text")

        with patch.object(app, "_call_claude_cli", hang):
            _ok, _s, err_en = app._bulk_repair_probe(lang="en")
            _ok, _s, err_fr = app._bulk_repair_probe(lang="fr")
        self.assertIn("did not answer a trivial call", err_en)
        self.assertIn("It had started writing", err_en)
        self.assertIn("n'a pas répondu", err_fr)
        self.assertIn("commencé à écrire", err_fr)

    def test_empty_probe_output_is_translated(self):
        with patch.object(app, "_call_claude_cli", lambda *a, **kw: "   "):
            _ok, _s, err = app._bulk_repair_probe(lang="en")
        self.assertIn("answered with no text", err)

    def test_abort_notification_follows_the_job_language(self):
        notified = []
        with patch.object(app, "_notify",
                          lambda head, body: notified.append((head, body))):
            app._bulk_repair_abort("some reason", lang="en")
        self.assertEqual(notified[0][0], "Skills repair stopped")
        # Etat du lot remis au propre pour les suites.
        with app._BULK_REPAIR_LOCK:
            app._BULK_REPAIR.update({"aborted": False, "abort_reason": None,
                                     "phase": "idle", "finished_at": None})


class DiagnosticVerdictTests(unittest.TestCase):
    def test_not_logged_in_verdict_is_translated(self):
        out_by_lang = {}
        for lang in ("fr", "en"):
            app._REQ_LANG.lang = lang
            try:
                with patch.object(app, "_claude_cli_path", lambda: "/bin/claude"), \
                        patch.object(app.subprocess, "run",
                                     lambda cmd, **kw: type("R", (), {
                                         "stdout": "2.1.173", "stderr": "",
                                         "returncode": 0})()), \
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
                        patch.object(app, "_check_cli_freshness",
                                     lambda **kw: None), \
                        patch.object(app, "_cli_probe",
                                     lambda o, **kw: {"ok": False,
                                                      "seconds": 45}), \
                        patch.object(app, "_cli_debug_tail",
                                     lambda **kw: "Anthropic profile login expired"):
                    out_by_lang[lang] = app._diagnose_claude_cli()
            finally:
                del app._REQ_LANG.lang
        self.assertIn("session Anthropic est expirée", out_by_lang["fr"]["error"])
        self.assertIn("session has expired", out_by_lang["en"]["error"])
        for out in out_by_lang.values():
            self.assertIn("claude /login", out["error"])


class OrphansAndSessionTests(unittest.TestCase):
    def test_cleanup_summary_is_translated(self):
        plugins = [{"full_name": "a@m", "extra_versions": ["1.0"]}]
        app._REQ_LANG.lang = "en"
        try:
            with patch.object(app, "list_plugins", lambda: plugins), \
                    patch.object(app, "cleanup_plugin_orphan",
                                 lambda n, v: (True, "ok")):
                ok, payload = app.cleanup_all_plugin_orphans()
        finally:
            del app._REQ_LANG.lang
        self.assertTrue(ok)
        self.assertIn("orphan plugin version(s) cleaned up",
                      payload["message"])

    def test_state_exposes_expiry_numbers_for_the_ui(self):
        import time as _t
        with patch.object(app, "_read_cli_oauth_expiry",
                          lambda: _t.time() - 3 * 86400 - 60):
            s = app._check_cli_session(force=True)
        self.assertEqual(s["days"], 3)
        self.assertGreaterEqual(s["hours"], 72)
        # Et l'UI a les cles pour construire la phrase dans sa langue.
        self.assertEqual(app.HTML.count("cli_session_expired_days:"), 2)
        self.assertEqual(app.HTML.count("cli_session_expired_hours:"), 2)
        self.assertIn("cli_session_days", app.HTML)

    def test_api_helper_sends_the_language_header(self):
        self.assertIn("'X-CC-Lang': CURRENT_LANG", app.HTML)

    def test_do_post_stores_the_request_language(self):
        import inspect
        src = inspect.getsource(app.Handler.do_POST)
        self.assertIn("_REQ_LANG.lang", src)
        self.assertIn("X-CC-Lang", src)


if __name__ == "__main__":
    unittest.main()
