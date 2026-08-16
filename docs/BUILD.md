# Build : le monofichier assemblé

Depuis v1.15.0, `src/app.py` (~11 000 lignes) n'est plus édité à la main :
c'est un **artefact assemblé** depuis trois sources dans `src/parts/`.

```
src/parts/backend.py   Python : config, CLI Claude, réparation, plugins, state
src/parts/ui.html      la page embarquée : HTML + CSS + JS + i18n fr/en
src/parts/server.py    Python : routes HTTP, Handler, garde, main()
        │
        ▼  python3 scripts/build.py
src/app.py             l'artefact livré — commité, zéro dépendance
```

L'assemblage est une concaténation exacte :

```
app.py = backend.py + 'HTML = r"""' + ui.html + '"""' + server.py
```

## Pourquoi garder le monofichier ?

- **L'auto-update** de l'app fait `git pull` puis copie `src/app.py` vers
  `~/Applications/claude-control/app.py` : un seul fichier à copier, pas de
  résolution de modules.
- **`scripts/repair.sh`** et l'installeur reposent sur la même copie unique.
- **Philosophie zéro-dépendance** : `python3 app.py` suffit, partout.

L'éclatement ne change rien au produit : il rend les sources éditables avec
le bon outillage (coloration HTML/JS, diff lisibles, revues ciblées).

## Flux de travail

1. Éditer `src/parts/backend.py`, `ui.html` ou `server.py`.
2. `python3 scripts/build.py` → régénère `src/app.py`.
3. Commiter **les deux côtés ensemble** (parts + app.py).

La CI exécute `python3 scripts/build.py --check` : si `app.py` ne
correspond pas à l'assemblage des parts, le build échoue avec la marche à
suivre. Une dérive ne passe jamais silencieusement.

## App.py édité directement par accident ?

```
python3 scripts/build.py --split
```

re-dérive les trois parts depuis `src/app.py` (l'opération inverse exacte
de l'assemblage — l'aller-retour est identique à l'octet près, c'est testé
dans `tests/test_v1150_build_split.py`).

## Règles du jeu

- `ui.html` ne doit jamais contenir `"""` (cela fermerait la constante
  `HTML` en plein milieu ; `build.py` refuse d'assembler dans ce cas).
- La version affichée vient de `version.txt`, pas des sources.
- Les tests importent `src/app.py` (l'artefact) : ils testent ce qui est
  livré, pas les parts.
