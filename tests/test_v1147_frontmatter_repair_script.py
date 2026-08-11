"""v1.14.7 - Tests du script de reparation des frontmatters casses.

Cinq SKILL.md de l'utilisateur portent la trace de la corruption d'avant
v1.14.5 : une `description:` refermee sur sa ligne, suivie des lignes de
continuation de l'ancien bloc YAML. Le frontmatter n'est plus valide, donc
le skill n'est plus charge.

Le script retire ces lignes. Deux exigences, parce qu'il ecrit dans les
fichiers reels de l'utilisateur :

- ne jamais toucher un fichier sain -- en particulier le scalaire
  multi-ligne entre guillemets, qui EST du YAML valide et ressemble
  pourtant beaucoup a la corruption ;
- ne rien perdre d'autre. Une premiere version soudait la derniere cle au
  `---` fermant : le YAML restait lisible par un split naif, et le defaut
  serait passe inapercu sans comparaison au fichier d'origine.
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "repair_fm",
    Path(__file__).resolve().parents[1] / "scripts"
    / "repair-corrupted-frontmatter.py")
repair_fm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repair_fm)

try:
    import yaml
except ImportError:
    yaml = None

CORRUPTED = ('---\nname: dc\n'
             'description: "Nouvelle description generee."\n'
             '  Desktop Commander usage tracking. Use when the user asks\n'
             '  about quotas.\n'
             'tags: [ops]\n---\n# Corps du skill\n')

HEALTHY = {
    "simple": '---\nname: ok\ndescription: "Normale."\ntags: [a]\n---\ncorps\n',
    "bloc_folded": ('---\nname: b\ndescription: >-\n  Un bloc valide\n'
                    '  sur deux lignes.\ntags: [a]\n---\ncorps\n'),
    "bloc_literal": ('---\nname: c\ndescription: |\n  une\n  deux\n'
                     'tags: [a]\n---\ncorps\n'),
    "quote_multiligne": ('---\nname: d\ndescription: "une valeur entre\n'
                         '  guillemets qui continue"\ntags: [a]\n---\ncorps\n'),
    "sans_frontmatter": "# juste du markdown\n",
}


class DetectionTests(unittest.TestCase):
    def test_corrupted_is_detected(self):
        self.assertIsNotNone(repair_fm.repaired(CORRUPTED))

    def test_healthy_files_are_left_alone(self):
        for label, text in HEALTHY.items():
            with self.subTest(cas=label):
                self.assertIsNone(repair_fm.repaired(text))

    def test_multiline_quoted_scalar_is_not_touched(self):
        """Le faux positif dangereux : c'est du YAML valide, et il ressemble
        a la corruption."""
        self.assertIsNone(repair_fm.repaired(HEALTHY["quote_multiligne"]))


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.out = repair_fm.repaired(CORRUPTED)

    def test_orphan_lines_are_gone(self):
        self.assertNotIn("about quotas.", self.out)
        self.assertNotIn("Desktop Commander usage tracking", self.out)

    def test_new_description_is_kept(self):
        self.assertIn('description: "Nouvelle description generee."', self.out)

    def test_other_keys_survive(self):
        self.assertIn("name: dc", self.out)
        self.assertIn("tags: [ops]", self.out)

    def test_closing_marker_stays_on_its_own_line(self):
        """Le defaut invisible : `tags: [ops]---` restait lisible par un
        split naif sur '---', mais le frontmatter etait faux."""
        self.assertIn("tags: [ops]\n---\n", self.out)
        self.assertNotIn("]---", self.out)

    def test_body_is_untouched(self):
        self.assertTrue(self.out.endswith("# Corps du skill\n"))

    def test_only_the_orphans_are_removed(self):
        removed = len(CORRUPTED.splitlines()) - len(self.out.splitlines())
        self.assertEqual(removed, 2)

    def test_app_reads_the_result(self):
        d = Path(tempfile.mkdtemp()) / "s"
        d.mkdir()
        (d / "SKILL.md").write_text(self.out)
        self.assertEqual(app.read_skill_meta(d)["description"],
                         "Nouvelle description generee.")

    @unittest.skipIf(yaml is None, "PyYAML absent")
    def test_result_is_valid_yaml(self):
        end = self.out.find("\n---", 3)
        parsed = yaml.safe_load(self.out[3:end])
        self.assertEqual(parsed["description"], "Nouvelle description generee.")
        self.assertEqual(parsed["tags"], ["ops"])

    @unittest.skipIf(yaml is None, "PyYAML absent")
    def test_original_really_was_invalid(self):
        """Sans ca, le test ci-dessus ne prouverait rien."""
        end = CORRUPTED.find("\n---", 3)
        with self.assertRaises(yaml.YAMLError):
            yaml.safe_load(CORRUPTED[3:end])


class DryRunTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "casse").mkdir()
        self.target = self.root / "casse" / "SKILL.md"
        self.target.write_text(CORRUPTED)

    def _run(self, *argv):
        old = sys.argv
        sys.argv = ["repair", *argv, str(self.root)]
        try:
            return repair_fm.main()
        finally:
            sys.argv = old

    def test_dry_run_writes_nothing(self):
        self._run()
        self.assertEqual(self.target.read_text(), CORRUPTED)

    def test_apply_writes_and_backs_up(self):
        self._run("--apply")
        self.assertNotIn("about quotas.", self.target.read_text())
        backup = self.target.with_suffix(".md.bak-frontmatter")
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(), CORRUPTED)

    def test_apply_is_idempotent(self):
        self._run("--apply")
        once = self.target.read_text()
        self._run("--apply")
        self.assertEqual(self.target.read_text(), once)


if __name__ == "__main__":
    unittest.main()
