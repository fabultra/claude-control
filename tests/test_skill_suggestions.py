"""v1.7.5 - Verifie que skill_optimization_suggestions emet bien un
suggestion 'duplicate' / 'duplicate_many' quand un skill utilisateur a le
meme nom qu'un skill plugin. Avant le fix, l'intersection se faisait sur
state['skills'] qui dedoublonne deja, donc le suggestion ne tirait jamais
(le bouton 'Supprimer les doublons' restait inaccessible cote UI).
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class DuplicateSuggestionTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.skills_dir = root / "skills"
        self.disabled_dir = root / "skills-disabled"
        self.skills_dir.mkdir()
        self.disabled_dir.mkdir()

        self._orig = (
            app.SKILLS_DIR, app.SKILLS_DISABLED_DIR,
            app._list_plugin_skills, app.get_skill_usage, app.load_config,
        )
        app.SKILLS_DIR = self.skills_dir
        app.SKILLS_DISABLED_DIR = self.disabled_dir
        app._list_plugin_skills = lambda: [
            {"name": n, "_source": "plugin", "meta": {"category": "", "description": "", "tags": []}}
            for n in self._plugin_skill_names
        ]
        app.get_skill_usage = lambda days=30: {"ok": False, "counts": {}, "ranked": []}
        app.load_config = lambda: {"mcpServers": {}, "_disabledMcps": {}}
        self._plugin_skill_names = []

    def tearDown(self):
        (app.SKILLS_DIR, app.SKILLS_DISABLED_DIR,
         app._list_plugin_skills, app.get_skill_usage, app.load_config) = self._orig
        self.tmpdir.cleanup()

    def _write_skill(self, name, body="---\nname: x\ndescription: a description that is long enough\n---\nfoo"):
        d = self.skills_dir / name
        d.mkdir()
        (d / "SKILL.md").write_text(body)

    def test_duplicate_suggestion_fires_when_user_and_plugin_share_name(self):
        """C'est le coeur du fix v1.7.5 : avant, la suggestion ne tirait jamais
        parce que get_state() avait deja dedoublonne. Maintenant on lit
        directement _list_plugin_skills."""
        self._write_skill("shared-name")
        self._write_skill("user-only")
        self._plugin_skill_names = ["shared-name", "plugin-only"]
        result = app.skill_optimization_suggestions()
        kinds = [s["kind"] for s in result["suggestions"]]
        self.assertIn("duplicate", kinds)
        dup_sug = next(s for s in result["suggestions"] if s["kind"] == "duplicate")
        self.assertEqual(dup_sug["items"], ["shared-name"])

    def test_duplicate_many_when_more_than_5(self):
        for n in ["a", "b", "c", "d", "e", "f", "g"]:
            self._write_skill(n)
        self._plugin_skill_names = ["a", "b", "c", "d", "e", "f", "g"]
        result = app.skill_optimization_suggestions()
        kinds = [s["kind"] for s in result["suggestions"]]
        self.assertIn("duplicate_many", kinds)

    def test_no_duplicate_suggestion_when_no_overlap(self):
        self._write_skill("user-only")
        self._plugin_skill_names = ["plugin-only"]
        result = app.skill_optimization_suggestions()
        kinds = [s["kind"] for s in result["suggestions"]]
        self.assertNotIn("duplicate", kinds)
        self.assertNotIn("duplicate_many", kinds)


if __name__ == "__main__":
    unittest.main()
