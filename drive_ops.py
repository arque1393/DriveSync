import os
import io
import ssl
import time
import random
import socket
from http.client import IncompleteRead
from typing import Callable, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# Transient network errors worth retrying.
# ssl.SSLError covers certificate errors that go away once truststore is injected
# and intermittent TLS handshake resets from the corporate proxy.
_RETRYABLE = (IncompleteRead, ConnectionResetError, TimeoutError,
              socket.gaierror, OSError, EOFError, ssl.SSLError)

# files().list() accepts both flags.
# files().get / .create / .update / .get_media only accept supportsAllDrives.
_LIST_FLAGS = dict(supportsAllDrives=True, includeItemsFromAllDrives=True)
_ITEM_FLAGS = dict(supportsAllDrives=True)


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
    # ── 1. Direct ID (most reliable) ─────────────────────────────────────────
    if folder_id:
        try:
            meta = service.files().get(
                fileId=folder_id,
                fields='id, name',
                **_ITEM_FLAGS,
            ).execute()
            print(f"📁 Using Drive folder: {meta['name']} (ID: {folder_id})")
            return folder_id
        except Exception as e:
            raise RuntimeError(
                f"Cannot access Drive folder ID '{folder_id}'.\n"
                f"Make sure the folder exists and is shared with the service account.\n"
                f"Detail: {e}"
            ) from e

    # ── 2. Search by name (owned + shared-with-me) ───────────────────────────
    query = (
        f"name='{folder_name}' and "
        f"mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    results = service.files().list(
        q=query,
        fields='files(id, name)',
        corpora='user',
        **_LIST_FLAGS,
    ).execute()

    files = results.get('files', [])
    if files:
        fid = files[0]['id']
        print(f"📁 Using existing Drive folder: {folder_name} (ID: {fid})")
        return fid

    # ── 3. Create (last resort — warn loudly) ────────────────────────────────
    print(
        f"⚠️  No folder named '{folder_name}' found in Drive.\n"
        f"   If your data is in a folder shared with the service account,\n"
        f"   paste its ID into the setup GUI (--setup) under 'Drive Folder ID'."
    )
    folder = service.files().create(
        body={'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder'},
        fields='id',
        **_ITEM_FLAGS,
    ).execute()
    fid = folder['id']
    print(f"📁 Created new Drive folder: {folder_name} (ID: {fid})")
    return fid


def get_or_create_folder_path(
    get_service: Callable, drive_root_id: str, folder_path: List[str]
) -> str:
    """Walk/create nested folders in Drive, return the leaf folder ID."""
    service = get_service()
    parent_id = drive_root_id

    for folder_name in folder_path:
        query = (
            f"name='{folder_name}' and '{parent_id}' in parents and "
            f"mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        results = service.files().list(
            q=query,
            fields='files(id, name)',
            **_LIST_FLAGS,
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
                fields='id',
                **_ITEM_FLAGS,
            ).execute()
            parent_id = folder['id']

    return parent_id


def upload_file(
    get_service: Callable,
    drive_root_id: str,
    local_path: str,
    rel_path: str,
    folder_path: List[str],
    metadata: dict,
    metadata_lock,
):
    """Upload or update a single file in Google Drive."""
    file_name = os.path.basename(local_path)
    try:
        service = get_service()
        mtime = os.path.getmtime(local_path)
        parent_id = get_or_create_folder_path(get_service, drive_root_id, folder_path)

        query = f"name='{file_name}' and '{parent_id}' in parents and trashed=false"
        results = service.files().list(
            q=query,
            fields='files(id, name, modifiedTime)',
            **_LIST_FLAGS,
        ).execute()
        existing = results.get('files', [])

        media = MediaFileUpload(local_path, resumable=True)
        if existing:
            file = service.files().update(
                fileId=existing[0]['id'],
                media_body=media,
                fields='id, modifiedTime',
                **_ITEM_FLAGS,
            ).execute()
            print(f"📤 Updated: {rel_path}")
        else:
            file = service.files().create(
                body={'name': file_name, 'parents': [parent_id]},
                media_body=media,
                fields='id, modifiedTime',
                **_ITEM_FLAGS,
            ).execute()
            print(f"📤 Uploaded: {rel_path}")

        with metadata_lock:
            metadata['files'][rel_path] = {
                'mtime': mtime,
                'drive_id': file['id'],
                'drive_mtime': file.get('modifiedTime'),
            }

    except Exception as e:
        print(f"❌ Error uploading {rel_path}: {e}")


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
    """
    Download a single file from Google Drive with exponential-backoff retries.

    Retries on transient network errors (IncompleteRead, DNS failure, SSL reset).
    Gives up after max_retries attempts and prints a final error.
    """
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    rel_path = os.path.relpath(local_path, local_folder)

    for attempt in range(max_retries + 1):
        try:
            service = get_service()
            request = service.files().get_media(fileId=file_id, **_ITEM_FLAGS)

            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

            with open(local_path, 'wb') as f:
                f.write(fh.getvalue())

            print(f"📥 Downloaded: {rel_path}")

            mtime = os.path.getmtime(local_path)
            with metadata_lock:
                metadata['files'][rel_path] = {
                    'mtime': mtime,
                    'drive_id': file_id,
                    'drive_mtime': drive_mtime,
                }
            return  # success

        except _RETRYABLE as e:
            if attempt == max_retries:
                print(f"❌ Error downloading {file_name} (gave up after {max_retries} retries): {e}")
                return
            # Exponential backoff: 2s, 4s, 8s, 16s … plus a small random jitter
            delay = (2 ** attempt) + random.uniform(0, 1)
            print(f"⚠️  Retrying {file_name} in {delay:.1f}s "
                  f"(attempt {attempt + 1}/{max_retries}) — {e}")
            time.sleep(delay)

        except Exception as e:
            # Non-retryable error (e.g. permission denied) — fail immediately
            print(f"❌ Error downloading {file_name}: {e}")
            return


def scan_drive_files(
    get_service: Callable,
    folder_id: str,
    prefix: str = '',
    max_workers: int = 10,
) -> Dict[str, Dict]:
    """
    Recursively list all files under a Drive folder.
    Returns a flat dict keyed by relative path.
    Subfolders are scanned in parallel.
    """
    drive_files: Dict[str, Dict] = {}
    subfolders: List = []
    page_token = None
    service = get_service()

    while True:
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields='nextPageToken, files(id, name, mimeType, modifiedTime)',
            pageToken=page_token,
            **_LIST_FLAGS,
        ).execute()

        for file in results.get('files', []):
            file_name = file['name']
            file_path = os.path.join(prefix, file_name) if prefix else file_name

            if file['mimeType'] == 'application/vnd.google-apps.folder':
                subfolders.append((file['id'], file_path))
            else:
                drive_files[file_path] = {
                    'id': file['id'],
                    'mtime': file.get('modifiedTime'),
                    'name': file_name,
                }

        page_token = results.get('nextPageToken')
        if not page_token:
            break

    if subfolders:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _scan_with_retry, get_service, fid, fpath, max_workers
                ): fpath
                for fid, fpath in subfolders
            }
            for future in as_completed(futures):
                try:
                    drive_files.update(future.result())
                except Exception as e:
                    print(f"❌ Error scanning subfolder {futures[future]}: {e}")

    return drive_files


def _scan_with_retry(
    get_service: Callable,
    folder_id: str,
    prefix: str,
    max_workers: int,
    max_retries: int = 3,
) -> Dict[str, Dict]:
    """scan_drive_files with exponential-backoff retries on transient errors."""
    for attempt in range(max_retries + 1):
        try:
            return scan_drive_files(get_service, folder_id, prefix, max_workers)
        except _RETRYABLE as e:
            if attempt == max_retries:
                raise
            delay = (2 ** attempt) + random.uniform(0, 1)
            print(f"⚠️  Retrying scan of '{prefix}' in {delay:.1f}s "
                  f"(attempt {attempt + 1}/{max_retries}) — {e}")
            time.sleep(delay)
