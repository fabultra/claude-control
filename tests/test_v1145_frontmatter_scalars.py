"""v1.14.5 - Tests : les descriptions multi-lignes, lues et reecrites.

Symptome rapporte : l'app annonce "description trop courte" sur des skills
dont la description ne l'est pas.

Cause : le frontmatter etait lu ligne par ligne. YAML permet pourtant
quatre formes multi-lignes, toutes courantes des qu'une description depasse
la largeur d'un ecran :

    description: >-        -> l'app lisait ">-"   (2 caracteres)
    description: |         -> l'app lisait "|"    (1 caractere)
    description:           -> l'app ne lisait RIEN, et declarait le skill
      valeur en dessous       "sans description"
    description: debut     -> l'app ne lisait que la premiere ligne
      suite indentee

Le seuil de qualite est a 120 caracteres : toute troncature declenche
l'alerte. D'ou des skills parfaitement decrits marques "a reparer".

Et le defaut grave, celui qui detruisait des donnees : la reecriture
remplacait la seule ligne `description:` et laissait ses lignes de
continuation orphelines. Le frontmatter devenait invalide -- le skill
cessait d'etre lu -- pendant que l'app affichait "reparation reussie".
Reparer un skill sain le cassait definitivement.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402

try:
    import yaml
except ImportError:  # l'app ne depend pas de PyYAML, les tests non plus
    yaml = None

LONG = ("Desktop Commander usage tracking. Use when the user asks about "
        "desktop commander usage, quotas, or tool call volume over time.")


def _skill(frontmatter_body):
    d = Path(tempfile.mkdtemp()) / "skill"
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: demo\n{frontmatter_body}\ntags: [ops]\n---\n# corps\n")
    return d


class MultiLineDescriptionsAreReadTests(unittest.TestCase):
    """Les quatre formes que l'app tronquait."""

    def _desc(self, fm):
        return app.read_skill_meta(_skill(fm))["description"]

    def test_plain_single_line_still_works(self):
        """Le cas courant ne doit pas regresser."""
        self.assertEqual(self._desc(f"description: {LONG}"), LONG)

    def test_folded_block(self):
        d = self._desc("description: >-\n  Desktop Commander usage tracking.\n"
                       "  Use when the user asks about quotas.")
        self.assertEqual(
            d, "Desktop Commander usage tracking. Use when the user asks "
               "about quotas.")

    def test_literal_block_keeps_newlines(self):
        d = self._desc("description: |\n  ligne une\n  ligne deux")
        self.assertEqual(d, "ligne une\nligne deux")

    def test_value_starting_on_the_next_line(self):
        """Le pire cas : l'app declarait le skill sans description du tout."""
        d = self._desc("description:\n  Desktop Commander usage tracking.\n"
                       "  Use when the user asks about quotas.")
        self.assertIn("Use when the user asks", d)

    def test_indented_continuation(self):
        d = self._desc("description: Desktop Commander usage tracking.\n"
                       "  Use when the user asks about quotas.")
        self.assertIn("Use when the user asks", d)

    def test_folded_blank_line_separates_paragraphs(self):
        d = self._desc("description: >-\n  un deux\n\n  trois quatre")
        self.assertEqual(d, "un deux\ntrois quatre")

    def test_other_keys_are_not_swallowed(self):
        """Une entree s'arrete a la cle suivante, sinon `tags` finirait
        dans la description."""
        meta = app.read_skill_meta(_skill(
            "description: >-\n  une description repliee\ncategory: ops"))
        self.assertNotIn("category", meta["description"])
        self.assertEqual(meta["category"], "ops")
        self.assertEqual(meta["tags"], ["ops"])


class QualityVerdictTests(unittest.TestCase):
    """Le symptome tel que l'utilisateur le voit."""

    FORMS = {
        "plain": f"description: {LONG}",
        "folded": "description: >-\n  Desktop Commander usage tracking. Use\n"
                  "  when the user asks about desktop commander usage, quotas,\n"
                  "  or tool call volume over a period.",
        "literal": "description: |\n  Desktop Commander usage tracking. Use\n"
                   "  when the user asks about desktop commander usage, quotas,\n"
                   "  or tool call volume over a period.",
        "next_line": "description:\n  Desktop Commander usage tracking. Use\n"
                     "  when the user asks about desktop commander usage,\n"
                     "  quotas, or tool call volume over a period.",
    }

    def test_no_form_is_reported_as_too_short(self):
        for label, fm in self.FORMS.items():
            with self.subTest(form=label):
                desc = app.read_skill_meta(_skill(fm))["description"]
                quality, reason = app._skill_quality_detail(desc)
                self.assertNotEqual(reason, "too_short", f"{label} : {desc!r}")
                self.assertNotEqual(reason, "no_description")
                self.assertGreaterEqual(len(desc), app._SKILL_DESC_MIN_CHARS)


class RewriteDoesNotCorruptTests(unittest.TestCase):
    """Le defaut destructeur : reparer cassait le skill."""

    ORIG = ("---\nname: dc\ndescription: >-\n"
            "  Desktop Commander usage tracking. Use when the user asks\n"
            "  about quotas.\ntags: [ops]\n---\n# corps\n")

    def test_continuation_lines_are_removed(self):
        out = app._update_skill_frontmatter(self.ORIG, {"description": "neuve"})
        self.assertNotIn("about quotas.", out.split("---")[1])

    def test_other_keys_survive(self):
        out = app._update_skill_frontmatter(self.ORIG, {"description": "neuve"})
        self.assertIn("name: dc", out)
        self.assertIn("tags: [ops]", out)

    def test_body_survives(self):
        out = app._update_skill_frontmatter(self.ORIG, {"description": "neuve"})
        self.assertTrue(out.rstrip().endswith("# corps"))

    def test_result_is_readable_again(self):
        """Le round-trip est la vraie garantie : ce que l'app ecrit, elle
        doit savoir le relire."""
        out = app._update_skill_frontmatter(self.ORIG, {"description": LONG})
        d = Path(tempfile.mkdtemp()) / "s"
        d.mkdir()
        (d / "SKILL.md").write_text(out)
        self.assertEqual(app.read_skill_meta(d)["description"], LONG)

    @unittest.skipIf(yaml is None, "PyYAML absent")
    def test_result_is_valid_yaml(self):
        out = app._update_skill_frontmatter(self.ORIG, {"description": "neuve"})
        parsed = yaml.safe_load(out.split("---")[1])
        self.assertEqual(parsed["description"], "neuve")
        self.assertEqual(parsed["tags"], ["ops"])

    def test_literal_block_is_replaced_whole(self):
        orig = ("---\ndescription: |\n  une\n  deux\nname: x\n---\nbody")
        out = app._update_skill_frontmatter(orig, {"description": "neuve"})
        self.assertNotIn("  deux", out.split("---")[1])
        self.assertIn("name: x", out)

    def test_missing_key_is_appended(self):
        out = app._update_skill_frontmatter("---\nname: x\n---\nbody",
                                            {"description": "neuve"})
        self.assertIn("description:", out)
        self.assertIn("name: x", out)


class ScalarUnquotingTests(unittest.TestCase):
    """L'app ecrit ses valeurs via json.dumps : les relire au strip('\"')
    rendait litteralement les echappements."""

    def test_escaped_quotes_are_decoded(self):
        self.assertEqual(app._fm_unquote('"desc avec \\"guillemets\\""'),
                         'desc avec "guillemets"')

    def test_escaped_newline_is_decoded(self):
        self.assertEqual(app._fm_unquote('"une\\ndeux"'), "une\ndeux")

    def test_single_quotes(self):
        self.assertEqual(app._fm_unquote("'valeur'"), "valeur")

    def test_bare_value_is_untouched(self):
        self.assertEqual(app._fm_unquote("  valeur nue  "), "valeur nue")

    def test_apostrophe_inside_plain_value_survives(self):
        """strip(\"'\") mangeait les apostrophes de bord."""
        self.assertEqual(app._fm_unquote("l'app d'origine"), "l'app d'origine")

    def test_round_trip_through_the_writer(self):
        value = 'Une "description" avec : deux-points, accents é et \'quotes\''
        out = app._update_skill_frontmatter("---\nname: x\n---\nb",
                                            {"description": value})
        d = Path(tempfile.mkdtemp()) / "s"
        d.mkdir()
        (d / "SKILL.md").write_text(out)
        self.assertEqual(app.read_skill_meta(d)["description"], value)


class EntrySpanTests(unittest.TestCase):
    def test_span_covers_continuation_lines(self):
        lines = ["name: x", "description: >-", "  une", "  deux", "tags: [a]"]
        spans = {k: (s, e) for k, s, e in app._frontmatter_entries(lines)}
        self.assertEqual(spans["description"], (1, 4))
        self.assertEqual(spans["name"], (0, 1))
        self.assertEqual(spans["tags"], (4, 5))

    def test_list_items_are_not_mistaken_for_keys(self):
        lines = ["tags:", "  - un", "  - deux", "name: x"]
        spans = {k: (s, e) for k, s, e in app._frontmatter_entries(lines)}
        self.assertEqual(spans["tags"], (0, 3))
        self.assertIn("name", spans)


if __name__ == "__main__":
    unittest.main()
