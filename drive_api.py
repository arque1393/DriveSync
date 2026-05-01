"""drive_api.py — Async Google Drive REST client.

Replaces google-api-python-client (synchronous httplib2) with aiohttp so
every network call becomes a non-blocking coroutine.

Why this exists
───────────────
google-api-python-client uses httplib2 under the hood, which is a blocking
HTTP library.  Wrapping it in asyncio.to_thread() would give us async syntax
but no async benefit — the OS thread still blocks.  By calling the Drive REST
API directly through aiohttp we get true non-blocking I/O:

    Thread model:   10 threads × 400 ms/call  =  ~4 s (sequential per thread)
    Async model:   30 coroutines × 400 ms/call =  ~400 ms  (all in flight at once)

Authentication uses google-auth (not google-api-python-client), which is
compatible with asyncio.  Token refresh is sync, so it runs in a thread via
asyncio.to_thread().

Usage
─────
    async with DriveSession(creds) as ds:
        result = await ds.list_files(q="...", fields="...")
        await ds.download(file_id, "/local/path/file.pdf")
        info   = await ds.upload("/local/file.pdf", "file.pdf", parent_id)
"""

import asyncio
import json
import os
from typing import Any, Dict, List, Optional

import aiofiles
import aiofiles.os
import aiohttp
from google.auth.transport.requests import Request

# ── Drive REST endpoints ──────────────────────────────────────────────────────
_DRIVE_BASE  = 'https://www.googleapis.com/drive/v3'
_UPLOAD_BASE = 'https://www.googleapis.com/upload/drive/v3'

# Chunk size for resumable uploads and streaming downloads (10 MB)
_CHUNK = 10 * 1024 * 1024

# Files under this threshold use the simpler multipart upload (5 MB)
_SMALL_FILE = 5 * 1024 * 1024

# Params required on every files().list call to see shared-with-me content
_LIST_PARAMS = {'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true'}
# Params required on get / create / update / download
_ITEM_PARAMS = {'supportsAllDrives': 'true'}


class DriveSession:
    """
    Async context manager: one aiohttp.ClientSession + credential lifecycle.

        async with DriveSession(creds) as ds:
            data = await ds.list_files(q='...', fields='...')
    """

    def __init__(self, creds) -> None:
        self._creds   = creds
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock    = asyncio.Lock()   # serialises token refresh

    async def __aenter__(self) -> 'DriveSession':
        # limit=100 allows up to 100 concurrent connections to googleapis.com
        # ttl_dns_cache=300 caches DNS for 5 min — avoids repeated lookups
        connector     = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
        self._session = aiohttp.ClientSession(connector=connector)
        await self._ensure_token()
        return self

    async def __aexit__(self, *_) -> None:
        if self._session:
            await self._session.close()

    # ── Auth ──────────────────────────────────────────────────────────────────

    async def _ensure_token(self) -> None:
        """Refresh OAuth token if expired. Sync google-auth runs in a thread."""
        async with self._lock:
            if not self._creds.valid:
                await asyncio.to_thread(self._creds.refresh, Request())

    async def _headers(self) -> Dict[str, str]:
        await self._ensure_token()
        return {'Authorization': f'Bearer {self._creds.token}'}

    # ── Read operations ───────────────────────────────────────────────────────

    async def list_files(
        self,
        q: str,
        fields: str,
        page_size: int = 1000,
        page_token: Optional[str] = None,
        corpora: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, str] = {
            **_LIST_PARAMS,
            'q': q,
            'fields': fields,
            'pageSize': str(page_size),
        }
        if page_token:
            params['pageToken'] = page_token
        if corpora:
            params['corpora'] = corpora

        async with self._session.get(
            f'{_DRIVE_BASE}/files',
            headers=await self._headers(),
            params=params,
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_file(self, file_id: str, fields: str = 'id,name,mimeType') -> Dict:
        async with self._session.get(
            f'{_DRIVE_BASE}/files/{file_id}',
            headers=await self._headers(),
            params={**_ITEM_PARAMS, 'fields': fields},
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    # ── Folder creation ───────────────────────────────────────────────────────

    async def create_folder(self, name: str, parent_id: Optional[str] = None) -> str:
        """Create a Drive folder and return its ID."""
        body: Dict[str, Any] = {
            'name': name,
            'mimeType': 'application/vnd.google-apps.folder',
        }
        if parent_id:
            body['parents'] = [parent_id]

        async with self._session.post(
            f'{_DRIVE_BASE}/files',
            headers={**await self._headers(), 'Content-Type': 'application/json'},
            params={**_ITEM_PARAMS, 'fields': 'id'},
            data=json.dumps(body),
        ) as resp:
            resp.raise_for_status()
            return (await resp.json())['id']

    # ── Download ──────────────────────────────────────────────────────────────

    async def download(self, file_id: str, local_path: str) -> None:
        """Stream a Drive file to disk in 10 MB chunks — O(1) memory usage."""
        await aiofiles.os.makedirs(os.path.dirname(local_path) or '.', exist_ok=True)
        async with self._session.get(
            f'{_DRIVE_BASE}/files/{file_id}',
            headers=await self._headers(),
            params={**_ITEM_PARAMS, 'alt': 'media'},
        ) as resp:
            resp.raise_for_status()
            async with aiofiles.open(local_path, 'wb') as fh:
                async for chunk in resp.content.iter_chunked(_CHUNK):
                    await fh.write(chunk)

    # ── Upload ────────────────────────────────────────────────────────────────

    async def upload(
        self,
        local_path: str,
        file_name: str,
        parent_id: str,
        existing_id: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        Upload or update a local file, returning {'id': ..., 'modifiedTime': ...}.

        Routing:
          < 5 MB  →  multipart upload    (single request, no session setup)
          ≥ 5 MB  →  resumable upload    (chunked, recoverable on network error)
        """
        size = (await aiofiles.os.stat(local_path)).st_size
        if size < _SMALL_FILE:
            return await self._upload_multipart(local_path, file_name, parent_id, existing_id)
        return await self._upload_resumable(local_path, file_name, parent_id, existing_id, size)

    async def _upload_multipart(
        self,
        local_path: str,
        file_name: str,
        parent_id: str,
        existing_id: Optional[str],
    ) -> Dict:
        metadata: Dict[str, Any] = {'name': file_name}
        if not existing_id:
            metadata['parents'] = [parent_id]

        async with aiofiles.open(local_path, 'rb') as fh:
            content = await fh.read()

        # multipart/related — Drive requires this exact subtype
        mp = aiohttp.MultipartWriter('related')
        mp.append_json(metadata)
        mp.append(content, {'Content-Type': 'application/octet-stream'})

        params = {**_ITEM_PARAMS, 'uploadType': 'multipart', 'fields': 'id,modifiedTime'}

        if existing_id:
            ctx = self._session.patch(
                f'{_UPLOAD_BASE}/files/{existing_id}',
                headers=await self._headers(), params=params, data=mp,
            )
        else:
            ctx = self._session.post(
                f'{_UPLOAD_BASE}/files',
                headers=await self._headers(), params=params, data=mp,
            )
        async with ctx as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _upload_resumable(
        self,
        local_path: str,
        file_name: str,
        parent_id: str,
        existing_id: Optional[str],
        file_size: int,
    ) -> Dict:
        metadata: Dict[str, Any] = {'name': file_name}
        if not existing_id:
            metadata['parents'] = [parent_id]

        # ── Step 1: initiate a resumable upload session ───────────────────────
        init_headers = {
            **await self._headers(),
            'Content-Type': 'application/json; charset=UTF-8',
            'X-Upload-Content-Type': 'application/octet-stream',
            'X-Upload-Content-Length': str(file_size),
        }
        params  = {**_ITEM_PARAMS, 'uploadType': 'resumable', 'fields': 'id,modifiedTime'}
        method  = self._session.patch if existing_id else self._session.post
        url     = (f'{_UPLOAD_BASE}/files/{existing_id}' if existing_id
                   else f'{_UPLOAD_BASE}/files')

        async with method(url, headers=init_headers, params=params,
                          data=json.dumps(metadata)) as resp:
            resp.raise_for_status()
            session_url = resp.headers['Location']

        # ── Step 2: stream file in chunks ─────────────────────────────────────
        uploaded = 0
        async with aiofiles.open(local_path, 'rb') as fh:
            while uploaded < file_size:
                await fh.seek(uploaded)
                chunk = await fh.read(_CHUNK)
                if not chunk:
                    break

                end = uploaded + len(chunk) - 1
                async with self._session.put(
                    session_url,
                    headers={
                        'Content-Range': f'bytes {uploaded}-{end}/{file_size}',
                        'Content-Length': str(len(chunk)),
                    },
                    data=chunk,
                ) as resp:
                    if resp.status in (200, 201):
                        return await resp.json()
                    if resp.status != 308:     # 308 = Resume Incomplete (expected)
                        resp.raise_for_status()
                    # Server confirms how many bytes it received
                    rng = resp.headers.get('Range', '')
                    uploaded = int(rng.split('-')[1]) + 1 if rng else uploaded + len(chunk)

        raise RuntimeError(f'Resumable upload for {local_path!r} completed without final response')
