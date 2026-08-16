"""v1.15.0 - Tests : la suggestion unitaire tourne en job de fond.

Constat d'audit : "Suggérer" sur UN skill tenait dans une seule requete
HTTP qui pouvait durer des minutes (echelle a 3 barreaux + auto-heal +
relance) -- thread serveur bloque, fetch a la merci d'un timeout
navigateur, rechargement de page = travail perdu. Meme motif que le lot :
POST -start retourne un id immediatement, le travail vit dans un thread,
l'UI interroge /api/suggest-job/<id> toutes les 2 s avec temps ecoule.
suggest_skill_description reste inchangee : le job n'est qu'un porteur.
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class _Inline:
    """Thread synchrone : _run s'execute pendant le start()."""

    def __init__(self, target=None, name=None, daemon=None):
        self._t = target

    def start(self):
        self._t()


class SuggestJobTests(unittest.TestCase):
    def setUp(self):
        with app._SUGGEST_JOBS_LOCK:
            app._SUGGEST_JOBS.clear()

    tearDown = setUp

    def test_start_returns_before_the_work_finishes(self):
        gate = threading.Event()

        def slow(name, lang=None):
            gate.wait(5)
            return True, {"suggestion": "enfin"}

        with patch.object(app, "suggest_skill_description", slow):
            t0 = time.monotonic()
            ok, out = app.start_suggest_job("demo", lang="fr")
            started_in = time.monotonic() - t0
        self.assertTrue(ok)
        self.assertLess(started_in, 1.0)  # retour immediat, travail en fond
        st = app.suggest_job_status(out["job"])
        self.assertTrue(st["running"])
        self.assertEqual(st["name"], "demo")
        gate.set()
        deadline = time.monotonic() + 5
        while st["running"] and time.monotonic() < deadline:
            time.sleep(0.02)
            st = app.suggest_job_status(out["job"])
        self.assertFalse(st["running"])
        self.assertEqual(st["result"],
                         {"success": True, "suggestion": "enfin"})
        self.assertIsInstance(st["seconds"], float)

    def test_double_click_reuses_the_running_job(self):
        gate = threading.Event()
        with patch.object(app, "suggest_skill_description",
                          lambda n, lang=None: (gate.wait(5), (True, {}))[1]):
            _ok, first = app.start_suggest_job("demo")
            _ok, second = app.start_suggest_job("demo")
        gate.set()
        self.assertEqual(first["job"], second["job"])
        self.assertTrue(second.get("already_running"))

    def test_result_envelope_matches_the_sync_route(self):
        """dict -> fusionne avec success ; str -> message. L'UI applique le
        MEME rendu que sur la route synchrone."""
        with patch.object(app.threading, "Thread", _Inline), \
                patch.object(app, "suggest_skill_description",
                             lambda n, lang=None: (False, {
                                 "error": "x", "error_code": "cli_timeout"})):
            _ok, out = app.start_suggest_job("demo")
        st = app.suggest_job_status(out["job"])
        self.assertEqual(st["result"]["error_code"], "cli_timeout")
        self.assertFalse(st["result"]["success"])

        with patch.object(app.threading, "Thread", _Inline), \
                patch.object(app, "suggest_skill_description",
                             lambda n, lang=None: (False, "texte brut")):
            _ok, out = app.start_suggest_job("demo2")
        self.assertEqual(app.suggest_job_status(out["job"])["result"],
                         {"success": False, "message": "texte brut"})

    def test_a_crash_in_the_worker_is_contained(self):
        def boom(n, lang=None):
            raise OSError("disque plein")

        with patch.object(app.threading, "Thread", _Inline), \
                patch.object(app, "suggest_skill_description", boom):
            _ok, out = app.start_suggest_job("demo")
        st = app.suggest_job_status(out["job"])
        self.assertFalse(st["running"])
        self.assertFalse(st["result"]["success"])
        self.assertIn("OSError", st["result"]["message"])

    def test_empty_name_is_refused(self):
        ok, _ = app.start_suggest_job("")
        self.assertFalse(ok)

    def test_unknown_job_reads_as_none(self):
        self.assertIsNone(app.suggest_job_status("sg-fantome"))

    def test_finished_jobs_are_pruned_running_never(self):
        gate = threading.Event()
        with patch.object(app, "suggest_skill_description",
                          lambda n, lang=None: (gate.wait(5), (True, {}))[1]):
            _ok, running = app.start_suggest_job("occupe")
        with patch.object(app, "_SUGGEST_JOBS_KEEP", 2), \
                patch.object(app.threading, "Thread", _Inline), \
                patch.object(app, "suggest_skill_description",
                             lambda n, lang=None: (True, {})):
            ids = [app.start_suggest_job(f"s{i}")[1]["job"] for i in range(5)]
        gate.set()
        # Le job en cours n'est jamais purge ; les termines les plus vieux
        # sont partis.
        self.assertIsNotNone(app.suggest_job_status(running["job"]))
        self.assertIsNone(app.suggest_job_status(ids[0]))
        self.assertIsNotNone(app.suggest_job_status(ids[-1]))

    def test_lang_travels_to_the_underlying_suggestion(self):
        seen = []
        with patch.object(app.threading, "Thread", _Inline), \
                patch.object(app, "suggest_skill_description",
                             lambda n, lang=None: seen.append((n, lang))
                             or (True, {})):
            app.start_suggest_job("demo", lang="en")
        self.assertEqual(seen, [("demo", "en")])


class WiringTests(unittest.TestCase):
    def test_routes_exist(self):
        import inspect
        self.assertIn("/api/suggest-skill-description-start",
                      inspect.getsource(app.Handler.do_POST))
        self.assertIn("/api/suggest-job/",
                      inspect.getsource(app.Handler.do_GET))

    def test_ui_uses_the_background_flow(self):
        self.assertIn("suggest-skill-description-start", app.HTML)
        self.assertIn("/api/suggest-job/", app.HTML)

    def test_lost_job_message_exists_in_both_languages(self):
        for lang in ("fr", "en"):
            self.assertIn("suggest_job_lost", app._SRV_MSG[lang])


if __name__ == "__main__":
    unittest.main()
