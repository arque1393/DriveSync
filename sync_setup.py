"""sync_setup.py — Tkinter GUI for configuring Google Drive Sync.

Run directly or triggered via  main.py --setup.
Saves choices to user_config.json which config.py reads at startup.
"""

import json
import os
import threading
import tkinter as tk
from tkinter import filedialog
from pathlib import Path
from typing import Callable, Optional

USER_CONFIG_FILE = 'user_config.json'

# ── Catppuccin Mocha palette ──────────────────────────────────────────────────
C = {
    'base':     '#1e1e2e',
    'mantle':   '#181825',
    'surface0': '#313244',
    'surface1': '#45475a',
    'overlay':  '#6c7086',
    'text':     '#cdd6f4',
    'subtext0': '#a6adc8',
    'subtext1': '#bac2de',
    'blue':     '#89b4fa',
    'sapphire': '#74c7ec',
    'green':    '#a6e3a1',
    'red':      '#f38ba8',
    'yellow':   '#f9e2af',
}

FONT_TITLE  = ('Segoe UI', 17, 'bold')
FONT_SUB    = ('Segoe UI', 10)
FONT_LABEL  = ('Segoe UI', 10, 'bold')
FONT_INPUT  = ('Segoe UI', 10)
FONT_BTN    = ('Segoe UI', 10, 'bold')
FONT_SMALL  = ('Segoe UI', 9)
FONT_PATH   = ('Segoe UI', 9, 'bold')


# ── Config persistence ────────────────────────────────────────────────────────

def load_config() -> dict:
    if os.path.exists(USER_CONFIG_FILE):
        try:
            with open(USER_CONFIG_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_config(config: dict) -> None:
    with open(USER_CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


# ── Shared widget helpers ─────────────────────────────────────────────────────

def _bordered_frame(parent) -> tk.Frame:
    border = tk.Frame(parent, bg=C['surface1'], pady=1, padx=1)
    border.pack(fill='x')
    return border


def _inner_frame(border: tk.Frame) -> tk.Frame:
    inner = tk.Frame(border, bg=C['surface0'], padx=10, pady=7)
    inner.pack(fill='x')
    return inner


def _focus_ring(widget, border: tk.Frame) -> None:
    widget.bind('<FocusIn>',  lambda _e: border.config(bg=C['blue']))
    widget.bind('<FocusOut>', lambda _e: border.config(bg=C['surface1']))


def _section_label(parent, text: str) -> None:
    tk.Label(
        parent, text=text,
        font=FONT_LABEL, fg=C['subtext1'], bg=C['base'], anchor='w',
    ).pack(fill='x', pady=(14, 5))


def _section_label_with_hint(parent, bold_text: str, hint: str) -> None:
    row = tk.Frame(parent, bg=C['base'])
    row.pack(fill='x', pady=(14, 5))
    tk.Label(row, text=bold_text, font=FONT_LABEL,
             fg=C['subtext1'], bg=C['base']).pack(side='left')
    tk.Label(row, text=f'  {hint}', font=FONT_SMALL,
             fg=C['overlay'], bg=C['base']).pack(side='left')


def _make_entry(parent, var: tk.StringVar) -> tk.Entry:
    border = _bordered_frame(parent)
    entry = tk.Entry(
        _inner_frame(border), textvariable=var,
        font=FONT_INPUT, fg=C['text'], bg=C['surface0'],
        insertbackground=C['blue'], relief='flat', highlightthickness=0,
    )
    entry.pack(fill='x')
    _focus_ring(entry, border)
    return entry


def _make_spinbox(parent, var: tk.IntVar, from_: int, to: int) -> tk.Spinbox:
    border = _bordered_frame(parent)
    spin = tk.Spinbox(
        _inner_frame(border),
        from_=from_, to=to, textvariable=var,
        font=FONT_INPUT, fg=C['text'], bg=C['surface0'],
        buttonbackground=C['surface1'],
        insertbackground=C['blue'], relief='flat', highlightthickness=0,
    )
    spin.pack(fill='x')
    _focus_ring(spin, border)
    return spin


def _make_button(parent, text: str, command, accent=False, **pack_opts) -> tk.Button:
    bg  = C['blue']     if accent else C['surface0']
    fg  = C['base']     if accent else C['subtext0']
    abg = C['sapphire'] if accent else C['surface1']
    afg = C['base']     if accent else C['text']
    btn = tk.Button(
        parent, text=text, command=command,
        font=FONT_BTN, fg=fg, bg=bg,
        activebackground=abg, activeforeground=afg,
        relief='flat', padx=14, pady=8, cursor='hand2',
    )
    btn.pack(**pack_opts)
    btn.bind('<Enter>', lambda _e: btn.config(bg=abg))
    btn.bind('<Leave>', lambda _e: btn.config(bg=bg))
    return btn


# ── Drive folder browser dialog ───────────────────────────────────────────────

def _open_drive_browser(
    parent: tk.Tk,
    on_select: Callable[[str, str], None],
) -> None:
    """
    Modal dialog for browsing Google Drive folders.
    Calls on_select(folder_id, folder_name) when the user confirms.

    Navigation:
      • Single-click  → highlight a folder
      • Double-click  → enter the highlighted folder
      • ← Back        → go up to the parent folder
      • Select button → confirm the currently highlighted folder
    """
    from googleapiclient.discovery import build

    dlg = tk.Toplevel(parent)
    dlg.title('Browse Google Drive Folders')
    dlg.resizable(False, False)
    dlg.configure(bg=C['base'])
    dlg.grab_set()

    W, H = 500, 460
    dlg.update_idletasks()
    cx = (dlg.winfo_screenwidth()  - W) // 2
    cy = (dlg.winfo_screenheight() - H) // 2
    dlg.geometry(f'{W}x{H}+{cx}+{cy}')

    # ── Navigation state ──────────────────────────────────────
    # Each entry in the stack is (folder_id_or_None, folder_name).
    # None means the virtual root (owned root + shared-with-me).
    _stack:    list         = []        # history of visited (id, name)
    _cur_id:   list         = [None]    # current folder ID (None = root)
    _cur_name: list         = ['My Drive']
    _folders:  list         = [[]]      # [(id, name)] at current level
    _sel_idx:  list         = [None]    # highlighted listbox index

    # ── Header (path + Back button) ───────────────────────────
    nav = tk.Frame(dlg, bg=C['mantle'], padx=16, pady=12)
    nav.pack(fill='x')

    back_btn = tk.Button(
        nav, text='← Back',
        font=FONT_BTN, fg=C['subtext0'], bg=C['surface0'],
        activebackground=C['surface1'], activeforeground=C['text'],
        relief='flat', padx=12, pady=6, cursor='hand2',
        state='disabled',
    )
    back_btn.pack(side='left', padx=(0, 14))

    path_var = tk.StringVar(value='📁  My Drive')
    path_lbl = tk.Label(
        nav, textvariable=path_var,
        font=FONT_PATH, fg=C['blue'], bg=C['mantle'],
        anchor='w',
    )
    path_lbl.pack(side='left', fill='x', expand=True)

    tk.Frame(dlg, bg=C['surface1'], height=1).pack(fill='x')

    # ── Folder list ────────────────────────────────────────────
    body = tk.Frame(dlg, bg=C['base'], padx=16, pady=10)
    body.pack(fill='both', expand=True)

    info_var = tk.StringVar(value='⏳  Loading folders…')
    info_lbl = tk.Label(
        body, textvariable=info_var,
        font=FONT_SMALL, fg=C['subtext0'], bg=C['base'], anchor='w',
    )
    info_lbl.pack(fill='x', pady=(0, 6))

    lb_border = tk.Frame(body, bg=C['surface1'], pady=1, padx=1)
    lb_border.pack(fill='both', expand=True)

    scroll = tk.Scrollbar(lb_border, bg=C['surface1'],
                          troughcolor=C['surface0'], relief='flat', width=10)
    scroll.pack(side='right', fill='y')

    listbox = tk.Listbox(
        lb_border,
        bg=C['surface0'], fg=C['text'],
        selectbackground=C['blue'], selectforeground=C['base'],
        font=FONT_INPUT, relief='flat', borderwidth=0,
        highlightthickness=0, activestyle='none',
        yscrollcommand=scroll.set,
    )
    listbox.pack(side='left', fill='both', expand=True)
    scroll.config(command=listbox.yview)

    # ── Footer ─────────────────────────────────────────────────
    tk.Frame(dlg, bg=C['surface1'], height=1).pack(fill='x')
    footer = tk.Frame(dlg, bg=C['mantle'], padx=16, pady=14)
    footer.pack(fill='x')

    sel_var = tk.StringVar(value='Double-click to enter a folder  ·  click once to select it')
    tk.Label(
        footer, textvariable=sel_var,
        font=FONT_SMALL, fg=C['overlay'], bg=C['mantle'], anchor='w',
    ).pack(fill='x', pady=(0, 10))

    btn_row = tk.Frame(footer, bg=C['mantle'])
    btn_row.pack(fill='x')

    cancel_btn = tk.Button(
        btn_row, text='Cancel',
        font=FONT_BTN, fg=C['subtext0'], bg=C['surface0'],
        activebackground=C['surface1'], activeforeground=C['text'],
        relief='flat', padx=16, pady=8, cursor='hand2',
        command=dlg.destroy,
    )
    cancel_btn.pack(side='left')

    open_btn = tk.Button(
        btn_row, text='Open  →',
        font=FONT_BTN, fg=C['subtext0'], bg=C['surface0'],
        activebackground=C['surface1'], activeforeground=C['text'],
        relief='flat', padx=14, pady=8, cursor='hand2',
        state='disabled',
    )
    open_btn.pack(side='right', padx=(8, 0))

    select_btn = tk.Button(
        btn_row, text='✓  Select',
        font=FONT_BTN, fg=C['base'], bg=C['blue'],
        activebackground=C['sapphire'], activeforeground=C['base'],
        relief='flat', padx=16, pady=8, cursor='hand2',
        state='disabled',
    )
    select_btn.pack(side='right')

    # ── Helpers ────────────────────────────────────────────────

    def _path_str() -> str:
        if not _stack:
            return '📁  My Drive'
        parts = ['My Drive'] + [name for _, name in _stack[1:]] + [_cur_name[0]]
        return '📁  ' + ' / '.join(parts)

    def _load(folder_id: Optional[str]) -> None:
        info_var.set('⏳  Loading…')
        info_lbl.config(fg=C['subtext0'])
        listbox.delete(0, 'end')
        open_btn.config(state='disabled')
        select_btn.config(state='disabled')
        _sel_idx[0] = None

        def _worker():
            try:
                from auth import get_credentials
                svc = build('drive', 'v3', credentials=get_credentials())
                folders: list = []

                if folder_id is None:
                    # Virtual root: show owned root folders + shared-with-me folders
                    queries = [
                        ("'root' in parents and "
                         "mimeType='application/vnd.google-apps.folder' and trashed=false"),
                        ("sharedWithMe=true and "
                         "mimeType='application/vnd.google-apps.folder' and trashed=false"),
                    ]
                    seen: set = set()
                    for q in queries:
                        token = None
                        while True:
                            resp = svc.files().list(
                                q=q,
                                fields='nextPageToken, files(id, name)',
                                orderBy='name',
                                supportsAllDrives=True,
                                includeItemsFromAllDrives=True,
                                pageToken=token,
                            ).execute()
                            for f in resp.get('files', []):
                                if f['id'] not in seen:
                                    seen.add(f['id'])
                                    folders.append((f['id'], f['name']))
                            token = resp.get('nextPageToken')
                            if not token:
                                break
                else:
                    token = None
                    while True:
                        resp = svc.files().list(
                            q=(f"'{folder_id}' in parents and "
                               f"mimeType='application/vnd.google-apps.folder' and trashed=false"),
                            fields='nextPageToken, files(id, name)',
                            orderBy='name',
                            supportsAllDrives=True,
                            includeItemsFromAllDrives=True,
                            pageToken=token,
                        ).execute()
                        for f in resp.get('files', []):
                            folders.append((f['id'], f['name']))
                        token = resp.get('nextPageToken')
                        if not token:
                            break

                folders.sort(key=lambda x: x[1].lower())
                dlg.after(0, lambda: _on_loaded(folders))

            except Exception as exc:
                err = str(exc)
                if 'credentials' in err.lower() or 'not found' in err.lower():
                    err = 'Service account key not found — run --setup first.'
                dlg.after(0, lambda e=err: _on_error(e))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_loaded(folders: list) -> None:
        _folders[0] = folders
        listbox.delete(0, 'end')
        if folders:
            for _, name in folders:
                listbox.insert('end', f'  📁   {name}')
            count = len(folders)
            info_var.set(f'{count} folder{"s" if count != 1 else ""} — '
                         f'double-click to enter, single-click to select')
            info_lbl.config(fg=C['subtext0'])
        else:
            listbox.insert('end', '  (no subfolders here)')
            info_var.set('No subfolders — you can still select the current folder above')
            info_lbl.config(fg=C['overlay'])

    def _on_error(msg: str) -> None:
        info_var.set(f'✗  {msg}')
        info_lbl.config(fg=C['red'])

    def _on_listbox_select(_evt) -> None:
        sel = listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(_folders[0]):
            return
        _sel_idx[0] = idx
        _, name = _folders[0][idx]
        sel_var.set(f'📁  {name}')
        select_btn.config(state='normal')
        open_btn.config(state='normal')

    def _enter_folder(idx: int) -> None:
        if idx >= len(_folders[0]):
            return
        fid, fname = _folders[0][idx]
        _stack.append((_cur_id[0], _cur_name[0]))
        _cur_id[0]   = fid
        _cur_name[0] = fname
        path_var.set(_path_str())
        back_btn.config(state='normal')
        sel_var.set('Double-click to enter a folder  ·  click once to select it')
        _load(fid)

    def _on_open() -> None:
        if _sel_idx[0] is not None:
            _enter_folder(_sel_idx[0])

    def _on_back() -> None:
        if not _stack:
            return
        prev_id, prev_name = _stack.pop()
        _cur_id[0]   = prev_id
        _cur_name[0] = prev_name
        path_var.set(_path_str())
        back_btn.config(state='normal' if _stack else 'disabled')
        sel_var.set('Double-click to enter a folder  ·  click once to select it')
        _load(prev_id)

    def _on_confirm() -> None:
        if _sel_idx[0] is None or not _folders[0]:
            return
        idx = _sel_idx[0]
        if idx >= len(_folders[0]):
            return
        fid, fname = _folders[0][idx]
        on_select(fid, fname)
        dlg.destroy()

    def _on_dbl_click(evt) -> None:
        idx = listbox.nearest(evt.y)
        if 0 <= idx < len(_folders[0]):
            _enter_folder(idx)

    listbox.bind('<<ListboxSelect>>', _on_listbox_select)
    listbox.bind('<Double-Button-1>', _on_dbl_click)
    back_btn.config(command=_on_back)
    open_btn.config(command=_on_open)
    select_btn.config(command=_on_confirm)

    _load(None)


# ── Setup window ──────────────────────────────────────────────────────────────

def run_setup() -> Optional[dict]:
    """
    Open the setup window.
    Returns the saved config dict on Save, or None if cancelled.
    """
    existing = load_config()
    result: list = [None]

    root = tk.Tk()
    root.title('Google Drive Sync — Setup')
    root.resizable(False, False)
    root.configure(bg=C['base'])

    W, H = 560, 760
    root.update_idletasks()
    cx = (root.winfo_screenwidth()  - W) // 2
    cy = (root.winfo_screenheight() - H) // 2
    root.geometry(f'{W}x{H}+{cx}+{cy}')

    # ── Header ────────────────────────────────────────────────
    header = tk.Frame(root, bg=C['mantle'], pady=24)
    header.pack(fill='x')
    tk.Label(
        header, text='☁  Google Drive Sync',
        font=FONT_TITLE, fg=C['blue'], bg=C['mantle'],
    ).pack()
    tk.Label(
        header, text='Configure which folders to synchronise',
        font=FONT_SUB, fg=C['subtext0'], bg=C['mantle'],
    ).pack(pady=(5, 0))
    tk.Frame(root, bg=C['surface1'], height=1).pack(fill='x')

    # ── Body ──────────────────────────────────────────────────
    body = tk.Frame(root, bg=C['base'], padx=36, pady=16)
    body.pack(fill='both', expand=True)

    # ── Local folder ──────────────────────────────────────────
    _section_label(body, '📂  Local Folder')
    folder_row = tk.Frame(body, bg=C['base'])
    folder_row.pack(fill='x')

    folder_var = tk.StringVar(value=existing.get('local_folder', str(Path.home())))
    folder_border = tk.Frame(folder_row, bg=C['surface1'], pady=1, padx=1)
    folder_border.pack(side='left', fill='x', expand=True, padx=(0, 10))
    folder_entry = tk.Entry(
        _inner_frame(folder_border), textvariable=folder_var,
        font=FONT_INPUT, fg=C['text'], bg=C['surface0'],
        insertbackground=C['blue'], relief='flat', highlightthickness=0,
    )
    folder_entry.pack(fill='x')
    _focus_ring(folder_entry, folder_border)

    def _browse_local():
        path = filedialog.askdirectory(
            title='Select Local Sync Folder',
            initialdir=folder_var.get() or str(Path.home()),
        )
        if path:
            folder_var.set(path)

    _make_button(folder_row, 'Browse…', _browse_local, accent=True, side='right')

    # ── Drive folder name ─────────────────────────────────────
    _section_label(body, '☁   Google Drive Folder Name')
    drive_var = tk.StringVar(value=existing.get('drive_folder_name', 'Obsidian'))
    _make_entry(body, drive_var)

    # ── Drive folder ID ───────────────────────────────────────
    _section_label_with_hint(body, '🔑  Drive Folder ID', 'paste from URL or use Browse Drive')

    tk.Label(
        body,
        text='  drive.google.com/drive/folders/  ← copy the ID after the last  /',
        font=('Segoe UI', 8), fg=C['overlay'], bg=C['base'], anchor='w',
    ).pack(fill='x', pady=(0, 4))

    # ID entry (full width)
    folder_id_var = tk.StringVar(value=existing.get('drive_folder_id', ''))
    _make_entry(body, folder_id_var)

    # Verify feedback label
    verify_var = tk.StringVar()
    verify_lbl = tk.Label(
        body, textvariable=verify_var,
        font=FONT_SMALL, fg=C['green'], bg=C['base'], anchor='w',
    )
    verify_lbl.pack(fill='x', pady=(4, 0))

    # Verify + Browse buttons on one row
    id_btn_row = tk.Frame(body, bg=C['base'])
    id_btn_row.pack(fill='x', pady=(8, 0))

    # ── Verify ────────────────────────────────────────────────
    verify_btn = _make_button(id_btn_row, 'Verify', lambda: None,
                              accent=False, side='left', padx=(0, 8))

    def _do_verify():
        fid = folder_id_var.get().strip()
        if not fid:
            verify_var.set('⚠  Paste a folder ID first.')
            verify_lbl.config(fg=C['yellow'])
            return
        verify_var.set('⏳  Checking…')
        verify_lbl.config(fg=C['subtext0'])
        verify_btn.config(state='disabled')

        def _worker():
            try:
                from auth import get_credentials
                from googleapiclient.discovery import build
                svc = build('drive', 'v3', credentials=get_credentials())
                meta = svc.files().get(
                    fileId=fid,
                    fields='name, mimeType',
                    supportsAllDrives=True,
                ).execute()
                name = meta.get('name', '?')
                if meta.get('mimeType') == 'application/vnd.google-apps.folder':
                    root.after(0, lambda: (
                        verify_var.set(f'✓  Found: "{name}" — service account has access'),
                        verify_lbl.config(fg=C['green']),
                    ))
                else:
                    root.after(0, lambda: (
                        verify_var.set(f'⚠  "{name}" is a file, not a folder.'),
                        verify_lbl.config(fg=C['yellow']),
                    ))
            except Exception as exc:
                msg = str(exc)
                if 'notFound' in msg or '404' in msg:
                    msg = 'Not found — check the ID, or share the folder with the service account first.'
                elif '403' in msg or 'forbidden' in msg.lower():
                    msg = 'Access denied — share the folder with the service account (Editor role).'
                root.after(0, lambda m=msg: (
                    verify_var.set(f'✗  {m}'),
                    verify_lbl.config(fg=C['red']),
                ))
            finally:
                root.after(0, lambda: verify_btn.config(state='normal'))

        threading.Thread(target=_worker, daemon=True).start()

    verify_btn.config(command=_do_verify)

    # ── Browse Drive ──────────────────────────────────────────
    def _on_drive_selected(fid: str, fname: str) -> None:
        folder_id_var.set(fid)
        drive_var.set(fname)
        verify_var.set(f'✓  Selected via browser: "{fname}"')
        verify_lbl.config(fg=C['green'])

    def _browse_drive():
        _open_drive_browser(root, _on_drive_selected)

    _make_button(id_btn_row, 'Browse Drive…', _browse_drive,
                 accent=True, side='left')

    # ── Sync interval ─────────────────────────────────────────
    _section_label(body, '⏱   Sync Interval (s)')
    interval_var = tk.IntVar(value=existing.get('sync_interval', 300))
    _make_spinbox(body, interval_var, 30, 86400)

    # ── Async concurrency knobs ───────────────────────────────
    _section_label_with_hint(body, '⚡  Concurrency', 'scan / upload / download — raise for faster sync on good connections')
    cols = tk.Frame(body, bg=C['base'])
    cols.pack(fill='x', pady=(4, 0))
    cols.columnconfigure(0, weight=1, uniform='col')
    cols.columnconfigure(1, weight=1, uniform='col')
    cols.columnconfigure(2, weight=1, uniform='col')

    c0 = tk.Frame(cols, bg=C['base'])
    c0.grid(row=0, column=0, sticky='nsew', padx=(0, 6))
    c1 = tk.Frame(cols, bg=C['base'])
    c1.grid(row=0, column=1, sticky='nsew', padx=(0, 6))
    c2 = tk.Frame(cols, bg=C['base'])
    c2.grid(row=0, column=2, sticky='nsew')

    _section_label(c0, 'Scan')
    scan_var = tk.IntVar(value=existing.get('scan_concurrency', 30))
    _make_spinbox(c0, scan_var, 1, 100)

    _section_label(c1, 'Upload')
    upload_var = tk.IntVar(value=existing.get('upload_concurrency', 5))
    _make_spinbox(c1, upload_var, 1, 20)

    _section_label(c2, 'Download')
    download_var = tk.IntVar(value=existing.get('download_concurrency', 5))
    _make_spinbox(c2, download_var, 1, 20)

    # ── Status ────────────────────────────────────────────────
    status_var = tk.StringVar()
    status_lbl = tk.Label(
        body, textvariable=status_var,
        font=FONT_SMALL, fg=C['green'], bg=C['base'],
    )
    status_lbl.pack(pady=(18, 0))

    def _set_status(msg: str, color: str) -> None:
        status_var.set(msg)
        status_lbl.config(fg=color)

    # ── Footer ────────────────────────────────────────────────
    tk.Frame(root, bg=C['surface1'], height=1).pack(fill='x')
    footer = tk.Frame(root, bg=C['mantle'], padx=36, pady=18)
    footer.pack(fill='x')

    def _on_save() -> None:
        local = folder_var.get().strip()
        drive = drive_var.get().strip()
        try:
            interval = int(interval_var.get())
            scan_c   = int(scan_var.get())
            upload_c = int(upload_var.get())
            dl_c     = int(download_var.get())
        except (tk.TclError, ValueError):
            _set_status('⚠  All numeric fields must be whole numbers.', C['red'])
            return
        if not local:
            _set_status('⚠  Local folder path cannot be empty.', C['red']); return
        if not os.path.isdir(local):
            _set_status(f'⚠  Folder not found: {local}', C['red']); return
        if not drive:
            _set_status('⚠  Drive folder name cannot be empty.', C['red']); return
        if not (30 <= interval <= 86400):
            _set_status('⚠  Interval must be between 30 and 86 400 s.', C['yellow']); return

        cfg = {
            'local_folder':        local,
            'drive_folder_name':   drive,
            'drive_folder_id':     folder_id_var.get().strip(),
            'sync_interval':       interval,
            'scan_concurrency':    max(1, min(100, scan_c)),
            'upload_concurrency':  max(1, min(20, upload_c)),
            'download_concurrency': max(1, min(20, dl_c)),
        }
        save_config(cfg)
        result[0] = cfg
        _set_status('✓  Configuration saved.', C['green'])
        root.after(700, root.destroy)

    _make_button(footer, 'Cancel',             root.destroy, accent=False, side='left')
    _make_button(footer, 'Save & Continue  →', _on_save,     accent=True,  side='right')

    root.bind('<Return>', lambda _e: _on_save())
    root.bind('<Escape>', lambda _e: root.destroy())

    root.mainloop()
    return result[0]


if __name__ == '__main__':
    cfg = run_setup()
    if cfg:
        print('Saved config:', json.dumps(cfg, indent=2))
    else:
        print('Setup cancelled.')
