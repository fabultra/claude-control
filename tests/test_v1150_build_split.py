"""v1.15.0 - Tests : le monofichier est un artefact assemble, sans derive.

Constat d'audit : ~11 000 lignes de Python + HTML + CSS + JS + i18n dans
un seul app.py, inedit-able avec un outillage normal. Eclatement :
src/parts/{backend.py, ui.html, server.py}, assembles par
scripts/build.py en un app.py identique a l'octet pres -- l'artefact
monofichier reste commite (l'auto-update le copie tel quel).

Ce que ces tests verrouillent :
- l'egalite stricte app.py == assemblage des parts (le --check de la CI) ;
- l'aller-retour split -> build identique a l'octet pres ;
- la contrainte de la couture (jamais de triple-quote dans ui.html) ;
- chaque part Python est syntaxiquement valide seule ;
- la CI execute bien le --check.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PARTS = REPO / "src/parts"
APP = REPO / "src/app.py"
BUILD = REPO / "scripts/build.py"
OPEN = 'HTML = r"""'
CLOSE = '"""\n'


def _assemble_from_parts():
    return ((PARTS / "backend.py").read_text()
            + OPEN
            + (PARTS / "ui.html").read_text()
            + CLOSE
            + (PARTS / "server.py").read_text())


class AssemblyTests(unittest.TestCase):
    def test_committed_app_py_equals_the_parts(self):
        """La meme egalite que le --check de la CI, recalculee ici sans
        passer par build.py : deux implementations du meme contrat."""
        self.assertEqual(_assemble_from_parts(), APP.read_text())

    def test_build_check_command_passes(self):
        r = subprocess.run([sys.executable, str(BUILD), "--check"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_check_fails_loudly_on_drift(self):
        """Une edition d'app.py sans regeneration doit CASSER la CI avec la
        marche a suivre, jamais passer silencieusement."""
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp)
            (fake / "scripts").mkdir()
            (fake / "src").mkdir()
            shutil.copytree(PARTS, fake / "src/parts")
            shutil.copy2(BUILD, fake / "scripts/build.py")
            drifted = APP.read_text() + "\n# edition sauvage\n"
            (fake / "src/app.py").write_text(drifted)
            r = subprocess.run(
                [sys.executable, str(fake / "scripts/build.py"), "--check"],
                capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--split", r.stdout + r.stderr)

    def test_split_then_build_roundtrips_byte_for_byte(self):
        original = APP.read_text()
        i = original.index(OPEN)
        ui_start = i + len(OPEN)
        close = original.index('"""', ui_start)
        backend = original[:i]
        ui = original[ui_start:close]
        server = original[close + len(CLOSE):]
        self.assertEqual(backend + OPEN + ui + CLOSE + server, original)

    def test_ui_part_contains_no_triple_quote(self):
        self.assertNotIn('"""', (PARTS / "ui.html").read_text())

    def test_python_parts_are_valid_syntax_on_their_own(self):
        for name in ("backend.py", "server.py"):
            with self.subTest(part=name):
                compile((PARTS / name).read_text(), name, "exec")

    def test_the_artifact_announces_its_sources(self):
        head = APP.read_text()[:2000]
        self.assertIn("FICHIER ASSEMBLE", head)
        self.assertIn("scripts/build.py", head)

    def test_ci_runs_the_build_check(self):
        workflow = (REPO / ".github/workflows/tests.yml").read_text()
        self.assertIn("build.py --check", workflow)


class SplitShapeTests(unittest.TestCase):
    def test_parts_have_the_expected_responsibilities(self):
        backend = (PARTS / "backend.py").read_text()
        server = (PARTS / "server.py").read_text()
        ui = (PARTS / "ui.html").read_text()
        # backend : la logique metier, pas le Handler HTTP.
        self.assertIn("def _call_claude_cli", backend)
        self.assertNotIn("class Handler", backend)
        # server : routes + main, pas de logique CLI.
        self.assertIn("class Handler", server)
        self.assertIn("def main():", server)
        # ui : la page complete.
        self.assertTrue(ui.startswith("<!DOCTYPE html>"))
        self.assertTrue(ui.rstrip().endswith("</html>"))

    def test_docs_exist_for_build_and_cli_health(self):
        self.assertTrue((REPO / "docs/BUILD.md").is_file())
        health = (REPO / "docs/CLI-HEALTH.md").read_text()
        # La carte nomme les briques cles de la machine a etats.
        for brick in ("_call_claude_cli", "_CLI_RUNGS", "auto-heal",
                      "_check_cli_session", "_pretrust_cli_workdir",
                      "Terminal"):
            self.assertIn(brick, health)


if __name__ == "__main__":
    unittest.main()
