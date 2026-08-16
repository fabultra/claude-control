"""v1.14.20 - Tests : toutes les versions orphelines nettoyees en un clic.

Demande utilisateur : la Vue d'ensemble annoncait "8 version(s)
orpheline(s) de plugin" mais la correction demandait huit clics et huit
confirmations. Le lot reutilise, orphelin par orphelin, le chemin eprouve
de cleanup_plugin_orphan (backup .zip puis suppression, version active
jamais touchee) et rend un compte rendu par camps (nettoyes / echecs).
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


def _plugins_with_orphans():
    return [
        {"full_name": "sentry@claude-plugins-official",
         "extra_versions": ["1.0.0", "1.3.1"]},
        {"full_name": "stripe@claude-plugins-official",
         "extra_versions": ["0.1.0"]},
        {"full_name": "superpowers@claude-plugins-official",
         "extra_versions": []},
    ]


class CleanupAllTests(unittest.TestCase):
    def test_every_orphan_goes_through_the_proven_single_path(self):
        calls = []
        with patch.object(app, "list_plugins", _plugins_with_orphans), \
                patch.object(app, "cleanup_plugin_orphan",
                             lambda n, v: calls.append((n, v)) or (True, "ok")):
            ok, payload = app.cleanup_all_plugin_orphans()
        self.assertTrue(ok)
        self.assertEqual(len(calls), 3)
        self.assertIn(("sentry@claude-plugins-official", "1.3.1"), calls)
        self.assertEqual(len(payload["cleaned"]), 3)
        self.assertEqual(payload["failed"], [])
        self.assertIn("3 version(s) orpheline(s) nettoyée(s)",
                      payload["message"])
        self.assertIn("orphan-plugins", payload["message"])

    def test_one_failure_does_not_stop_the_batch(self):
        def flaky(n, v):
            if v == "1.0.0":
                return False, "Dossier orphelin introuvable"
            return True, "ok"

        with patch.object(app, "list_plugins", _plugins_with_orphans), \
                patch.object(app, "cleanup_plugin_orphan", flaky):
            ok, payload = app.cleanup_all_plugin_orphans()
        self.assertTrue(ok)  # au moins un nettoye
        self.assertEqual(len(payload["cleaned"]), 2)
        self.assertEqual(len(payload["failed"]), 1)
        self.assertIn("introuvable", payload["failed"][0]["error"])
        self.assertIn("1 en échec", payload["message"])

    def test_all_failing_is_reported_as_a_failure(self):
        with patch.object(app, "list_plugins", _plugins_with_orphans), \
                patch.object(app, "cleanup_plugin_orphan",
                             lambda n, v: (False, "boom")):
            ok, payload = app.cleanup_all_plugin_orphans()
        self.assertFalse(ok)
        self.assertEqual(payload["cleaned"], [])
        self.assertEqual(len(payload["failed"]), 3)

    def test_an_exception_on_one_orphan_is_contained(self):
        def explosive(n, v):
            if v == "0.1.0":
                raise OSError("disque plein")
            return True, "ok"

        with patch.object(app, "list_plugins", _plugins_with_orphans), \
                patch.object(app, "cleanup_plugin_orphan", explosive):
            ok, payload = app.cleanup_all_plugin_orphans()
        self.assertTrue(ok)
        self.assertEqual(len(payload["cleaned"]), 2)
        self.assertIn("OSError", payload["failed"][0]["error"])

    def test_nothing_to_do_is_a_calm_success(self):
        with patch.object(app, "list_plugins", lambda: [
                {"full_name": "x", "extra_versions": []}]):
            ok, payload = app.cleanup_all_plugin_orphans()
        self.assertTrue(ok)
        self.assertIn("Aucune", payload["message"])

    def test_unlistable_plugins_fail_without_touching_anything(self):
        def boom():
            raise RuntimeError("registre illisible")

        forbidden = lambda n, v: (_ for _ in ()).throw(
            AssertionError("aucun nettoyage sans listing"))
        with patch.object(app, "list_plugins", boom), \
                patch.object(app, "cleanup_plugin_orphan", forbidden):
            ok, msg = app.cleanup_all_plugin_orphans()
        self.assertFalse(ok)
        self.assertIn("Listing", str(msg))


class WiringTests(unittest.TestCase):
    def test_route_is_registered_and_serialised(self):
        import inspect
        self.assertIn("/api/plugin-cleanup-all",
                      inspect.getsource(app.Handler.do_POST))
        self.assertIn("/api/plugin-cleanup-all", app._CONFIG_MUTATING_ROUTES)

    def test_overview_issue_line_carries_the_action(self):
        self.assertIn("cleanupAllOrphans(", app.HTML)
        self.assertEqual(app.HTML.count("btn_cleanup_all_orphans:"), 2)
        self.assertEqual(app.HTML.count("confirm_cleanup_all_orphans:"), 2)
        # La confirmation nomme les garanties qui comptent.
        self.assertIn("jamais touchées", app.HTML)
        self.assertIn("never touched", app.HTML)


if __name__ == "__main__":
    unittest.main()
