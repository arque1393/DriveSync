import os
import json
from typing import Dict

from config import METADATA_FILE


def load_metadata() -> Dict:
    """Load sync metadata from file."""
    if os.path.exists(METADATA_FILE):
        try:
            with open(METADATA_FILE, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            print("⚠️  Metadata file corrupted, starting fresh")
    return {'files': {}, 'drive_files': {}}


def save_metadata(metadata: Dict):
    """Save sync metadata to file."""
    with open(METADATA_FILE, 'w') as f:
        json.dump(metadata, f, indent=2)