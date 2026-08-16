"""v1.15.0 - Tests : les backups ont enfin une politique de retention.

Constat d'audit : chaque suppression/reparation cree un filet de securite
(zip, json, dossier de run Terminal) et RIEN n'est jamais purge -- des mois
d'usage accumulent des centaines d'archives dans ~/.claude/backups/ et
~/.claude/claude-control-terminal-repair/. Politique retenue : purger ce
qui a plus de 30 jours en gardant TOUJOURS les 10 entrees les plus
recentes de chaque racine (un utilisateur inactif six mois conserve ses
derniers filets). Invariante de construction, comme _CLI_AUTO_HEAL : la
purge n'est armee QUE par main() -- une suite de tests qui oublierait de
rediriger BACKUP_DIR ne peut pas vider les vrais backups d'une machine de
dev.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402

OLD = time.time() - 90 * 86400      # bien au-dela des 30 jours
FRESH = time.time() - 3600          # une heure


def _touch(path, when, content="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.utime(path, (when, when))
    return path


def _make_dir(path, when):
    path.mkdir(parents=True, exist_ok=True)
    (path / "out-000.txt").write_text("resultat")
    os.utime(path, (when, when))
    return path


class PurgeEntriesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_old_files_go_recent_files_stay(self):
        old = _touch(self.tmp / "config.old.json", OLD)
        recent = _touch(self.tmp / "config.recent.json", FRESH)
        deleted, kept, errors = app._purge_dir_entries(
            self.tmp, "files", time.time() - 30 * 86400, min_keep=0)
        self.assertEqual((deleted, kept, errors), (1, 1, 0))
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_min_keep_floor_survives_even_when_everything_is_old(self):
        """Le plancher : jamais moins de N filets, meme apres six mois."""
        files = [_touch(self.tmp / f"backup-{i:02d}.zip", OLD - i * 60)
                 for i in range(15)]
        deleted, kept, _ = app._purge_dir_entries(
            self.tmp, "files", time.time() - 30 * 86400, min_keep=10)
        self.assertEqual((deleted, kept), (5, 10))
        # Les 10 GARDES sont les plus recents (mtime decroissant = index 0-9).
        for f in files[:10]:
            self.assertTrue(f.exists(), f)
        for f in files[10:]:
            self.assertFalse(f.exists(), f)

    def test_files_mode_never_touches_subdirectories(self):
        """orphan-plugins/ vit SOUS BACKUP_DIR : c'est une racine a part,
        jamais une entree purgeable du parent."""
        sub = self.tmp / "orphan-plugins"
        inner = _touch(sub / "precieux.zip", OLD)
        os.utime(sub, (OLD, OLD))
        deleted, kept, _ = app._purge_dir_entries(
            self.tmp, "files", time.time() - 30 * 86400, min_keep=0)
        self.assertEqual(deleted, 0)
        self.assertTrue(inner.exists())

    def test_dirs_mode_removes_whole_old_run_dirs(self):
        old_run = _make_dir(self.tmp / "20250101-120000", OLD)
        fresh_run = _make_dir(self.tmp / "20260816-090000", FRESH)
        stray = _touch(self.tmp / ".DS_Store", OLD)
        deleted, _, errors = app._purge_dir_entries(
            self.tmp, "dirs", time.time() - 30 * 86400, min_keep=0)
        self.assertEqual((deleted, errors), (1, 0))
        self.assertFalse(old_run.exists())
        self.assertTrue(fresh_run.exists())
        self.assertTrue(stray.exists())  # mode dirs : fichiers ignores

    def test_symlink_is_detached_never_followed(self):
        target = _make_dir(self.tmp / "ailleurs" / "vrai", OLD)
        root = self.tmp / "racine"
        root.mkdir()
        link = root / "lien"
        link.symlink_to(target)
        os.utime(root / "lien", (OLD, OLD), follow_symlinks=False)
        deleted, _, errors = app._purge_dir_entries(
            root, "dirs", time.time() - 30 * 86400, min_keep=0)
        self.assertEqual((deleted, errors), (1, 0))
        self.assertFalse(os.path.lexists(link))
        self.assertTrue((target / "out-000.txt").exists())  # cible intacte

    def test_missing_root_is_a_quiet_zero(self):
        self.assertEqual(app._purge_dir_entries(
            self.tmp / "absent", "files", time.time(), 0), (0, 0, 0))


class ArmingInvariantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._state = dict(app._RETENTION_STATE)

    def tearDown(self):
        app._RETENTION_STATE.update(self._state)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_disarmed_purge_deletes_nothing(self):
        """La garantie : sans main(), meme des chemins pointes sur de vieux
        fichiers ne sont pas touches."""
        victim = _touch(self.tmp / "backups" / "vieux.zip", OLD)
        app._RETENTION_STATE["armed"] = False
        with patch.object(app, "BACKUP_DIR", self.tmp / "backups"), \
                patch.object(app, "ORPHAN_BACKUP_DIR", self.tmp / "o"), \
                patch.object(app, "TERMINAL_REPAIR_DIR", self.tmp / "t"):
            self.assertEqual(app.purge_old_backups(), (0, 0))
        self.assertTrue(victim.exists())

    def test_armed_purge_covers_the_three_roots(self):
        b = self.tmp / "backups"
        o = self.tmp / "orphans"
        t = self.tmp / "terminal"
        _touch(b / "vieux.zip", OLD)
        _touch(o / "vieux-orphan.zip", OLD)
        _make_dir(t / "20250101-000000", OLD)
        _touch(b / "recent.zip", FRESH)
        app._RETENTION_STATE["armed"] = True
        with patch.object(app, "BACKUP_DIR", b), \
                patch.object(app, "ORPHAN_BACKUP_DIR", o), \
                patch.object(app, "TERMINAL_REPAIR_DIR", t), \
                patch.object(app, "BACKUP_RETENTION_MIN_KEEP", 0):
            deleted, errors = app.purge_old_backups()
        self.assertEqual((deleted, errors), (3, 0))
        self.assertTrue((b / "recent.zip").exists())

    def test_default_state_is_disarmed(self):
        self.assertFalse(self._state["armed"])


class WiringTests(unittest.TestCase):
    def test_main_arms_and_schedules_the_loop(self):
        import inspect
        src = inspect.getsource(app.main)
        self.assertIn('_RETENTION_STATE["armed"] = True', src)
        self.assertIn("_retention_loop", src)
        self.assertIn("86400", src)  # une fois par jour

    def test_policy_constants_are_sane(self):
        self.assertGreaterEqual(app.BACKUP_RETENTION_DAYS, 7)
        self.assertGreaterEqual(app.BACKUP_RETENTION_MIN_KEEP, 5)


if __name__ == "__main__":
    unittest.main()
