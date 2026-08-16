#!/usr/bin/env python3
"""Claude Control - assemblage du monofichier src/app.py.

v1.15.0 - le fichier unique (~11 000 lignes) etait devenu le principal
obstacle a la maintenance : Python, HTML, CSS, JS et les deux dictionnaires
i18n vivaient dans le meme app.py, sans coloration ni outillage adaptes.
Les sources sont desormais eclatees dans src/parts/ :

    backend.py   Python : config, CLI Claude, reparation, plugins, state
    ui.html      la page embarquee : HTML + CSS + JS + i18n fr/en
    server.py    Python : routes HTTP, Handler, garde, main()

src/app.py RESTE l'artefact livre -- commite, copie tel quel par
l'auto-update (git pull + copie) et par scripts/repair.sh, execute sans
aucune dependance. Ce script ne fait que concatener :

    app.py = backend.py + 'HTML = r\"\"\"' + ui.html + '\"\"\"' + server.py

Usage :
    python3 scripts/build.py            # regenere src/app.py
    python3 scripts/build.py --check    # verifie qu'app.py == assemblage (CI)
    python3 scripts/build.py --split    # re-derive src/parts/ depuis app.py
                                        # (secours si app.py a ete edite direct)

La CI execute --check : editer app.py sans regenerer les parts (ou
l'inverse) casse le build, jamais silencieusement.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARTS = ROOT / "src/parts"
TARGET = ROOT / "src/app.py"
OPEN = 'HTML = r"""'
CLOSE = '"""\n'
PART_FILES = ("backend.py", "ui.html", "server.py")


def assemble():
    backend = (PARTS / "backend.py").read_text(encoding="utf-8")
    ui = (PARTS / "ui.html").read_text(encoding="utf-8")
    server = (PARTS / "server.py").read_text(encoding="utf-8")
    if '"""' in ui:
        sys.exit("build.py: ui.html contient \"\"\" -- cela fermerait la "
                 "constante HTML en plein milieu. Retire ou encode cette "
                 "sequence.")
    if not backend.endswith("\n"):
        sys.exit("build.py: backend.py doit se terminer par un saut de ligne.")
    return backend + OPEN + ui + CLOSE + server


def split(text):
    """Coupe app.py en (backend, ui, server). Inverse exact d'assemble()."""
    i = text.index(OPEN)
    ui_start = i + len(OPEN)
    close = text.index('"""', ui_start)
    if text[close:close + len(CLOSE)] != CLOSE:
        sys.exit("build.py: fermeture de la constante HTML inattendue.")
    return text[:i], text[ui_start:close], text[close + len(CLOSE):]


def cmd_build():
    TARGET.write_text(assemble(), encoding="utf-8")
    print(f"build.py: {TARGET.relative_to(ROOT)} regenere "
          f"({TARGET.stat().st_size} octets).")


def cmd_check():
    missing = [n for n in PART_FILES if not (PARTS / n).is_file()]
    if missing:
        sys.exit(f"build.py --check: parts manquantes : {missing}")
    expected = assemble()
    actual = TARGET.read_text(encoding="utf-8")
    if expected != actual:
        sys.exit(
            "build.py --check: src/app.py ne correspond pas a src/parts/.\n"
            "  - edite dans src/parts/ ? -> python3 scripts/build.py\n"
            "  - edite app.py directement ? -> python3 scripts/build.py "
            "--split\n"
            "puis commite les deux cotes ensemble.")
    print("build.py --check: OK (app.py == assemblage des parts).")


def cmd_split():
    backend, ui, server = split(TARGET.read_text(encoding="utf-8"))
    PARTS.mkdir(parents=True, exist_ok=True)
    (PARTS / "backend.py").write_text(backend, encoding="utf-8")
    (PARTS / "ui.html").write_text(ui, encoding="utf-8")
    (PARTS / "server.py").write_text(server, encoding="utf-8")
    print(f"build.py --split: parts re-derivees dans "
          f"{PARTS.relative_to(ROOT)}/.")


def main(argv):
    if argv == ["--check"]:
        cmd_check()
    elif argv == ["--split"]:
        cmd_split()
    elif argv in ([], ["--build"]):
        cmd_build()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
