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
import time
from datetime import datetime
from pathlib import Path

from config import (
    LOCAL_FOLDER, DRIVE_FOLDER_NAME, DRIVE_FOLDER_ID,
    SYNC_INTERVAL, DOWNLOAD_RETRIES,
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


class GoogleDriveSync:
    """Bidirectional Google Drive sync — fully async internals."""

    def __init__(self, sync_interval: int = 300) -> None:
        self.sync_interval = sync_interval
        self.creds         = get_credentials()
        self.metadata      = load_metadata()
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

        files_to_upload = [
            (str(Path(LOCAL_FOLDER) / rel), rel)
            for rel in local_files
            if (os.path.getmtime(str(Path(LOCAL_FOLDER) / rel))
                > self.metadata['files'].get(rel, {}).get('mtime', 0))
        ]

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

        files_to_download = []
        for rel_path, info in drive_files.items():
            local_path = str(Path(LOCAL_FOLDER) / rel_path)
            entry      = (info['id'], info['name'], local_path, info['mtime'])

            if not os.path.exists(local_path):
                files_to_download.append(entry)
            elif rel_path in self.metadata['files']:
                stored = self.metadata['files'][rel_path]
                if info['mtime'] != stored.get('drive_mtime'):
                    if os.path.getmtime(local_path) == stored.get('mtime', 0):
                        files_to_download.append(entry)
                    else:
                        print(f"⚠️  Conflict: {rel_path} (keeping local version)")
            else:
                print(f"ℹ️  Skipping {rel_path}: not in sync history (will upload)")

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
          ⚡  [conflict]  both sides changed — real sync would keep local
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

                # ── Classify uploads ──────────────────────────────────────────
                to_upload_new:  list = []   # not in metadata → brand new
                to_upload_mod:  list = []   # in metadata but mtime advanced

                for rel in sorted(local_files):
                    full  = str(Path(LOCAL_FOLDER) / rel)
                    mtime = os.path.getmtime(full)
                    stored = self.metadata['files'].get(rel)
                    if stored is None:
                        to_upload_new.append(rel)
                    elif mtime > stored.get('mtime', 0):
                        to_upload_mod.append(rel)

                # ── Classify downloads + conflicts ────────────────────────────
                to_dl_new:   list = []   # not on local disk
                to_dl_upd:   list = []   # Drive changed, local unchanged
                conflicts:   list = []   # both sides changed

                for rel, info in sorted(drive_files.items()):
                    local_path = str(Path(LOCAL_FOLDER) / rel)
                    stored     = self.metadata['files'].get(rel)

                    if not os.path.exists(local_path):
                        to_dl_new.append(rel)
                    elif stored is None:
                        pass   # local exists, untracked → sync_up will handle it
                    elif info['mtime'] != stored.get('drive_mtime'):
                        if os.path.getmtime(local_path) == stored.get('mtime', 0):
                            to_dl_upd.append(rel)
                        else:
                            conflicts.append(rel)

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
            print(f"  Conflicts  (both sides changed — real sync keeps local)")
            print(rule)
            for c in conflicts:
                print(f"   ⚡  {c}")

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
