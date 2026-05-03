"""app/services/conflict_store.py — Detect and resolve conflict pairs.

After the sync engine runs, ghost entries (mtime=None) in sync_metadata.json
mark files where both versions were kept.  This module scans those entries,
finds the .local.DEVICE and .drive copies, and provides resolution actions.

Resolution actions
──────────────────
keep_mine   — .local.DEVICE copy becomes the final file, .drive deleted
keep_theirs — .drive copy becomes the final file, .local.DEVICE deleted
keep_both   — ghost entry removed, both copies stay as separate files
decide_later — no-op; ghost stays, user will see conflict again next review
"""

import os
import shutil
from pathlib import Path
from typing import List

from app.state import ConflictItem


def find_pending_conflicts(metadata: dict, local_folder: str) -> List[ConflictItem]:
    """
    Scan sync_metadata.json for ghost entries and find the two conflict copies.

    A ghost entry looks like: {'mtime': None, 'drive_id': '...', 'drive_mtime': '...'}
    The two copies follow the naming convention set by _conflict_paths() in
    google_drive_sync.py:
        original.local.HOSTNAME.ext   ← local version
        original.drive.ext            ← Drive version
    """
    items: List[ConflictItem] = []

    for rel_path, entry in metadata.get('files', {}).items():
        if entry.get('mtime') is not None:
            continue                              # not a ghost entry

        p      = Path(rel_path)
        stem   = p.stem
        ext    = p.suffix
        parent = p.parent

        # Enumerate all .local.* files in that directory matching this stem
        abs_parent = Path(local_folder) / parent
        local_copy_rel = None
        local_copy_abs = None

        if abs_parent.exists():
            for candidate in abs_parent.iterdir():
                name = candidate.name
                # Match: stem.local.<anything>.ext
                if (name.startswith(f'{stem}.local.')
                        and name.endswith(ext)
                        and name != f'{stem}.local{ext}'):
                    local_copy_rel = str(parent / name) if str(parent) != '.' else name
                    local_copy_abs = str(candidate)
                    break

        drive_name     = f'{stem}.drive{ext}'
        drive_copy_rel = str(parent / drive_name) if str(parent) != '.' else drive_name
        drive_copy_abs = str(Path(local_folder) / drive_copy_rel)

        local_exists = local_copy_abs is not None and os.path.exists(local_copy_abs)
        drive_exists = os.path.exists(drive_copy_abs)

        if local_exists or drive_exists:
            items.append(ConflictItem(
                original=rel_path,
                local_copy=local_copy_rel or '',
                drive_copy=drive_copy_rel,
                local_path=local_copy_abs or '',
                drive_path=drive_copy_abs,
                local_exists=local_exists,
                drive_exists=drive_exists,
            ))

    return items


def resolve_keep_mine(item: ConflictItem, metadata: dict, local_folder: str) -> None:
    """
    User chose their local copy as the final version.

    1. Rename .local.DEVICE → original path.
    2. Delete .drive copy.
    3. Remove ghost entry; insert real entry with current mtime.
       (sync_up will upload it on the next cycle.)
    """
    orig_path = str(Path(local_folder) / item.original)

    # Rename local copy to original
    if item.local_exists and os.path.exists(item.local_path):
        os.makedirs(os.path.dirname(orig_path) or '.', exist_ok=True)
        shutil.move(item.local_path, orig_path)

    # Delete drive copy
    if item.drive_exists and os.path.exists(item.drive_path):
        try:
            os.unlink(item.drive_path)
        except OSError:
            pass

    # Update metadata: remove ghost, let sync_up handle the re-upload
    metadata['files'].pop(item.original, None)
    metadata['files'].pop(item.local_copy, None)
    metadata['files'].pop(item.drive_copy, None)
    # No entry means sync_up will treat it as new → uploads on next cycle


def resolve_keep_theirs(item: ConflictItem, metadata: dict, local_folder: str) -> None:
    """
    User chose the Drive copy as the final version.

    1. Rename .drive → original path (the Drive content now lives locally).
    2. Delete .local.DEVICE copy.
    3. Update metadata so the file is treated as already synced (no re-upload).
    """
    orig_path = str(Path(local_folder) / item.original)

    # Rename drive copy to original
    if item.drive_exists and os.path.exists(item.drive_path):
        os.makedirs(os.path.dirname(orig_path) or '.', exist_ok=True)
        shutil.move(item.drive_path, orig_path)

    # Delete local copy
    if item.local_exists and os.path.exists(item.local_path):
        try:
            os.unlink(item.local_path)
        except OSError:
            pass

    # Update metadata: keep the ghost's drive_id/drive_mtime, set mtime to now
    ghost = metadata['files'].get(item.original, {})
    if os.path.exists(orig_path):
        metadata['files'][item.original] = {
            'mtime':      os.path.getmtime(orig_path),
            'drive_id':   ghost.get('drive_id', ''),
            'drive_mtime': ghost.get('drive_mtime', ''),
        }
    else:
        metadata['files'].pop(item.original, None)

    metadata['files'].pop(item.local_copy, None)
    metadata['files'].pop(item.drive_copy, None)


def resolve_keep_both(item: ConflictItem, metadata: dict) -> None:
    """
    User is happy with both copies as separate files.

    Remove the ghost entry so the engine stops seeing this as a conflict.
    Both .local.DEVICE and .drive files remain as standalone files
    and will be picked up by sync_up on the next cycle.
    """
    metadata['files'].pop(item.original, None)
    # Leave the two copies without metadata entries → sync_up will upload them


def resolve_decide_later(item: ConflictItem) -> None:
    """No-op — ghost entry stays, conflict reappears on next review."""
    pass
