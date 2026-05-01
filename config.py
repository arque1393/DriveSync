import json
import os

USER_CONFIG_FILE = 'user_config.json'

SCOPES     = ['https://www.googleapis.com/auth/drive']
CREDS_FILE = (
    'secrets/client_secret_116989766183-v0e50u650rdhah0j933fsloke77hp1od'
    '.apps.googleusercontent.com.json'
)
TOKEN_FILE    = 'secrets/token.pickle'
METADATA_FILE = 'sync_metadata.json'

# ── Defaults (overridden by user_config.json when present) ───────────────────
_DEFAULTS = {
    'local_folder':     '/home/aritrarc1/GDrive',
    'drive_folder_name': 'Obsidian',
    'sync_interval':    300,
    'max_workers':      10,
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
SYNC_INTERVAL     = _user.get('sync_interval',     _DEFAULTS['sync_interval'])
MAX_WORKERS       = _user.get('max_workers',       _DEFAULTS['max_workers'])
