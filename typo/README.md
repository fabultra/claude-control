# Typo K.lub — atelier

Construction d'une police complète à partir des lettres de référence du
logotype **K.lub** (K majuscule + `l u b` minuscules).

## Fichiers

- `klub_type.py` — l'atelier : chaque glyphe est dessiné en coordonnées
  fonte (UPM 1000, y vers le haut, ligne de base à 0) à partir de
  composants partagés (fûts, empattements, drapeaux, panses, épaules),
  puis aplati en un contour unique via `skia-pathops` (union booléenne).
  Le script génère la planche `specimen-minuscules.svg`.
- `specimen-minuscules.svg` — planche de proposition des minuscules.
- `render.html` — aperçu local (`chromium --headless --screenshot`).

## Utilisation

```bash
pip install skia-pathops
python3 klub_type.py
```

## État

- [x] Minuscules a–z + point (proposition v1)
- [ ] Majuscules A–Z (le K de référence comme maître)
- [ ] Chiffres, ponctuation, accents français (é è à ç ù …)
- [ ] Compilation .ttf/.otf/.woff2 via fontTools (les contours aplatis
      sont prêts pour `T2CharStringPen`/`TTGlyphPen`)
- [ ] Espacement fin + crénage des paires clés (av, To, k., etc.)

## Métriques retenues

| Repère | Valeur |
| --- | --- |
| Hauteur d'x | 495 |
| Ascendantes | 725 |
| Descendantes | −235 |
| Fût | 120 |
| Délié | 38 |
| Dépassement optique | 10 |
