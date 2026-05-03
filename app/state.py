"""app/state.py — Shared reactive application state.

Single source of truth for the whole app.  Screens subscribe via
AppState.subscribe(callback) and call AppState.notify() to trigger re-renders.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

HISTORY_FILE = "sync_history.json"
MAX_HISTORY  = 50


@dataclass
class SyncStats:
    uploaded:   int   = 0
    downloaded: int   = 0
    conflicts:  int   = 0
    failed_up:  int   = 0
    failed_down: int  = 0
    duration:   float = 0.0
    timestamp:  Optional[datetime] = None
    error:      Optional[str] = None

    @property
    def success(self) -> bool:
        return self.error is None


@dataclass
class ConflictItem:
    """One resolved conflict pair waiting for user review."""
    original:      str   # e.g. "Notes/research.md"
    local_copy:    str   # e.g. "Notes/research.local.HOSTNAME.md"
    drive_copy:    str   # e.g. "Notes/research.drive.md"
    local_path:    str   # absolute path to local copy
    drive_path:    str   # absolute path to drive copy
    local_exists:  bool = True
    drive_exists:  bool = True
    kind:          str  = 'type1'   # 'type1' or 'type2'


class AppState:
    """
    Central state container.  All mutable state lives here.

    Usage
    ─────
        state = AppState()
        state.subscribe(lambda: page.update())   # re-render on any change
        state.is_syncing = True
        state.notify()
    """

    def __init__(self) -> None:
        self.is_syncing:       bool             = False
        self.sync_paused:      bool             = False
        self.auto_sync:        bool             = False
        self.last_stats:       Optional[SyncStats] = None
        self.current_progress: Optional[str]   = None   # live status line
        self.pending_conflicts: List[ConflictItem] = []
        self._listeners:       List[Callable]  = []

    # ── Subscriptions ─────────────────────────────────────────────────────────

    def subscribe(self, fn: Callable) -> None:
        if fn not in self._listeners:
            self._listeners.append(fn)

    def unsubscribe(self, fn: Callable) -> None:
        self._listeners.discard(fn) if hasattr(self._listeners, 'discard') \
            else self._listeners.remove(fn) if fn in self._listeners else None

    def notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                pass

    # ── History persistence ───────────────────────────────────────────────────

    def append_history(self, stats: SyncStats) -> None:
        history = self.load_history()
        history.insert(0, {
            'timestamp': stats.timestamp.isoformat() if stats.timestamp else None,
            'uploaded':  stats.uploaded,
            'downloaded': stats.downloaded,
            'conflicts': stats.conflicts,
            'failed_up': stats.failed_up,
            'failed_down': stats.failed_down,
            'duration':  round(stats.duration, 1),
            'error':     stats.error,
        })
        history = history[:MAX_HISTORY]
        try:
            with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(history, f, indent=2)
        except OSError:
            pass

    @staticmethod
    def load_history() -> list:
        if not Path(HISTORY_FILE).exists():
            return []
        try:
            with open(HISTORY_FILE, encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

    # ── Convenience properties ────────────────────────────────────────────────

    @property
    def conflict_count(self) -> int:
        return len(self.pending_conflicts)

    @property
    def status_line(self) -> str:
        if self.is_syncing:
            return self.current_progress or "Syncing…"
        if self.last_stats is None:
            return "Never synced"
        if self.last_stats.error:
            return f"Failed: {self.last_stats.error[:60]}"
        if self.last_stats.timestamp:
            delta = datetime.now() - self.last_stats.timestamp
            mins  = int(delta.total_seconds() / 60)
            if mins < 1:
                return "Just synced"
            if mins == 1:
                return "1 min ago"
            if mins < 60:
                return f"{mins} min ago"
            hrs = mins // 60
            return f"{hrs}h ago"
        return "Synced"
