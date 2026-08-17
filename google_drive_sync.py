"""google_drive_sync.py — Bidirectional Google Drive sync (async edition).

Concurrency architecture
────────────────────────
                    asyncio event loop  (single OS thread)
                           │
          ┌────────────────┴──────────────────┐
          │ asyncio.gather()                  │
          ▼                                   ▼
     sync_up()                          sync_down()
  (local scan + upload)            (drive scan + download)
          │                                   │
          │ asyncio.to_thread()               │ Semaphore(30)
          ▼                                   ▼
   scan_local_files()              scan_drive_files()
   [runs in thread —               [up to 30 folder listings
    Path.rglob is sync]             in flight simultaneously]
          │                                   │
          │ Semaphore(5)                       │ Semaphore(5)
          ▼                                   ▼
   upload_file() × N              download_file() × N
   [asyncio gather,                [asyncio gather,
    5 concurrent uploads]           5 concurrent downloads]

What changed vs the thread version
────────────────────────────────────
Before │ ThreadPoolExecutor(10) for scan + upload
       │ ThreadPoolExecutor(3)  for download
       │ ThreadPoolExecutor(2)  for sync_up ∥ sync_down
       │ google-api-python-client (httplib2 — blocking)
       │ io.BytesIO buffer for downloads (full file in RAM)
─ ─ ─ ─┤
After  │ asyncio.gather() everywhere — zero extra threads
       │ asyncio.Semaphore(30/5/5) caps concurrent requests
       │ aiohttp — true non-blocking HTTP
       │ aiofiles stream to disk — O(1) memory per download
       │ asyncio.to_thread() only for sync-only libs (pathlib, google-auth)

Expected improvement (853 files, ~80 folders)
──────────────────────────────────────────────
  Drive scan:   31 s  →  ~6–10 s   (30× more concurrency, no thread overhead)
  Downloads:    limited by bandwidth, more concurrent = faster for many small files
  Uploads:      same gain as downloads
  Total cycle:  ~32 s  →  ~8–15 s  (rough estimate, network-dependent)
"""

import asyncio
import os
import platform
import shutil
import socket
import time
from datetime import datetime
from pathlib import Path

from config import (
    LOCAL_FOLDER, DRIVE_FOLDER_NAME, DRIVE_FOLDER_ID,
    METADATA_FILE, DOWNLOAD_RETRIES,
    SCAN_CONCURRENCY, UPLOAD_CONCURRENCY, DOWNLOAD_CONCURRENCY,
)
from auth import get_credentials
from metadata import load_metadata, save_metadata
from local_ops import scan_local_files, get_drive_path
from drive_api import DriveSession
import drive_ops_async as drive_ops

# Relative path of the metadata file within LOCAL_FOLDER (always at root level).
_METADATA_NAME = 'sync_metadata.json'


def _fmt(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s"


# ── Device identification ─────────────────────────────────────────────────────

def _get_device_name() -> str:
    """Return a safe, filesystem-friendly hostname (≤ 24 chars, alphanumeric)."""
    raw  = socket.gethostname() or platform.node() or 'local'
    safe = ''.join(c for c in raw if c.isalnum() or c in '-_')
    return (safe[:24] or 'local').upper()


def _conflict_paths(rel_path: str, device_name: str):
    """
    Return (local_conflict_rel, drive_conflict_rel) for a conflicted file.

    Examples
    ────────
    research.md          → research.local.HOSTNAME.md   /  research.drive.md
    notes.excalidraw.md  → notes.excalidraw.local.HOSTNAME.md  /  notes.excalidraw.drive.md
    image.png            → image.local.HOSTNAME.png     /  image.drive.png
    """
    # Use PurePosixPath so rel keys stay forward-slash on every platform.
    from pathlib import PurePosixPath
    p      = PurePosixPath(rel_path)
    stem   = p.stem
    ext    = p.suffix
    parent = p.parent

    return (
        str(parent / f'{stem}.local.{device_name}{ext}'),
        str(parent / f'{stem}.drive{ext}'),
    )


# ── Sync decision helpers (run in a thread — batches all stat() calls) ────────
# Calling asyncio.to_thread() once for the whole loop is far cheaper than
# calling aiofiles.os.stat() 853 times individually (one thread dispatch each).

def _find_uploads(local_files, metadata, local_folder):
    """Return list of (full_path, rel_path) for files newer than stored mtime.

    Ghost entries (mtime=None) are skipped — they represent conflict-resolved
    paths where the local copy was renamed away; the original no longer exists.
    """
    result = []
    for rel in local_files:
        if rel == _METADATA_NAME:
            continue  # pushed to Drive separately after save_metadata
        full = str(Path(local_folder) / rel)
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            continue  # file removed between scan and stat — skip silently
        # 'mtime or 0' converts None (ghost sentinel) to 0 without crashing
        stored_mtime = metadata['files'].get(rel, {}).get('mtime') or 0
        if mtime > stored_mtime:
            result.append((full, rel))
    return result


def _classify_preview(local_files, drive_files, metadata, local_folder):
    """
    Full classification for --dry-run.  All stat/exists/getsize calls batched here.
    Returns (upload_new, upload_mod, dl_new, dl_updated, to_reconcile, conflicts).

    conflicts entries: (rel, local_new_rel, kind)
      kind = 'type1' (both modified, different size)
           | 'type2' (never synced, different size)
    to_reconcile: list of rel paths that are same-size → adopted silently.
    """
    device_name = _get_device_name()

    upload_new, upload_mod = [], []
    for rel in sorted(local_files):
        if rel == _METADATA_NAME:
            continue
        full         = str(Path(local_folder) / rel)
        mtime        = os.path.getmtime(full)
        stored       = metadata['files'].get(rel)
        stored_mtime = (stored.get('mtime') or 0) if stored else 0
        if stored is None:
            upload_new.append(rel)
        elif mtime > stored_mtime:
            upload_mod.append(rel)

    dl_new, dl_upd, to_reconcile, conflicts = [], [], [], []
    for rel, info in sorted(drive_files.items()):
        local_path = str(Path(local_folder) / rel)
        stored     = metadata['files'].get(rel)

        # Metadata file: Drive always wins — never shown as a conflict.
        if rel == _METADATA_NAME:
            if stored is None or info['mtime'] != stored.get('drive_mtime'):
                dl_new.append(rel)
            continue

        if stored is not None:
            if info['mtime'] == stored.get('drive_mtime'):
                pass  # unchanged
            elif stored.get('mtime') is None or not os.path.exists(local_path):
                dl_new.append(rel)
            else:
                local_mtime = os.path.getmtime(local_path)
                if local_mtime == (stored.get('mtime') or 0):
                    dl_upd.append(rel)
                else:
                    drive_size = info.get('size', -1)
                    local_size = os.path.getsize(local_path)
                    if drive_size >= 0 and local_size == drive_size:
                        to_reconcile.append(rel)
                    else:
                        local_new, _ = _conflict_paths(rel, device_name)
                        conflicts.append((rel, local_new, 'type1'))
        elif not os.path.exists(local_path):
            dl_new.append(rel)
        else:
            drive_size = info.get('size', -1)
            local_size = os.path.getsize(local_path)
            if drive_size >= 0 and local_size == drive_size:
                to_reconcile.append(rel)
            else:
                local_new, _ = _conflict_paths(rel, device_name)
                conflicts.append((rel, local_new, 'type2'))

    return upload_new, upload_mod, dl_new, dl_upd, to_reconcile, conflicts


def _find_downloads(drive_files, metadata, local_folder, device_name='local'):
    """
    Classify every Drive file into one of four buckets.

    Conflict resolution uses file-size comparison first:
      • same size  → files are likely identical → adopt Drive's ID/mtime,
                     no download needed (avoids false conflicts between devices)
      • different  → Drive wins: local copy is renamed to .local.DEVICE backup
                     and the Drive version is downloaded to the original path.

    Returns
    ───────
    to_download  : list of (drive_id, name, local_path, drive_mtime)
    to_reconcile : list of (rel, drive_id, drive_mtime, local_path)
                   files whose size matches — just update metadata, no download
    conflicts    : list of (original_rel, drive_id, name, drive_mtime,
                            local_new_rel, kind)
                   kind is 'type1' (both modified) or 'type2' (never synced)
    msgs         : informational strings (empty; kept for API compat)
    """
    to_download:  list = []
    to_reconcile: list = []
    conflicts:    list = []
    msgs:         list = []

    for rel, info in drive_files.items():
        local_path = str(Path(local_folder) / rel)
        stored     = metadata['files'].get(rel)

        # Metadata file: Drive always wins — download if Drive version differs,
        # never create conflict copies regardless of local modifications.
        if rel == _METADATA_NAME:
            if stored is None or info['mtime'] != stored.get('drive_mtime'):
                to_download.append((info['id'], info['name'], local_path, info['mtime']))
            continue

        if stored is not None:
            # ── Known file (was synced before, or is a ghost entry) ───────
            ghost = stored.get('mtime') is None   # conflict-resolved sentinel

            if info['mtime'] == stored.get('drive_mtime'):
                # Drive unchanged since last sync → nothing to do
                # (Respects intentional local deletions — no auto-restore)
                pass

            else:
                # Drive has new content
                if ghost:
                    # Ghost: no local copy, Drive updated → re-download to original
                    to_download.append((info['id'], info['name'], local_path, info['mtime']))

                elif not os.path.exists(local_path):
                    # Locally deleted AND Drive changed → restore Drive version
                    to_download.append((info['id'], info['name'], local_path, info['mtime']))

                else:
                    local_mtime = os.path.getmtime(local_path)
                    if local_mtime == (stored.get('mtime') or 0):
                        # Drive changed, local unchanged → safe download
                        to_download.append((info['id'], info['name'], local_path, info['mtime']))
                    else:
                        # ⚡ TYPE 1 — BOTH sides changed
                        # Size check: if identical size → probably same content
                        drive_size = info.get('size', -1)
                        local_size = os.path.getsize(local_path)
                        if drive_size >= 0 and local_size == drive_size:
                            # Same size → adopt Drive's version (no download)
                            to_reconcile.append((rel, info['id'], info['mtime'], local_path))
                        else:
                            # Different content → Drive wins
                            local_new, _ = _conflict_paths(rel, device_name)
                            conflicts.append((rel, info['id'], info['name'],
                                              info['mtime'], local_new, 'type1'))

        elif not os.path.exists(local_path):
            # ── Brand-new Drive file, not tracked, not local → download ───
            to_download.append((info['id'], info['name'], local_path, info['mtime']))

        else:
            # ⚡ TYPE 2 — same path exists on BOTH sides, never synced
            # Size check: if identical size → probably same content, just adopt
            drive_size = info.get('size', -1)
            local_size = os.path.getsize(local_path)
            if drive_size >= 0 and local_size == drive_size:
                to_reconcile.append((rel, info['id'], info['mtime'], local_path))
            else:
                # Different content → Drive wins
                local_new, _ = _conflict_paths(rel, device_name)
                conflicts.append((rel, info['id'], info['name'],
                                   info['mtime'], local_new, 'type2'))

    return to_download, to_reconcile, conflicts, msgs


def _resolve_conflicts(conflicts, metadata, local_folder):
    """
    Execute Drive-wins conflict resolution:

      1. Rename local copy  →  filename.local.DEVICE.ext  (backup preserved)
      2. Clear metadata for the original path so the download pipeline writes
         the Drive version there and records proper metadata.
      3. Return download entries for the Drive version at the ORIGINAL path.
      4. The renamed local backup has no metadata entry → sync_up will upload
         it to Drive on the next cycle so the user's work is never lost.
    """
    drive_downloads: list = []

    for original_rel, drive_id, drive_name, drive_mtime, local_new_rel, _kind in conflicts:

        orig_path      = str(Path(local_folder) / original_rel)
        local_new_path = str(Path(local_folder) / local_new_rel)

        # 1. Rename local copy → .local.DEVICE backup.
        #    On Windows a concurrent upload may hold the file open; fall back
        #    to copy+delete so the backup is always created.
        if os.path.exists(orig_path):
            os.makedirs(os.path.dirname(local_new_path) or '.', exist_ok=True)
            try:
                shutil.move(orig_path, local_new_path)
            except (PermissionError, OSError):
                shutil.copy2(orig_path, local_new_path)
                try:
                    os.unlink(orig_path)
                except OSError:
                    pass   # leave original; cleaned next cycle

        # 2. Clear stale metadata — download pipeline will record fresh entry.
        metadata['files'].pop(original_rel, None)

        # 3. Queue Drive version for download to the ORIGINAL path.
        drive_downloads.append((drive_id, drive_name, orig_path, drive_mtime))

    return drive_downloads


def _reconcile_metadata_with_drive(drive_files: dict, metadata: dict) -> None:
    """Clear metadata entries for files no longer present in Drive.

    Without this, a file deleted from Drive keeps its stored mtime entry.
    On the next cycle _find_uploads sees local_mtime == stored_mtime and
    skips the file — it never gets re-uploaded.  Clearing the entry makes
    _find_uploads treat the file as new and queue it for upload.
    """
    drive_ids = {info['id'] for info in drive_files.values()}
    to_clear = [
        rel for rel, stored in metadata['files'].items()
        if rel != _METADATA_NAME
        and stored.get('drive_id')
        and stored['drive_id'] not in drive_ids
    ]
    for rel in to_clear:
        del metadata['files'][rel]
    if to_clear:
        print(f"   🔁 {len(to_clear)} file(s) removed from Drive — queued for re-upload")


def _reconcile_metadata_with_local(local_files: set, metadata: dict) -> None:
    """Clear metadata entries for files absent from the local folder.

    Called only after reloading Drive's metadata onto a device that has never
    synced (or has an empty/new local folder).  Without this, every Drive file
    looks 'unchanged' (drive_mtime matches stored) so _find_downloads skips
    them all and nothing is downloaded.  Clearing the entries makes those files
    appear brand-new to _find_downloads so they get downloaded.

    Ghost entries (mtime=None) are intentional conflict markers — left intact.
    Regular entries with a matching local file are also left intact so
    intentional local deletions on this device are still respected when the
    metadata was NOT freshly pulled from Drive.
    """
    to_clear = [
        rel for rel, stored in metadata['files'].items()
        if rel != _METADATA_NAME
        and stored.get('mtime') is not None   # skip ghost sentinels
        and rel not in local_files
    ]
    for rel in to_clear:
        del metadata['files'][rel]
    if to_clear:
        print(f"   🔁 {len(to_clear)} file(s) absent locally — queued for download")


class GoogleDriveSync:
    """Bidirectional Google Drive sync — fully async internals."""

    def __init__(
        self,
        sync_interval:  int = 300,
        conflict_keep: str | None = None,   # 'local' | 'drive' | None (keep both)
    ) -> None:
        self.sync_interval  = sync_interval
        self.conflict_keep  = conflict_keep
        self.creds          = get_credentials()
        self.metadata       = load_metadata()
        os.makedirs(LOCAL_FOLDER, exist_ok=True)

    # ── sync_up ───────────────────────────────────────────────────────────────

    async def sync_up(
        self,
        ds: DriveSession,
        root_id: str,
        meta_lock: asyncio.Lock,
        local_files=None,   # pre-scanned by _run_cycle; None → scan here
    ) -> dict:
        print("\n🔼 Checking for local changes to upload...")

        if local_files is None:
            t0          = time.perf_counter()
            local_files = await asyncio.to_thread(scan_local_files, LOCAL_FOLDER)
            t_scan      = time.perf_counter() - t0
            print(f"   📂 Local scan:  {_fmt(t_scan)}  ({len(local_files)} files found)")
        else:
            print(f"   📂 {len(local_files)} local files")

        # Batch all stat() calls into one thread — avoids 853 individual
        # event-loop blockages from os.path.getmtime in a comprehension.
        files_to_upload = await asyncio.to_thread(
            _find_uploads, local_files, self.metadata, LOCAL_FOLDER
        )

        if not files_to_upload:
            print("✅ No local changes to upload")
            return {'upload': 0.0}

        print(f"\n📋 {len(files_to_upload)} file(s) queued to upload:")
        for _, rel in files_to_upload:
            print(f"   ↑  {rel}")

        sem = asyncio.Semaphore(UPLOAD_CONCURRENCY)
        t1  = time.perf_counter()

        results = await asyncio.gather(
            *[drive_ops.upload_file(
                ds, root_id, full, rel, get_drive_path(rel),
                self.metadata, meta_lock, sem,
            ) for full, rel in files_to_upload],
            return_exceptions=True,
        )

        failed   = [files_to_upload[i][1] for i, r in enumerate(results)
                    if isinstance(r, Exception)]
        uploaded = len(files_to_upload) - len(failed)
        t_upload = time.perf_counter() - t1

        print(f"\n✅ Uploaded {uploaded}/{len(files_to_upload)} file(s)  "
              f"[{_fmt(t_upload)}]")
        if failed:
            print(f"⚠️  {len(failed)} upload(s) failed:")
            for f in failed:
                print(f"   ✗  {f}")

        return {'upload': t_upload}

    # ── sync_down ─────────────────────────────────────────────────────────────

    async def sync_down(
        self,
        ds: DriveSession,
        root_id: str,
        meta_lock: asyncio.Lock,
        drive_files=None,   # pre-scanned by _run_cycle; None → scan here
    ) -> dict:
        print("\n🔽 Checking for Drive changes to download...")

        if drive_files is None:
            t0          = time.perf_counter()
            drive_files = await drive_ops.scan_drive_files(
                ds, root_id, concurrency=SCAN_CONCURRENCY
            )
            t_scan = time.perf_counter() - t0
            print(f"   ☁  Drive scan:  {_fmt(t_scan)}  ({len(drive_files)} files indexed)")
        else:
            print(f"   ☁  {len(drive_files)} Drive files")

        # Batch all exists()/getmtime()/getsize() calls into one thread.
        files_to_download, to_reconcile, conflicts, _ = await asyncio.to_thread(
            _find_downloads, drive_files, self.metadata, LOCAL_FOLDER,
            _get_device_name(),
        )

        # ── Reconcile same-size files (no download needed) ────────────────
        if to_reconcile:
            print(f"\n✓  {len(to_reconcile)} file(s) match Drive size — adopted (no download):")
            for rel, drive_id, drive_mtime, local_path in to_reconcile:
                mtime = os.path.getmtime(local_path)
                async with meta_lock:
                    self.metadata['files'][rel] = {
                        'mtime':      mtime,
                        'drive_id':   drive_id,
                        'drive_mtime': drive_mtime,
                    }
                print(f"   ✓  {rel}")

        # ── Resolve conflicts using the configured policy ─────────────────
        # Conflicts here only contain files with genuinely DIFFERENT sizes.
        # Default policy: Drive wins — local copy saved as .local.DEVICE backup.
        if conflicts:
            ck = self.conflict_keep
            if ck == 'local':
                # Local wins: skip Drive download entirely.
                # sync_up already queued the local version for upload.
                print(f"\n⚡ {len(conflicts)} conflict(s) — Local wins (--conflict-keep=local):")
                for orig, _, _, _, _, kind in conflicts:
                    label = "both modified" if kind == 'type1' else "new file collision"
                    print(f"   [{label}]  {orig}  → keeping local, Drive overwritten on next upload")
            else:
                # Default (or --conflict-keep=drive): Drive wins.
                # Local copy renamed to .local.DEVICE backup; Drive version
                # downloaded to the original path.
                extra = await asyncio.to_thread(
                    _resolve_conflicts, conflicts, self.metadata, LOCAL_FOLDER
                )
                files_to_download.extend(extra)
                tag = ' (--conflict-keep=drive)' if ck == 'drive' else ''
                print(f"\n⚡ {len(conflicts)} conflict(s) — Drive wins{tag}:")
                for orig, _, _, _, local_new, kind in conflicts:
                    label = "both modified" if kind == 'type1' else "new file collision"
                    print(f"   [{label}]  {orig}")
                    print(f"      ├─ local backup → {local_new}")
                    print(f"      └─ Drive version → {orig}  (original path)")

        if not files_to_download:
            print("✅ No Drive changes to download")
            return {'download': 0.0}

        print(f"\n📋 {len(files_to_download)} file(s) queued to download:")
        for _fid, _fname, lpath, _dm in files_to_download:
            print(f"   ↓  {Path(lpath).relative_to(LOCAL_FOLDER).as_posix()}")

        sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
        t1  = time.perf_counter()

        results = await asyncio.gather(
            *[drive_ops.download_file(
                ds, fid, fname, lpath, dmtime,
                self.metadata, meta_lock, LOCAL_FOLDER, sem, DOWNLOAD_RETRIES,
            ) for fid, fname, lpath, dmtime in files_to_download],
            return_exceptions=True,
        )

        failed     = [files_to_download[i][2] for i, r in enumerate(results)
                      if isinstance(r, Exception)]
        downloaded = len(files_to_download) - len(failed)
        t_download = time.perf_counter() - t1

        print(f"\n✅ Downloaded {downloaded}/{len(files_to_download)} file(s)  "
              f"[{_fmt(t_download)}]")
        if failed:
            print(f"⚠️  {len(failed)} download(s) failed:")
            for f in failed:
                print(f"   ✗  {Path(f).relative_to(LOCAL_FOLDER).as_posix()}")

        return {'download': t_download}

    # ── metadata pre-pull / post-push ────────────────────────────────────────

    async def _pull_metadata_from_drive(
        self, ds: DriveSession, root_id: str, drive_files: dict | None = None
    ) -> dict | None:
        """Download Drive's metadata file if it is newer than our local copy.

        Accepts the pre-scanned drive_files dict to avoid an extra API call.
        Returns {mtime, drive_id, drive_mtime} if downloaded, else None.
        """
        stored             = self.metadata['files'].get(_METADATA_NAME)
        stored_drive_mtime = stored.get('drive_mtime') if stored else None

        if drive_files is not None:
            # Use the pre-scanned results — no extra API round-trip needed.
            if _METADATA_NAME not in drive_files:
                return None
            info        = drive_files[_METADATA_NAME]
            drive_id    = info['id']
            drive_mtime = info['mtime']
        else:
            resp = await ds.list_files(
                q=(f"name='{_METADATA_NAME}' and '{root_id}' in parents"
                   " and trashed=false"),
                fields='files(id,name,modifiedTime)',
                page_size=1,
            )
            files = resp.get('files', [])
            if not files:
                return None
            drive_id    = files[0]['id']
            drive_mtime = files[0]['modifiedTime']

        if drive_mtime == stored_drive_mtime:
            return None  # Drive unchanged since last push

        local_path = Path(METADATA_FILE)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        await ds.download(drive_id, str(local_path))
        print("   📄 Pulled newer metadata from Drive — reloading state")
        return {
            'mtime':      local_path.stat().st_mtime,
            'drive_id':   drive_id,
            'drive_mtime': drive_mtime,
        }

    async def _push_metadata_to_drive(
        self,
        ds: DriveSession,
        root_id: str,
        meta_lock: asyncio.Lock,
    ) -> None:
        """Upload the just-saved metadata file to Drive."""
        meta_path = Path(METADATA_FILE)
        if not meta_path.exists():
            return
        sem = asyncio.Semaphore(1)
        try:
            await drive_ops.upload_file(
                ds, root_id, str(meta_path), _METADATA_NAME, [],
                self.metadata, meta_lock, sem, DOWNLOAD_RETRIES,
            )
            print("   📄 Metadata pushed to Drive")
        except Exception as e:
            print(f"   ⚠️  Failed to push metadata to Drive: {e}")

    # ── dry-run preview ───────────────────────────────────────────────────────

    async def _preview_cycle(self) -> None:
        """
        Scan both sides and show every pending change — nothing is transferred.

        Runs the same scan + decision logic as a real sync cycle but stops
        before any upload or download call.  Both scans run concurrently so
        the preview is as fast as the drive-scan alone (~6–10 s).

        Output legend
        ─────────────
          ↑  [new]       local file not yet on Drive
          ↑  [modified]  local file newer than last synced version
          ↓  [new]       Drive file not yet on local disk
          ↓  [updated]   Drive file changed since last sync, local unchanged
          ⚡  [conflict]  both sides changed — real sync keeps BOTH versions
                          local → filename.local.DEVICE.ext
                          drive → filename.drive.ext
        """
        ts = datetime.now()
        rule = '─' * 60
        print(f"\n{'═'*60}")
        print(f"🔍 Dry-run — pending changes only, nothing will be modified")
        print(f"   Scanned at {ts.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'═'*60}")

        drive_ops.clear_folder_cache()

        try:
            async with DriveSession(self.creds) as ds:
                root_id = await drive_ops.get_or_create_root_folder(
                    ds, DRIVE_FOLDER_NAME, folder_id=DRIVE_FOLDER_ID or None
                )

                # Both scans run concurrently — same pattern as the real cycle
                t0 = time.perf_counter()
                local_files, drive_files = await asyncio.gather(
                    asyncio.to_thread(scan_local_files, LOCAL_FOLDER),
                    drive_ops.scan_drive_files(ds, root_id, concurrency=SCAN_CONCURRENCY),
                )
                t_scan = time.perf_counter() - t0
                print(f"\n   📂 Local:  {len(local_files)} files  │  "
                      f"☁  Drive: {len(drive_files)} files  │  "
                      f"scanned in {_fmt(t_scan)}")

                # Batch all stat()/exists()/getsize() calls into a single thread.
                (to_upload_new, to_upload_mod,
                 to_dl_new, to_dl_upd,
                 to_reconcile, conflicts) = await asyncio.to_thread(
                    _classify_preview,
                    local_files, drive_files, self.metadata, LOCAL_FOLDER,
                )

        except Exception as e:
            print(f"\n❌ Preview failed: {e}")
            import traceback
            traceback.print_exc()
            return

        # ── Print results ─────────────────────────────────────────────────────
        def _section(title: str, items: list, icon: str, label: str) -> None:
            if not items:
                return
            print(f"\n{rule}")
            print(f"  {title}")
            print(rule)
            for item in items:
                print(f"   {icon}  {item}  [{label}]")

        _section("Would upload  (Local → Drive)", to_upload_new, "↑", "new")
        _section("Would upload  (Local → Drive)", to_upload_mod, "↑", "modified")
        _section("Would download  (Drive → Local)", to_dl_new,  "↓", "new on Drive")
        _section("Would download  (Drive → Local)", to_dl_upd,  "↓", "Drive updated")
        _section("Same size — adopt Drive ID (no transfer)", to_reconcile, "✓", "identical")

        if conflicts:
            print(f"\n{rule}")
            print(f"  Conflicts  (Drive wins — local saved as .local.DEVICE backup)")
            print(rule)
            for orig, local_new, kind in conflicts:
                label = "both modified" if kind == 'type1' else "new file collision"
                print(f"   ⚡  {orig}  [{label}]")
                print(f"      ├─ local backup → {local_new}")
                print(f"      └─ Drive version → {orig}  (original path)")

        # ── Summary ───────────────────────────────────────────────────────────
        n_up   = len(to_upload_new) + len(to_upload_mod)
        n_down = len(to_dl_new)     + len(to_dl_upd)
        n_rec  = len(to_reconcile)
        n_conf = len(conflicts)

        print(f"\n{'═'*60}")
        if n_up == 0 and n_down == 0 and n_conf == 0 and n_rec == 0:
            print("  ✅ Everything is in sync — nothing to do.")
        else:
            parts = []
            if n_up:   parts.append(f"↑ {n_up} to upload")
            if n_down: parts.append(f"↓ {n_down} to download")
            if n_rec:  parts.append(f"✓ {n_rec} to adopt")
            if n_conf: parts.append(f"⚡ {n_conf} conflict{'s' if n_conf != 1 else ''}")
            print(f"  📊  {'  │  '.join(parts)}")
        print(f"  ⏱   scanned in {_fmt(t_scan)}")
        print(f"{'═'*60}\n")

    # ── core async sync cycle ─────────────────────────────────────────────────

    async def _run_cycle(self) -> None:
        """One full bidirectional sync cycle — called by sync() and run()."""
        ts = datetime.now()
        print(f"\n{'='*60}")
        print(f"🔄 Starting sync at {ts.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}")

        drive_ops.clear_folder_cache()
        t0 = time.perf_counter()

        # Fresh lock per cycle (created inside running loop — always safe)
        meta_lock = asyncio.Lock()

        try:
            async with DriveSession(self.creds) as ds:
                root_id = await drive_ops.get_or_create_root_folder(
                    ds, DRIVE_FOLDER_NAME, folder_id=DRIVE_FOLDER_ID or None
                )

                # Phase 1 — Scan both sides concurrently so neither waits on
                # the other; this preserves the original concurrency benefit.
                t_s0 = time.perf_counter()
                local_files, drive_files = await asyncio.gather(
                    asyncio.to_thread(scan_local_files, LOCAL_FOLDER),
                    drive_ops.scan_drive_files(ds, root_id, concurrency=SCAN_CONCURRENCY),
                )
                t_scan = time.perf_counter() - t_s0
                print(f"   📂 {len(local_files)} local  │  "
                      f"☁  {len(drive_files)} Drive  [{_fmt(t_scan)}]")

                # Phase 2 — Metadata sync.
                # Pull Drive's copy if newer (e.g. another device synced first),
                # then reconcile: clear entries for Drive-deleted files so they
                # get re-uploaded instead of being silently skipped.
                drive_meta = await self._pull_metadata_from_drive(ds, root_id, drive_files)
                if drive_meta is not None:
                    self.metadata = await asyncio.to_thread(load_metadata)
                    self.metadata['files'][_METADATA_NAME] = drive_meta
                    # Pulled from Drive → this may be a new/empty local folder.
                    # Clear entries for locally-absent files so _find_downloads
                    # treats them as new and downloads them.  Only safe here
                    # because we just reloaded a foreign device's metadata —
                    # there are no intentional local deletions to respect.
                    _reconcile_metadata_with_local(local_files, self.metadata)
                _reconcile_metadata_with_drive(drive_files, self.metadata)

                # Phase 3 — Upload/download with pre-scanned file lists so
                # neither method needs to repeat the scan.
                up, down = await asyncio.gather(
                    self.sync_up(ds, root_id, meta_lock, local_files),
                    self.sync_down(ds, root_id, meta_lock, drive_files),
                )

                # Persist then push — both inside the session so the upload
                # can reuse the same aiohttp connection pool.
                await asyncio.to_thread(save_metadata, self.metadata)
                await self._push_metadata_to_drive(ds, root_id, meta_lock)
                # Save again so the updated drive_mtime for sync_metadata.json
                # (written by _push_metadata_to_drive) is on disk — prevents
                # an unnecessary re-pull on the next --sync-once invocation.
                await asyncio.to_thread(save_metadata, self.metadata)

            total = time.perf_counter() - t0

            print(f"\n✅ Sync completed in {_fmt(total)}")
            print(f"   ⏱  scan {_fmt(t_scan)} │ "
                  f"upload {_fmt(up.get('upload', 0.0))} │ "
                  f"download {_fmt(down.get('download', 0.0))}")

        except Exception as e:
            print(f"\n❌ Sync failed: {e}")
            import traceback
            traceback.print_exc()

    # ── Force push (local → Drive, one-way) ──────────────────────────────────

    async def _force_push_cycle(self) -> None:
        """
        Hard sync: upload every local file to Drive, local is authoritative.

        Differences from a normal sync cycle
        ─────────────────────────────────────
        • ALL local files are uploaded, not just ones newer than stored mtime.
        • Drive scan is used only to look up existing file IDs so Drive can
          be updated in-place instead of creating duplicates.  No downloads.
        • Drive-only files (not present locally) are listed but NOT deleted —
          run --dry-run afterwards to verify, then remove manually if needed.
        """
        ts = datetime.now()
        print(f"\n{'='*60}")
        print(f"🔼 FORCE PUSH  (local → Drive)  —  local is authoritative")
        print(f"   {ts.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}")

        drive_ops.clear_folder_cache()
        t0        = time.perf_counter()
        meta_lock = asyncio.Lock()

        try:
            async with DriveSession(self.creds) as ds:
                root_id = await drive_ops.get_or_create_root_folder(
                    ds, DRIVE_FOLDER_NAME, folder_id=DRIVE_FOLDER_ID or None
                )

                # Phase 1 — scan both sides concurrently so we can reuse
                # Drive file IDs (avoids one list_files query per upload).
                t_s0 = time.perf_counter()
                local_files, drive_files = await asyncio.gather(
                    asyncio.to_thread(scan_local_files, LOCAL_FOLDER),
                    drive_ops.scan_drive_files(ds, root_id, concurrency=SCAN_CONCURRENCY),
                )
                t_scan = time.perf_counter() - t_s0
                print(f"\n   📂 Local: {len(local_files)} files  │  "
                      f"☁  Drive: {len(drive_files)} files  [{_fmt(t_scan)}]")

                # Phase 2 — build upload list (ALL local files, not just changed)
                files_to_upload = []
                for rel in sorted(local_files):
                    if rel == _METADATA_NAME:
                        continue
                    full      = str(Path(LOCAL_FOLDER) / rel)
                    known_id  = drive_files.get(rel, {}).get('id')   # None = new file
                    files_to_upload.append((full, rel, known_id))

                # Report Drive-only files (not deleted — just informational)
                drive_only = sorted(
                    r for r in drive_files
                    if r not in local_files and r != _METADATA_NAME
                )
                if drive_only:
                    print(f"\n   ℹ  {len(drive_only)} file(s) exist on Drive but NOT locally "
                          f"(not deleted — remove manually if unwanted):")
                    for r in drive_only[:10]:
                        print(f"      ☁  {r}")
                    if len(drive_only) > 10:
                        print(f"      … and {len(drive_only) - 10} more")

                if not files_to_upload:
                    print("\n   No local files to push.")
                    return

                preview_limit = 20
                print(f"\n📋 Pushing {len(files_to_upload)} local file(s) to Drive:")
                for _, rel, kid in files_to_upload[:preview_limit]:
                    tag = 'update' if kid else 'new'
                    print(f"   ↑  {rel}  [{tag}]")
                if len(files_to_upload) > preview_limit:
                    print(f"   … and {len(files_to_upload) - preview_limit} more")

                # Phase 3 — concurrent upload
                sem = asyncio.Semaphore(UPLOAD_CONCURRENCY)
                t_u0 = time.perf_counter()

                results = await asyncio.gather(
                    *[drive_ops.upload_file(
                        ds, root_id, full, rel, get_drive_path(rel),
                        self.metadata, meta_lock, sem,
                        known_id=kid,
                    ) for full, rel, kid in files_to_upload],
                    return_exceptions=True,
                )

                failed   = [files_to_upload[i][1] for i, r in enumerate(results)
                            if isinstance(r, Exception)]
                uploaded = len(files_to_upload) - len(failed)
                t_upload = time.perf_counter() - t_u0

                print(f"\n✅ Pushed {uploaded}/{len(files_to_upload)} file(s)  "
                      f"[{_fmt(t_upload)}]")
                if failed:
                    print(f"⚠️  {len(failed)} upload(s) failed:")
                    for f in failed:
                        print(f"   ✗  {f}")

                # Phase 4 — persist metadata and push it to Drive
                await asyncio.to_thread(save_metadata, self.metadata)
                await self._push_metadata_to_drive(ds, root_id, meta_lock)
                await asyncio.to_thread(save_metadata, self.metadata)

            total = time.perf_counter() - t0
            print(f"\n✅ Force push completed in {_fmt(total)}")
            if drive_only:
                print(f"   ℹ  {len(drive_only)} Drive-only file(s) were left untouched")

        except Exception as e:
            print(f"\n❌ Force push failed: {e}")
            import traceback
            traceback.print_exc()

    # ── Public API ────────────────────────────────────────────────────────────

    def preview(self) -> None:
        """Show pending changes without transferring anything (--dry-run)."""
        asyncio.run(self._preview_cycle())

    def force_push(self) -> None:
        """Push ALL local files to Drive — local is authoritative (--force-push)."""
        asyncio.run(self._force_push_cycle())

    def sync(self) -> None:
        """Run one sync cycle and return (used by --sync-once)."""
        asyncio.run(self._run_cycle())

    def run(self) -> None:
        """Run continuous sync loop until Ctrl-C."""
        print(f"🚀 Google Drive Sync started  (async engine)")
        print(f"📂 Local folder:   {LOCAL_FOLDER}")
        print(f"☁️  Drive folder:   {DRIVE_FOLDER_NAME}")
        print(f"⏱️  Sync interval:  {self.sync_interval} s")
        print(f"🔀 Concurrency:    scan={SCAN_CONCURRENCY}  "
              f"up={UPLOAD_CONCURRENCY}  down={DOWNLOAD_CONCURRENCY}")
        print(f"\nPress Ctrl+C to stop\n")

        async def _loop() -> None:
            while True:
                await self._run_cycle()
                print(f"\n💤 Sleeping for {self.sync_interval} seconds...")
                await asyncio.sleep(self.sync_interval)

        try:
            asyncio.run(_loop())
        except KeyboardInterrupt:
            print("\n\n👋 Sync stopped by user")
            save_metadata(self.metadata)
