# ai-budget

A beautiful `htop`-style terminal dashboard for tracking API budgets across **pi** and **Claude Code**.

Shows per-agent spend, top sessions by cost, and fastest-growing sessions — all from your local session data. Auto-refreshes, config-driven, and incremental (only re-parses files that changed).

<img width="912" alt="ai-budget dashboard" src="https://github.com/user-attachments/assets/placeholder">

## Features

- **Budget gauge** — colored progress bar with spent / total
- **Per-agent breakdown** — pi vs Claude Code totals
- **Top sessions table** — sortable by cost (`1`), tokens (`2`), or growth rate (`3`)
- **Fastest growing** — sessions active in the last 2 hours, ranked by $/hr
- **Incremental parsing** — only re-reads files whose mtime changed; parallel I/O via thread pool
- **Config-driven** — budget amount, reset cadence, window size, refresh interval
- **One-shot mode** — `ai-budget --summary` prints a quick report

## Install

### With uv (recommended)

```bash
# Install globally from GitHub
uv tool install git+https://github.com/dtmirizzi/ai-budget.git

# Or install from a local clone
git clone https://github.com/dtmirizzi/ai-budget.git
cd ai-budget
uv tool install .
```

### With pip

```bash
pip install git+https://github.com/dtmirizzi/ai-budget.git
```

### Run directly (no install)

```bash
uv run --with textual https://github.com/dtmirizzi/ai-budget.git
```

Or clone and run:

```bash
git clone https://github.com/dtmirizzi/ai-budget.git
cd ai-budget
uv run ai-budget
```

## Usage

```bash
# Launch the TUI dashboard
ai-budget

# One-shot summary (no TUI)
ai-budget --summary

# Custom config path
ai-budget --config ~/my-config.json
```

### Keybindings

| Key | Action |
|-----|--------|
| `q` | Quit |
| `r` | Force refresh |
| `s` | Reverse sort order |
| `1` | Sort by cost |
| `2` | Sort by tokens |
| `3` | Sort by growth rate |

## Configuration

Default config is created at `~/.config/ai-budget/config.json` on first run.

```json
{
  "budget": {
    "amount": 100.0,
    "currency": "USD",
    "reset": "monthly"
  },
  "window": {
    "type": "rolling",
    "days": 30
  },
  "paths": {
    "pi_sessions": "~/.pi/agent/sessions",
    "claude_code_sessions": "~/Library/Application Support/Claude/local-agent-mode-sessions",
    "claude_code_sessions_alt": "~/Library/Application Support/Claude/claude-code-sessions"
  },
  "refresh_interval_seconds": 60,
  "display": {
    "top_sessions_count": 20
  }
}
```

| Field | Description |
|-------|-------------|
| `budget.amount` | Total budget in USD |
| `budget.reset` | Reset cadence: `"daily"`, `"weekly"`, `"monthly"` |
| `window.type` | `"rolling"` (last N days) or `"monthly"` / `"weekly"` (calendar) |
| `window.days` | Days to include (for `rolling` type) |
| `refresh_interval_seconds` | Auto-refresh interval (default 60) |

## Data sources

**Pi** — reads `~/.pi/agent/sessions/<workspace>/*.jsonl`, extracts `message.usage.cost.total` and `message.usage.totalTokens` from each conversation entry.

**Claude Code** — walks `~/Library/Application Support/Claude/local-agent-mode-sessions/` for `audit.jsonl` files, extracts `result.total_cost_usd` and matches sibling metadata JSONs for session titles, models, and timestamps.

## How it works

On first launch, every session file is parsed (parallelized across 8 threads). On subsequent refreshes, only files whose **mtime changed** are re-parsed — the rest come from an in-memory cache. The refresh interval defaults to 60 seconds (configurable).

The dashboard filters sessions to the configured **budget window** (rolling or calendar) and renders everything via [Textual](https://textual.textualize.io/), the same framework powering tools like `posting` and `toolong`.