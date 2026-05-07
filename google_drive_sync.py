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
    DOWNLOAD_RETRIES,
    SCAN_CONCURRENCY, UPLOAD_CONCURRENCY, DOWNLOAD_CONCURRENCY,
)
from auth import get_credentials
from metadata import load_metadata, save_metadata
from local_ops import scan_local_files, get_drive_path
from drive_api import DriveSession
import drive_ops_async as drive_ops


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
    p      = Path(rel_path)
    stem   = p.stem        # everything before the last extension
    ext    = p.suffix      # last extension only (e.g. '.md')
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
        full         = str(Path(local_folder) / rel)
        mtime        = os.path.getmtime(full)
        # 'mtime or 0' converts None (ghost sentinel) to 0 without crashing
        stored_mtime = metadata['files'].get(rel, {}).get('mtime') or 0
        if mtime > stored_mtime:
            result.append((full, rel))
    return result


def _classify_preview(local_files, drive_files, metadata, local_folder):
    """
    Full classification for --dry-run.  All stat/exists calls batched here.
    Returns (upload_new, upload_mod, dl_new, dl_updated, conflicts).

    conflicts entries are (rel, local_new_rel, drive_new_rel, kind) so the
    preview screen can show the exact paths that would be created.
    """
    device_name = _get_device_name()

    upload_new, upload_mod = [], []
    for rel in sorted(local_files):
        full         = str(Path(local_folder) / rel)
        mtime        = os.path.getmtime(full)
        stored       = metadata['files'].get(rel)
        stored_mtime = (stored.get('mtime') or 0) if stored else 0
        if stored is None:
            upload_new.append(rel)
        elif mtime > stored_mtime:
            upload_mod.append(rel)

    dl_new, dl_upd, conflicts = [], [], []
    for rel, info in sorted(drive_files.items()):
        local_path = str(Path(local_folder) / rel)
        stored     = metadata['files'].get(rel)

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
                    local_new, drive_new = _conflict_paths(rel, device_name)
                    conflicts.append((rel, local_new, drive_new, 'type1'))
        elif not os.path.exists(local_path):
            dl_new.append(rel)
        else:
            local_new, drive_new = _conflict_paths(rel, device_name)
            conflicts.append((rel, local_new, drive_new, 'type2'))

    return upload_new, upload_mod, dl_new, dl_upd, conflicts


def _find_downloads(drive_files, metadata, local_folder, device_name='local'):
    """
    Classify every Drive file into one of four buckets.

    Conflict types (HIGH risk) produce entries in `conflicts` instead of
    being silently resolved in favour of local — both versions will be kept.

    Returns
    ───────
    to_download : list of (drive_id, name, local_path, drive_mtime)
    conflicts   : list of (original_rel, drive_id, name, drive_mtime,
                           local_new_rel, drive_new_rel, kind)
                  kind is 'type1' or 'type2'
    msgs        : informational strings (empty now; kept for API compat)
    """
    to_download: list = []
    conflicts:   list = []
    msgs:        list = []

    for rel, info in drive_files.items():
        local_path = str(Path(local_folder) / rel)
        stored     = metadata['files'].get(rel)

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
                        # ⚡ TYPE 1 — BOTH sides changed: keep both versions
                        local_new, drive_new = _conflict_paths(rel, device_name)
                        conflicts.append((rel, info['id'], info['name'],
                                          info['mtime'], local_new, drive_new, 'type1'))

        elif not os.path.exists(local_path):
            # ── Brand-new Drive file, not tracked, not local → download ───
            to_download.append((info['id'], info['name'], local_path, info['mtime']))

        else:
            # ⚡ TYPE 2 — same path exists on BOTH sides, never synced: keep both
            local_new, drive_new = _conflict_paths(rel, device_name)
            conflicts.append((rel, info['id'], info['name'],
                               info['mtime'], local_new, drive_new, 'type2'))

    return to_download, conflicts, msgs


def _resolve_conflicts(conflicts, metadata, local_folder):
    """
    Physically execute conflict resolution for each HIGH-RISK conflict:

      1. Rename local copy  →  filename.local.DEVICE.ext
      2. Insert a ghost metadata entry for the original path so the engine
         does not re-download it on the next cycle.
      3. Return download entries for the Drive copies (filename.drive.ext).
         The normal download pipeline writes those files and records them in
         metadata automatically.
      4. The renamed local copy is intentionally left OUT of metadata so
         sync_up will upload it to Drive on the very next cycle — ensuring
         the user's work is never lost.

    Ghost entry schema: {'mtime': None, 'drive_id': ..., 'drive_mtime': ...}
      mtime=None means "we know Drive has this file, but we chose not to keep
      a local copy at this path." The file is only re-downloaded if Drive
      changes it again.
    """
    drive_downloads: list = []

    for original_rel, drive_id, drive_name, drive_mtime, \
            local_new_rel, drive_new_rel, kind in conflicts:

        orig_path      = str(Path(local_folder) / original_rel)
        local_new_path = str(Path(local_folder) / local_new_rel)
        drive_new_path = str(Path(local_folder) / drive_new_rel)

        # 1. Rename local copy.
        #    On Windows a concurrent upload may hold the file open; fall back
        #    to copy+delete so the conflict copy is always created.
        if os.path.exists(orig_path):
            os.makedirs(os.path.dirname(local_new_path) or '.', exist_ok=True)
            try:
                shutil.move(orig_path, local_new_path)
            except (PermissionError, OSError):
                shutil.copy2(orig_path, local_new_path)
                try:
                    os.unlink(orig_path)
                except OSError:
                    pass   # leave original in place; will be cleaned next cycle

        # 2. Remove stale original metadata (if any) and insert ghost
        metadata['files'].pop(original_rel, None)
        metadata['files'][original_rel] = {
            'mtime':      None,      # ghost sentinel — no local copy
            'drive_id':   drive_id,
            'drive_mtime': drive_mtime,
        }

        # 3. Queue Drive copy (downloaded by normal pipeline → auto-recorded)
        drive_downloads.append((drive_id, drive_name, drive_new_path, drive_mtime))

    return drive_downloads


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
    ) -> dict:
        print("\n🔼 Checking for local changes to upload...")

        # scan_local_files uses pathlib (sync) — run in thread so we don't
        # block the event loop during directory traversal
        t0          = time.perf_counter()
        local_files = await asyncio.to_thread(scan_local_files, LOCAL_FOLDER)
        t_scan      = time.perf_counter() - t0
        print(f"   📂 Local scan:  {_fmt(t_scan)}  ({len(local_files)} files found)")

        # Batch all stat() calls into one thread — avoids 853 individual
        # event-loop blockages from os.path.getmtime in a comprehension.
        files_to_upload = await asyncio.to_thread(
            _find_uploads, local_files, self.metadata, LOCAL_FOLDER
        )

        if not files_to_upload:
            print("✅ No local changes to upload")
            return {'scan': t_scan, 'upload': 0.0}

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

        return {'scan': t_scan, 'upload': t_upload}

    # ── sync_down ─────────────────────────────────────────────────────────────

    async def sync_down(
        self,
        ds: DriveSession,
        root_id: str,
        meta_lock: asyncio.Lock,
    ) -> dict:
        print("\n🔽 Checking for Drive changes to download...")

        t0          = time.perf_counter()
        drive_files = await drive_ops.scan_drive_files(
            ds, root_id, concurrency=SCAN_CONCURRENCY
        )
        t_scan = time.perf_counter() - t0
        print(f"   ☁  Drive scan:  {_fmt(t_scan)}  ({len(drive_files)} files indexed)")

        # Batch all exists()/getmtime() calls into one thread.
        files_to_download, conflicts, _ = await asyncio.to_thread(
            _find_downloads, drive_files, self.metadata, LOCAL_FOLDER,
            _get_device_name(),
        )

        # ── Resolve conflicts using the configured policy ─────────────────
        if conflicts:
            ck = self.conflict_keep
            if ck == 'drive':
                # Drive wins: download the Drive version directly to the
                # original path, overwriting the local file.
                print(f"\n⚡ {len(conflicts)} conflict(s) — Drive wins (--conflict-keep=drive):")
                for orig, drive_id, drive_name, drive_mtime, _, _, kind in conflicts:
                    local_path = str(Path(LOCAL_FOLDER) / orig)
                    files_to_download.append((drive_id, drive_name, local_path, drive_mtime))
                    label = "both modified" if kind == 'type1' else "new file collision"
                    print(f"   [{label}]  {orig}  → overwriting local with Drive version")

            elif ck == 'local':
                # Local wins: skip the Drive download entirely.
                # sync_up will upload the local version on this or the next cycle
                # because local mtime > stored mtime (already in upload queue).
                print(f"\n⚡ {len(conflicts)} conflict(s) — Local wins (--conflict-keep=local):")
                for orig, _, _, _, _, _, kind in conflicts:
                    label = "both modified" if kind == 'type1' else "new file collision"
                    print(f"   [{label}]  {orig}  → keeping local, Drive will be overwritten on upload")

            else:
                # Default: keep both as .local.DEVICE and .drive copies
                extra = await asyncio.to_thread(
                    _resolve_conflicts, conflicts, self.metadata, LOCAL_FOLDER
                )
                files_to_download.extend(extra)
                print(f"\n⚡ {len(conflicts)} conflict(s) detected — keeping both versions:")
                for orig, _, _, _, local_new, drive_new, kind in conflicts:
                    label = "both modified" if kind == 'type1' else "new file collision"
                    print(f"   [{label}]  {orig}")
                    print(f"      ├─ local → {local_new}")
                    print(f"      └─ drive → {drive_new}")

        if not files_to_download:
            print("✅ No Drive changes to download")
            return {'scan': t_scan, 'download': 0.0}

        print(f"\n📋 {len(files_to_download)} file(s) queued to download:")
        for _, fname, lpath, _ in files_to_download:
            print(f"   ↓  {os.path.relpath(lpath, LOCAL_FOLDER)}")

        sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
        t1  = time.perf_counter()

        results = await asyncio.gather(
            *[drive_ops.download_file(
                ds, fid, fname, lpath, dmtime,
                self.metadata, meta_lock, LOCAL_FOLDER, sem, DOWNLOAD_RETRIES,
            ) for fid, fname, lpath, dmtime in files_to_download],
            return_exceptions=True,
        )

        failed     = [files_to_download[i][1] for i, r in enumerate(results)
                      if isinstance(r, Exception)]
        downloaded = len(files_to_download) - len(failed)
        t_download = time.perf_counter() - t1

        print(f"\n✅ Downloaded {downloaded}/{len(files_to_download)} file(s)  "
              f"[{_fmt(t_download)}]")
        if failed:
            print(f"⚠️  {len(failed)} download(s) failed:")
            for f in failed:
                print(f"   ✗  {f}")

        return {'scan': t_scan, 'download': t_download}

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

                # Batch all stat()/exists() calls into a single thread.
                (to_upload_new, to_upload_mod,
                 to_dl_new, to_dl_upd, conflicts) = await asyncio.to_thread(
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

        if conflicts:
            print(f"\n{rule}")
            print(f"  Conflicts  (both versions will be kept — HIGH RISK)")
            print(rule)
            for orig, local_new, drive_new, kind in conflicts:
                label = "both modified" if kind == 'type1' else "new file collision"
                print(f"   ⚡  {orig}  [{label}]")
                print(f"      ├─ local → {local_new}")
                print(f"      └─ drive → {drive_new}")

        # ── Summary ───────────────────────────────────────────────────────────
        n_up   = len(to_upload_new) + len(to_upload_mod)
        n_down = len(to_dl_new)     + len(to_dl_upd)
        n_conf = len(conflicts)

        print(f"\n{'═'*60}")
        if n_up == 0 and n_down == 0 and n_conf == 0:
            print("  ✅ Everything is in sync — nothing to do.")
        else:
            parts = []
            if n_up:    parts.append(f"↑ {n_up} to upload")
            if n_down:  parts.append(f"↓ {n_down} to download")
            if n_conf:  parts.append(f"⚡ {n_conf} conflict{'s' if n_conf != 1 else ''}")
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
                # sync_up and sync_down share the same DriveSession and run
                # concurrently — they operate on disjoint file sets so the
                # only shared state (self.metadata) is guarded by meta_lock.
                up, down = await asyncio.gather(
                    self.sync_up(ds, root_id, meta_lock),
                    self.sync_down(ds, root_id, meta_lock),
                )

            # save_metadata is a sync atomic file-write — run in thread
            await asyncio.to_thread(save_metadata, self.metadata)
            total = time.perf_counter() - t0

            print(f"\n✅ Sync completed in {_fmt(total)}")
            print(f"   ⏱  local scan {_fmt(up['scan'])} │ "
                  f"upload {_fmt(up['upload'])} │ "
                  f"drive scan {_fmt(down['scan'])} │ "
                  f"download {_fmt(down.get('download', 0.0))}")

        except Exception as e:
            print(f"\n❌ Sync failed: {e}")
            import traceback
            traceback.print_exc()

    # ── Public API ────────────────────────────────────────────────────────────

    def preview(self) -> None:
        """Show pending changes without transferring anything (--dry-run)."""
        asyncio.run(self._preview_cycle())

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
