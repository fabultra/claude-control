"""v1.6.5 - Verifie que _mcp_log_says_frozen ne tue plus les MCPs sains
mais idle. Avant le fix, l'heuristique 'mtime du log > 2x window ET Claude
Desktop responsive' declarait frozen tout MCP qui n'ecrivait pas dans son log
depuis quelques minutes — ce qui est le cas de la plupart des MCPs sains
quand ils ne traitent pas de requete. Resultat : le watchdog killait des
MCPs fonctionnels en boucle.
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class FrozenDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.fake_logs = Path(self.tmpdir.name)
        self._orig_logs = app.CLAUDE_LOGS_DIR
        app.CLAUDE_LOGS_DIR = self.fake_logs

    def tearDown(self):
        app.CLAUDE_LOGS_DIR = self._orig_logs
        self.tmpdir.cleanup()

    def _write_log(self, name, body, age_seconds=0):
        log = self.fake_logs / f"mcp-server-{name}.log"
        log.write_text(body)
        if age_seconds:
            past = time.time() - age_seconds
            os.utime(log, (past, past))
        return log

    def test_recent_log_with_error_marker_is_frozen(self):
        self._write_log("test", "starting...\nMCP test transport closed unexpectedly\n")
        self.assertTrue(app._mcp_log_says_frozen("test", within_seconds=60))

    def test_recent_log_without_marker_is_not_frozen(self):
        self._write_log("test", "ok ok ok\nrequest handled in 3ms\n")
        self.assertFalse(app._mcp_log_says_frozen("test", within_seconds=60))

    def test_old_log_without_marker_is_not_frozen(self):
        """Le coeur du fix v1.6.5 : un MCP idle dont le log est ancien n'est
        PAS un MCP frozen. Avant, _mcp_log_says_frozen retournait True parce
        que mtime > 2x window — ce qui faisait kill un MCP sain."""
        self._write_log("idle", "request handled\n", age_seconds=600)
        self.assertFalse(app._mcp_log_says_frozen("idle", within_seconds=60))

    def test_old_log_with_marker_still_detects_freeze(self):
        """Un MCP qui a ecrit une erreur puis est devenu silencieux est bien
        un freeze, meme si le log est ancien."""
        self._write_log("dead", "boom\nprocess exiting early\n", age_seconds=600)
        self.assertTrue(app._mcp_log_says_frozen("dead", within_seconds=60))

    def test_no_log_file_is_not_frozen(self):
        self.assertFalse(app._mcp_log_says_frozen("nonexistent", within_seconds=60))


class DcLogFreezeTypeClassifierTests(unittest.TestCase):
    """v1.8.0 P0 - distingue Type A (frozen_backend) vs Type B
    (frozen_ui_rendering) en parsant les correspondances client_ids
    <-> server_ids dans le log Claude. Fixtures reelles dans tests/fixtures/.
    """

    @property
    def fixtures_dir(self):
        return Path(__file__).parent / "fixtures"

    def test_type_a_frozen_backend(self):
        """Fixture synthetique : 3 client ids, 2 server ids, id 102 sans
        reponse. Verdict attendu : frozen_backend, unanswered = [102]."""
        result = app._classify_dc_log_freeze_type(self.fixtures_dir / "log_type_a_frozen_backend.txt")
        self.assertEqual(result["type"], "frozen_backend")
        self.assertEqual(result["details"]["unanswered_client_ids"], [102])
        self.assertEqual(result["details"]["client_ids_count"], 3)
        self.assertEqual(result["details"]["server_ids_count"], 2)

    def test_type_b_frozen_ui_rendering_real_fixture(self):
        """Fixture reelle 2026-05-05 : DC repond a tous les client ids (236-260),
        Claude Desktop n'envoie plus de nouvelles requetes pendant 23 min.
        Verdict attendu : frozen_ui_rendering, bonus signals tous a True."""
        result = app._classify_dc_log_freeze_type(self.fixtures_dir / "log_type_b_ui_rendering.txt")
        self.assertEqual(result["type"], "frozen_ui_rendering")
        self.assertEqual(result["details"]["unanswered_client_ids"], [])
        # Bonus signals attendus (cf. header de la fixture) :
        self.assertTrue(result["details"]["duplicate_read_file"],
                        "4 read_file identiques sur SKILL.md devraient declencher duplicate_read_file")
        self.assertTrue(result["details"]["large_payload"],
                        "Reponse SKILL.md > 25k chars devrait declencher large_payload>20k")
        self.assertTrue(result["details"]["track_ui_event_burst"],
                        "14 track_ui_event en quelques secondes devraient declencher burst")

    def test_graceful_shutdown_detection(self):
        """Fixture reelle 2026-05-06 17:21 : DC s'est arrete proprement
        (Server transport closed intentional). _log_shows_graceful_shutdown
        doit retourner True."""
        path = self.fixtures_dir / "log_graceful_shutdown.txt"
        self.assertTrue(app._log_shows_graceful_shutdown(path))

    def test_type_b_fixture_does_not_show_graceful_shutdown(self):
        """Anti-regression : le log Type B est full d'activite, pas de
        marqueur de shutdown. _log_shows_graceful_shutdown doit retourner
        False."""
        path = self.fixtures_dir / "log_type_b_ui_rendering.txt"
        self.assertFalse(app._log_shows_graceful_shutdown(path))

    def test_inconclusive_when_log_empty(self):
        """Edge case : log vide ou sans Message from client/server -> on
        ne classe pas (verdict inconclusive)."""
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write("just some random log lines\nno protocol here\n")
            path = f.name
        try:
            result = app._classify_dc_log_freeze_type(path)
            self.assertEqual(result["type"], "inconclusive")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
