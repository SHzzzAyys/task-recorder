# Task Recorder

Terminal task tracker for managing parallel Claude Code / Codex sessions.

Auto-detects running Claude Code sessions and displays a live dashboard — no manual input needed.

## Install

```bash
git clone https://github.com/SHzzzAyys/task-recorder.git
cd task-recorder
pip install -r requirements.txt
```

Optional: add a PowerShell alias for quick access:

```powershell
# Add to $PROFILE
function task { python D:\Projects\task-recorder\task_recorder.py @args }
```

## Usage

### Live Dashboard

```bash
task watch                  # real-time monitor, auto-detect Claude sessions
task watch --minutes 120    # scan sessions active within 120 min
task watch --refresh 3      # refresh every 3 seconds
```

Auto-detects sessions from `~/.claude/projects/` and shows:

- **LIVE** (green) — active within 2 min
- **IDLE** (yellow) — 2–10 min inactive
- **---** (gray) — 10+ min inactive

### One-time Scan

```bash
task scan                   # print current sessions and exit
```

### Manual Tasks

```bash
# Add
task add "refactor auth module" --agent "Claude-1" --priority high

# List
task list
task list --status working
task list --agent "Claude-1"

# Status updates
task start 3                # -> working
task done 3                 # -> done
task fail 3                 # -> failed

# Notes
task note 3 "base framework done, writing tests"

# Delete
task rm 3

# Clean up
task clean                  # remove all done tasks

# Stats
task stats
```

## Tech Stack

- **Python 3** + **click** (CLI) + **rich** (terminal UI)
- **SQLite** for manual task storage (auto-created `tasks.db`)
- Reads Claude Code session files (`.jsonl`) for auto-detection

## License

MIT
