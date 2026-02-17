# Google Drive Sync

A bidirectional sync tool that synchronizes a local folder with Google Drive at regular intervals.

## Features

✅ **Bidirectional Sync**: Automatically uploads local changes and downloads Drive changes  
✅ **Interval-based**: Syncs at configurable intervals (default: 5 minutes)  
✅ **No Resource Locking**: Files are only opened during sync operations, not held continuously  
✅ **Folder Structure**: Preserves directory hierarchy in Google Drive  
✅ **Smart Sync**: Tracks file metadata to avoid unnecessary re-uploads  
✅ **OAuth Authentication**: Secure authentication with Google Drive API  

## Setup

### 1. Prerequisites

- Python 3.14+
- Google Cloud Project with Drive API enabled
- OAuth 2.0 credentials (already in `secrets/` folder)

### 2. Install Dependencies

```bash
# Using uv (recommended)
uv sync

# Or using pip
pip install -r requirements.txt
```

### 3. Create Local Sync Folder

```bash
sudo mkdir -p /usr/home/GDrive
sudo chown $USER:$USER /usr/home/GDrive
```

### 4. First Run - Authentication

On first run, the program will open a browser for OAuth authentication:

```bash
python main.py
```

Follow the prompts to authorize the application. The token will be saved for future use.

## Usage

### Continuous Sync (Default)

Sync every 5 minutes (default):
```bash
python main.py
```

Custom sync interval (e.g., every 2 minutes = 120 seconds):
```bash
python main.py --interval 120
```

### Single Sync (Testing)

Run sync once and exit:
```bash
python main.py --sync-once
```

### Stop Syncing

Press `Ctrl+C` to stop the sync process gracefully.

## How It Works

1. **Upload Phase**: Scans local folder for new/modified files and uploads to Drive
2. **Download Phase**: Scans Drive folder for new/modified files and downloads locally
3. **Metadata Tracking**: Stores file modification times in `sync_metadata.json` to avoid re-syncing
4. **Conflict Resolution**: If both local and Drive versions are modified, keeps the local version

## Configuration

Edit `google_drive_sync.py` to customize:

- `LOCAL_FOLDER`: Local directory to sync (default: `/usr/home/GDrive`)
- `DRIVE_FOLDER_NAME`: Google Drive folder name (default: `GDriveSync`)
- `SCOPES`: Google Drive API scopes

## File Structure

```
DriveSync/
├── google_drive_sync.py    # Main sync logic
├── main.py                  # Entry point with CLI
├── secrets/                 # OAuth credentials (gitignored)
│   └── client_secret_*.json
├── token.pickle             # Auth token (auto-generated, gitignored)
├── sync_metadata.json       # Sync state (auto-generated, gitignored)
└── pyproject.toml           # Dependencies
```

## Troubleshooting

### Authentication Issues

If you get authentication errors:
1. Delete `token.pickle`
2. Run `python main.py` again to re-authenticate

### Permission Errors

If you can't access `/usr/home/GDrive`:
```bash
sudo chown -R $USER:$USER /usr/home/GDrive
```

### Sync Not Working

Check that:
1. OAuth credentials are in `secrets/` folder
2. Local folder exists and is writable
3. Internet connection is active

## Security Notes

⚠️ **Never commit these files**:
- `token.pickle` - Contains your authentication token
- `sync_metadata.json` - Contains sync state
- `secrets/` folder - Contains OAuth credentials

These are already in `.gitignore`.

## License

MIT License - Feel free to use and modify as needed.
