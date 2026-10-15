# ai-budget

A beautiful `htop`-style terminal dashboard for tracking API budgets across **pi** and **Claude Code**.

Rainbow budget bar, sparkline trend, sortable session tables, and one-key session inspection — all from local data with incremental refreshes.

## Features

- **Rainbow budget gauge** — gradient bar (green → yellow → orange → pink → purple)
- **Sparkline** — real-time budget growth over time using Unicode block chars (▁▃▅▇█)
- **Fastest growing** — sessions active in the last 2 hours, ranked by $/hr *(default view)*
- **Top sessions** — sortable by cost (`1`), tokens (`2`), or growth rate (`3`)
- **Toggle views** — `Tab` swaps fastest ↔ top; Enter inspects any session
- **Per-agent breakdown** — pi vs Claude Code totals with session counts
- **Weekly budgets** — configurable reset day (Monday, Friday, etc.)
- **Incremental parsing** — only re-reads files whose mtime changed; parallel I/O via thread pool
- **Session detail modal** — press Enter on any row to see model, cost, tokens, file path
- **Smart titles** — extracts first user message for pi sessions; uses Claude Code metadata titles
- **Config-driven** — budget amount, reset cadence, window size, refresh interval
- **One-shot mode** — `ai-budget --summary` prints a quick report

## Install

### With uv (recommended)

```bash
uv tool install git+https://github.com/dtmirizzi/ai-budget.git
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
ai-budget              # launch the TUI dashboard
ai-budget --summary    # one-shot report
ai-budget --config ~/my-config.json
```

### Keybindings

| Key | Action |
|-----|--------|
| `q` | Quit |
| `r` | Force refresh |
| `Tab` | Toggle fastest ↔ top view |
| `Enter` | Inspect highlighted session (detail modal) |
| `Esc` | Close detail modal |
| `s` | Reverse sort order |
| `1` | Sort by cost |
| `2` | Sort by tokens |
| `3` | Sort by growth rate |

## Configuration

Default config at `~/.config/ai-budget/config.json`.

### Weekly budget (reset every Monday)

```json
{
  "budget": {
    "amount": 100.0,
    "reset": "weekly"
  },
  "window": {
    "type": "weekly",
    "reset_day": "monday"
  },
  "refresh_interval_seconds": 60,
  "display": {
    "top_sessions_count": 20
  }
}
```

### Monthly rolling window

```json
{
  "budget": {
    "amount": 500.0,
    "reset": "monthly"
  },
  "window": {
    "type": "rolling",
    "days": 30
  },
  "refresh_interval_seconds": 60
}
```

| Field | Description |
|-------|-------------|
| `budget.amount` | Total budget in USD |
| `budget.reset` | `"daily"`, `"weekly"`, or `"monthly"` |
| `window.type` | `"rolling"`, `"weekly"`, or `"monthly"` |
| `window.reset_day` | Day of week for weekly reset: `"monday"` through `"sunday"` |
| `window.days` | Days to include (for `rolling` type) |
| `refresh_interval_seconds` | Auto-refresh interval (default 60) |

## Data sources

**Pi** — `~/.pi/agent/sessions/<workspace>/*.jsonl` — extracts cost, tokens, model, first user prompt as title.

**Claude Code** — `~/Library/Application Support/Claude/local-agent-mode-sessions/` — parses `audit.jsonl` for `total_cost_usd`, matches sibling metadata JSONs for session titles, models, and timestamps.

## How it works

On first launch, every session file is parsed in parallel across 8 threads. On subsequent refreshes, only files whose **mtime changed** are re-parsed — the rest come from an in-memory cache. A ring buffer tracks total spend over time for the sparkline.

Built with [Textual](https://textual.textualize.io/).