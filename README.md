# Claude Control

> Manage your Claude Desktop MCPs, Skills, and Claude Code plugins from a small local web app — toggle, search, diagnose, import. One click, one browser tab, zero terminal.

Built by [Sekoia](https://sekoia.ca) · macOS only · 100% local · Zero tracking.

## Why

If you use Claude Desktop with multiple MCP servers, you've probably hit:

- Claude Desktop hangs when too many MCPs are loaded simultaneously
- Editing `claude_desktop_config.json` by hand is tedious
- Skills sit in `~/.claude/skills/` with no easy way to enable/disable them
- An MCP says "not running" and you have to dig through `~/Library/Logs/Claude/` to find out why
- Plugin caches accumulate stale version directories you don't even know are there

Claude Control fixes all that with a clean web UI that runs locally on your Mac.

## Features

**MCPs**
- Toggle any MCP on/off without touching JSON
- See live running status (green pulse = process alive)
- **Click "pourquoi ?"** on a non-running MCP → modal with the last error from Claude Desktop logs + a one-line suggested fix (auth, missing binary, missing dependency, port conflict, network, rate limit, ...)
- **Presets**: save the current selection as `Klide`, `Audit client`, `Boulevard Commun` etc. Switch contexts in one click

**Skills**
- Toggle on/off (moves between `~/.claude/skills/` and `~/.claude/skills-disabled/`)
- **Group by category** via a `category:` field in the skill's `SKILL.md` frontmatter, with collapsible sections
- **Live search** by name and `description` — categories with no match are hidden, counters update

**Claude Code plugins**
- Read-only listing of plugins from `~/.claude/plugins/installed_plugins.json`
- Per plugin: version, marketplace, count of skills/MCPs/commands/hooks, click to expand the full content listing
- Toggle individually (writes `enabledPlugins` in `~/.claude/settings.json`, with backup)
- **Orphan version detection**: a copper "orphan: vX.Y.Z" badge appears on plugins whose cache contains stale version directories. One click + confirm → the dir is zipped to `~/.claude/backups/claude-control/orphan-plugins/` then deleted

**Imports**
- MCPs from JSON, file path, Git repo, or local ZIP
- Skills from folder, Git repo, pasted markdown, or local ZIP

**Quality of life**
- Restart Claude Desktop with one button
- Restart Claude Control itself in place via `os.execv` (no quit-and-relaunch after an update)
- Auto-update from GitHub Releases (in-app "Update" badge → click → app self-restarts on the new code)
- Automatic timestamped backups of `claude_desktop_config.json` and `settings.json` before any change
- Crash and launcher logs at `~/Library/Logs/claude-control*.log`

## Prerequisites

- macOS 10.13+
- Python 3 (preinstalled on macOS 10.15+ via the Apple stub, or `brew install python3`, or python.org)
- git (`xcode-select --install` if missing)

## Install

```bash
curl -fsSL https://claude.sekoia.ca/install.sh | bash
```

Or manually:

```bash
git clone https://github.com/fabultra/claude-control.git ~/dev/claude-control
bash ~/dev/claude-control/scripts/install.sh
```

The installer:
- clones the repo to `~/dev/claude-control`
- copies `app.py` to `~/Applications/claude-control/`
- builds a fresh `.app` bundle at **`~/Applications/Claude Control.app`** (always local — never on iCloud-synced `~/Desktop`)
- writes a launcher that tries `python3` at `/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin`, then `$PATH`

After install: Finder → Applications → double-click **Claude Control.app** (or drag it to your Dock for one-click access).

## How it works

- Single-file Python 3 server (stdlib only — no `pip install`, no dependencies)
- Listens on `http://localhost:8765`
- Reads/writes `~/Library/Application Support/Claude/claude_desktop_config.json`
- Reads/writes `~/.claude/skills/` and `~/.claude/skills-disabled/`
- Reads `~/.claude/plugins/installed_plugins.json` and writes `enabledPlugins` in `~/.claude/settings.json`
- Backups in `~/.claude/backups/claude-control/`
- Auto-update polls GitHub Releases hourly

## Updating

When a new release is published, the app shows an "Update available" badge.
Click it — the app pulls `app.py`, then restarts itself in place via `os.execv`.
No quit and relaunch needed.

## If the .app icon stops launching

Two macOS pitfalls can leave the bundle in a state where double-clicking the
icon shows "L'application Claude Control.app n'est plus ouverte" with no
visible activity:

1. The bundle ended up on iCloud-synced `~/Desktop` (older installer) and got partially evicted.
2. macOS LaunchServices cached a stale entry pointing nowhere.

To recover **without opening Terminal**:

1. Open **Script Editor** (Spotlight → "Script Editor")
2. Paste this and click ▶ Run:
   ```applescript
   try
       do shell script "curl -fsSL https://raw.githubusercontent.com/fabultra/claude-control/main/scripts/repair.sh | bash"
       display dialog "Done. Double-click Claude Control.app in ~/Applications" buttons {"OK"} default button 1
   on error errMsg
       display dialog ("Error: " & errMsg) buttons {"OK"} with icon stop
   end try
   ```

`scripts/repair.sh` is idempotent: it pulls the latest code, kills any
running instance, removes the iCloud-tainted bundle from `~/Desktop` if
present, rebuilds a fresh bundle in `~/Applications`, removes the
quarantine attribute, and refreshes LaunchServices.

## Development

The whole app is in [`src/app.py`](src/app.py) — a single Python file using only stdlib.

```bash
# Run locally during development
python3 src/app.py

# Generate the icon (requires uv)
bash scripts/build-icon.sh

# Reinstall / repair the .app bundle
bash scripts/repair.sh
```

A GitHub Action publishes a release automatically when `version.txt` lands on `main`.

## Stack

- **Python 3** (stdlib only — no dependencies)
- **HTML + Tailwind CDN** for the UI
- **Git** for updates
- **Vercel** hosts the landing page at [claude.sekoia.ca](https://claude.sekoia.ca)

## License

MIT — see [LICENSE](LICENSE).

Made with care in Montréal.
