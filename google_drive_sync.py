import os
import json
import time
import pickle
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Set
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
import io

# --- CONFIGURATION ---
LOCAL_FOLDER = "/home/aritrarc1/GDrive"
SCOPES = ['https://www.googleapis.com/auth/drive']
CREDS_FILE = 'secrets/client_secret_116989766183-v0e50u650rdhah0j933fsloke77hp1od.apps.googleusercontent.com.json'
TOKEN_FILE = 'token.pickle'
METADATA_FILE = 'sync_metadata.json'
DRIVE_FOLDER_NAME = 'GDriveSync'  # Root folder name in Google Drive

class GoogleDriveSync:
    """Bidirectional Google Drive sync with interval-based syncing."""
    
    def __init__(self, sync_interval: int = 300):
        """
        Initialize the sync service.
        
        Args:
            sync_interval: Time in seconds between sync operations (default: 300 = 5 minutes)
        """
        self.sync_interval = sync_interval
        self.service = self.authenticate()
        self.drive_root_id = self._get_or_create_drive_folder()
        self.metadata = self._load_metadata()
        
        # Ensure local folder exists
        os.makedirs(LOCAL_FOLDER, exist_ok=True)
        
    def authenticate(self):
        """Authenticate with Google Drive using OAuth."""
        creds = None
        
        # Load existing token if it exists
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, 'rb') as token:
                creds = pickle.load(token)
        
        # Refresh or create new token
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                print("🔄 Refreshing authentication token...")
                creds.refresh(Request())
            else:
                if not os.path.exists(CREDS_FILE):
                    raise FileNotFoundError(
                        f"Credentials file not found: {CREDS_FILE}\n"
                        "Please ensure the OAuth client secret file is in the secrets/ folder."
                    )
                print("🔐 Starting authentication flow...")
                flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)
            
            # Save the token for next time
            with open(TOKEN_FILE, 'wb') as token:
                pickle.dump(creds, token)
            print("✅ Authentication successful!")
        
        return build('drive', 'v3', credentials=creds)
    
    def _get_or_create_drive_folder(self) -> str:
        """Get or create the root sync folder in Google Drive."""
        # Search for existing folder
        query = f"name='{DRIVE_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        results = self.service.files().list(
            q=query,
            spaces='drive',
            fields='files(id, name)'
        ).execute()
        
        files = results.get('files', [])
        
        if files:
            folder_id = files[0]['id']
            print(f"📁 Using existing Drive folder: {DRIVE_FOLDER_NAME} (ID: {folder_id})")
            return folder_id
        
        # Create new folder
        file_metadata = {
            'name': DRIVE_FOLDER_NAME,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        folder = self.service.files().create(
            body=file_metadata,
            fields='id'
        ).execute()
        folder_id = folder['id']
        print(f"📁 Created new Drive folder: {DRIVE_FOLDER_NAME} (ID: {folder_id})")
        return folder_id
    
    def _load_metadata(self) -> Dict:
        """Load sync metadata from file."""
        if os.path.exists(METADATA_FILE):
            try:
                with open(METADATA_FILE, 'r') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                print("⚠️  Metadata file corrupted, starting fresh")
                return {'files': {}, 'drive_files': {}}
        return {'files': {}, 'drive_files': {}}
    
    def _save_metadata(self):
        """Save sync metadata to file."""
        with open(METADATA_FILE, 'w') as f:
            json.dump(self.metadata, f, indent=2)
    
    def _get_relative_path(self, full_path: str) -> str:
        """Get path relative to LOCAL_FOLDER."""
        return os.path.relpath(full_path, LOCAL_FOLDER)
    
    def _get_drive_path(self, local_rel_path: str) -> List[str]:
        """Convert local relative path to Drive folder hierarchy."""
        if local_rel_path == '.':
            return []
        parts = Path(local_rel_path).parts
        return list(parts[:-1])  # Exclude filename
    
    def _get_or_create_drive_folder_path(self, folder_path: List[str]) -> str:
        """Get or create nested folders in Drive, return final folder ID."""
        parent_id = self.drive_root_id
        
        for folder_name in folder_path:
            # Search for folder
            query = f"name='{folder_name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
            results = self.service.files().list(
                q=query,
                spaces='drive',
                fields='files(id, name)'
            ).execute()
            
            files = results.get('files', [])
            
            if files:
                parent_id = files[0]['id']
            else:
                # Create folder
                file_metadata = {
                    'name': folder_name,
                    'mimeType': 'application/vnd.google-apps.folder',
                    'parents': [parent_id]
                }
                folder = self.service.files().create(
                    body=file_metadata,
                    fields='id'
                ).execute()
                parent_id = folder['id']
        
        return parent_id
    
    def _upload_file(self, local_path: str):
        """Upload a file to Google Drive."""
        rel_path = self._get_relative_path(local_path)
        file_name = os.path.basename(local_path)
        
        try:
            # Get modification time
            mtime = os.path.getmtime(local_path)
            
            # Get or create parent folder in Drive
            folder_path = self._get_drive_path(rel_path)
            parent_id = self._get_or_create_drive_folder_path(folder_path)
            
            # Check if file already exists in Drive
            query = f"name='{file_name}' and '{parent_id}' in parents and trashed=false"
            results = self.service.files().list(
                q=query,
                spaces='drive',
                fields='files(id, name, modifiedTime)'
            ).execute()
            
            files = results.get('files', [])
            
            file_metadata = {'name': file_name, 'parents': [parent_id]}
            media = MediaFileUpload(local_path, resumable=True)
            
            if files:
                # Update existing file
                file_id = files[0]['id']
                file = self.service.files().update(
                    fileId=file_id,
                    media_body=media,
                    fields='id, modifiedTime'
                ).execute()
                print(f"📤 Updated: {rel_path}")
            else:
                # Create new file
                file = self.service.files().create(
                    body=file_metadata,
                    media_body=media,
                    fields='id, modifiedTime'
                ).execute()
                print(f"📤 Uploaded: {rel_path}")
            
            # Update metadata
            self.metadata['files'][rel_path] = {
                'mtime': mtime,
                'drive_id': file['id'],
                'drive_mtime': file.get('modifiedTime')
            }
            
        except Exception as e:
            print(f"❌ Error uploading {rel_path}: {e}")
    
    def _download_file(self, file_id: str, file_name: str, local_path: str, drive_mtime: str):
        """Download a file from Google Drive."""
        try:
            request = self.service.files().get_media(fileId=file_id)
            
            # Ensure parent directory exists
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            
            # Download file
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
            
            # Write to file and close immediately
            with open(local_path, 'wb') as f:
                f.write(fh.getvalue())
            
            rel_path = self._get_relative_path(local_path)
            print(f"📥 Downloaded: {rel_path}")
            
            # Update metadata
            mtime = os.path.getmtime(local_path)
            self.metadata['files'][rel_path] = {
                'mtime': mtime,
                'drive_id': file_id,
                'drive_mtime': drive_mtime
            }
            
        except Exception as e:
            print(f"❌ Error downloading {file_name}: {e}")
    
    def _scan_local_files(self) -> Set[str]:
        """Scan local folder and return set of relative paths."""
        local_files = set()
        for root, dirs, files in os.walk(LOCAL_FOLDER):
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = self._get_relative_path(full_path)
                local_files.add(rel_path)
        return local_files
    
    def _scan_drive_files(self, folder_id: str = None, prefix: str = '') -> Dict[str, Dict]:
        """Recursively scan Drive folder and return dict of files with metadata."""
        if folder_id is None:
            folder_id = self.drive_root_id
        
        drive_files = {}
        page_token = None
        
        while True:
            query = f"'{folder_id}' in parents and trashed=false"
            results = self.service.files().list(
                q=query,
                spaces='drive',
                fields='nextPageToken, files(id, name, mimeType, modifiedTime)',
                pageToken=page_token
            ).execute()
            
            for file in results.get('files', []):
                file_name = file['name']
                file_path = os.path.join(prefix, file_name) if prefix else file_name
                
                if file['mimeType'] == 'application/vnd.google-apps.folder':
                    # Recursively scan subfolder
                    subfolder_files = self._scan_drive_files(file['id'], file_path)
                    drive_files.update(subfolder_files)
                else:
                    drive_files[file_path] = {
                        'id': file['id'],
                        'mtime': file.get('modifiedTime'),
                        'name': file_name
                    }
            
            page_token = results.get('nextPageToken')
            if not page_token:
                break
        
        return drive_files
    
    def sync_up(self):
        """Upload local changes to Google Drive."""
        print("\n🔼 Checking for local changes to upload...")
        local_files = self._scan_local_files()
        uploaded = 0
        
        for rel_path in local_files:
            full_path = os.path.join(LOCAL_FOLDER, rel_path)
            mtime = os.path.getmtime(full_path)
            
            # Check if file needs upload
            if rel_path not in self.metadata['files']:
                # New file
                self._upload_file(full_path)
                uploaded += 1
            else:
                # Check if modified
                stored_mtime = self.metadata['files'][rel_path].get('mtime', 0)
                if mtime > stored_mtime:
                    self._upload_file(full_path)
                    uploaded += 1
        
        if uploaded > 0:
            print(f"✅ Uploaded {uploaded} file(s)")
        else:
            print("✅ No local changes to upload")
    
    def sync_down(self):
        """Download changes from Google Drive."""
        print("\n🔽 Checking for Drive changes to download...")
        drive_files = self._scan_drive_files()
        downloaded = 0
        
        for rel_path, file_info in drive_files.items():
            local_path = os.path.join(LOCAL_FOLDER, rel_path)
            
            # Check if file needs download
            if not os.path.exists(local_path):
                # New file from Drive
                self._download_file(
                    file_info['id'],
                    file_info['name'],
                    local_path,
                    file_info['mtime']
                )
                downloaded += 1
            else:
                # Check if Drive version is newer
                if rel_path in self.metadata['files']:
                    stored_drive_mtime = self.metadata['files'][rel_path].get('drive_mtime')
                    if file_info['mtime'] != stored_drive_mtime:
                        # Drive file was modified
                        local_mtime = os.path.getmtime(local_path)
                        stored_local_mtime = self.metadata['files'][rel_path].get('mtime', 0)
                        
                        # Only download if local file hasn't changed
                        if local_mtime == stored_local_mtime:
                            self._download_file(
                                file_info['id'],
                                file_info['name'],
                                local_path,
                                file_info['mtime']
                            )
                            downloaded += 1
                        else:
                            print(f"⚠️  Conflict detected: {rel_path} (keeping local version)")
        
        if downloaded > 0:
            print(f"✅ Downloaded {downloaded} file(s)")
        else:
            print("✅ No Drive changes to download")
    
    def sync(self):
        """Perform a full bidirectional sync."""
        print(f"\n{'='*60}")
        print(f"🔄 Starting sync at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}")
        
        try:
            self.sync_up()
            self.sync_down()
            self._save_metadata()
            print(f"\n✅ Sync completed successfully!")
        except Exception as e:
            print(f"\n❌ Sync failed: {e}")
    
    def run(self):
        """Run continuous sync loop."""
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
            self._save_metadata()
