"""app/services/sync_service.py — Background sync runner for the mobile app.

Wraps GoogleDriveSync and runs it on a background thread so the Flet UI stays
responsive.  Progress events are posted back via a callback that the UI
subscribes to.

Threading model
───────────────
  Flet UI thread (main)          Background sync thread
       │                                  │
       │  SyncService.start()             │
       │ ─────────────────────────►       │  asyncio.run(_run_cycle())
       │                                  │      ├─ sync_up()
       │  on_event('progress', msg) ◄─────│      └─ sync_down()
       │  page.update()                   │
       │                                  │  asyncio.sleep(interval)
       │  on_event('complete', stats) ◄───│
       │  page.update()                   │
"""

import asyncio
import sys
import threading
import time
from contextlib import contextmanager, redirect_stdout
from datetime import datetime
from io import StringIO
from typing import Callable, Optional

from app.state import AppState, SyncStats
from app.services.conflict_store import find_pending_conflicts


class _ProgressCapture:
    """
    File-like object that intercepts print() calls from the sync engine and
    forwards each line to a callback.
    """
    def __init__(self, callback: Callable[[str], None]) -> None:
        self._cb  = callback
        self._buf = ""

    def write(self, text: str) -> None:
        self._buf += text
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            line = line.strip()
            if line:
                self._cb(line)

    def flush(self) -> None:
        pass


class SyncService:
    """
    Manages background sync lifecycle for the mobile app.

    Events emitted via on_event(event_name, data):
      'progress'  — str: a status line from the sync engine
      'complete'  — SyncStats: cycle finished successfully
      'error'     — str: exception message
      'conflicts' — int: number of unresolved conflict pairs found
    """

    def __init__(self, state: AppState, page_update: Callable) -> None:
        self._state       = state
        self._page_update = page_update
        self._thread:  Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._sync_obj = None   # GoogleDriveSync instance, created lazily

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start_background(self) -> None:
        """Start continuous background sync loop."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="SyncLoop")
        self._thread.start()
        self._state.auto_sync = True
        self._state.notify()

    def stop_background(self) -> None:
        """Signal the background loop to stop after the current cycle."""
        self._stop_evt.set()
        self._state.auto_sync = False
        self._state.notify()

    def sync_once(self) -> None:
        """Run one sync cycle in a fire-and-forget thread."""
        t = threading.Thread(target=self._run_once, daemon=True, name="SyncOnce")
        t.start()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _get_sync(self):
        """Lazy-init GoogleDriveSync (imports deferred so app loads fast)."""
        if self._sync_obj is None:
            from google_drive_sync import GoogleDriveSync
            from config import SYNC_INTERVAL
            self._sync_obj = GoogleDriveSync(sync_interval=SYNC_INTERVAL)
        return self._sync_obj

    def _loop(self) -> None:
        sync = self._get_sync()
        while not self._stop_evt.is_set():
            self._do_cycle(sync)
            # Sleep in small increments so stop_evt is checked promptly
            for _ in range(sync.sync_interval * 2):
                if self._stop_evt.is_set():
                    break
                time.sleep(0.5)

    def _run_once(self) -> None:
        self._do_cycle(self._get_sync())

    def _do_cycle(self, sync) -> None:
        t0    = time.perf_counter()
        stats = SyncStats(timestamp=datetime.now())
        self._state.is_syncing      = True
        self._state.current_progress = "Starting sync…"
        self._state.notify()
        self._page_update()

        capture = _ProgressCapture(self._on_line)

        try:
            with redirect_stdout(capture):
                asyncio.run(sync._run_cycle())

            # Parse last_stats from the sync object's state isn't available
            # directly, so we infer from the progress lines collected.
            stats.duration = time.perf_counter() - t0
            self._state.last_stats = stats
            self._state.append_history(stats)

            # Refresh conflict list from metadata
            self._refresh_conflicts(sync)

        except Exception as exc:
            stats.error    = str(exc)
            stats.duration = time.perf_counter() - t0
            self._state.last_stats = stats
            self._state.append_history(stats)
            self._emit('error', str(exc))

        finally:
            self._state.is_syncing       = False
            self._state.current_progress = None
            self._state.notify()
            self._page_update()

    def _on_line(self, line: str) -> None:
        """Called for every printed line from the sync engine."""
        self._state.current_progress = line

        # Count stats from well-known output patterns
        stats = self._state.last_stats or SyncStats(timestamp=datetime.now())
        if line.startswith("📤"):
            stats.uploaded += 1
        elif line.startswith("📥"):
            stats.downloaded += 1
        elif line.startswith("⚡"):
            pass   # conflict message — handled via metadata scan
        elif "upload(s) failed" in line:
            try:
                stats.failed_up = int(line.split()[1])
            except (ValueError, IndexError):
                pass
        elif "download(s) failed" in line:
            try:
                stats.failed_down = int(line.split()[1])
            except (ValueError, IndexError):
                pass

        self._state.last_stats = stats
        self._state.notify()
        self._page_update()

    def _refresh_conflicts(self, sync) -> None:
        """Scan metadata for ghost entries and update the conflict list."""
        try:
            from config import LOCAL_FOLDER
            conflicts = find_pending_conflicts(sync.metadata, LOCAL_FOLDER)
            self._state.pending_conflicts = conflicts
            self._state.notify()
            self._page_update()
        except Exception:
            pass

    def _emit(self, event: str, data) -> None:
        self._state.notify()
        self._page_update()
