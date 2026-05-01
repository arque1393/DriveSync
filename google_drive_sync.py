import os
import time
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from googleapiclient.discovery import build

from config import (LOCAL_FOLDER, DRIVE_FOLDER_NAME, DRIVE_FOLDER_ID,
                    MAX_WORKERS, DOWNLOAD_WORKERS, DOWNLOAD_RETRIES)
from auth import get_credentials
from metadata import load_metadata, save_metadata
from local_ops import scan_local_files, get_drive_path
import drive_ops


def _fmt(seconds: float) -> str:
    """Human-readable duration: '1.2s' or '1m 23s'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s"


class GoogleDriveSync:
    """Bidirectional Google Drive sync with interval-based syncing."""

    def __init__(self, sync_interval: int = 300):
        self.sync_interval = sync_interval
        self.creds         = get_credentials()
        self.service       = build('drive', 'v3', credentials=self.creds)
        self._thread_local = threading.local()
        self.drive_root_id = drive_ops.get_or_create_root_folder(
            self.service, DRIVE_FOLDER_NAME, folder_id=DRIVE_FOLDER_ID or None
        )
        self.metadata       = load_metadata()
        self._metadata_lock = threading.Lock()
        os.makedirs(LOCAL_FOLDER, exist_ok=True)

    def _get_thread_service(self):
        """Return a thread-local Drive service (httplib2 is not thread-safe)."""
        if not hasattr(self._thread_local, 'service'):
            self._thread_local.service = build('drive', 'v3', credentials=self.creds)
        return self._thread_local.service

    # ── sync_up ───────────────────────────────────────────────────────────────

    def sync_up(self) -> dict:
        """Upload local changes to Google Drive. Returns timing breakdown."""
        print("\n🔼 Checking for local changes to upload...")

        t0 = time.perf_counter()
        local_files = scan_local_files(LOCAL_FOLDER)
        t_scan = time.perf_counter() - t0
        print(f"   📂 Local scan:  {_fmt(t_scan)}  ({len(local_files)} files found)")

        files_to_upload = []
        for rel_path in local_files:
            full_path    = os.path.join(LOCAL_FOLDER, rel_path)
            mtime        = os.path.getmtime(full_path)
            stored_mtime = self.metadata['files'].get(rel_path, {}).get('mtime', 0)
            if mtime > stored_mtime:
                files_to_upload.append((full_path, rel_path))

        if not files_to_upload:
            print("✅ No local changes to upload")
            return {'scan': t_scan, 'upload': 0.0}

        print(f"\n📋 {len(files_to_upload)} file(s) queued to upload:")
        for _, rel in files_to_upload:
            print(f"   ↑  {rel}")

        uploaded  = 0
        failed_up = []
        t1 = time.perf_counter()

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    drive_ops.upload_file,
                    self._get_thread_service,
                    self.drive_root_id,
                    full_path,
                    rel_path,
                    get_drive_path(rel_path),
                    self.metadata,
                    self._metadata_lock,
                ): rel_path
                for full_path, rel_path in files_to_upload
            }
            for future in as_completed(futures):
                rel = futures[future]
                try:
                    future.result()
                    uploaded += 1
                except Exception as e:
                    failed_up.append(rel)
                    print(f"❌ Failed ↑  {rel}: {e}")

        t_upload = time.perf_counter() - t1
        print(f"\n✅ Uploaded {uploaded}/{len(files_to_upload)} file(s)  "
              f"[upload {_fmt(t_upload)}]")
        if failed_up:
            print(f"⚠️  {len(failed_up)} upload(s) failed:")
            for f in failed_up:
                print(f"   ✗  {f}")

        return {'scan': t_scan, 'upload': t_upload}

    # ── sync_down ─────────────────────────────────────────────────────────────

    def sync_down(self) -> dict:
        """Download Drive changes to local folder. Returns timing breakdown."""
        print("\n🔽 Checking for Drive changes to download...")

        t0 = time.perf_counter()
        drive_files = drive_ops.scan_drive_files(
            self._get_thread_service, self.drive_root_id, max_workers=MAX_WORKERS
        )
        t_scan = time.perf_counter() - t0
        print(f"   ☁  Drive scan:  {_fmt(t_scan)}  ({len(drive_files)} files indexed)")

        files_to_download = []
        for rel_path, file_info in drive_files.items():
            local_path = os.path.join(LOCAL_FOLDER, rel_path)
            entry      = (file_info['id'], file_info['name'], local_path, file_info['mtime'])

            if not os.path.exists(local_path):
                files_to_download.append(entry)

            elif rel_path in self.metadata['files']:
                stored = self.metadata['files'][rel_path]
                if file_info['mtime'] != stored.get('drive_mtime'):
                    local_mtime = os.path.getmtime(local_path)
                    if local_mtime == stored.get('mtime', 0):
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
            rel = os.path.relpath(lpath, LOCAL_FOLDER)
            print(f"   ↓  {rel}")

        downloaded  = 0
        failed_down = []
        t1 = time.perf_counter()

        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
            futures = {
                executor.submit(
                    drive_ops.download_file,
                    self._get_thread_service,
                    fid, fname, lpath, dmtime,
                    self.metadata,
                    self._metadata_lock,
                    LOCAL_FOLDER,
                    DOWNLOAD_RETRIES,
                ): fname
                for fid, fname, lpath, dmtime in files_to_download
            }
            for future in as_completed(futures):
                fname = futures[future]
                try:
                    future.result()
                    downloaded += 1
                except Exception as e:
                    failed_down.append(fname)
                    print(f"❌ Failed ↓  {fname}: {e}")

        t_download = time.perf_counter() - t1
        print(f"\n✅ Downloaded {downloaded}/{len(files_to_download)} file(s)  "
              f"[download {_fmt(t_download)}]")
        if failed_down:
            print(f"⚠️  {len(failed_down)} download(s) failed:")
            for f in failed_down:
                print(f"   ✗  {f}")

        return {'scan': t_scan, 'download': t_download}

    # ── sync ──────────────────────────────────────────────────────────────────

    def sync(self):
        """Perform a full bidirectional sync."""
        ts = datetime.now()
        print(f"\n{'='*60}")
        print(f"🔄 Starting sync at {ts.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}")

        # Clear folder ID cache so stale paths don't linger between cycles
        drive_ops.clear_folder_cache()
        t0 = time.perf_counter()

        try:
            # sync_up (local scan + upload) and sync_down (drive scan + download)
            # are independent — run them in parallel so total time is
            # max(up, down) instead of up + down.
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_up   = pool.submit(self.sync_up)
                f_down = pool.submit(self.sync_down)
                up   = f_up.result()
                down = f_down.result()

            save_metadata(self.metadata)
            total = time.perf_counter() - t0

            print(f"\n✅ Sync completed in {_fmt(total)}")
            print(f"   ⏱  local scan  {_fmt(up['scan'])}  "
                  f"│  upload  {_fmt(up['upload'])}  "
                  f"│  drive scan  {_fmt(down['scan'])}  "
                  f"│  download  {_fmt(down.get('download', 0.0))}")

        except Exception as e:
            print(f"\n❌ Sync failed: {e}")

    # ── run ───────────────────────────────────────────────────────────────────

    def run(self):
        """Run a continuous sync loop."""
        print(f"🚀 Google Drive Sync started")
        print(f"📂 Local folder: {LOCAL_FOLDER}")
        print(f"☁️  Drive folder: {DRIVE_FOLDER_NAME}")
        print(f"⏱️  Sync interval: {self.sync_interval} seconds")
        print(f"\nPress Ctrl+C to stop\n")

        try:
            while True:
                self.sync()
                print(f"\n💤 Sleeping for {self.sync_interval} seconds...")
                time.sleep(self.sync_interval)
        except KeyboardInterrupt:
            print("\n\n👋 Sync stopped by user")
            save_metadata(self.metadata)
