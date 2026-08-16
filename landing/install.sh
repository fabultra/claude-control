#!/bin/bash
# Claude Control - one-line installer (thin wrapper).
#
# This file is what claude.sekoia.ca/install.sh serves. It delegates to the
# canonical installer in the repo so it can never drift from scripts/install.sh.
#
# Source of truth: https://github.com/fabultra/claude-control/blob/main/scripts/install.sh
#
# v1.15.0 - l'ancien `exec /bin/bash <(curl ...)` STREAMAIT l'installeur
# dans bash : une coupure reseau en plein transfert executait un script
# tronque (un prefixe de commandes arbitraire). Desormais on telecharge
# dans un fichier temporaire, on verifie que curl a termine SANS erreur et
# que le marqueur de fin du script est present, et seulement ensuite on
# execute. Ce wrapper lui-meme est servi en `curl | bash` : son corps vit
# dans main() appele en derniere ligne, donc tronque il est inerte.
set -euo pipefail

main() {
    # Pas de `local` ici : le trap EXIT se declenche apres la sortie de
    # main(), ou une locale n'existe plus (erreur sous set -u).
    src="https://raw.githubusercontent.com/fabultra/claude-control/main/scripts/install.sh"
    tmp="$(mktemp "${TMPDIR:-/tmp}/claude-control-install.XXXXXX")"
    trap 'rm -f "${tmp:-}"' EXIT
    if ! curl -fsSL --retry 3 "$src" -o "$tmp"; then
        echo "  Echec du telechargement de l'installeur ($src)." >&2
        echo "  Verifie ta connexion puis relance la commande." >&2
        exit 1
    fi
    if ! grep -q "claude-control-install-end" "$tmp"; then
        echo "  Installeur incomplet ou corrompu (marqueur de fin absent) - abandon." >&2
        exit 1
    fi
    bash "$tmp"
}

main "$@"
