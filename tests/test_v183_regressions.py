"""v1.8.3 - Tests regression pour Bug D (MCPB en standby classifiees
'Inactifs' a tort) et Bug E (extension cochee qui ne demarre jamais,
investigation main.log).
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class BugDStandbyMcpRunningTests(unittest.TestCase):
    """Bug D : log MCPB age > 120s mais pas de shutdown gracieux ->
    extension chargee en standby legitime, doit etre 'running' dans l'UI.
    L'ancien critere mtime <= 120s la classait a tort 'Inactifs'."""

    @property
    def fixtures_dir(self):
        return Path(__file__).parent / "fixtures"

    def test_log_standby_not_graceful_shutdown(self):
        """La fixture standby ne contient aucun marqueur de shutdown ->
        _log_shows_graceful_shutdown returns False -> UI peut classer
        running."""
        path = self.fixtures_dir / "log_standby_mcp.txt"
        self.assertFalse(app._log_shows_graceful_shutdown(path))

    def test_graceful_shutdown_fixture_still_detected(self):
        """Anti-regression : la fixture graceful_shutdown reste detectee
        comme telle (apres mes refactors de _read_log_tail seek-based)."""
        path = self.fixtures_dir / "log_graceful_shutdown.txt"
        self.assertTrue(app._log_shows_graceful_shutdown(path))


class ReadLogTailEfficientTests(unittest.TestCase):
    """v1.8.3 - Anti-regression sur le seek-based _read_log_tail :
    doit fonctionner sur petits fichiers (< 64 KB), gros fichiers
    (> 64 KB), et fichiers absents."""

    def test_small_file_full_read(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write("line1\nline2\nline3\n")
            path = f.name
        try:
            lines = app._read_log_tail(path, max_lines=10)
            self.assertEqual(lines, ["line1", "line2", "line3"])
        finally:
            os.unlink(path)

    def test_max_lines_truncation(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            for i in range(50):
                f.write(f"line{i}\n")
            path = f.name
        try:
            lines = app._read_log_tail(path, max_lines=5)
            self.assertEqual(lines, ["line45", "line46", "line47", "line48", "line49"])
        finally:
            os.unlink(path)

    def test_large_file_seek_based_caps_bytes(self):
        """Garantit qu'on ne lit jamais plus que max_bytes meme si le
        fichier est tres gros. La premiere ligne (potentiellement
        coupee) est jetee."""
        # Cree un fichier de 200 KB de lignes numerotees
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            for i in range(20000):
                f.write(f"line{i:06d}-padding-padding-padding\n")
            path = f.name
        try:
            lines = app._read_log_tail(path, max_lines=10, max_bytes=4_000)
            # On a lu seulement les ~4 KB finaux. Les lignes contiennent
            # toutes 'line' au debut (sauf eventuellement la 1ere coupee
            # qu'on a jetee).
            self.assertLessEqual(len(lines), 10)
            for ln in lines:
                self.assertTrue(ln.startswith("line"), f"unexpected: {ln!r}")
            # Les lignes sont les plus recentes (numero eleve)
            self.assertIn("line019999", lines[-1])
        finally:
            os.unlink(path)

    def test_missing_file_returns_empty(self):
        self.assertEqual(app._read_log_tail("/nonexistent/path"), [])


class BugEMainLogScanTests(unittest.TestCase):
    """Bug E : extension cochee mais aucun fichier mcp-server-X.log ->
    jamais demarree. _scan_main_log_for_extension_failures cherche dans
    main.log les signaux d'echec (allowlist, error, blocked, ...) pour
    fournir un hint a l'utilisateur."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.fake_logs = Path(self.tmpdir.name)
        self._orig = app.CLAUDE_LOGS_DIR
        app.CLAUDE_LOGS_DIR = self.fake_logs

    def tearDown(self):
        app.CLAUDE_LOGS_DIR = self._orig
        self.tmpdir.cleanup()

    def _write_main_log(self, content):
        (self.fake_logs / "main.log").write_text(content)

    def test_finds_allowlist_blocked_for_stripe(self):
        self._write_main_log(
            "2026-05-06 15:19:00 [info] Loading extension Stripe\n"
            "2026-05-06 15:19:00 [error] Extension stripe blocked by dxt:allowlist\n"
            "2026-05-06 15:19:01 [info] Continuing with other extensions\n"
        )
        hints = app._scan_main_log_for_extension_failures("Stripe")
        self.assertIsNotNone(hints)
        self.assertEqual(len(hints), 1)
        self.assertIn("blocked", hints[0])

    def test_returns_none_when_no_failure(self):
        self._write_main_log(
            "2026-05-06 15:19:00 [info] Loading extension Stripe\n"
            "2026-05-06 15:19:01 [info] Stripe loaded successfully\n"
        )
        hints = app._scan_main_log_for_extension_failures("Stripe")
        self.assertIsNone(hints)

    def test_returns_none_when_main_log_missing(self):
        # Pas de main.log dans la fake_logs dir
        hints = app._scan_main_log_for_extension_failures("Stripe")
        self.assertIsNone(hints)

    def test_caps_to_3_matches(self):
        lines = ["2026-05-06 [error] stripe failed to start"] * 10
        self._write_main_log("\n".join(lines) + "\n")
        hints = app._scan_main_log_for_extension_failures("stripe")
        self.assertIsNotNone(hints)
        self.assertLessEqual(len(hints), 3)

    def test_does_not_match_unrelated_errors(self):
        """Anti-regression : une erreur sur 'desktop-commander' ne doit
        pas remonter si on cherche 'stripe'."""
        self._write_main_log(
            "2026-05-06 [error] desktop-commander failed to load\n"
        )
        hints = app._scan_main_log_for_extension_failures("Stripe")
        self.assertIsNone(hints)


if __name__ == "__main__":
    unittest.main()
