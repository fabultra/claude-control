#!/bin/bash
# Claude Control - installer
# Usage: curl -fsSL https://claude.sekoia.ca/install.sh | bash
#    or: bash ~/dev/claude-control/scripts/install.sh
#
# Clones the repo, generates the icon if uv is available, then delegates
# the .app bundle build to scripts/repair.sh — which is also the canonical
# recovery path (see README), so installs and repairs share one launcher.

set -e

REPO_URL="${CLAUDE_CONTROL_REPO:-https://github.com/fabultra/claude-control.git}"
TARGET_DIR="$HOME/dev/claude-control"

cyan() { printf "\033[36m%s\033[0m\n" "$1"; }
green() { printf "\033[32m%s\033[0m\n" "$1"; }
red() { printf "\033[31m%s\033[0m\n" "$1"; }

cyan ""
cyan "  Claude Control - installation"
cyan "  ============================="
cyan ""

command -v git >/dev/null || { red "git non installe. Installe avec: xcode-select --install"; exit 1; }
command -v python3 >/dev/null || { red "python3 non installe."; exit 1; }
command -v uv >/dev/null || cyan "  Note: uv recommande pour la generation d'icone (sinon ignore)."

# Nettoyage d'un .app existant cote Bureau (souvent synchronise iCloud, source
# des problemes 'L'application n'est plus ouverte').
rm -rf "$HOME/Desktop/Claude Control.app"
rm -f "$HOME/Desktop/Claude Control.command"

if [ -d "$TARGET_DIR/.git" ]; then
    cyan "  Mise a jour du repo existant..."
    git -C "$TARGET_DIR" pull --ff-only
else
    cyan "  Clonage du repo..."
    rm -rf "$TARGET_DIR"
    mkdir -p "$(dirname "$TARGET_DIR")"
    git clone --depth 1 "$REPO_URL" "$TARGET_DIR"
fi

# Genere l'icone si possible (avant le build du bundle pour que repair.sh la copie)
if command -v uv >/dev/null && [ -f "$TARGET_DIR/scripts/build-icon.sh" ]; then
    cyan "  Generation de l'icone..."
    bash "$TARGET_DIR/scripts/build-icon.sh" || cyan "  (icone optionnelle, on continue sans)"
fi

# Construction du .app dans ~/Applications via le script de reparation partage
cyan "  Construction de Claude Control.app dans ~/Applications/..."
CLAUDE_CONTROL_REPO="$REPO_URL" bash "$TARGET_DIR/scripts/repair.sh"

green ""
green "  Installation terminee"
green ""
cyan "  -> Finder > Applications > double-clique 'Claude Control.app'"
cyan "  -> (ou glisse-le dans le Dock pour acces rapide)"
cyan ""
