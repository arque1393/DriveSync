import json
import os

USER_CONFIG_FILE = 'user_config.json'

SCOPES               = ['https://www.googleapis.com/auth/drive']
SERVICE_ACCOUNT_FILE = 'secrets/mydesktopsync-ecd639c07433.json'
METADATA_FILE        = 'sync_metadata.json'

# ── Defaults (overridden by user_config.json when present) ───────────────────
_DEFAULTS = {
    'local_folder':      '/home/aritrarc1/GDrive',
    'drive_folder_name': 'Obsidian',
    'drive_folder_id':   '',
    'sync_interval':     300,
    'scan_workers':      10,   # parallel threads for folder scanning (lightweight)
    'download_workers':  3,    # parallel threads for file downloads (heavy — keep low)
    'download_retries':  4,    # how many times to retry a failed download
}


def _load_user_config() -> dict:
    if os.path.exists(USER_CONFIG_FILE):
        try:
            with open(USER_CONFIG_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


_user = _load_user_config()

LOCAL_FOLDER      = _user.get('local_folder',      _DEFAULTS['local_folder'])
DRIVE_FOLDER_NAME = _user.get('drive_folder_name', _DEFAULTS['drive_folder_name'])
DRIVE_FOLDER_ID   = _user.get('drive_folder_id',   _DEFAULTS['drive_folder_id'])
SYNC_INTERVAL     = _user.get('sync_interval',     _DEFAULTS['sync_interval'])
SCAN_WORKERS      = _user.get('scan_workers',      _DEFAULTS['scan_workers'])
DOWNLOAD_WORKERS  = _user.get('download_workers',  _DEFAULTS['download_workers'])
DOWNLOAD_RETRIES  = _user.get('download_retries',  _DEFAULTS['download_retries'])

# Legacy alias — scan operations still use this
MAX_WORKERS = SCAN_WORKERS
