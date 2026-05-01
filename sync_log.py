"""sync_log.py — Missed-file log for one sync cycle.

Collects every failure (upload, download, scan, conflict) in a thread-safe
list, then appends them to missed_sync.log and prints a console summary.
"""

import threading
from collections import Counter
from datetime import datetime
from typing import List, Tuple

from config import LOG_FILE

# Direction labels used in the log
UP       = 'UP  '
DOWN     = 'DOWN'
SCAN     = 'SCAN'
CONFLICT = 'CONF'


class MissedLog:
    """
    Accumulates sync failures for one cycle.

    Usage:
        log = MissedLog()
        log.add(DOWN, 'path/to/file.pdf', 'IncompleteRead …')
        log.flush(datetime.now())   # append to missed_sync.log
        log.print_summary()         # console summary
    """

    def __init__(self) -> None:
        self._entries: List[Tuple[str, str, str]] = []   # (direction, path, error)
        self._lock = threading.Lock()

    def add(self, direction: str, path: str, error: str = '') -> None:
        with self._lock:
            self._entries.append((direction, path, error.strip()))

    @property
    def count(self) -> int:
        return len(self._entries)

    def flush(self, ts: datetime) -> None:
        """Append this cycle's failures to LOG_FILE."""
        if not self._entries:
            return
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(
                f'\n── {ts:%Y-%m-%d %H:%M:%S} '
                f'── {len(self._entries)} missed '
                f'{"─" * 36}\n'
            )
            for direction, path, error in self._entries:
                f.write(f'  [{direction}]  {path}\n')
                if error:
                    f.write(f'           ↳ {error}\n')
            f.write(f'{"─" * 62}\n')

    def print_summary(self) -> None:
        """Print a short console summary after flush."""
        if not self._entries:
            return

        counts = Counter(d for d, _, _ in self._entries)
        labels = [(UP, 'upload'), (DOWN, 'download'), (SCAN, 'scan failure'), (CONFLICT, 'conflict')]
        parts  = [
            f"{n} {word}{'s' if n != 1 else ''}"
            for key, word in labels
            if (n := counts[key])
        ]

        print(f'\n⚠️  {self.count} file(s) not synced ({", ".join(parts)}) — see {LOG_FILE}')
        for direction, path, _ in self._entries[:6]:
            print(f'   [{direction}]  {path}')
        if self.count > 6:
            print(f'   … and {self.count - 6} more')
