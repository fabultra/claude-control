#!/bin/bash
# Claude Control - one-shot repair script.
#
# Use case: the .app icon is broken (most often because the bundle ended up on
# iCloud-synced ~/Desktop and macOS LaunchServices is confused), or app.py /
# version.txt got out of sync with each other.
#
# Invoke from AppleScript:
#   do shell script "curl -fsSL https://raw.githubusercontent.com/fabultra/claude-control/main/scripts/repair.sh | bash"
# or directly from a terminal:
#   curl -fsSL https://raw.githubusercontent.com/fabultra/claude-control/main/scripts/repair.sh | bash
#
# What it does:
#   - pulls the latest src/app.py and version.txt into ~/dev/claude-control
#   - copies app.py to ~/Applications/claude-control/app.py
#   - kills any running claude-control python so the new .app can bind 8765
#   - removes the iCloud-tainted .app from ~/Desktop
#   - builds a fresh, robust .app in ~/Applications (NOT iCloud)
#   - removes quarantine and refreshes LaunchServices
#
# Idempotent.

set -e

REPO_URL="${CLAUDE_CONTROL_REPO:-https://github.com/fabultra/claude-control.git}"
REPO_SLUG="${CLAUDE_CONTROL_SLUG:-fabultra/claude-control}"
TARGET_DIR="$HOME/dev/claude-control"
APP_DIR="$HOME/Applications/claude-control"
BUNDLE="$HOME/Applications/Claude Control.app"
OLD_BUNDLE="$HOME/Desktop/Claude Control.app"

echo "[claude-control-repair] start"

if [ -d "$TARGET_DIR/.git" ]; then
    echo "[repair] updating $TARGET_DIR"
    git -C "$TARGET_DIR" fetch origin
    git -C "$TARGET_DIR" reset --hard origin/main
else
    echo "[repair] cloning into $TARGET_DIR"
    rm -rf "$TARGET_DIR"
    mkdir -p "$(dirname "$TARGET_DIR")"
    git clone --depth 1 "$REPO_URL" "$TARGET_DIR"
fi

echo "$REPO_SLUG" > "$TARGET_DIR/.github-repo"

mkdir -p "$APP_DIR"
cp "$TARGET_DIR/src/app.py" "$APP_DIR/app.py"
chmod +x "$APP_DIR/app.py"

# Stop any old python instance so the new .app can bind 8765
pkill -f "python.*claude-control/app.py" 2>/dev/null || true
sleep 0.5

# Out with the old (Desktop / iCloud / ~/Applications) bundles
rm -rf "$OLD_BUNDLE" "$BUNDLE"

mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"

VERSION="$(tr -d '[:space:]' < "$TARGET_DIR/version.txt" 2>/dev/null || echo "1.0.0")"

cat > "$BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Claude Control</string>
    <key>CFBundleDisplayName</key><string>Claude Control</string>
    <key>CFBundleIdentifier</key><string>ca.sekoia.claudecontrol</string>
    <key>CFBundleVersion</key><string>${VERSION}</string>
    <key>CFBundleShortVersionString</key><string>${VERSION}</string>
    <key>CFBundleExecutable</key><string>launch</string>
    <key>CFBundleIconFile</key><string>icon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>LSMinimumSystemVersion</key><string>10.13</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

cat > "$BUNDLE/Contents/MacOS/launch" <<'LAUNCH'
#!/bin/bash
LOG="$HOME/Library/Logs/claude-control-launch.log"
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
echo "[$(date)] launching"
for PY in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [ -x "$PY" ]; then
        echo "using $PY"
        exec "$PY" "$HOME/Applications/claude-control/app.py"
    fi
done
PY="$(command -v python3 || true)"
if [ -n "$PY" ] && [ -x "$PY" ]; then
    echo "using $PY (from PATH)"
    exec "$PY" "$HOME/Applications/claude-control/app.py"
fi
echo "FATAL: no python3 found"
/usr/bin/osascript -e 'display dialog "python3 introuvable. Installe Python via python.org ou Homebrew." buttons {"OK"} with icon stop with title "Claude Control"' || true
LAUNCH

chmod +x "$BUNDLE/Contents/MacOS/launch"

if [ -f "$TARGET_DIR/scripts/icon.icns" ]; then
    cp "$TARGET_DIR/scripts/icon.icns" "$BUNDLE/Contents/Resources/icon.icns"
fi

xattr -dr com.apple.quarantine "$BUNDLE" 2>/dev/null || true
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$BUNDLE" 2>/dev/null || true

echo "[claude-control-repair] done — version $VERSION installed at $BUNDLE"
