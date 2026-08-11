#!/usr/bin/env python3
"""Repare les SKILL.md dont le frontmatter a ete casse par une reparation.

Avant la v1.14.5, l'app remplacait la seule LIGNE `description:` lors d'une
reparation. Sur une description ecrite en bloc YAML (`>-`, `|`, ou valeur
indentee), ses lignes de continuation restaient derriere la nouvelle valeur :

    description: "Nouvelle description generee."
      Desktop Commander usage tracking. Use when the user asks
      about quotas.
    tags: [ops]

Le frontmatter n'est plus du YAML valide -- "expected <block end>" -- donc le
skill cesse d'etre charge, alors que l'app affichait "reparation reussie".

Ce script retire les lignes orphelines. La nouvelle description, elle, est
correcte : c'est bien celle qu'on voulait garder.

    python3 scripts/repair-corrupted-frontmatter.py            # apercu seul
    python3 scripts/repair-corrupted-frontmatter.py --apply    # applique

Rien n'est ecrit sans --apply. Avec --apply, chaque fichier modifie est
d'abord copie en .bak-frontmatter a cote de lui.
"""
import argparse
import shutil
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

DEFAULT_ROOTS = [
    Path.home() / ".claude/skills",
    Path.home() / ".claude/skills-disabled",
]


def split_frontmatter(text):
    """Retourne (lignes_frontmatter, index_debut) ou (None, None)."""
    if not text.startswith("---"):
        return None, None
    end = text.find("\n---", 3)
    if end == -1:
        return None, None
    return text[3:end].strip("\n").splitlines(), end


def orphan_span(lines):
    """Localise les lignes orphelines qui suivent une `description:` deja
    close sur sa ligne. Retourne (debut, fin) ou None.

    Une valeur entre guillemets refermes sur la meme ligne ne peut pas se
    poursuivre : toute ligne indentee qui suit est un residu.
    """
    for i, line in enumerate(lines):
        if not line.startswith("description:"):
            continue
        value = line[len("description:"):].strip()
        closed = (len(value) >= 2 and value[0] == value[-1] == '"'
                  and not value.endswith('\\"'))
        if not closed:
            return None
        j = i + 1
        while j < len(lines) and (not lines[j].strip()
                                  or lines[j][:1] in (" ", "\t")):
            j += 1
        return (i + 1, j) if j > i + 1 else None
    return None


def repaired(text):
    """Retourne le contenu repare, ou None s'il n'y a rien a faire."""
    lines, end = split_frontmatter(text)
    if lines is None:
        return None
    span = orphan_span(lines)
    if not span:
        return None
    kept = lines[:span[0]] + lines[span[1]:]
    if yaml is not None:
        # Critere le plus sur quand PyYAML est la : ne reparer que ce qui
        # etait casse, et seulement si le resultat est valide.
        try:
            yaml.safe_load("\n".join(lines))
            return None          # deja valide : on n'y touche pas
        except yaml.YAMLError:
            pass
        try:
            yaml.safe_load("\n".join(kept))
        except yaml.YAMLError:
            return None          # la retouche ne suffit pas : on s'abstient
    # `end` pointe le \n qui precede le --- fermant : il faut le remettre,
    # sinon la derniere cle et le --- se retrouvent soudes sur une ligne.
    return "---\n" + "\n".join(kept) + "\n" + text[end + 1:]


def iter_skill_files(roots):
    for root in roots:
        if root.is_dir():
            yield from sorted(root.glob("*/SKILL.md"))
            yield from sorted(root.glob("*/*/SKILL.md"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="ecrit les corrections (defaut : apercu seul)")
    ap.add_argument("paths", nargs="*", type=Path,
                    help="dossiers a parcourir (defaut : ~/.claude/skills)")
    args = ap.parse_args()

    roots = args.paths or DEFAULT_ROOTS
    if yaml is None:
        print("PyYAML absent : detection structurelle seule, "
              "relis l'apercu avant d'appliquer.\n")

    # Dire ce qui a ete regarde, pas seulement ce qui a ete trouve. Sans ca,
    # "aucun frontmatter casse" est indistinguable de "je n'ai scanne aucun
    # fichier" -- et c'est exactement l'ambiguite qui a fait croire que cinq
    # fichiers casses etaient sains.
    for root in roots:
        state = "absent" if not root.exists() else (
            "pas un dossier" if not root.is_dir() else None)
        if state:
            print(f"  {root} : {state}")

    scanned = 0
    touched = 0
    for path in iter_skill_files(roots):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        scanned += 1
        fixed = repaired(text)
        if fixed is None:
            continue
        touched += 1
        removed = len(text.splitlines()) - len(fixed.splitlines())
        print(f"{'CORRIGE ' if args.apply else 'A CORRIGER'} {path}")
        for line in text.splitlines():
            if line and line[:1] in (" ", "\t") and line not in fixed:
                print(f"    - {line.strip()[:88]}")
        print(f"    ({removed} ligne(s) orpheline(s))")
        if args.apply:
            shutil.copy2(path, path.with_suffix(".md.bak-frontmatter"))
            path.write_text(fixed)

    if not scanned:
        print("Aucun SKILL.md scanne. Racines examinees :")
        for root in roots:
            print(f"  {root}")
        print("Passe le bon dossier en argument, par exemple :\n"
              "  python3 scripts/repair-corrupted-frontmatter.py ~/.claude/skills")
        return 1
    if not touched:
        print(f"{scanned} SKILL.md scanne(s), aucun frontmatter casse.")
    elif not args.apply:
        print(f"\n{touched} fichier(s). Relance avec --apply pour ecrire "
              f"(une copie .bak-frontmatter est faite a cote de chacun).")
    else:
        print(f"\n{touched} fichier(s) repare(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
