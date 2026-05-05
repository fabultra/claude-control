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


if __name__ == "__main__":
    unittest.main()
