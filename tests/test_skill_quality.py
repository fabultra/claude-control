"""v1.7.7 - Tests pour _skill_quality, le classifier de qualite des skills
qui pilote le health banner et le filtre 'Qualite' de la sidebar dans la
tab Skills refondue.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class SkillQualityTests(unittest.TestCase):
    def test_no_description_is_broken(self):
        self.assertEqual(app._skill_quality(None), "broken")
        self.assertEqual(app._skill_quality(""), "broken")
        self.assertEqual(app._skill_quality("   "), "broken")

    def test_short_description_is_enrich(self):
        self.assertEqual(app._skill_quality("Short"), "enrich")
        # Exactement 29 chars
        self.assertEqual(app._skill_quality("a" * 29), "enrich")

    def test_30_chars_or_more_is_excellent(self):
        # Boundary inclusive : 30 chars = excellent
        self.assertEqual(app._skill_quality("a" * 30), "excellent")
        self.assertEqual(
            app._skill_quality("This skill helps with file processing and validation."),
            "excellent",
        )

    def test_strips_whitespace_for_length_check(self):
        # 28 chars utiles + 10 espaces = enrich
        self.assertEqual(app._skill_quality("  " + "a" * 28 + "  "), "enrich")
        # 30 chars utiles + espaces = excellent
        self.assertEqual(app._skill_quality("  " + "a" * 30 + "  "), "excellent")


if __name__ == "__main__":
    unittest.main()
