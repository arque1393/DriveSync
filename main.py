#!/usr/bin/env python3
"""
Google Drive Sync - Bidirectional sync between local folder and Google Drive
"""
import argparse


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Sync local folder with Google Drive at regular intervals'
    )
    parser.add_argument(
        '--interval', type=int, default=None,
        help='Sync interval in seconds (overrides saved config)',
    )
    parser.add_argument(
        '--sync-once', action='store_true',
        help='Run sync once and exit (useful for testing)',
    )
    parser.add_argument(
        '--setup', action='store_true',
        help='Open the setup GUI to configure sync folders before syncing',
    )

    args = parser.parse_args()

    if args.setup:
        from sync_setup import run_setup
        cfg = run_setup()
        if cfg is None:
            print('Setup cancelled. Exiting.')
            return 0

    # Imports are intentionally deferred so that config.py reads
    # user_config.json *after* the setup GUI may have saved new values.
    from google_drive_sync import GoogleDriveSync
    from config import SYNC_INTERVAL

    interval = args.interval if args.interval is not None else SYNC_INTERVAL

    try:
        sync = GoogleDriveSync(sync_interval=interval)

        if args.sync_once:
            print('🔄 Running single sync...')
            sync.sync()
            print('\n✅ Single sync completed!')
        else:
            sync.run()

    except FileNotFoundError as e:
        print(f'\n❌ Error: {e}')
        print('\nPlease ensure:')
        print('1. OAuth credentials file exists in secrets/ folder')
        print('2. The file name matches CREDS_FILE in config.py')
        return 1
    except KeyboardInterrupt:
        print('\n\n👋 Goodbye!')
        return 0
    except Exception as e:
        print(f'\n❌ Unexpected error: {e}')
        return 1

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
