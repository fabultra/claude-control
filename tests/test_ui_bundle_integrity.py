"""v1.14.3 - Garde-fou sur le bundle JS servi par l'app.

Toute l'UI vit dans une chaine Python, et rien ne la validait. Une virgule
oubliee dans le dictionnaire de traductions ne casse pas la ligne fautive :
elle casse le parsing du script ENTIER, donc l'application entiere devient
une page morte -- sans qu'aucun test ne bronche et sans erreur cote serveur.

C'est arrive en ecrivant cette version meme : une entree i18n ajoutee sans
virgule finale. Le suite de tests restait verte.

Deux invariants ici :
  1. chaque bloc <script> inline parse ;
  2. les dictionnaires fr et en portent les memes cles -- tr() retombe
     silencieusement sur le francais quand une cle manque en anglais, donc
     un oubli passe inapercu jusqu'a ce qu'un utilisateur anglophone tombe
     sur une phrase francaise.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


def _html_templates():
    """Les grandes chaines du module qui contiennent du <script>."""
    out = []
    for name in dir(app):
        val = getattr(app, name)
        if isinstance(val, str) and "<script" in val and len(val) > 2000:
            out.append((name, val))
    return out


def _slice_object_literal(src, anchor):
    """Extrait `anchor{...}` en comptant les accolades hors chaines.

    Un simple src[index:] embarquerait tout le code navigateur qui suit
    (localStorage, document...) et l'eval planterait sous node.
    """
    start = src.index(anchor)
    i = start + len(anchor) - 1          # sur l'accolade ouvrante
    depth, quote, esc = 0, None, False
    while i < len(src):
        c = src[i]
        if esc:
            esc = False
        elif quote:
            if c == "\\":
                esc = True
            elif c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError(f"litteral non termine pour {anchor!r}")


def _script_blocks(html):
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    return [b for b in blocks if b.strip()]


class ScriptSyntaxTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("node absent")

    def test_every_inline_script_parses(self):
        templates = _html_templates()
        self.assertTrue(templates, "aucun template HTML trouve dans app.py")
        checked = 0
        for tpl_name, html in templates:
            for i, block in enumerate(_script_blocks(html)):
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                                 encoding="utf-8") as fh:
                    fh.write(block)
                    tmp = fh.name
                r = subprocess.run(["node", "--check", tmp],
                                   capture_output=True, text=True, timeout=60)
                Path(tmp).unlink(missing_ok=True)
                self.assertEqual(
                    r.returncode, 0,
                    f"script #{i} de {tpl_name} ne parse pas :\n{r.stderr[:800]}")
                checked += 1
        self.assertGreater(checked, 0, "aucun bloc <script> inline analyse")


class TranslationParityTests(unittest.TestCase):
    """tr() retombe sur T.fr quand la cle manque en anglais : le defaut est
    invisible tant qu'on ne lit pas l'UI en anglais."""

    def _dicts(self):
        if not shutil.which("node"):
            self.skipTest("node absent")
        html = _html_templates()[0][1] if len(_html_templates()) == 1 else None
        for _, h in _html_templates():
            if "const T = {" in h:
                html = h
                break
        self.assertIsNotNone(html, "declaration `const T` introuvable")
        block = next(b for b in _script_blocks(html) if "const T = {" in b)
        decl = _slice_object_literal(block, "const T = {")
        # On laisse node evaluer la declaration puis rendre les cles : plus
        # fiable qu'un parsing maison sur des valeurs qui contiennent des
        # accolades, des apostrophes et des sauts de ligne.
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(decl + ";")
            fh.write("\nconsole.log(JSON.stringify("
                     "{fr: Object.keys(T.fr), en: Object.keys(T.en)}));\n")
            tmp = fh.name
        r = subprocess.run(["node", tmp], capture_output=True, text=True, timeout=60)
        Path(tmp).unlink(missing_ok=True)
        self.assertEqual(r.returncode, 0, f"eval de T impossible :\n{r.stderr[:600]}")
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_no_key_missing_in_english(self):
        keys = self._dicts()
        missing = sorted(set(keys["fr"]) - set(keys["en"]))
        self.assertEqual(missing, [],
                         f"cles absentes de l'anglais (l'UI EN affichera du "
                         f"francais) : {missing}")

    def test_no_orphan_key_in_english(self):
        keys = self._dicts()
        orphan = sorted(set(keys["en"]) - set(keys["fr"]))
        self.assertEqual(orphan, [],
                         f"cles anglaises sans equivalent francais : {orphan}")


if __name__ == "__main__":
    unittest.main()
