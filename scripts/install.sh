#!/bin/bash
# Claude Control - installer
# Usage: curl -fsSL https://claude.sekoia.ca/install.sh | bash
#    or: bash ~/dev/claude-control/scripts/install.sh

set -e

REPO_URL="${CLAUDE_CONTROL_REPO:-https://github.com/fabultra/claude-control.git}"
TARGET_DIR="$HOME/dev/claude-control"
APP_DIR="$HOME/Applications/claude-control"
DESKTOP_APP="$HOME/Desktop/Claude Control.app"

cyan() { printf "\033[36m%s\033[0m\n" "$1"; }
green() { printf "\033[32m%s\033[0m\n" "$1"; }
red() { printf "\033[31m%s\033[0m\n" "$1"; }

cyan ""
cyan "  Claude Control - installation"
cyan "  ============================="
cyan ""

# Verifs
command -v git >/dev/null || { red "git non installe. Installe avec: xcode-select --install"; exit 1; }
command -v python3 >/dev/null || { red "python3 non installe."; exit 1; }
command -v uv >/dev/null || cyan "  Note: uv recommande pour la generation d'icone (sinon ignore)."

# Clone ou pull
if [ -d "$TARGET_DIR/.git" ]; then
    cyan "  Mise a jour du repo existant..."
    git -C "$TARGET_DIR" pull --ff-only
else
    cyan "  Clonage du repo..."
    rm -rf "$TARGET_DIR"
    git clone --depth 1 "$REPO_URL" "$TARGET_DIR"
fi

# Sauvegarde le nom du repo pour l'auto-update
mkdir -p "$TARGET_DIR"
echo "$REPO_URL" | sed -E 's|.*github\.com[:/]([^/]+/[^/.]+).*|\1|' > "$TARGET_DIR/.github-repo"

# Copie app.py vers son emplacement d'execution
mkdir -p "$APP_DIR"
cp "$TARGET_DIR/src/app.py" "$APP_DIR/app.py"
chmod +x "$APP_DIR/app.py"

# Genere l'icone si possible
if command -v uv >/dev/null && [ -f "$TARGET_DIR/scripts/build-icon.sh" ]; then
    cyan "  Generation de l'icone..."
    bash "$TARGET_DIR/scripts/build-icon.sh" || cyan "  (icone optionnelle, on continue sans)"
fi

# Build la .app
cyan "  Construction de Claude Control.app..."
rm -rf "$DESKTOP_APP"
mkdir -p "$DESKTOP_APP/Contents/MacOS" "$DESKTOP_APP/Contents/Resources"

# Si une icone existe, copie-la
if [ -f "$TARGET_DIR/scripts/icon.icns" ]; then
    cp "$TARGET_DIR/scripts/icon.icns" "$DESKTOP_APP/Contents/Resources/icon.icns"
fi

cat > "$DESKTOP_APP/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Claude Control</string>
    <key>CFBundleDisplayName</key><string>Claude Control</string>
    <key>CFBundleIdentifier</key><string>ca.sekoia.claudecontrol</string>
    <key>CFBundleVersion</key><string>1.0.0</string>
    <key>CFBundleShortVersionString</key><string>1.0.0</string>
    <key>CFBundleExecutable</key><string>launch</string>
    <key>CFBundleIconFile</key><string>icon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>LSMinimumSystemVersion</key><string>10.13</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

cat > "$DESKTOP_APP/Contents/MacOS/launch" << 'LAUNCH'
#!/bin/bash
exec /usr/bin/python3 "$HOME/Applications/claude-control/app.py"
LAUNCH

chmod +x "$DESKTOP_APP/Contents/MacOS/launch"
touch "$DESKTOP_APP"
xattr -dr com.apple.quarantine "$DESKTOP_APP" 2>/dev/null || true

# Cleanup ancien .command
rm -f "$HOME/Desktop/Claude Control.command"

green ""
green "  ✓ Installation terminee"
green ""
cyan "  → Double-clique sur 'Claude Control.app' sur ton Desktop"
cyan ""
