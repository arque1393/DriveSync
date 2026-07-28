"""drive_ops_async.py — Async Drive operations built on DriveSession.

Thread vs Async decision
────────────────────────
┌─────────────────────────┬────────────────────────────┬─────────────────────┐
│ Operation               │ Approach                   │ Why                 │
├─────────────────────────┼────────────────────────────┼─────────────────────┤
│ Drive folder scan       │ asyncio + Semaphore(30)    │ Pure HTTP I/O       │
│ File download           │ asyncio + Semaphore(5)     │ Streaming I/O       │
│ File upload             │ asyncio + Semaphore(5)     │ I/O bound           │
│ Folder path resolution  │ async + in-memory cache    │ HTTP I/O + caching  │
│ Local file scan         │ asyncio.to_thread()        │ Sync disk lib       │
│ Token refresh           │ asyncio.to_thread()        │ Sync google-auth    │
│ Metadata save           │ asyncio.to_thread()        │ Sync disk write     │
└─────────────────────────┴────────────────────────────┴─────────────────────┘

Concurrency model
─────────────────
asyncio is single-threaded cooperative multitasking.  Every `await` is a
yield point — while one coroutine waits for a network response the event loop
runs other coroutines.  A Semaphore(N) caps how many coroutines can be
*inside* an HTTP call at any time, preventing rate-limit errors.

Folder path cache
─────────────────
asyncio is single-threaded so the cache dict needs no lock.  Two coroutines
can't mutate it at the exact same moment.  We do get "duplicate first-call"
races (both miss the cache, both query the API) but that's idempotent and rare.
"""

import asyncio
import os
import random
from typing import Dict, List, Optional, Tuple

import aiofiles.os
from drive_api import DriveSession

# ── Folder path cache (cleared every sync cycle) ─────────────────────────────
_folder_cache: Dict[str, str]       = {}
_folder_locks: Dict[str, asyncio.Lock] = {}


def clear_folder_cache() -> None:
    _folder_cache.clear()
    _folder_locks.clear()


def _cache_key(root_id: str, parts: List[str]) -> str:
    return root_id + '/' + '/'.join(parts)


# ── Root folder ───────────────────────────────────────────────────────────────

async def get_or_create_root_folder(
    ds: DriveSession,
    folder_name: str,
    folder_id: Optional[str] = None,
) -> str:
    """Resolve the root sync folder. Direct ID → name search → create."""
    if folder_id:
        meta = await ds.get_file(folder_id, fields='id,name')
        print(f"📁 Using Drive folder: {meta['name']} (ID: {folder_id})")
        return folder_id

    safe_name = folder_name.replace("'", "\\'")
    resp  = await ds.list_files(
        q=(f"name='{safe_name}' and "
           f"mimeType='application/vnd.google-apps.folder' and trashed=false"),
        fields='files(id,name)',
        corpora='user',
    )
    files = resp.get('files', [])
    if files:
        fid = files[0]['id']
        print(f"📁 Using existing Drive folder: {folder_name} (ID: {fid})")
        return fid

    print(f"⚠️  Folder '{folder_name}' not found — creating it.")
    fid = await ds.create_folder(folder_name)
    print(f"📁 Created Drive folder: {folder_name} (ID: {fid})")
    return fid


# ── Nested folder resolution (cached) ────────────────────────────────────────

async def get_or_create_folder_path(
    ds: DriveSession,
    drive_root_id: str,
    folder_path: List[str],
) -> str:
    """
    Resolve a nested folder path, creating missing levels.
    Results cached per cycle — uploading N files in the same dir costs
    1×depth API calls instead of N×depth.
    """
    if not folder_path:
        return drive_root_id

    full_key = _cache_key(drive_root_id, folder_path)
    if full_key in _folder_cache:
        return _folder_cache[full_key]

    parent_id = drive_root_id
    for depth, name in enumerate(folder_path):
        key = _cache_key(drive_root_id, folder_path[:depth + 1])
        if key in _folder_cache:
            parent_id = _folder_cache[key]
            continue

        # Serialize per-folder creation: without this, concurrent uploads into
        # the same subfolder each miss the cache, each call list_files, all see
        # "folder not found", and each call create_folder — producing duplicates.
        # Lock creation is safe without its own lock because asyncio is
        # single-threaded and there is no await between the check and the set.
        if key not in _folder_locks:
            _folder_locks[key] = asyncio.Lock()

        async with _folder_locks[key]:
            # Double-check: another coroutine may have created the folder while
            # we were waiting for the lock.
            if key in _folder_cache:
                parent_id = _folder_cache[key]
            else:
                safe = name.replace("'", "\\'")
                resp = await ds.list_files(
                    q=(f"name='{safe}' and '{parent_id}' in parents and "
                       f"mimeType='application/vnd.google-apps.folder' and trashed=false"),
                    fields='files(id)',
                    page_size=10,
                )
                files = resp.get('files', [])
                parent_id = files[0]['id'] if files else await ds.create_folder(name, parent_id)
                _folder_cache[key] = parent_id

    return parent_id


# ── Drive tree scan ───────────────────────────────────────────────────────────

async def _scan_one_folder(
    ds: DriveSession,
    folder_id: str,
    prefix: str,
    sem: asyncio.Semaphore,
    max_retries: int = 3,
) -> Dict[str, Dict]:
    """
    Recursively list all files under folder_id.

    Each folder's page-listing is gated by `sem` to limit concurrent HTTP
    requests.  Subfolders are scanned concurrently via asyncio.gather —
    no ThreadPoolExecutor, no nested executor creation overhead.

    Before (threads):  ~80 folders × 40ms thread-create = ~3200ms overhead
    After (async):     ~0ms overhead — coroutines are just function frames
    """
    for attempt in range(max_retries + 1):
        try:
            files:      Dict[str, Dict]       = {}
            subfolders: List[Tuple[str, str]] = []
            page_token: Optional[str]         = None

            async with sem:                        # gate the HTTP call
                while True:
                    resp = await ds.list_files(
                        q=f"'{folder_id}' in parents and trashed=false",
                        fields='nextPageToken,files(id,name,mimeType,modifiedTime)',
                        page_token=page_token,
                    )
                    for f in resp.get('files', []):
                        name = f['name']
                        path = os.path.join(prefix, name) if prefix else name
                        if f['mimeType'] == 'application/vnd.google-apps.folder':
                            subfolders.append((f['id'], path))
                        else:
                            files[path] = {
                                'id':   f['id'],
                                'mtime': f.get('modifiedTime'),
                                'name': name,
                            }
                    page_token = resp.get('nextPageToken')
                    if not page_token:
                        break

            # Recurse into all subfolders concurrently — the semaphore
            # naturally caps how many actually run their HTTP call at once.
            if subfolders:
                results = await asyncio.gather(
                    *[_scan_one_folder(ds, fid, fpath, sem, max_retries)
                      for fid, fpath in subfolders],
                    return_exceptions=True,
                )
                for i, r in enumerate(results):
                    if isinstance(r, Exception):
                        print(f"❌ Error scanning '{subfolders[i][1]}': {r}")
                    else:
                        files.update(r)

            return files

        except Exception as e:
            if attempt == max_retries:
                raise
            delay = (2 ** attempt) + random.uniform(0, 1)
            print(f"⚠️  Retrying scan of '{prefix}' in {delay:.1f}s "
                  f"(attempt {attempt + 1}/{max_retries}) — {e}")
            await asyncio.sleep(delay)

    return {}


async def scan_drive_files(
    ds: DriveSession,
    root_id: str,
    concurrency: int = 30,
) -> Dict[str, Dict]:
    """
    Scan the full Drive tree under root_id using one shared Semaphore.

    Expected speedup vs threads:
      ThreadPoolExecutor(10):  ~31 s  (thread overhead + sequential per level)
      asyncio Semaphore(30):   ~6–10 s  (30 concurrent requests, zero thread cost)
    """
    sem = asyncio.Semaphore(concurrency)
    return await _scan_one_folder(ds, root_id, '', sem)


# ── File download ─────────────────────────────────────────────────────────────

async def download_file(
    ds: DriveSession,
    file_id: str,
    file_name: str,
    local_path: str,
    drive_mtime: str,
    metadata: dict,
    metadata_lock: asyncio.Lock,
    local_folder: str,
    sem: asyncio.Semaphore,
    max_retries: int = 4,
) -> None:
    """Download with retry. Uses aiofiles for non-blocking disk writes."""
    rel_path = os.path.relpath(local_path, local_folder)

    for attempt in range(max_retries + 1):
        try:
            async with sem:
                await ds.download(file_id, local_path)

            print(f"📥 Downloaded: {rel_path}")
            mtime = (await aiofiles.os.stat(local_path)).st_mtime
            async with metadata_lock:
                metadata['files'][rel_path] = {
                    'mtime':      mtime,
                    'drive_id':   file_id,
                    'drive_mtime': drive_mtime,
                }
            return

        except Exception as e:
            if attempt == max_retries:
                raise
            delay = (2 ** attempt) + random.uniform(0, 1)
            print(f"⚠️  Retrying {file_name} in {delay:.1f}s "
                  f"(attempt {attempt + 1}/{max_retries}) — {e}")
            await asyncio.sleep(delay)


# ── File upload ───────────────────────────────────────────────────────────────

async def upload_file(
    ds: DriveSession,
    drive_root_id: str,
    local_path: str,
    rel_path: str,
    folder_path: List[str],
    metadata: dict,
    metadata_lock: asyncio.Lock,
    sem: asyncio.Semaphore,
    max_retries: int = 3,
) -> None:
    """Upload with retry. Raises on final failure so gather() surfaces it."""
    file_name = os.path.basename(local_path)
    parent_id = await get_or_create_folder_path(ds, drive_root_id, folder_path)

    for attempt in range(max_retries + 1):
        try:
            mtime  = (await aiofiles.os.stat(local_path)).st_mtime
            safe_name = file_name.replace("'", "\\'")
            resp   = await ds.list_files(
                q=f"name='{safe_name}' and '{parent_id}' in parents and trashed=false",
                fields='files(id)',
                page_size=10,
            )
            existing_id = resp['files'][0]['id'] if resp.get('files') else None

            async with sem:
                result = await ds.upload(local_path, file_name, parent_id, existing_id)

            action = 'Updated' if existing_id else 'Uploaded'
            print(f"📤 {action}: {rel_path}")

            async with metadata_lock:
                metadata['files'][rel_path] = {
                    'mtime':      mtime,
                    'drive_id':   result['id'],
                    'drive_mtime': result.get('modifiedTime'),
                }
            return

        except Exception as e:
            if attempt == max_retries:
                raise
            delay = (2 ** attempt) + random.uniform(0, 1)
            print(f"⚠️  Retrying upload {rel_path} in {delay:.1f}s "
                  f"(attempt {attempt + 1}/{max_retries}) — {e}")
            await asyncio.sleep(delay)
