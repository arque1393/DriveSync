from pathlib import Path
from typing import List, Set

# Windows/macOS system files that should never be synced to Drive.
_SKIP_NAMES: frozenset = frozenset({
    'desktop.ini', 'Desktop.ini',   # Windows folder customisation
    'Thumbs.db', 'thumbs.db',       # Windows thumbnail cache
    '.DS_Store',                     # macOS folder metadata
    'Icon\r',                        # macOS custom folder icon
})


def get_drive_path(local_rel_path: str) -> List[str]:
    """Return the parent folder components of a relative path as a list."""
    return list(Path(local_rel_path).parts[:-1])


def scan_local_files(local_folder: str) -> Set[str]:
    """Return a set of relative paths for every non-system file under local_folder."""
    root   = Path(local_folder)
    result = set()
    for p in root.rglob('*'):
        if not p.is_file():
            continue
        if p.name in _SKIP_NAMES:
            continue
        if p.name.startswith('~$'):   # Office lock/temp files
            continue
        result.add(p.relative_to(root).as_posix())
    return result
