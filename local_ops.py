from pathlib import Path
from typing import List, Set


def get_drive_path(local_rel_path: str) -> List[str]:
    """Return the parent folder components of a relative path as a list."""
    return list(Path(local_rel_path).parts[:-1])


def scan_local_files(local_folder: str) -> Set[str]:
    """Return a set of relative paths for every file under local_folder."""
    root = Path(local_folder)
    return {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()}
