import os
import io
from typing import Callable, Dict, List
from concurrent.futures import ThreadPoolExecutor, as_completed

from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload


def get_or_create_root_folder(service, folder_name: str) -> str:
    """Get or create the root sync folder in Google Drive, return its ID."""
    query = (
        f"name='{folder_name}' and "
        f"mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    results = service.files().list(
        q=query, spaces='drive', fields='files(id, name)'
    ).execute()

    files = results.get('files', [])
    if files:
        folder_id = files[0]['id']
        print(f"📁 Using existing Drive folder: {folder_name} (ID: {folder_id})")
        return folder_id

    folder = service.files().create(
        body={'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder'},
        fields='id'
    ).execute()
    folder_id = folder['id']
    print(f"📁 Created new Drive folder: {folder_name} (ID: {folder_id})")
    return folder_id


def get_or_create_folder_path(
    get_service: Callable, drive_root_id: str, folder_path: List[str]
) -> str:
    """Walk/create nested folders in Drive and return the leaf folder ID."""
    service = get_service()
    parent_id = drive_root_id

    for folder_name in folder_path:
        query = (
            f"name='{folder_name}' and '{parent_id}' in parents and "
            f"mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        results = service.files().list(
            q=query, spaces='drive', fields='files(id, name)'
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
                fields='id'
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
            q=query, spaces='drive', fields='files(id, name, modifiedTime)'
        ).execute()
        existing = results.get('files', [])

        media = MediaFileUpload(local_path, resumable=True)
        if existing:
            file = service.files().update(
                fileId=existing[0]['id'],
                media_body=media,
                fields='id, modifiedTime'
            ).execute()
            print(f"📤 Updated: {rel_path}")
        else:
            file = service.files().create(
                body={'name': file_name, 'parents': [parent_id]},
                media_body=media,
                fields='id, modifiedTime'
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
):
    """Download a single file from Google Drive."""
    try:
        service = get_service()
        request = service.files().get_media(fileId=file_id)

        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        with open(local_path, 'wb') as f:
            f.write(fh.getvalue())

        rel_path = os.path.relpath(local_path, local_folder)
        print(f"📥 Downloaded: {rel_path}")

        mtime = os.path.getmtime(local_path)
        with metadata_lock:
            metadata['files'][rel_path] = {
                'mtime': mtime,
                'drive_id': file_id,
                'drive_mtime': drive_mtime,
            }

    except Exception as e:
        print(f"❌ Error downloading {file_name}: {e}")


def scan_drive_files(
    get_service: Callable,
    folder_id: str,
    prefix: str = '',
    max_workers: int = 10,
) -> Dict[str, Dict]:
    """Recursively list all files under a Drive folder.

    Subfolders are scanned in parallel; returns a flat dict keyed by relative path.
    """
    drive_files: Dict[str, Dict] = {}
    subfolders: List = []
    page_token = None
    service = get_service()

    while True:
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            spaces='drive',
            fields='nextPageToken, files(id, name, mimeType, modifiedTime)',
            pageToken=page_token,
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
                executor.submit(scan_drive_files, get_service, fid, fpath, max_workers): fpath
                for fid, fpath in subfolders
            }
            for future in as_completed(futures):
                try:
                    drive_files.update(future.result())
                except Exception as e:
                    print(f"❌ Error scanning subfolder {futures[future]}: {e}")

    return drive_files