#!/usr/bin/env python3
"""Google Drive Sync — bidirectional sync between a local folder and Google Drive."""

import argparse
import os
import sys


# ── Terminal colour helpers ───────────────────────────────────────────────────

def _tty() -> bool:
    return hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()

def _fmt(text: str, *codes: str) -> str:
    return f'\033[{";".join(codes)}m{text}\033[0m' if _tty() else text

def bold(t):   return _fmt(t, '1')
def blue(t):   return _fmt(t, '1', '94')
def cyan(t):   return _fmt(t, '96')
def yellow(t): return _fmt(t, '93')
def green(t):  return _fmt(t, '92')
def dim(t):    return _fmt(t, '2')


# ── Help ──────────────────────────────────────────────────────────────────────

def print_help() -> None:
    from config import (
        CREDS_FILE, TOKEN_FILE, SYNC_INTERVAL,
        LOCAL_FOLDER, DRIVE_FOLDER_NAME, DRIVE_FOLDER_ID, MAX_WORKERS,
    )

    token_status = '✅ saved' if os.path.exists(TOKEN_FILE) else '⚠️  not yet — browser needed on first run'

    rule = blue('─' * 62)

    print(f"""
{rule}
  {blue('☁  Google Drive Sync')}
  {dim('Bidirectional sync between a local folder and Google Drive')}
{rule}

{bold('USAGE')}
  python main.py {cyan('<flag>')} {yellow('[--interval N]')}

  At least one flag is required. Running with no arguments shows
  this help.

{bold('FLAGS')}
  {cyan('--run')}          Start the continuous sync loop  {dim('(Ctrl+C to stop)')}
  {cyan('--sync-once')}    Run a single sync cycle and exit
  {cyan('--dry-run')}      Scan both sides and show what would change — nothing transferred
  {cyan('--setup')}        Open the GUI to configure folders
                 {dim('Combine with --run or --sync-once to sync after setup')}
  {cyan('-h, --help')}     Show this help message

{bold('OPTIONS')}
  {yellow('--interval N')}  Override the sync interval for this session {dim('(seconds)')}
                 Saved default: {green(str(SYNC_INTERVAL) + ' s')}

{bold('CURRENT CONFIG')}  {dim('· change with --setup ·')}
  Local folder   {green(LOCAL_FOLDER)}
  Drive folder   {green(DRIVE_FOLDER_NAME)}
  Drive folder ID  {green(DRIVE_FOLDER_ID) if DRIVE_FOLDER_ID else yellow('(not set — name search used)')}
  Sync interval  {green(str(SYNC_INTERVAL) + ' s')}
  Max workers    {green(str(MAX_WORKERS))}

{bold('AUTH')}  {dim('· OAuth user credentials ·')}
  Credentials    {dim(CREDS_FILE)}
  Token          {dim(TOKEN_FILE)}  {dim(token_status)}
  {dim('First run opens a browser once. Token is saved and auto-refreshed')}
  {dim('on every subsequent run — no browser needed after that.')}

{bold('EXAMPLES')}
  {dim('python main.py --dry-run')}             Preview pending changes (safe, read-only)
  {dim('python main.py --run')}                 Start continuous sync
  {dim('python main.py --sync-once')}           Sync once and exit
  {dim('python main.py --setup')}               Configure folders only
  {dim('python main.py --setup --run')}         Configure then start syncing
  {dim('python main.py --setup --sync-once')}   Configure then sync once
  {dim('python main.py --run --interval 60')}   Sync every 60 seconds
{rule}
""")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('-h', '--help',     action='store_true')
    parser.add_argument('--setup',          action='store_true')
    parser.add_argument('--run',            action='store_true')
    parser.add_argument('--sync-once',      action='store_true', dest='sync_once')
    parser.add_argument('--dry-run',        action='store_true', dest='dry_run')
    parser.add_argument('--interval',       type=int, default=None,
                        metavar='N',
                        help='Sync interval in seconds (overrides saved config)')

    args = parser.parse_args()

    no_action = not any([args.setup, args.run, args.sync_once, args.dry_run])

    if args.help or no_action:
        print_help()
        return 0

    # ── Setup GUI ─────────────────────────────────────────────────────────────
    if args.setup:
        from sync_setup import run_setup
        cfg = run_setup()
        if cfg is None:
            print('Setup cancelled.')
            return 0
        if not args.run and not args.sync_once:
            print(f"\n✅ Config saved.  Run with {cyan('--run')} or {cyan('--sync-once')} to start syncing.")
            return 0

    # Deferred so config.py reads user_config.json *after* --setup may have saved it.
    from google_drive_sync import GoogleDriveSync
    from config import SYNC_INTERVAL

    interval = args.interval if args.interval is not None else SYNC_INTERVAL

    try:
        sync = GoogleDriveSync(sync_interval=interval)

        if args.dry_run:
            sync.preview()
        elif args.sync_once:
            print('🔄 Running single sync...')
            sync.sync()
            print('\n✅ Single sync completed!')
        else:
            sync.run()

    except FileNotFoundError as e:
        print(f'\n❌ {e}')
        print(f'\nRun  python main.py {cyan("--help")}  for setup instructions.')
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
