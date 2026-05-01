import os
from pathlib import Path
from typing import List, Set


def get_relative_path(full_path: str, local_folder: str) -> str:
    return os.path.relpath(full_path, local_folder)


def get_drive_path(local_rel_path: str) -> List[str]:
    """Return the parent folder components of a relative path as a list."""
    if local_rel_path == '.':
        return []
    return list(Path(local_rel_path).parts[:-1])


def scan_local_files(local_folder: str) -> Set[str]:
    """Return a set of relative paths for every file under local_folder."""
    local_files: Set[str] = set()
    for root, _dirs, files in os.walk(local_folder):
        for file in files:
            full_path = os.path.join(root, file)
            local_files.add(get_relative_path(full_path, local_folder))
    return local_files