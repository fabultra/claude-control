# -*- coding: utf-8 -*-
"""
Atelier typographique K.lub — construction des minuscules.

Dessine les glyphes en coordonnees "fonte" (UPM 1000, y vers le haut,
ligne de base a y=0) et genere une planche specimen SVG.

ADN du dessin, extrait des lettres de reference (K . l u b) :
  - serif elegante a contraste moyen-eleve, axe vertical
  - empattements de pied a congés (brackets) discrets
  - attaques de tete en drapeau incline (l, b, u)
  - point rond (le "." du logo)
  - graisse demi-grasse
"""

# ---------------------------------------------------------------- metriques
XH = 495          # hauteur d'x
ASC = 725         # ascendantes (b d f h k l)
DESC = -235       # descendantes (g j p q y)
OV = 10           # depassement optique des rondes
T_TOP = 645       # hauteur du t

STEM = 120        # fût vertical
THIN = 38         # delie (haut/bas des panses)
BAR = 44          # barres horizontales (e, f, t)
SIDE = 124        # flanc des rondes (leger renfort optique vs fût)

EXT = 60          # debord d'empattement de pied (par cote)
SLAB = 26         # epaisseur de l'empattement au bord
BRK = 90          # hauteur du congé (bracket)

FEXT = 72         # debord du drapeau de tete vers la gauche
FDROP = 30        # pente du drapeau
FH = 116          # hauteur totale de l'attaque drapeau

BALL_R = 56       # goutte terminale (f, j, r, y, a, c)

INK = "#1B1917"
PAPER = "#F2F0EA"


def fmt(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


def M(x, y): return f"M {fmt(x)} {fmt(y)}"
def L(x, y): return f"L {fmt(x)} {fmt(y)}"
def C(x1, y1, x2, y2, x, y):
    return f"C {fmt(x1)} {fmt(y1)} {fmt(x2)} {fmt(y2)} {fmt(x)} {fmt(y)}"


def circle(cx, cy, r, ccw=True):
    """Cercle en 4 cubiques. ccw=True => contour plein."""
    k = 0.5523 * r
    s = 1 if ccw else -1
    p = [M(cx + r, cy)]
    if ccw:
        p.append(C(cx + r, cy + k, cx + k, cy + r, cx, cy + r))
        p.append(C(cx - k, cy + r, cx - r, cy + k, cx - r, cy))
        p.append(C(cx - r, cy - k, cx - k, cy - r, cx, cy - r))
        p.append(C(cx + k, cy - r, cx + r, cy - k, cx + r, cy))
    else:
        p.append(C(cx + r, cy - k, cx + k, cy - r, cx, cy - r))
        p.append(C(cx - k, cy - r, cx - r, cy - k, cx - r, cy))
        p.append(C(cx - r, cy + k, cx - k, cy + r, cx, cy + r))
        p.append(C(cx + k, cy + r, cx + r, cy + k, cx + r, cy))
    p.append("Z")
    return " ".join(p)


# ------------------------------------------------------------- composants

def foot_right(xr):
    """Fragment : descend le flanc droit du fût dans l'empattement,
    du point (xr, BRK) jusqu'a (xr+EXT, 0)."""
    return [
        C(xr + 10, BRK - 34, xr + EXT - 16, SLAB + 10, xr + EXT, SLAB),
        L(xr + EXT, 0),
    ]


def foot_left(xl):
    """Fragment : remonte de (xl-EXT, 0) vers le flanc gauche (xl, BRK)."""
    return [
        L(xl - EXT, SLAB),
        C(xl - EXT + 16, SLAB + 10, xl - 10, BRK - 34, xl, BRK),
    ]


def vstem(xl, xr, ybot, ytop, foot=True, head="flag", flag_ext=None,
          foot_ext_l=None, foot_ext_r=None):
    """Fût vertical ferme, avec pied et tete optionnels.
    Trace en CCW (repere y vers le haut)."""
    fe = FEXT if flag_ext is None else flag_ext
    el = EXT if foot_ext_l is None else foot_ext_l
    er = EXT if foot_ext_r is None else foot_ext_r
    p = []
    if foot:
        p.append(M(xl - el, 0))
        p.append(L(xr + er, 0))
        p.append(L(xr + er, SLAB))
        p.append(C(xr + er - 16, SLAB + 10, xr + 10, BRK - 34, xr, BRK))
    else:
        p.append(M(xl, ybot))
        p.append(L(xr, ybot))
    # flanc droit
    p.append(L(xr, ytop))
    if head == "flag":
        # attaque en drapeau : arete superieure inclinee vers la gauche,
        # ventre concave qui rejoint le flanc gauche
        p.append(L(xl - fe, ytop - FDROP))
        p.append(C(xl - fe + 26, ytop - FDROP - 34, xl - 6, ytop - FH + 30,
                   xl, ytop - FH))
    else:
        p.append(L(xl, ytop))
    if foot:
        p.append(L(xl, BRK))
        p.append(C(xl - 10, BRK - 34, xl - el + 16, SLAB + 10, xl - el, SLAB))
        p.append(L(xl - el, 0))
    else:
        p.append(L(xl, ybot))
    p.append("Z")
    return " ".join(p)


def ring(xl, xr, ybot, ytop, side=SIDE, thin=THIN, hf=0.56, hfi=0.60):
    """Panse fermee (o et derives) : contour externe CCW + contre CW."""
    xm = (xl + xr) / 2
    ym = (ybot + ytop) / 2
    rx, ry = (xr - xl) / 2, (ytop - ybot) / 2
    out = " ".join([
        M(xr, ym),
        C(xr, ym + hf * ry, xm + hf * rx, ytop, xm, ytop),
        C(xm - hf * rx, ytop, xl, ym + hf * ry, xl, ym),
        C(xl, ym - hf * ry, xm - hf * rx, ybot, xm, ybot),
        C(xm + hf * rx, ybot, xr, ym - hf * ry, xr, ym),
        "Z",
    ])
    ixl, ixr = xl + side, xr - side
    iyb, iyt = ybot + thin, ytop - thin
    irx, iry = (ixr - ixl) / 2, (iyt - iyb) / 2
    inn = " ".join([
        M(ixr, ym),
        C(ixr, ym - hfi * iry, xm + hfi * irx, iyb, xm, iyb),
        C(xm - hfi * irx, iyb, ixl, ym - hfi * iry, ixl, ym),
        C(ixl, ym + hfi * iry, xm - hfi * irx, iyt, xm, iyt),
        C(xm + hfi * irx, iyt, ixr, ym + hfi * iry, ixr, ym),
        "Z",
    ])
    return [out, inn]


def arch_right(xa, xl2, xr2, y_sh_out=None, y_sh_in=None, foot=True):
    """Epaule + fût droit du n/m/h : bande fermee qui plonge dans le
    fût gauche en x=xa (recouvrement) et porte le fût droit [xl2, xr2].
    """
    if y_sh_out is None:
        y_sh_out = XH + 8
    if y_sh_in is None:
        y_sh_in = XH + 8 - THIN
    dig = 22          # penetration dans le fût gauche
    ylo = 172         # bas de l'attache interne
    yhi = 330         # haut de l'attache externe
    xmi = (xa + xl2) / 2 + 30   # sommet interne du contre
    xmo = (xa + xr2) / 2 + 14   # sommet externe de l'epaule
    p = [M(xa - dig, ylo)]
    # courbe interne (contre) : monte, passe sous l'epaule, redescend
    p.append(C(xa + 60, ylo + 150, xmi - 120, y_sh_in, xmi, y_sh_in))
    p.append(C(xmi + 105, y_sh_in, xl2, XH - 160, xl2, 300))
    # flanc gauche du fût droit + pied
    if foot:
        p.append(L(xl2, BRK))
        p.append(C(xl2 - 10, BRK - 34, xl2 - EXT + 16, SLAB + 10,
                   xl2 - EXT, SLAB))
        p.append(L(xl2 - EXT, 0))
        p.append(L(xr2 + EXT, 0))
        p.append(L(xr2 + EXT, SLAB))
        p.append(C(xr2 + EXT - 16, SLAB + 10, xr2 + 10, BRK - 34, xr2, BRK))
    else:
        p.append(L(xl2, 0))
        p.append(L(xr2, 0))
    # flanc droit, remonte vers l'epaule externe
    p.append(L(xr2, 320))
    p.append(C(xr2, XH - 90, xmo + 120, y_sh_out, xmo, y_sh_out))
    p.append(C(xmo - 130, y_sh_out, xa + 40, XH - 70, xa - dig, yhi))
    p.append("Z")
    return " ".join(p)


def bowl_bottom(xl1, xr1, xa2, y_sp_out=None, y_sp_in=None):
    """Cuvette du u : bande fermee, miroir vertical de l'epaule du n.
    Porte le fût gauche [xl1, xr1] (sans pied) et plonge dans le fût
    droit en x=xa2."""
    if y_sp_out is None:
        y_sp_out = -8
    if y_sp_in is None:
        y_sp_in = -8 + THIN
    dig = 22
    yhi_in = XH - 172          # haut de l'attache interne (fût droit)
    ylo_out = XH - 330         # bas de l'attache externe
    xmi = (xa2 + xr1) / 2 - 30
    xmo = (xa2 + xl1) / 2 - 14
    p = [M(xa2 + dig, yhi_in)]
    p.append(C(xa2 - 60, yhi_in - 150, xmi + 120, y_sp_in, xmi, y_sp_in))
    p.append(C(xmi - 105, y_sp_in, xr1, 160, xr1, XH - 300))
    p.append(L(xr1, XH))
    p.append(L(xl1, XH))
    p.append(L(xl1, XH - 320))
    p.append(C(xl1, 90, xmo - 120, y_sp_out, xmo, y_sp_out))
    p.append(C(xmo + 130, y_sp_out, xa2 - 40, 70, xa2 + dig, XH - yhi_in - 0))
    p.append("Z")
    return " ".join(p)


def top_serif(xc, w, y=XH, ext=34):
    """Petit empattement horizontal en tete d'une diagonale (v w x y)."""
    xl, xr = xc - w / 2 - ext, xc + w / 2 + ext
    return " ".join([
        M(xl, y), L(xr, y), L(xr, y - SLAB),
        C(xr - 14, y - SLAB - 8, xc + w / 2 + 6, y - 74, xc + w / 2, y - 80),
        L(xc - w / 2, y - 80),
        C(xc - w / 2 - 6, y - 74, xl + 14, y - SLAB - 8, xl, y - SLAB),
        "Z",
    ])


def diag(x0, y0, x1, y1, w):
    """Parallelogramme (epaisseur horizontale w) de (x0,y0) a (x1,y1)."""
    h = w / 2
    return " ".join([
        M(x0 - h, y0), L(x0 + h, y0), L(x1 + h, y1), L(x1 - h, y1), "Z",
    ])


def rect(xl, yb, xr, yt):
    return " ".join([M(xl, yb), L(xr, yb), L(xr, yt), L(xl, yt), "Z"])


# ------------------------------------------------- aplatissement (skia)

import pathops


def _parse_d(d):
    """Parse notre sous-ensemble de SVG path (M/L/C/Z absolus)."""
    toks = d.replace(",", " ").split()
    i, cur, start = 0, (0, 0), (0, 0)
    p = pathops.Path()
    while i < len(toks):
        c = toks[i]
        if c == "M":
            cur = (float(toks[i + 1]), float(toks[i + 2]))
            start = cur
            p.moveTo(*cur)
            i += 3
        elif c == "L":
            cur = (float(toks[i + 1]), float(toks[i + 2]))
            p.lineTo(*cur)
            i += 3
        elif c == "C":
            x1, y1, x2, y2, x, y = (float(v) for v in toks[i + 1:i + 7])
            p.cubicTo(x1, y1, x2, y2, x, y)
            cur = (x, y)
            i += 7
        elif c == "Z":
            p.close()
            cur = start
            i += 1
        else:
            raise ValueError(f"token inconnu {c!r}")
    return p


def _to_d(p):
    out = []
    for verb, pts in p:
        if verb == pathops.PathVerb.MOVE:
            out.append(M(pts[0][0], pts[0][1]))
        elif verb == pathops.PathVerb.LINE:
            out.append(L(pts[0][0], pts[0][1]))
        elif verb == pathops.PathVerb.CUBIC:
            (x1, y1), (x2, y2), (x, y) = pts
            out.append(C(x1, y1, x2, y2, x, y))
        elif verb == pathops.PathVerb.QUAD:
            (x1, y1), (x, y) = pts
            # eleve la quadratique en cubique
            out.append(f"Q {fmt(x1)} {fmt(y1)} {fmt(x)} {fmt(y)}")
        elif verb == pathops.PathVerb.CLOSE:
            out.append("Z")
    return " ".join(out)


def flatten(solids, holes=()):
    """Union des contours pleins, moins les contres. Retourne un d unique."""
    acc = None
    for d in solids:
        c = _parse_d(d)
        acc = c if acc is None else pathops.op(acc, c, pathops.PathOp.UNION)
    for d in holes:
        c = _parse_d(d)
        acc = pathops.op(acc, c, pathops.PathOp.DIFFERENCE)
    return _to_d(acc)


# ---------------------------------------------------------------- glyphes

GLYPHS = {}


def glyph(name, advance, contours, holes=()):
    if contours:
        GLYPHS[name] = {"advance": advance,
                        "contours": [flatten(contours, holes)]}
    else:
        GLYPHS[name] = {"advance": advance, "contours": []}


def build():
    # ---- o : la ronde maitresse
    o_out, o_in = ring(26, 546, -OV, XH + OV)
    glyph("o", 572, [o_out], [o_in])

    # ---- b d p q : fût + panse (recouvrement, la panse epouse le fût)
    st_l, st_r = 30, 30 + STEM
    b_out, b_in = ring(st_l, 566, -OV, XH + OV)
    glyph("b", 596, [vstem(st_l, st_r, 0, ASC, foot=False, head="flag"),
                     b_out], [b_in])
    # d : miroir de b — panse a gauche, fût a droite avec pied
    d_st_l, d_st_r = 596 - 30 - STEM, 596 - 30
    d_out, d_in = ring(30, d_st_r, -OV, XH + OV)
    glyph("d", 596, [vstem(d_st_l, d_st_r, 0, ASC, foot=True, head="flag"),
                     d_out], [d_in])
    # p : un seul fût DESC->XH avec pied en bas
    p_stem = [
        M(st_l - EXT, DESC), L(st_r + EXT, DESC), L(st_r + EXT, DESC + SLAB),
        C(st_r + EXT - 16, DESC + SLAB + 10, st_r + 10, DESC + BRK - 34,
          st_r, DESC + BRK),
        L(st_r, XH),
        L(st_l - FEXT, XH - FDROP),
        C(st_l - FEXT + 26, XH - FDROP - 34, st_l - 6, XH - FH + 30,
          st_l, XH - FH),
        L(st_l, DESC + BRK),
        C(st_l - 10, DESC + BRK - 34, st_l - EXT + 16, DESC + SLAB + 10,
          st_l - EXT, DESC + SLAB),
        L(st_l - EXT, DESC), "Z",
    ]
    glyph("p", 596, [" ".join(p_stem), b_out], [b_in])
    # q : miroir de p (fût a droite, pas de drapeau, tete plate)
    q_stem = [
        M(d_st_l - EXT, DESC), L(d_st_r + EXT, DESC),
        L(d_st_r + EXT, DESC + SLAB),
        C(d_st_r + EXT - 16, DESC + SLAB + 10, d_st_r + 10, DESC + BRK - 34,
          d_st_r, DESC + BRK),
        L(d_st_r, XH), L(d_st_l, XH),
        L(d_st_l, DESC + BRK),
        C(d_st_l - 10, DESC + BRK - 34, d_st_l - EXT + 16, DESC + SLAB + 10,
          d_st_l - EXT, DESC + SLAB),
        L(d_st_l - EXT, DESC), "Z",
    ]
    glyph("q", 596, [" ".join(q_stem), d_out], [d_in])

    # ---- l i : fûts purs
    glyph("l", 278, [vstem(80, 80 + STEM, 0, ASC)])
    glyph("i", 278, [vstem(80, 80 + STEM, 0, XH),
                     circle(80 + STEM / 2, 634, 58)])
    # j : fût + crochet descendant + goutte ; point
    j_l, j_r = 92, 92 + STEM
    j_tail = [
        M(j_r, XH), L(j_r, -70),
        C(j_r, -170, 160, DESC + 6, 74, DESC + 6),
        C(30, DESC + 6, 6, DESC + 40, 0, DESC + 70),
        L(58, DESC + 96),
        C(80, DESC + 60, 120, DESC + 62, 136, DESC + 116),
        L(j_l, -60), L(j_l, XH - FH),
        C(j_l - 6, XH - FH + 30, j_l - FEXT + 26, XH - FDROP - 34,
          j_l - FEXT, XH - FDROP),
        L(j_r, XH), "Z",
    ]
    glyph("j", 292, [" ".join(j_tail), circle(j_l + STEM / 2, 634, 58),
                     circle(52, DESC + 62, BALL_R)])

    # ---- n m h u r
    n_l1, n_r1 = 30, 30 + STEM
    n_l2, n_r2 = 595 - 30 - STEM, 595 - 30
    glyph("n", 595, [vstem(n_l1, n_r1, 0, XH),
                     arch_right(n_r1, n_l2, n_r2)])
    glyph("h", 600, [vstem(n_l1, n_r1, 0, ASC),
                     arch_right(n_r1, 600 - 30 - STEM, 600 - 30)])
    m_l2 = 30 + STEM + 242
    m_l3 = m_l2 + STEM + 242
    glyph("m", 30 + STEM + 242 + STEM + 242 + STEM + 30 + 8,
          [vstem(30, 30 + STEM, 0, XH),
           arch_right(30 + STEM, m_l2, m_l2 + STEM),
           arch_right(m_l2 + STEM, m_l3, m_l3 + STEM)])
    glyph("u", 595, [" ".join([  # fût gauche : drapeau, pas de pied
                         M(n_l1, 140), L(n_l1, XH - FH),
                         C(n_l1 - 6, XH - FH + 30, n_l1 - FEXT + 26,
                           XH - FDROP - 34, n_l1 - FEXT, XH - FDROP),
                         L(n_r1, XH), L(n_r1, 140), "Z",
                     ]),
                     bowl_bottom(n_l1, n_r1, n_l2),
                     vstem(n_l2, n_r2, 0, XH)])
    # r : fût + bras avec goutte
    r_l, r_r = 30, 30 + STEM
    r_arm = [
        M(r_r - 12, 300),
        C(r_r + 30, 388, 246, XH + 6, 306, XH + 6),
        C(346, XH + 6, 372, 480, 388, 452),
        L(352, 396),
        C(332, 424, 300, 430, 268, 414),
        C(230, 395, 196, 356, r_r - 12, 236),
        "Z",
    ]
    glyph("r", 412, [vstem(r_l, r_r, 0, XH, foot_ext_l=56, foot_ext_r=56),
                     " ".join(r_arm), circle(344, 430, BALL_R)])

    # ---- e
    e_xl, e_xr, e_adv = 28, 548, 576
    xm = (e_xl + e_xr) / 2
    ym = (XH) / 2
    bar_b, bar_t = 252, 252 + BAR
    e_out = [
        M(e_xr - 26, 158),                      # pointe du menton
        C(e_xr - 84, 60, 380, -OV, xm - 14, -OV),
        C(150, -OV, e_xl, 92, e_xl, ym),
        C(e_xl, 388, 152, XH + OV, xm, XH + OV),
        C(422, XH + OV, e_xr, 396, e_xr, 288),
        L(e_xr, bar_b), L(e_xl + SIDE + 10, bar_b),   # dessous de barre
        C(e_xl + SIDE + 4, 190, 210, 42, xm + 10, 42),
        C(382, 42, 436, 84, e_xr - 128, 152),
        "Z",
    ]
    e_eye = [
        M(e_xl + SIDE, bar_t),
        L(e_xl + SIDE, 310),
        C(e_xl + SIDE + 8, 396, 232, XH + OV - THIN, xm, XH + OV - THIN),
        C(402, XH + OV - THIN, e_xr - SIDE + 6, 400, e_xr - SIDE + 2, 316),
        L(e_xr - SIDE + 2, bar_t),
        "Z",
    ]
    glyph("e", e_adv, [" ".join(e_out)], [" ".join(e_eye)])

    # ---- c : goutte en haut, menton cisele en bas
    c_xl, c_xr, c_adv = 28, 520, 540
    cm = (c_xl + c_xr) / 2 + 10
    c_out = [
        M(c_xr - 22, 112),                       # menton, coin externe
        C(c_xr - 86, 34, 372, -OV, cm - 20, -OV),
        C(150, -OV, c_xl, 90, c_xl, ym),
        C(c_xl, 392, 152, XH + OV, cm - 16, XH + OV),
        C(360, XH + OV, 432, 452, 466, 402),
        L(414, 342),
        C(388, 380, 340, 400, 292, 388),
        C(214, 368, c_xl + SIDE, 320, c_xl + SIDE, ym),
        C(c_xl + SIDE, 128, 232, 34, cm - 6, 34),
        C(330, 34, 396, 82, 438, 148),
        "Z",
    ]
    glyph("c", c_adv, [" ".join(c_out), circle(432, 400, BALL_R)])

    # ---- a : deux etages
    a_adv = 556
    a_sl, a_sr = 386, 386 + STEM   # fût droit
    a_stem = vstem(a_sl, a_sr, 0, 430, foot=True, head="none",
                   foot_ext_l=48, foot_ext_r=48)
    a_hook = [   # crochet superieur, du fût vers la goutte a gauche
        M(a_sr, 430),
        C(a_sr - 18, 474, 330, XH + OV, 238, XH + OV),
        C(160, XH + OV, 104, 470, 78, 428),
        L(126, 380),
        C(148, 412, 188, 428, 232, 424),
        C(300, 418, 356, 380, a_sl, 310),
        L(a_sl, 360), "Z",
    ]
    a_bowl = [   # panse inferieure, s'attache au fût en haut et en bas
        M(a_sl + 20, 296),
        C(240, 278, 36, 228, 36, 120),
        C(36, 22, 150, -12, 264, -12),
        C(330, -12, 376, 4, a_sl + 20, 36),
        L(a_sl + 20, 116),
        C(360, 84, 310, 64, 258, 64),
        C(196, 64, 148, 92, 148, 138),
        C(148, 196, 260, 224, a_sl + 20, 248),
        "Z",
    ]
    glyph("a", a_adv, [a_stem, " ".join(a_hook), " ".join(a_bowl),
                       circle(96, 420, 50)])

    # ---- g : un seul etage, panse + queue ouverte
    g_adv = 585
    g_sl, g_sr = g_adv - 30 - STEM, g_adv - 30
    g_stem_tail = [
        M(g_sr, XH),
        L(g_sr, -68),
        C(g_sr, -168, 400, DESC - 0, 268, DESC - 0),
        C(178, DESC, 116, DESC + 28, 84, DESC + 62),
        L(134, DESC + 108),
        C(162, DESC + 74, 210, DESC + 62, 258, DESC + 66),
        C(340, DESC + 74, g_sl, DESC + 130, g_sl, -50),
        L(g_sl, XH - FH),
        C(g_sl - 6, XH - FH + 30, g_sl - FEXT + 26, XH - FDROP - 34,
          g_sl - FEXT, XH - FDROP),
        L(g_sr, XH),
        "Z",
    ]
    g_out, g_in = ring(28, g_sr, -OV, XH + OV)
    glyph("g", g_adv, [" ".join(g_stem_tail),
                       circle(112, DESC + 72, BALL_R + 2),
                       g_out], [g_in])

    # ---- k : fût ascendant + bras fin + jambe grasse (echo du K)
    k_adv = 556
    k_l, k_r = 30, 30 + STEM
    k_arm = diag(k_r - 8, 280, 448, XH - 34, 70)
    k_arm_serif = top_serif(448, 70, y=XH, ext=30)
    k_leg = [
        M(k_r - 10, 320), L(230, 320),
        C(300, 220, 380, 120, 470, 26),
        L(500, 0), L(346, 0),
        C(270, 110, 200, 214, k_r - 10, 250),
        "Z",
    ]
    k_leg_foot = " ".join([
        M(320, 0), L(522, 0), L(522, SLAB - 4),
        C(510, 30, 498, 40, 492, 46), L(374, 46),
        C(364, 40, 334, 30, 320, SLAB - 4), "Z",
    ])
    glyph("k", k_adv, [vstem(k_l, k_r, 0, ASC),
                       k_arm, k_arm_serif, " ".join(k_leg), k_leg_foot])

    # ---- s
    s_adv = 470
    s_path = [
        M(402, 414),
        C(360, 472, 298, XH + OV, 228, XH + OV),
        C(126, XH + OV, 56, 452, 56, 372),
        C(56, 290, 132, 256, 234, 232),
        C(316, 213, 352, 188, 352, 138),
        C(352, 84, 306, 56, 240, 56),
        C(170, 56, 120, 86, 88, 130),
        L(40, 80),
        C(82, 20, 152, -OV, 238, -OV),
        C(350, -OV, 414, 48, 414, 128),
        C(414, 218, 338, 252, 240, 275),
        C(158, 294, 118, 318, 118, 366),
        C(118, 410, 156, 438, 220, 438),
        C(278, 438, 320, 414, 352, 366),
        "Z",
    ]
    glyph("s", s_adv, [" ".join(s_path)])

    # ---- t
    t_adv = 372
    t_l, t_r = 104, 104 + STEM - 6
    t_path = [
        M(t_l, 575), L(t_r, 620),
        L(t_r, 130),
        C(t_r, 88, 248, 64, 286, 64),
        L(322, 128),
        C(330, 70, 306, 6, 240, -10),
        C(160, -12, t_l, 26, t_l, 96),
        "Z",
    ]
    t_bar = rect(6, XH - BAR, 344, XH)
    glyph("t", t_adv, [" ".join(t_path), t_bar])

    # ---- f
    f_adv = 348
    f_l, f_r = 88, 88 + STEM - 6
    f_path = [
        M(f_l - EXT, 0), L(f_r + EXT, 0), L(f_r + EXT, SLAB),
        C(f_r + EXT - 16, SLAB + 10, f_r + 10, BRK - 34, f_r, BRK),
        L(f_r, 560),
        C(f_r, 648, 260, ASC + 6, 322, ASC + 6),
        C(354, ASC + 6, 376, 700, 392, 676),
        L(356, 622),
        C(344, 642, 322, 648, 300, 636),
        C(268, 618, f_l, 596, f_l, 520),
        L(f_l, BRK),
        C(f_l - 10, BRK - 34, f_l - EXT + 16, SLAB + 10, f_l - EXT, SLAB),
        L(f_l - EXT, 0), "Z",
    ]
    f_bar = rect(-4, XH - BAR, 330, XH)
    glyph("f", f_adv, [" ".join(f_path), f_bar, circle(352, 652, BALL_R)])

    # ---- v w x y z
    v_adv = 536
    v_thick_top, v_thin_top = 132, 452
    glyph("v", v_adv, [
        diag(132, XH, 262, 16, 116),
        diag(452, XH, 250, 30, 52),
        top_serif(132, 116, ext=30),
        top_serif(452, 52, ext=34),
    ])
    w_adv = 810
    glyph("w", w_adv, [
        diag(118, XH, 218, 16, 110),
        diag(398, XH, 208, 30, 50),
        diag(398, XH, 560, 16, 0.1),   # remplace ci-dessous
    ])
    glyph("w", w_adv, [
        diag(116, XH, 214, 16, 108),
        diag(392, XH - 10, 206, 30, 50),
        diag(392, XH - 10, 556, 16, 104),
        diag(700, XH, 548, 30, 50),
        top_serif(116, 108, ext=28),
        top_serif(700, 50, ext=32),
    ])
    x_adv = 528
    glyph("x", x_adv, [
        diag(130, XH, 400, 0, 112),
        diag(398, XH, 128, 0, 52),
        top_serif(130, 112, ext=28),
        top_serif(398, 52, ext=32),
        top_serif(400, 112, y=SLAB + 54, ext=28).replace("Z", "Z"),
    ])
    # empattements bas du x : reutilise top_serif retourne — simple bandeau
    x_foot = [
        " ".join([M(400 - 56 - 28, 0), L(400 + 56 + 28, 0),
                  L(400 + 56 + 28, SLAB), C(400 + 56 + 14, SLAB + 8,
                  400 + 62, 74, 400 + 56, 80), L(400 - 56, 80),
                  C(400 - 62, 74, 400 - 56 - 14, SLAB + 8,
                    400 - 56 - 28, SLAB), "Z"]),
        " ".join([M(128 - 26 - 30, 0), L(128 + 26 + 30, 0),
                  L(128 + 26 + 30, SLAB), C(128 + 26 + 16, SLAB + 8,
                  128 + 32, 74, 128 + 26, 80), L(128 - 26, 80),
                  C(128 - 32, 74, 128 - 26 - 16, SLAB + 8,
                    128 - 26 - 30, SLAB), "Z"]),
    ]
    glyph("x", x_adv, [
        diag(130, XH, 400, 0, 112),
        diag(398, XH, 128, 0, 52),
        top_serif(130, 112, ext=28),
        top_serif(398, 52, ext=32),
        *x_foot,
    ])
    y_adv = 530
    y_tail_path = " ".join([
        M(430, XH), L(506, XH), L(310, -58),
        C(272, -152, 218, DESC + 8, 128, DESC + 8),
        C(82, DESC + 8, 40, DESC + 30, 12, DESC + 60),
        L(58, DESC + 108),
        C(86, DESC + 78, 124, DESC + 66, 170, DESC + 76),
        C(228, DESC + 96, 252, DESC + 170, 246, -18),
        "Z",
    ])
    glyph("y", y_adv, [
        diag(130, XH, 268, 6, 114),
        top_serif(130, 114, ext=28),
        y_tail_path,
        top_serif(468, 76, ext=28),
        circle(116, DESC + 66, BALL_R - 4),
    ])
    z_adv = 486
    z_diag = " ".join([M(300, XH - BAR), L(438, XH - BAR), L(178, BAR),
                       L(40, BAR), "Z"])
    glyph("z", z_adv, [
        rect(44, XH - BAR, 438, XH),
        z_diag,
        rect(40, 0, 446, BAR),
    ])

    # ---- ponctuation minimale pour le specimen
    glyph("period", 252, [circle(126, 62, 66)])
    glyph("space", 250, [])


build()


# --------------------------------------------------------------- specimen

def glyph_svg(name, pen_x, base_y, scale, fill=INK):
    g = GLYPHS[name]
    if not g["contours"]:
        return ""
    # tous les contours d'un glyphe dans un seul path : les contres
    # (contours en sens inverse) deviennent des vides via fill-rule nonzero
    d = " ".join(g["contours"])
    return (f'<g transform="translate({fmt(pen_x)} {fmt(base_y)}) '
            f'scale({scale} {-scale})" fill="{fill}">'
            f'<path d="{d}" fill-rule="nonzero"/></g>')


def line_svg(text, x, base_y, scale, tracking=0, fill=INK):
    out, pen = [], x
    for ch in text:
        name = {" ": "space", ".": "period"}.get(ch, ch)
        if name not in GLYPHS:
            pen += 250 * scale
            continue
        out.append(glyph_svg(name, pen, base_y, scale, fill))
        pen += (GLYPHS[name]["advance"] + tracking) * scale
    return "".join(out), pen


def specimen(path_svg):
    W = 2480
    rows = []
    y = 150

    def title(t, yy, size=34):
        return (f'<text x="120" y="{yy}" font-family="sans-serif" '
                f'font-size="{size}" letter-spacing="6" fill="#8A8377">{t}</text>')

    parts = [f'<rect width="{W}" height="3300" fill="{PAPER}"/>']

    parts.append(title("ALPHABET — PROPOSITION MINUSCULES", y))
    y += 90
    s = 0.30
    row1, _ = line_svg("abcdefghijklm", 120, y + 300 * s + 220 * s, s, 40)
    parts.append(row1)
    y += 420
    row2, _ = line_svg("nopqrstuvwxyz", 120, y + 300 * s + 220 * s, s, 40)
    parts.append(row2)
    y += 520

    parts.append(title("LOGOTYPE", y))
    y += 120
    s2 = 0.52
    logo, _ = line_svg("k.lub", 120, y + 725 * s2 * 0.86, s2, 10)
    parts.append(logo)
    y += 620

    parts.append(title("PANGRAMME", y))
    y += 70
    s3 = 0.145
    for seg in ["portez ce vieux whisky", "au juge blond qui fume"]:
        yy = y + (725 + 235) * s3
        seg_svg, _ = line_svg(seg, 120, yy, s3, 16)
        parts.append(seg_svg)
        y += 210

    parts.append(title("MOTS", y + 40))
    y += 150
    s4 = 0.22
    words, _ = line_svg("bulle bijou klub quartz", 120, y + 725 * s4, s4, 14)
    parts.append(words)
    y += 320

    H = y + 100
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" '
           f'height="{H}" viewBox="0 0 {W} {H}">' + "".join(parts) + "</svg>")
    svg = svg.replace(f'height="3300"', f'height="{H}"')
    with open(path_svg, "w") as f:
        f.write(svg)
    return path_svg


if __name__ == "__main__":
    specimen("/home/user/claude-control/typo/specimen-minuscules.svg")
    print("ok", len(GLYPHS), "glyphes")
