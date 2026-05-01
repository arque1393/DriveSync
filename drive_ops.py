import os
import ssl
import time
import random
import socket
import threading
from http.client import IncompleteRead
from typing import Callable, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait as _futures_wait

from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# Transient network errors worth retrying.
_RETRYABLE = (IncompleteRead, ConnectionResetError, TimeoutError,
              socket.gaierror, OSError, EOFError, ssl.SSLError)

# files().list() accepts both flags.
# files().get / .create / .update / .get_media only accept supportsAllDrives.
_LIST_FLAGS = dict(supportsAllDrives=True, includeItemsFromAllDrives=True)
_ITEM_FLAGS = dict(supportsAllDrives=True)

# ── Folder path cache ─────────────────────────────────────────────────────────
# Avoids re-querying the same Drive folder path for every file in an upload
# batch.  Key: "<root_id>/<part1>/<part2>/…"  Value: folder ID.
# Cleared at the start of each sync cycle by clear_folder_cache().
_folder_cache: Dict[str, str] = {}
_folder_cache_lock = threading.Lock()


def clear_folder_cache() -> None:
    with _folder_cache_lock:
        _folder_cache.clear()


def _cache_key(root_id: str, parts: List[str]) -> str:
    return root_id + '/' + '/'.join(parts)


# ── Root folder ───────────────────────────────────────────────────────────────

def get_or_create_root_folder(
    service,
    folder_name: str,
    folder_id: Optional[str] = None,
) -> str:
    """
    Resolve the root sync folder and return its Drive ID.

    Priority:
      1. folder_id set in config  → verify access and use directly.
      2. Search by name across all folders visible to the service account.
      3. Create a new folder as a last resort (prints a clear warning).
    """
    if folder_id:
        try:
            meta = service.files().get(
                fileId=folder_id, fields='id, name', **_ITEM_FLAGS,
            ).execute()
            print(f"📁 Using Drive folder: {meta['name']} (ID: {folder_id})")
            return folder_id
        except Exception as e:
            raise RuntimeError(
                f"Cannot access Drive folder ID '{folder_id}'.\n"
                f"Make sure it exists and is shared with the service account.\n"
                f"Detail: {e}"
            ) from e

    query = (
        f"name='{folder_name}' and "
        f"mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    results = service.files().list(
        q=query, fields='files(id, name)', corpora='user',
        pageSize=1000, **_LIST_FLAGS,
    ).execute()
    files = results.get('files', [])
    if files:
        fid = files[0]['id']
        print(f"📁 Using existing Drive folder: {folder_name} (ID: {fid})")
        return fid

    print(
        f"⚠️  No folder named '{folder_name}' found in Drive.\n"
        f"   Paste its ID into --setup under 'Drive Folder ID'."
    )
    folder = service.files().create(
        body={'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder'},
        fields='id', **_ITEM_FLAGS,
    ).execute()
    fid = folder['id']
    print(f"📁 Created new Drive folder: {folder_name} (ID: {fid})")
    return fid


# ── Folder path resolution (cached) ──────────────────────────────────────────

def get_or_create_folder_path(
    get_service: Callable, drive_root_id: str, folder_path: List[str]
) -> str:
    """
    Walk/create nested folders in Drive and return the leaf folder ID.

    Results are cached per-sync-cycle so that uploading N files in the same
    directory costs 1×depth API calls instead of N×depth.
    """
    if not folder_path:
        return drive_root_id

    full_key = _cache_key(drive_root_id, folder_path)
    with _folder_cache_lock:
        if full_key in _folder_cache:
            return _folder_cache[full_key]

    service   = get_service()
    parent_id = drive_root_id

    for depth, folder_name in enumerate(folder_path):
        partial_key = _cache_key(drive_root_id, folder_path[:depth + 1])

        with _folder_cache_lock:
            cached = _folder_cache.get(partial_key)
        if cached:
            parent_id = cached
            continue

        query = (
            f"name='{folder_name}' and '{parent_id}' in parents and "
            f"mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        results = service.files().list(
            q=query, fields='files(id, name)', pageSize=10, **_LIST_FLAGS,
        ).execute()
        files = results.get('files', [])

        if files:
            parent_id = files[0]['id']
        else:
            folder = service.files().create(
                body={
                    'name': folder_name,
                    'mimeType': 'application/vnd.google-apps.folder',
                    'parents': [parent_id],
                },
                fields='id', **_ITEM_FLAGS,
            ).execute()
            parent_id = folder['id']

        with _folder_cache_lock:
            _folder_cache[partial_key] = parent_id

    return parent_id


# ── Upload ────────────────────────────────────────────────────────────────────

def upload_file(
    get_service: Callable,
    drive_root_id: str,
    local_path: str,
    rel_path: str,
    folder_path: List[str],
    metadata: dict,
    metadata_lock,
    max_retries: int = 3,
):
    """Upload or update a single file in Google Drive with retry.

    Raises on final failure so the caller's future.result() surfaces the error
    and the failed-upload counter is accurate.
    """
    file_name = os.path.basename(local_path)
    parent_id = get_or_create_folder_path(get_service, drive_root_id, folder_path)

    for attempt in range(max_retries + 1):
        try:
            service = get_service()
            mtime   = os.path.getmtime(local_path)

            query = f"name='{file_name}' and '{parent_id}' in parents and trashed=false"
            existing = service.files().list(
                q=query, fields='files(id, name, modifiedTime)',
                pageSize=10, **_LIST_FLAGS,
            ).execute().get('files', [])

            media = MediaFileUpload(local_path, resumable=True)
            if existing:
                file = service.files().update(
                    fileId=existing[0]['id'], media_body=media,
                    fields='id, modifiedTime', **_ITEM_FLAGS,
                ).execute()
                print(f"📤 Updated: {rel_path}")
            else:
                file = service.files().create(
                    body={'name': file_name, 'parents': [parent_id]},
                    media_body=media, fields='id, modifiedTime', **_ITEM_FLAGS,
                ).execute()
                print(f"📤 Uploaded: {rel_path}")

            with metadata_lock:
                metadata['files'][rel_path] = {
                    'mtime': mtime,
                    'drive_id': file['id'],
                    'drive_mtime': file.get('modifiedTime'),
                }
            return  # success

        except _RETRYABLE as e:
            if attempt == max_retries:
                raise
            delay = (2 ** attempt) + random.uniform(0, 1)
            print(f"⚠️  Retrying upload {rel_path} in {delay:.1f}s "
                  f"(attempt {attempt + 1}/{max_retries}) — {e}")
            time.sleep(delay)
        # Non-retryable errors (e.g. permission denied) propagate immediately


# ── Download ──────────────────────────────────────────────────────────────────

def download_file(
    get_service: Callable,
    file_id: str,
    file_name: str,
    local_path: str,
    drive_mtime: str,
    metadata: dict,
    metadata_lock,
    local_folder: str,
    max_retries: int = 4,
):
    """Download a file from Google Drive with exponential-backoff retries."""
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    rel_path = os.path.relpath(local_path, local_folder)

    for attempt in range(max_retries + 1):
        try:
            service = get_service()
            request = service.files().get_media(fileId=file_id, **_ITEM_FLAGS)
            # Stream directly to disk — no BytesIO buffer, so memory usage is
            # constant regardless of file size (one chunk at a time ~10 MB).
            with open(local_path, 'wb') as fh:
                dl   = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = dl.next_chunk()

            print(f"📥 Downloaded: {rel_path}")
            mtime = os.path.getmtime(local_path)
            with metadata_lock:
                metadata['files'][rel_path] = {
                    'mtime': mtime,
                    'drive_id': file_id,
                    'drive_mtime': drive_mtime,
                }
            return

        except _RETRYABLE as e:
            if attempt == max_retries:
                print(f"❌ Error downloading {file_name} "
                      f"(gave up after {max_retries} retries): {e}")
                return
            delay = (2 ** attempt) + random.uniform(0, 1)
            print(f"⚠️  Retrying {file_name} in {delay:.1f}s "
                  f"(attempt {attempt + 1}/{max_retries}) — {e}")
            time.sleep(delay)

        except Exception as e:
            print(f"❌ Error downloading {file_name}: {e}")
            return


# ── Drive tree scan ───────────────────────────────────────────────────────────
#
# Old design: recursive scan_drive_files, each folder with subfolders created
# its own ThreadPoolExecutor.  On a tree with 80 folders that means ~800 thread
# creations/destructions (30-50ms each on Windows) plus sequential blocking at
# every level — easily 3-5 minutes for a large vault.
#
# New design: ONE executor for the whole tree.  Workers list a single folder and
# return (files, subfolders) to the MAIN thread.  The main thread merges files
# and submits subfolder jobs back into the same pool.  No nesting, no blocking
# per level, true breadth-first parallelism.

def _list_one_folder(
    get_service: Callable,
    folder_id: str,
    prefix: str,
    max_retries: int = 3,
) -> Tuple[Dict[str, Dict], List[Tuple[str, str]]]:
    """
    List the IMMEDIATE children of one Drive folder.
    Returns (files_dict, subfolders_list).
    Retries on transient network errors with exponential backoff.
    """
    for attempt in range(max_retries + 1):
        try:
            files:      Dict[str, Dict]         = {}
            subfolders: List[Tuple[str, str]]   = []
            page_token = None
            service    = get_service()

            while True:
                resp = service.files().list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    fields='nextPageToken, files(id, name, mimeType, modifiedTime)',
                    pageSize=1000,
                    pageToken=page_token,
                    **_LIST_FLAGS,
                ).execute()

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

            return files, subfolders

        except _RETRYABLE as e:
            if attempt == max_retries:
                raise
            delay = (2 ** attempt) + random.uniform(0, 1)
            print(f"⚠️  Retrying scan of '{prefix}' in {delay:.1f}s "
                  f"(attempt {attempt + 1}/{max_retries}) — {e}")
            time.sleep(delay)


def scan_drive_files(
    get_service: Callable,
    root_id: str,
    prefix: str = '',
    max_workers: int = 10,
) -> Dict[str, Dict]:
    """
    Scan the entire Drive folder tree under root_id.

    Uses a SINGLE ThreadPoolExecutor.  Workers list one folder each and return
    results to the main thread, which merges files and submits new subfolder
    jobs.  No nested pools, no per-level blocking.
    """
    all_files: Dict[str, Dict] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # pending is a set of Future objects; the main thread drives all work
        pending = {executor.submit(_list_one_folder, get_service, root_id, prefix)}

        while pending:
            done, pending = _futures_wait(pending, return_when=FIRST_COMPLETED)

            for future in done:
                try:
                    files, subfolders = future.result()
                    all_files.update(files)
                    for fid, fpath in subfolders:
                        pending.add(
                            executor.submit(_list_one_folder, get_service, fid, fpath)
                        )
                except Exception as e:
                    print(f"❌ Error scanning folder: {e}")

    return all_files
