# Claude Control

> Manage your Claude Desktop MCPs and Skills with a beautiful local web interface.

A small local app to take control of your Claude Desktop setup — toggle MCPs, manage Skills, restart Claude with one click, and import new ones from JSON, files or Git repos.

Built by [Sekoia](https://sekoia.ca) · macOS only · 100% local · Zero tracking.

![Screenshot](docs/screenshot.png)

## Why

If you use Claude Desktop with multiple MCP servers, you've probably hit:

- Claude Desktop hangs when too many MCPs are loaded simultaneously
- Editing `claude_desktop_config.json` by hand is tedious
- Skills sit in `~/.claude/skills/` with no easy way to enable/disable them
- Importing a new MCP means manual JSON merging

Claude Control fixes all that with a clean web UI that runs locally on your Mac.

## Features

- **Toggle MCPs** with one click (no JSON editing)
- **Toggle Skills** the same way (moves between `~/.claude/skills/` and `~/.claude/skills-disabled/`)
- **See running status** of each MCP server in real time
- **Restart Claude Desktop** with one button
- **Import MCPs** from JSON, local file, or Git repo
- **Import Skills** from local folder, Git repo, or pasted markdown
- **Auto-update** from GitHub when a new release is published
- **Automatic backups** of your config before any change

## Install

```bash
curl -fsSL https://claude.sekoia.ca/install.sh | bash
```

Or manually:

```bash
git clone https://github.com/fabultra/claude-control.git ~/dev/claude-control
bash ~/dev/claude-control/scripts/install.sh
```

After install, open Finder → Applications → double-click **Claude Control.app**
(or drag it to your Dock for one-click access).

## How it works

- **Local web server** in Python (stdlib only, no dependencies)
- Runs on `http://localhost:8765`
- Reads/writes `~/Library/Application Support/Claude/claude_desktop_config.json`
- Reads/writes `~/.claude/skills/` (active) and `~/.claude/skills-disabled/` (inactive)
- Backups stored in `~/.claude/backups/claude-control/`
- Auto-update polls GitHub Releases API hourly

## Updating

When a new release is published on GitHub, the app shows an "Update available" badge.
Click it — the app pulls the new `app.py` and restarts itself in place via
`os.execv`. No quit-and-relaunch needed.

## If the .app icon stops launching

Two macOS pitfalls can leave the bundle in a state where double-clicking the
icon shows "L'application Claude Control.app n'est plus ouverte" with no
visible activity:

1. The bundle ended up on iCloud-synced `~/Desktop` and got partially evicted.
2. macOS LaunchServices cached a stale entry pointing nowhere.

To recover without opening Terminal:

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

`repair.sh` reinstalls the bundle at `~/Applications/Claude Control.app`
(local, not iCloud), with a robust launcher that tries multiple `python3`
locations and logs to `~/Library/Logs/claude-control-launch.log`.

## Manual workflows

For users who prefer the command line, see [`docs/cli.md`](docs/cli.md).

## Development

The whole app is in [`src/app.py`](src/app.py) — a single Python file using only stdlib. Edit, test, push.

```bash
# Run locally during development
python3 src/app.py

# Generate the icon (requires uv)
bash scripts/build-icon.sh

# Build the .app bundle
bash scripts/build-app.sh
```

## Stack

- **Python 3** (stdlib only — no `pip install` needed)
- **HTML + Tailwind CDN** for the UI
- **Git** for updates
- **Vercel** hosts the landing page at [claude.sekoia.ca](https://claude.sekoia.ca)

## License

MIT — see [LICENSE](LICENSE).

Made with care in Montréal.
