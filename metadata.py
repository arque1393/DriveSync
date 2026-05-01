import os
import json
import tempfile
from pathlib import Path
from typing import Dict

from config import METADATA_FILE


def load_metadata() -> Dict:
    path = Path(METADATA_FILE)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            print("⚠️  Metadata file corrupted, starting fresh")
    return {'files': {}, 'drive_files': {}}


def save_metadata(metadata: Dict) -> None:
    """Atomically write metadata — safe against crashes mid-write.

    Writes to a temp file first, then replaces the target with os.replace().
    On POSIX this is an atomic rename; on Windows it's best-effort.
    Either way the original file is never left in a half-written state.
    """
    target = Path(METADATA_FILE)
    fd, tmp = tempfile.mkstemp(dir=target.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
