#!/usr/bin/env python3
"""
Google Drive Sync - Bidirectional sync between local folder and Google Drive
"""
import argparse
from google_drive_sync import GoogleDriveSync

def main():
    parser = argparse.ArgumentParser(
        description='Sync local folder with Google Drive at regular intervals'
    )
    parser.add_argument(
        '--interval',
        type=int,
        default=300,
        help='Sync interval in seconds (default: 300 = 5 minutes)'
    )
    parser.add_argument(
        '--sync-once',
        action='store_true',
        help='Run sync once and exit (useful for testing)'
    )
    
    args = parser.parse_args()
    
    try:
        sync = GoogleDriveSync(sync_interval=args.interval)
        
        if args.sync_once:
            print("🔄 Running single sync...")
            sync.sync()
            print("\n✅ Single sync completed!")
        else:
            sync.run()
            
    except FileNotFoundError as e:
        print(f"\n❌ Error: {e}")
        print("\nPlease ensure:")
        print("1. OAuth credentials file exists in secrets/ folder")
        print("2. The file name matches the CREDS_FILE in google_drive_sync.py")
        return 1
    except KeyboardInterrupt:
        print("\n\n👋 Goodbye!")
        return 0
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        return 1

if __name__ == "__main__":
    exit(main())
