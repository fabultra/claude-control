"""v1.7.4 - Tests pour le bulk delete des skills utilisateur en doublon
avec un plugin (action 'Vue d'ensemble' -> bouton de cleanup).
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class CleanupDuplicateUserSkillsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.skills_dir = root / "skills"
        self.disabled_dir = root / "skills-disabled"
        self.backup_dir = root / "backup"
        self.skills_dir.mkdir(parents=True)
        self.disabled_dir.mkdir(parents=True)
        self.backup_dir.mkdir(parents=True)

        self._orig = (app.SKILLS_DIR, app.SKILLS_DISABLED_DIR, app.BACKUP_DIR, app._list_plugin_skills)
        app.SKILLS_DIR = self.skills_dir
        app.SKILLS_DISABLED_DIR = self.disabled_dir
        app.BACKUP_DIR = self.backup_dir

        self._plugin_skill_names = []
        app._list_plugin_skills = lambda: [
            {"name": n, "_source": "plugin", "meta": {"category": "", "description": "", "tags": []}}
            for n in self._plugin_skill_names
        ]

    def tearDown(self):
        app.SKILLS_DIR, app.SKILLS_DISABLED_DIR, app.BACKUP_DIR, app._list_plugin_skills = self._orig
        self.tmpdir.cleanup()

    def _write_skill(self, base, name, body="---\nname: x\n---\nfoo"):
        d = base / name
        d.mkdir()
        (d / "SKILL.md").write_text(body)

    def test_no_duplicates_returns_ok_zero(self):
        self._write_skill(self.skills_dir, "alone")
        self._plugin_skill_names = ["other"]
        ok, payload = app.delete_user_skill_duplicates()
        self.assertTrue(ok)
        self.assertEqual(payload["deleted"], [])
        self.assertTrue((self.skills_dir / "alone").exists())

    def test_deletes_user_skill_when_plugin_has_same_name(self):
        self._write_skill(self.skills_dir, "shared")
        self._write_skill(self.skills_dir, "user-only")
        self._plugin_skill_names = ["shared"]
        ok, payload = app.delete_user_skill_duplicates()
        self.assertTrue(ok)
        self.assertEqual(payload["deleted"], ["shared"])
        self.assertEqual(payload["failed"], [])
        self.assertFalse((self.skills_dir / "shared").exists())
        self.assertTrue((self.skills_dir / "user-only").exists())
        # Backup zip individuel cree
        zips = list(self.backup_dir.glob("deleted-skill-shared-*.zip"))
        self.assertEqual(len(zips), 1)

    def test_deletes_disabled_user_skill_too(self):
        self._write_skill(self.disabled_dir, "disabled-shared")
        self._plugin_skill_names = ["disabled-shared"]
        ok, payload = app.delete_user_skill_duplicates()
        self.assertTrue(ok)
        self.assertEqual(payload["deleted"], ["disabled-shared"])
        self.assertFalse((self.disabled_dir / "disabled-shared").exists())

    def test_ignores_skills_dir_entries_without_skill_md(self):
        # Un dossier sans SKILL.md ne doit pas etre considere comme un skill user
        d = self.skills_dir / "not-a-skill"
        d.mkdir()
        (d / "README.md").write_text("not a skill")
        self._plugin_skill_names = ["not-a-skill"]
        ok, payload = app.delete_user_skill_duplicates()
        self.assertTrue(ok)
        self.assertEqual(payload["deleted"], [])
        self.assertTrue(d.exists())


class DeleteSkillNameValidationTests(unittest.TestCase):
    """v1.7.8 - regression : delete_skill rejetait les noms commencant par
    '_' (ex. '_archived-fireflies-setup') alors que c'est une convention
    utilisateur legitime. On garde les vraies protections path-traversal."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.skills_dir = root / "skills"
        self.disabled_dir = root / "skills-disabled"
        self.backup_dir = root / "backup"
        self.skills_dir.mkdir(parents=True)
        self.disabled_dir.mkdir(parents=True)
        self.backup_dir.mkdir(parents=True)
        self._orig = (app.SKILLS_DIR, app.SKILLS_DISABLED_DIR, app.BACKUP_DIR)
        app.SKILLS_DIR = self.skills_dir
        app.SKILLS_DISABLED_DIR = self.disabled_dir
        app.BACKUP_DIR = self.backup_dir

    def tearDown(self):
        app.SKILLS_DIR, app.SKILLS_DISABLED_DIR, app.BACKUP_DIR = self._orig
        self.tmpdir.cleanup()

    def _write_skill(self, name):
        d = self.skills_dir / name
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: x\n---")

    def test_underscore_prefix_is_now_allowed(self):
        """C'est le coeur du fix v1.7.8 : '_archived-X' est un nom legitime."""
        self._write_skill("_archived-fireflies-setup")
        ok, msg = app.delete_skill("_archived-fireflies-setup")
        self.assertTrue(ok, msg)
        self.assertFalse((self.skills_dir / "_archived-fireflies-setup").exists())

    def test_dot_prefix_still_rejected(self):
        ok, msg = app.delete_skill(".hidden")
        self.assertFalse(ok)
        self.assertIn("invalide", msg)

    def test_path_traversal_still_rejected(self):
        for evil in ("../etc", "foo/bar", "..\\windows", "../../passwd"):
            ok, msg = app.delete_skill(evil)
            self.assertFalse(ok, f"Should reject: {evil}")

    def test_empty_name_rejected(self):
        ok, _ = app.delete_skill("")
        self.assertFalse(ok)
        ok, _ = app.delete_skill(None)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
