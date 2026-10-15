#!/usr/bin/env python3
"""
ai-budget — a beautiful terminal dashboard for tracking Claude & Pi API budgets.
Usage: ai-budget [--config ~/.config/ai-budget/config.json]
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── textual imports ──────────────────────────────────────────────────────────
try:
    from textual.screen import Screen
    from textual.binding import Binding
    from textual.reactive import reactive
    from textual.widgets import DataTable, Footer, Header, Label, Static
    from textual.widget import Widget
except ImportError:
    print("textual is required: pip install textual")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/ai-budget/config.json")


def load_config(path: str | None = None) -> dict:
    path = path or DEFAULT_CONFIG_PATH
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {
            "budget": {"amount": 100.0, "currency": "USD", "reset": "monthly"},
            "window": {"type": "rolling", "days": 30},
            "paths": {
                "pi_sessions": "~/.pi/agent/sessions",
                "claude_code_sessions": "~/Library/Application Support/Claude/local-agent-mode-sessions",
                "claude_code_sessions_alt": "~/Library/Application Support/Claude/claude-code-sessions",
            },
            "refresh_interval_seconds": 60,
            "display": {"top_sessions_count": 20, "theme": "dracula"},
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Data model
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SessionData:
    session_id: str
    agent: str  # "pi" or "claude-code"
    title: str = ""
    cwd: str = ""
    model: str = ""
    total_cost: float = 0.0
    total_tokens: int = 0
    created_at: datetime | None = None
    last_activity_at: datetime | None = None
    workspace: str = ""
    source_path: str = ""  # filesystem path to the session file

    @property
    def cost_per_hour(self) -> float:
        if self.created_at is None or self.last_activity_at is None:
            return 0.0
        hours = (self.last_activity_at - self.created_at).total_seconds() / 3600
        return self.total_cost / hours if hours > 0 else 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Parsing helpers (stateless — called from worker threads)
# ═══════════════════════════════════════════════════════════════════════════════

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def _strip(text: str) -> str:
    return _ANSI_RE.sub('', text)


def _parse_jsonl(path: Path) -> list[dict]:
    entries = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except (OSError, PermissionError):
        pass
    return entries


def _extract_pi(entries: list[dict], source_path: str = "") -> SessionData | None:
    """Parse a pi session JSONL into a SessionData (called from thread pool)."""
    session_id = ""
    cwd = ""
    created_at = None
    last_activity = None
    model = ""
    total_cost = 0.0
    total_tokens = 0
    title = ""

    for entry in entries:
        t = entry.get("type")
        if t == "session":
            session_id = entry.get("id", session_id)
            cwd = entry.get("cwd", "")
            ts = entry.get("timestamp", "")
            if ts:
                try:
                    created_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    pass
        elif t == "model_change":
            model = _strip(entry.get("modelId", ""))
        elif t == "message":
            msg = entry.get("message", {})
            # Capture first user message as title
            if not title and msg.get("role") == "user":
                content = msg.get("content", [])
                if content and isinstance(content, list):
                    first_text = content[0].get("text", "")
                    if first_text:
                        title = _strip(first_text)[:120].strip()
            usage = msg.get("usage", {})
            if "cost" in usage:
                total_cost += usage["cost"].get("total", 0.0)
            total_tokens += usage.get("totalTokens", 0)

    if not session_id:
        return None

    # Last timestamp from end of file
    for entry in reversed(entries):
        ts = entry.get("timestamp", "")
        if ts:
            try:
                last_activity = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                pass
            break

    if total_cost <= 0 and total_tokens <= 0:
        return None

    return SessionData(
        session_id=session_id, agent="pi", title=title, cwd=cwd, model=model,
        total_cost=total_cost, total_tokens=total_tokens,
        created_at=created_at, last_activity_at=last_activity,
        source_path=source_path,
    )


def _extract_claude(entries: list[dict], meta: dict, source_path: str = "", is_sub: bool = False) -> SessionData | None:
    """Parse Claude Code audit entries into a SessionData (called from thread pool)."""
    total_cost = 0.0
    total_tokens = 0

    for entry in entries:
        t = entry.get("type")
        if t == "result":
            total_cost += entry.get("total_cost_usd", 0.0)
            usage = entry.get("usage", {})
            total_tokens += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        elif t == "assistant":
            usage = entry.get("message", {}).get("usage", {})
            total_tokens += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

    if total_cost <= 0 and total_tokens <= 0:
        return None

    title = _strip(meta.get("title", ""))
    model = _strip(meta.get("model", ""))
    cwd = _strip(meta.get("cwd", ""))

    created_at = None
    if meta.get("createdAt"):
        try:
            created_at = datetime.fromtimestamp(meta["createdAt"] / 1000, tz=timezone.utc)
        except (ValueError, OSError):
            pass
    last_activity = None
    if meta.get("lastActivityAt"):
        try:
            last_activity = datetime.fromtimestamp(meta["lastActivityAt"] / 1000, tz=timezone.utc)
        except (ValueError, OSError):
            pass

    # Fallback timestamps from entries
    if created_at is None:
        for entry in entries:
            ts = entry.get("timestamp") or entry.get("_audit_timestamp", "")
            if ts:
                try:
                    created_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    break
                except ValueError:
                    pass
    if last_activity is None:
        for entry in reversed(entries):
            ts = entry.get("timestamp") or entry.get("_audit_timestamp", "")
            if ts:
                try:
                    last_activity = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    break
                except ValueError:
                    pass

    # Session id
    session_id = ""
    for entry in entries:
        sid = entry.get("session_id") or entry.get("sessionId", "")
        if sid:
            session_id = sid
            break

    display_title = title or cwd or session_id
    if is_sub and not title and entries:
        pass  # keep display_title

    return SessionData(
        session_id=session_id, agent="claude-code", title=display_title,
        cwd=cwd, model=model, total_cost=total_cost, total_tokens=total_tokens,
        created_at=created_at, last_activity_at=last_activity,
        source_path=source_path,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Incremental session cache with parallel parsing
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class _CacheEntry:
    mtime: float
    session: SessionData


class SessionCache:
    """Caches parsed sessions keyed by file path. Only re-parses files whose
    mtime has changed since last read. Uses a thread pool for parallel I/O."""

    def __init__(self, max_workers: int = 8):
        self._pi: dict[str, _CacheEntry] = {}       # str(path) → _CacheEntry
        self._cc: dict[str, _CacheEntry] = {}
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._scan_count = 0

    # ── public API ──────────────────────────────────────────────────────────

    def collect_all(self, config: dict) -> tuple[list[SessionData], int, int]:
        """Return (all sessions, files scanned, files re-parsed)."""
        pi, scanned_pi, parsed_pi = self._collect_pi(config)
        cc, scanned_cc, parsed_cc = self._collect_claude(config)
        return pi + cc, scanned_pi + scanned_cc, parsed_pi + parsed_cc

    # ── pi sessions ─────────────────────────────────────────────────────────

    def _collect_pi(self, config: dict) -> tuple[list[SessionData], int, int]:
        pi_path = _expand(config.get("paths", {}).get("pi_sessions",
                                                       "~/.pi/agent/sessions"))
        if not pi_path.exists():
            return [], 0, 0

        # Discover files and their mtimes with scandir for speed
        current: dict[str, float] = {}  # str(path) → mtime
        for ws_dir in pi_path.iterdir():
            if not ws_dir.is_dir():
                continue
            try:
                for entry in os.scandir(ws_dir):
                    if entry.is_file() and entry.name.endswith(".jsonl"):
                        try:
                            current[entry.path] = entry.stat().st_mtime
                        except OSError:
                            pass
            except OSError:
                continue

        scanned = len(current)

        # Determine which files need re-parsing
        to_parse: list[str] = []
        for path, mtime in current.items():
            cached = self._pi.get(path)
            if cached is None or cached.mtime != mtime:
                to_parse.append(path)

        # Remove cached entries for files that no longer exist
        gone = [p for p in self._pi if p not in current]
        for p in gone:
            del self._pi[p]

        parsed = len(to_parse)
        if to_parse:
            self._parse_pi_parallel(to_parse, current)

        # Build result from cache, annotated with workspace
        results: list[SessionData] = []
        for path, entry in self._pi.items():
            s = entry.session
            # Derive workspace from path: ~/.pi/agent/sessions/<workspace>/<file>
            ws = Path(path).parent.name
            results.append(SessionData(
                session_id=s.session_id, agent=s.agent, title=s.title,
                cwd=s.cwd, model=s.model, total_cost=s.total_cost,
                total_tokens=s.total_tokens, created_at=s.created_at,
                last_activity_at=s.last_activity_at, workspace=ws,
                source_path=s.source_path or path,
            ))

        return results, scanned, parsed

    def _parse_pi_parallel(self, paths: list[str], mtimes: dict[str, float]) -> None:
        """Parse multiple pi session files in parallel, update cache."""
        futures = {self._executor.submit(_parse_and_extract_pi, p): p for p in paths}
        for future in as_completed(futures):
            path = futures[future]
            try:
                session = future.result()
            except Exception:
                continue
            if session is not None:
                self._pi[path] = _CacheEntry(mtime=mtimes[path], session=session)
            else:
                # No useful data — still cache to avoid re-parsing empty files
                self._pi[path] = _CacheEntry(
                    mtime=mtimes[path],
                    session=SessionData(session_id="", agent="pi", total_cost=0.0),
                )

    # ── claude-code sessions ─────────────────────────────────────────────────

    def _collect_claude(self, config: dict) -> tuple[list[SessionData], int, int]:
        paths_config = config.get("paths", {})
        base_paths = [
            paths_config.get("claude_code_sessions",
                             "~/Library/Application Support/Claude/local-agent-mode-sessions"),
            paths_config.get("claude_code_sessions_alt",
                             "~/Library/Application Support/Claude/claude-code-sessions"),
        ]

        # Build a map of audit_file_path → metadata_dict
        # We do this in one pass: walk, find meta JSONs, find their sibling
        # session dirs, enumerate audit.jsonl and project sub-jsonl files.
        work_items: list[tuple[Path, dict, bool]] = []  # (path, meta, is_sub)
        seen_audit_paths: set[str] = set()

        for base_path_str in base_paths:
            bp = _expand(base_path_str)
            if not bp.exists():
                continue

            for root, dirs, files in os.walk(bp):
                rp = Path(root)

                # Gather meta files
                meta_by_fname: dict[str, dict] = {}
                for fname in files:
                    if fname.endswith(".json") and not fname.startswith("."):
                        try:
                            meta_by_fname[fname] = json.loads((rp / fname).read_text())
                        except (json.JSONDecodeError, OSError):
                            pass

                for dname in dirs:
                    sd = rp / dname
                    meta = meta_by_fname.get(f"{dname}.json", {})

                    # Main audit.jsonl
                    audit = sd / "audit.jsonl"
                    if audit.exists():
                        ap = str(audit)
                        if ap not in seen_audit_paths:
                            seen_audit_paths.add(ap)
                            work_items.append((audit, meta, False))

                    # Sub-sessions in .claude/projects/
                    proj = sd / ".claude" / "projects"
                    if proj.exists():
                        for pd in proj.iterdir():
                            if pd.is_dir():
                                for pf in pd.iterdir():
                                    if pf.suffix == ".jsonl":
                                        pp = str(pf)
                                        if pp not in seen_audit_paths:
                                            seen_audit_paths.add(pp)
                                            work_items.append((pf, meta, True))

        scanned = len(work_items)

        # Determine which files need re-parsing
        current_mtimes: dict[str, float] = {}
        to_parse: list[tuple[Path, dict, bool]] = []
        for path, meta, is_sub in work_items:
            key = str(path)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            current_mtimes[key] = mtime
            cached = self._cc.get(key)
            if cached is None or cached.mtime != mtime:
                to_parse.append((path, meta, is_sub))

        # Remove stale entries
        gone = [k for k in self._cc if k not in current_mtimes]
        for k in gone:
            del self._cc[k]

        parsed = len(to_parse)
        if to_parse:
            self._parse_cc_parallel(to_parse, current_mtimes)

        # Build result
        results: list[SessionData] = []
        for key, entry in self._cc.items():
            s = entry.session
            if s.total_cost > 0 or s.total_tokens > 0:
                results.append(SessionData(
                    session_id=s.session_id, agent=s.agent, title=s.title,
                    cwd=s.cwd, model=s.model, total_cost=s.total_cost,
                    total_tokens=s.total_tokens, created_at=s.created_at,
                    last_activity_at=s.last_activity_at, workspace=s.workspace,
                    source_path=s.source_path or key,
                ))

        return results, scanned, parsed

    def _parse_cc_parallel(
        self,
        items: list[tuple[Path, dict, bool]],
        mtimes: dict[str, float],
    ) -> None:
        """Parse multiple claude-code audit files in parallel."""
        futures = {}
        for path, meta, is_sub in items:
            futures[self._executor.submit(_parse_and_extract_cc, path, meta, is_sub)] = path

        for future in as_completed(futures):
            path = futures[future]
            key = str(path)
            try:
                session = future.result()
            except Exception:
                continue
            if session is not None:
                self._cc[key] = _CacheEntry(mtime=mtimes[key], session=session)
            else:
                self._cc[key] = _CacheEntry(
                    mtime=mtimes[key],
                    session=SessionData(session_id="", agent="claude-code", total_cost=0.0),
                )


# ── Thread-pool worker functions (module-level for pickling) ─────────────────

def _parse_and_extract_pi(path: str) -> SessionData | None:
    entries = _parse_jsonl(Path(path))
    if not entries:
        return None
    return _extract_pi(entries, source_path=path)


def _parse_and_extract_cc(path: Path, meta: dict, is_sub: bool) -> SessionData | None:
    entries = _parse_jsonl(path)
    if not entries:
        return None
    return _extract_claude(entries, meta, source_path=str(path), is_sub=is_sub)


# ═══════════════════════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════════════════════

def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


def _mix_hex(c0: str, c1: str, t: float) -> str:
    """Linear interpolation between two #RRGGBB hex colors."""
    t = max(0.0, min(1.0, t))
    r0, g0, b0 = int(c0[1:3], 16), int(c0[3:5], 16), int(c0[5:7], 16)
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r = int(r0 + (r1 - r0) * t)
    g = int(g0 + (g1 - g0) * t)
    b = int(b0 + (b1 - b0) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def filter_by_window(sessions: list[SessionData], config: dict) -> list[SessionData]:
    window_config = config.get("window", {})
    window_days = window_config.get("days", 30)
    window_type = window_config.get("type", "rolling")

    if window_type == "rolling":
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    elif window_type == "monthly":
        now = datetime.now(timezone.utc)
        cutoff = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif window_type == "weekly":
        now = datetime.now(timezone.utc)
        # Configurable reset day — default Monday (0)
        day_names = {"monday": 0, "tuesday": 1, "wednesday": 2,
                     "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
        reset_day_name = window_config.get("reset_day", "monday").lower()
        target_weekday = day_names.get(reset_day_name, 0)
        days_since = (now.weekday() - target_weekday) % 7
        cutoff = (now - timedelta(days=days_since)).replace(
            hour=0, minute=0, second=0, microsecond=0)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    return [s for s in sessions
            if (s.created_at or s.last_activity_at) is None
            or (s.created_at or s.last_activity_at) >= cutoff]


# ═══════════════════════════════════════════════════════════════════════════════
# TUI  —  Textual dashboard
# ═══════════════════════════════════════════════════════════════════════════════


class BudgetBar(Widget):
    spent: float = reactive(0.0)
    budget: float = reactive(100.0)

    # ── rainbow gradient stops (hue-based rich colors) ──────────────────
    _GRADIENT = [
        (0.00, "#00ff87"),   # neon green
        (0.20, "#60f050"),   # green
        (0.40, "#f0e040"),   # yellow
        (0.55, "#f0a020"),   # orange
        (0.70, "#f06040"),   # red-orange
        (0.85, "#f04080"),   # hot pink
        (1.00, "#e020ff"),   # purple
    ]

    @staticmethod
    def _interp_color(t: float) -> str:
        """Interpolate through the gradient at position t (0..1)."""
        stops = BudgetBar._GRADIENT
        if t <= stops[0][0]:
            return stops[0][1]
        if t >= stops[-1][0]:
            return stops[-1][1]
        for i in range(len(stops) - 1):
            t0, c0 = stops[i]
            t1, c1 = stops[i + 1]
            if t0 <= t <= t1:
                frac = (t - t0) / (t1 - t0)
                return _mix_hex(c0, c1, frac)
        return stops[-1][1]

    def render(self) -> str:
        ratio = min(self.spent / self.budget, 1.0) if self.budget > 0 else 1.0
        width = max(self.size.width - 22, 10)
        filled = int(ratio * width)
        rest = width - filled

        # Build filled portion character-by-character with gradient
        bar = ""
        for i in range(filled):
            t = i / width if width > 0 else 0
            color = self._interp_color(t)
            bar += f"[{color}]█[/]"
        # Empty zone — dim purple
        bar += f"[#302040]{'░' * rest}[/]"

        spent_color = self._interp_color(ratio)
        return (
            f" Budget  {bar}  "
            f"[{spent_color}]${self.spent:,.2f}[/]"
            f" / ${self.budget:,.2f}"
        )


class AgentSummary(Widget):
    pi_cost: float = reactive(0.0)
    pi_sessions: int = reactive(0)
    claude_cost: float = reactive(0.0)
    claude_sessions: int = reactive(0)

    def render(self) -> str:
        pi_s = f"[bold cyan]pi[/] ${self.pi_cost:,.2f}  ({self.pi_sessions} sessions)"
        cc_s = (f"[bold magenta]claude-code[/] ${self.claude_cost:,.2f}"
                f"  ({self.claude_sessions} sessions)")
        return f" Agents  {pi_s}    {cc_s}"


class RefreshStatus(Widget):
    last_refresh: float = reactive(0.0)
    interval: float = reactive(60.0)
    files_scanned: int = reactive(0)
    files_parsed: int = reactive(0)

    def render(self) -> str:
        ago = time.time() - self.last_refresh
        if ago < 1:
            age = "[dim]just now[/]"
        else:
            age = f"[dim]{ago:.0f}s ago[/]"
        return (
            f" {age}  (every {self.interval:.0f}s)  "
            f"[dim]scanned {self.files_scanned} files, "
            f"re-parsed {self.files_parsed}[/]"
        )


class Sparkline(Widget):
    """Mini budget-growth chart using Unicode block characters."""

    _BLOCKS = " ▁▂▃▄▅▆▇█"  # 9 levels (0-8)
    points: list[float] = []

    def render(self) -> str:
        pts = self.points
        if len(pts) < 2:
            return "[dim]  ⏳ collecting data…[/]"

        mn, mx = min(pts), max(pts)
        span = mx - mn
        if span == 0:
            span = 1

        # Map each point to a block char (level 0–8)
        chars = []
        for v in pts:
            level = int((v - mn) / span * 8)
            chars.append(self._BLOCKS[min(level, 8)])

        line = "".join(chars)

        # Color the line with rainbow gradient
        colored = ""
        n = len(chars)
        for i, ch in enumerate(chars):
            color = BudgetBar._interp_color(i / max(n - 1, 1) * 0.7)
            colored += f"[{color}]{ch}[/]"

        return f"[dim]${mn:,.2f}[/] {colored} [dim]${mx:,.2f}[/]"


class SessionDetail(Screen):
    """Modal showing session metadata and first/last messages."""

    CSS = """
    SessionDetail {
        align: center middle;
    }
    #detail-box {
        width: 70;
        height: auto;
        max-height: 90%;
        border: solid $secondary;
        background: $surface;
        padding: 1 2;
    }
    #detail-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    def __init__(self, session: SessionData):
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        yield Vertical(id="detail-box")

    def on_mount(self) -> None:
        s = self.session
        box = self.query_one("#detail-box", Vertical)

        agent_color = "cyan" if s.agent == "pi" else "magenta"
        title = s.title or s.cwd or s.session_id[:16]

        lines = [
            f"[bold {agent_color}]{s.agent}[/]  {title}",
            "",
            f"[dim]Model:[/]      {(s.model or '—')}",
            f"[dim]Cost:[/]       ${s.total_cost:,.4f}",
            f"[dim]Tokens:[/]     {s.total_tokens:,}",
            f"[dim]Rate:[/]       ${s.cost_per_hour:,.2f}/hr",
        ]
        if s.created_at:
            lines.append(f"[dim]Started:[/]    {s.created_at.strftime('%Y-%m-%d %H:%M')}")
        if s.last_activity_at:
            delta = datetime.now(timezone.utc) - s.last_activity_at
            ago = f"{delta.total_seconds() / 3600:.1f}h ago" if delta.total_seconds() > 3600 else f"{delta.total_seconds() / 60:.0f}m ago"
            lines.append(f"[dim]Last active:[/] {s.last_activity_at.strftime('%Y-%m-%d %H:%M')} ({ago})")
        if s.cwd:
            lines.append(f"[dim]CWD:[/]        {s.cwd}")
        if s.source_path:
            lines.append(f"[dim]File:[/]       {s.source_path}")
        lines.append("")
        lines.append("[dim]esc[/] close")

        for line in lines:
            box.mount(Label(line))

    def action_dismiss(self) -> None:
        self.dismiss()


class BudgetDashboard(App):
    """Htop-style budget dashboard for pi & Claude Code."""

    CSS = """
    BudgetBar       { height: 1; margin: 0 1; }
    AgentSummary    { height: 1; margin: 0 1; }
    RefreshStatus   { height: 1; margin: 0 1; }
    Sparkline       { height: 1; margin: 0 1; }
    #main-table     { height: 1fr; margin: 0 1; }
    #secondary-table { height: auto; max-height: 12; margin: 0 1 1 1; }
    #totals         { height: 1; margin: 0 1; }
    DataTable       { border: solid $primary-background; }
    .section-label  { height: 1; margin: 1 1 0 1; text-style: bold; color: $secondary; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit"),
        Binding("r", "refresh", "Force refresh"),
        Binding("tab", "toggle_view", "Toggle view"),
        Binding("enter", "inspect_session", "Inspect"),
        Binding("s", "toggle_sort", "Reverse sort"),
        Binding("1", "sort_cost", "Sort by cost"),
        Binding("2", "sort_tokens", "Sort by tokens"),
        Binding("3", "sort_growth", "Sort by rate"),
    ]

    sort_key: str = "cost"
    sort_reverse: bool = True
    show_fastest: bool = True  # default: fastest growing view
    sessions: list[SessionData] = []
    active_sessions: list[SessionData] = []
    _cache: SessionCache | None = None
    _history: deque[float] = deque(maxlen=60)
    _main_rows: list[SessionData] = []  # for cursor→session lookup

    def __init__(self, config_path: str | None = None):
        super().__init__()
        self.config = load_config(config_path)
        self.refresh_interval = self.config.get("refresh_interval_seconds", 60)
        self._cache = SessionCache(max_workers=8)
        self._history = deque(maxlen=60)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield BudgetBar()
        yield AgentSummary()
        yield RefreshStatus()
        yield Sparkline()
        yield Label("  Fastest Growing", id="main-label", classes="section-label")
        yield DataTable(id="main-table")
        yield Label("", id="totals")
        yield Label("  Top Sessions", id="secondary-label", classes="section-label")
        yield DataTable(id="secondary-table")
        yield Static("", id="status-line")
        yield Footer()

    def on_mount(self) -> None:
        self.collect_data()
        self.update_display()
        self.set_interval(self.refresh_interval, self.tick)

    def tick(self) -> None:
        self.collect_data()
        self.update_display()

    def collect_data(self) -> None:
        t0 = time.time()
        assert self._cache is not None
        all_sessions, scanned, parsed = self._cache.collect_all(self.config)
        self.last_collect = t0
        self._last_scanned = scanned
        self._last_parsed = parsed
        self.sessions = filter_by_window(all_sessions, self.config)

        now = datetime.now(timezone.utc)
        self.active_sessions = [
            s for s in self.sessions
            if s.last_activity_at and (now - s.last_activity_at) < timedelta(hours=2)
        ]

        # Record total spend for sparkline
        total_spent = sum(s.total_cost for s in self.sessions)
        self._history.append(total_spent)

    def _sorted(self) -> list[SessionData]:
        key_map = {
            "cost": lambda s: s.total_cost,
            "tokens": lambda s: s.total_tokens,
            "growth": lambda s: s.cost_per_hour,
        }
        return sorted(self.sessions, key=key_map.get(self.sort_key, lambda s: s.total_cost),
                      reverse=self.sort_reverse)

    def update_display(self) -> None:
        budget_amount = self.config.get("budget", {}).get("amount", 100.0)
        total_spent = sum(s.total_cost for s in self.sessions)

        # ── Sparkline ────────────────────────────────────────────────────
        spark = self.query_one(Sparkline)
        spark.points = list(self._history)

        # ── Refresh status ───────────────────────────────────────────────
        rs = self.query_one(RefreshStatus)
        rs.last_refresh = self.last_collect
        rs.interval = self.refresh_interval
        rs.files_scanned = getattr(self, "_last_scanned", 0)
        rs.files_parsed = getattr(self, "_last_parsed", 0)

        # ── Budget bar ───────────────────────────────────────────────────
        bb = self.query_one(BudgetBar)
        bb.spent = total_spent
        bb.budget = budget_amount

        # ── Agent summary ────────────────────────────────────────────────
        pi_cost = sum(s.total_cost for s in self.sessions if s.agent == "pi")
        pi_cnt = sum(1 for s in self.sessions if s.agent == "pi")
        cc_cost = sum(s.total_cost for s in self.sessions if s.agent == "claude-code")
        cc_cnt = sum(1 for s in self.sessions if s.agent == "claude-code")
        ag = self.query_one(AgentSummary)
        ag.pi_cost = pi_cost
        ag.pi_sessions = pi_cnt
        ag.claude_cost = cc_cost
        ag.claude_sessions = cc_cnt

        # ── Main table & Secondary table (swap based on view) ─────────────
        if self.show_fastest:
            main_label, secondary_label = "Fastest Growing", "Top Sessions"
            main_data = sorted(self.active_sessions, key=lambda x: -x.cost_per_hour)[:10]
            main_cols = ("Agent", "Session / Title", "Cost", "Rate/hr", "Last Activity")
            secondary_data = self._sorted()
            secondary_cols = ("Agent", "Session / Title", "Model", "Cost", "Tokens", "Workspace")
        else:
            main_label, secondary_label = "Top Sessions", "Fastest Growing"
            main_data = self._sorted()
            main_cols = ("Agent", "Session / Title", "Model", "Cost", "Tokens", "Workspace")
            secondary_data = sorted(self.active_sessions, key=lambda x: -x.cost_per_hour)[:10]
            secondary_cols = ("Agent", "Session / Title", "Cost", "Rate/hr", "Last Activity")

        self.query_one("#main-label", Label).update(f"  {main_label}")
        self.query_one("#secondary-label", Label).update(f"  {secondary_label}")

        # ── Populate main table ──────────────────────────────────────────
        main_table = self.query_one("#main-table", DataTable)
        main_table.clear(columns=True)
        main_table.add_columns(*main_cols)
        main_table.cursor_type = "row"

        top_n = self.config.get("display", {}).get("top_sessions_count", 20)
        self._main_rows = list(main_data[:top_n]) if not self.show_fastest else list(main_data[:10])

        if self.show_fastest:
            for s in main_data:
                agent = "[bold cyan]π[/]" if s.agent == "pi" else "[bold magenta]C[/]"
                title = (s.title or s.cwd or s.session_id[:12])[:45]
                rate = s.cost_per_hour
                if rate > 5:
                    rate_s = f"[bold red]${rate:,.2f}/hr[/]"
                elif rate > 1:
                    rate_s = f"[bold yellow]${rate:,.2f}/hr[/]"
                else:
                    rate_s = f"${rate:,.2f}/hr"
                ago = ""
                if s.last_activity_at:
                    delta = datetime.now(timezone.utc) - s.last_activity_at
                    secs = delta.total_seconds()
                    if secs < 60:
                        ago = f"[green]{secs:.0f}s ago[/]"
                    elif secs < 3600:
                        ago = f"[yellow]{secs / 60:.0f}m ago[/]"
                    else:
                        ago = f"[dim]{secs / 3600:.1f}h ago[/]"
                main_table.add_row(agent, title, f"${s.total_cost:,.2f}", rate_s, ago)
        else:
            for s in main_data[:top_n]:
                agent = "[bold cyan]π[/]" if s.agent == "pi" else "[bold magenta]C[/]"
                title = (s.title or s.cwd or s.session_id[:12])[:50]
                model = (s.model or "—")[:30]
                ws = (s.workspace or "—")[:35]
                if s.total_cost > 5:
                    cost_s = f"[bold red]${s.total_cost:,.2f}[/]"
                elif s.total_cost > 1:
                    cost_s = f"[bold yellow]${s.total_cost:,.2f}[/]"
                else:
                    cost_s = f"[green]${s.total_cost:,.2f}[/]"
                main_table.add_row(agent, title, model, cost_s, f"{s.total_tokens:,}", ws)

        # ── Populate secondary table ─────────────────────────────────────
        sec_table = self.query_one("#secondary-table", DataTable)
        sec_table.clear(columns=True)
        sec_table.add_columns(*secondary_cols)

        if self.show_fastest:
            for s in secondary_data[:top_n]:
                agent = "[bold cyan]π[/]" if s.agent == "pi" else "[bold magenta]C[/]"
                title = (s.title or s.cwd or s.session_id[:12])[:50]
                model = (s.model or "—")[:30]
                ws = (s.workspace or "—")[:35]
                if s.total_cost > 5:
                    cost_s = f"[bold red]${s.total_cost:,.2f}[/]"
                elif s.total_cost > 1:
                    cost_s = f"[bold yellow]${s.total_cost:,.2f}[/]"
                else:
                    cost_s = f"[green]${s.total_cost:,.2f}[/]"
                sec_table.add_row(agent, title, model, cost_s, f"{s.total_tokens:,}", ws)
        else:
            for s in secondary_data:
                agent = "[bold cyan]π[/]" if s.agent == "pi" else "[bold magenta]C[/]"
                title = (s.title or s.cwd or s.session_id[:12])[:45]
                rate = s.cost_per_hour
                if rate > 5:
                    rate_s = f"[bold red]${rate:,.2f}/hr[/]"
                elif rate > 1:
                    rate_s = f"[bold yellow]${rate:,.2f}/hr[/]"
                else:
                    rate_s = f"${rate:,.2f}/hr"
                ago = ""
                if s.last_activity_at:
                    delta = datetime.now(timezone.utc) - s.last_activity_at
                    secs = delta.total_seconds()
                    if secs < 60:
                        ago = f"[green]{secs:.0f}s ago[/]"
                    elif secs < 3600:
                        ago = f"[yellow]{secs / 60:.0f}m ago[/]"
                    else:
                        ago = f"[dim]{secs / 3600:.1f}h ago[/]"
                sec_table.add_row(agent, title, f"${s.total_cost:,.2f}", rate_s, ago)

        # ── Totals ───────────────────────────────────────────────────────
        total_tokens = sum(s.total_tokens for s in self.sessions)
        self.query_one("#totals", Label).update(
            f"  [dim]{len(self.sessions)} sessions · "
            f"${total_spent:,.2f} of ${budget_amount:,.2f} · "
            f"{total_tokens:,} tokens[/]"
        )

        # ── Status line ──────────────────────────────────────────────────
        reset_type = self.config.get("budget", {}).get("reset", "monthly")
        window_days = self.config.get("window", {}).get("days", 30)
        window_type = self.config.get("window", {}).get("type", "rolling")
        reset_label = reset_type
        if reset_type == "weekly":
            day = self.config.get("window", {}).get("reset_day", "monday")
            reset_label = f"weekly ({day})"
        view_label = "fastest" if self.show_fastest else "top"
        self.query_one("#status-line", Static).update(
            f" [dim]view: {view_label} (tab) · sort: {self.sort_key} (1/2/3) · "
            f"reset: {reset_label} · window: {window_type} {window_days}d · "
            f"refresh: {self.refresh_interval}s[/]"
        )

    # ── Actions ─────────────────────────────────────────────────────────────
    def action_refresh(self) -> None:
        self.collect_data()
        self.update_display()

    def action_inspect_session(self) -> None:
        """Open detail view for the highlighted session."""
        try:
            main_table = self.query_one("#main-table", DataTable)
            row = main_table.cursor_coordinate.row
            if 0 <= row < len(self._main_rows):
                session = self._main_rows[row]
                self.push_screen(SessionDetail(session))
        except Exception:
            pass

    def action_toggle_view(self) -> None:
        self.show_fastest = not self.show_fastest
        self.update_display()

    def action_toggle_sort(self) -> None:
        self.sort_reverse = not self.sort_reverse
        self.update_display()

    def action_sort_cost(self) -> None:
        self.sort_key = "cost"
        self.update_display()

    def action_sort_tokens(self) -> None:
        self.sort_key = "tokens"
        self.update_display()

    def action_sort_growth(self) -> None:
        self.sort_key = "growth"
        self.update_display()


# ═══════════════════════════════════════════════════════════════════════════════
# CLI entry points
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_summary(config_path: str | None = None) -> None:
    config = load_config(config_path)
    cache = SessionCache(max_workers=8)
    all_sessions, scanned, parsed = cache.collect_all(config)
    sessions = filter_by_window(all_sessions, config)

    total_spent = sum(s.total_cost for s in sessions)
    budget = config.get("budget", {}).get("amount", 100.0)
    total_tokens = sum(s.total_tokens for s in sessions)
    pi_cost = sum(s.total_cost for s in sessions if s.agent == "pi")
    cc_cost = sum(s.total_cost for s in sessions if s.agent == "claude-code")

    pct = (total_spent / budget * 100) if budget > 0 else 0
    print(f"\n  Budget: ${total_spent:,.2f} / ${budget:,.2f}  ({pct:.0f}% used)")
    print(f"  Sessions: {len(sessions)}  ·  Tokens: {total_tokens:,}")
    print(f"  pi:          ${pi_cost:,.2f}")
    print(f"  claude-code: ${cc_cost:,.2f}")
    print(f"\n  Top 10 sessions by cost:")
    for s in sorted(sessions, key=lambda x: -x.total_cost)[:10]:
        agent = "π " if s.agent == "pi" else "C "
        title = (s.title or s.cwd or s.session_id[:12])[:55]
        print(f"    {agent} ${s.total_cost:>8,.2f}  {s.model:28s}  {title}")
    print()


def main():
    config_path = None
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        print("\nOptions:")
        print("  --config PATH    Config file (default: ~/.config/ai-budget/config.json)")
        print("  --summary, -s    One-shot summary, no TUI")
        print("  --help, -h       Show this help")
        return

    for i, arg in enumerate(args):
        if arg == "--config" and i + 1 < len(args):
            config_path = args[i + 1]

    if "--summary" in args or "-s" in args:
        cmd_summary(config_path)
        return

    app = BudgetDashboard(config_path=config_path)
    app.run()


if __name__ == "__main__":
    main()