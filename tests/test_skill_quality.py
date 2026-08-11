"""v1.7.7 / v1.14.1 - Tests pour _skill_quality, le classifier de qualite des
skills qui pilote le health banner et le filtre 'Qualite' de la sidebar.

v1.14.1 : le seuil passe de 30 a 120 caracteres ET exige un marqueur de
declenchement. Motivation : a 30 caracteres, "Debug Railway" etait classe
"excellent" alors que Claude ne peut pas savoir QUAND declencher le skill.
Une description remplie n'est pas une description utile — c'est le seul
signal de declenchement, pas un resume.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


# Description realiste qui coche les deux criteres (>= 120 chars + marqueur).
GOOD = ("Diagnostique les echecs de deploiement Railway (build failed, crash, "
        "502, service down) : lit les logs et propose un correctif. A utiliser "
        "des qu'un deploiement Railway echoue.")


class SkillQualityTests(unittest.TestCase):
    def test_no_description_is_broken(self):
        for empty in (None, "", "   "):
            self.assertEqual(app._skill_quality(empty), "broken")

    def test_short_description_is_enrich(self):
        self.assertEqual(app._skill_quality("Short"), "enrich")
        self.assertEqual(app._skill_quality("Debug Railway"), "enrich")

    def test_just_below_threshold_is_enrich(self):
        desc = "a" * (app._SKILL_DESC_MIN_CHARS - 1) + " use when x"
        # trop court malgre le marqueur
        self.assertEqual(app._skill_quality(desc[:app._SKILL_DESC_MIN_CHARS - 1]), "enrich")

    def test_long_but_no_trigger_terms_is_enrich(self):
        """Le coeur du correctif v1.14.1 : une description longue qui ne dit
        jamais QUAND declencher reste insuffisante."""
        desc = ("Ce skill contient une collection d'utilitaires internes pour "
                "manipuler des donnees tabulaires, normaliser des colonnes et "
                "produire des exports au format attendu par l'equipe.")
        self.assertGreaterEqual(len(desc), app._SKILL_DESC_MIN_CHARS)
        self.assertEqual(app._skill_quality(desc), "enrich")

    def test_long_with_trigger_terms_is_excellent(self):
        self.assertEqual(app._skill_quality(GOOD), "excellent")

    def test_english_trigger_cue_recognised(self):
        desc = ("Generates and validates OpenAPI specifications from an existing "
                "Express or FastAPI codebase, including request and response "
                "schemas. Use when the user asks for API documentation.")
        self.assertEqual(app._skill_quality(desc), "excellent")

    def test_strips_whitespace_for_length_check(self):
        padded = "  " + GOOD + "  "
        self.assertEqual(app._skill_quality(padded), "excellent")
        short = "  " + "a" * (app._SKILL_DESC_MIN_CHARS - 2) + "  "
        self.assertEqual(app._skill_quality(short), "enrich")


class SkillQualityReasonTests(unittest.TestCase):
    """v1.14.1 - la raison est exposee a l'UI pour dire QUOI corriger."""

    def test_reason_no_description(self):
        self.assertEqual(app._skill_quality_detail(""), ("broken", "no_description"))

    def test_reason_too_short(self):
        self.assertEqual(app._skill_quality_detail("Debug Railway"),
                         ("enrich", "too_short"))

    def test_reason_no_trigger_terms(self):
        desc = ("Ce skill contient une collection d'utilitaires internes pour "
                "manipuler des donnees tabulaires et produire des exports au "
                "format attendu par l'equipe interne.")
        self.assertEqual(app._skill_quality_detail(desc),
                         ("enrich", "no_trigger_terms"))

    def test_reason_ok(self):
        self.assertEqual(app._skill_quality_detail(GOOD), ("excellent", "ok"))


if __name__ == "__main__":
    unittest.main()
