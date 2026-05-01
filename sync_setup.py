"""sync_setup.py — Tkinter GUI for configuring Google Drive Sync.

Run directly or triggered via  main.py --setup.
Saves choices to user_config.json which config.py reads at startup.
"""

import json
import os
import tkinter as tk
from tkinter import filedialog
from pathlib import Path
from typing import Optional

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


# ── Widget helpers ────────────────────────────────────────────────────────────

def _bordered_frame(parent) -> tk.Frame:
    """Thin coloured border wrapping an inner surface-coloured frame."""
    border = tk.Frame(parent, bg=C['surface1'], pady=1, padx=1)
    border.pack(fill='x')
    return border


def _inner_frame(border: tk.Frame) -> tk.Frame:
    inner = tk.Frame(border, bg=C['surface0'], padx=10, pady=7)
    inner.pack(fill='x')
    return inner


def _focus_ring(entry_widget, border: tk.Frame) -> None:
    entry_widget.bind('<FocusIn>',  lambda _e: border.config(bg=C['blue']))
    entry_widget.bind('<FocusOut>', lambda _e: border.config(bg=C['surface1']))


def _section_label(parent, text: str) -> None:
    tk.Label(
        parent, text=text,
        font=FONT_LABEL, fg=C['subtext1'], bg=C['base'], anchor='w',
    ).pack(fill='x', pady=(14, 5))


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
    bg  = C['blue']    if accent else C['surface0']
    fg  = C['base']    if accent else C['subtext0']
    abg = C['sapphire'] if accent else C['surface1']
    afg = C['base']    if accent else C['text']
    btn = tk.Button(
        parent, text=text, command=command,
        font=FONT_BTN, fg=fg, bg=bg,
        activebackground=abg, activeforeground=afg,
        relief='flat', padx=20, pady=10, cursor='hand2',
    )
    btn.pack(**pack_opts)
    btn.bind('<Enter>', lambda _e: btn.config(bg=abg))
    btn.bind('<Leave>', lambda _e: btn.config(bg=bg))
    return btn


# ── Main GUI ──────────────────────────────────────────────────────────────────

def run_setup() -> Optional[dict]:
    """
    Open the setup window.

    Returns the saved config dict when the user clicks Save, or None if
    they cancel / close the window.
    """
    existing = load_config()
    result: list[Optional[dict]] = [None]

    root = tk.Tk()
    root.title('Google Drive Sync — Setup')
    root.resizable(False, False)
    root.configure(bg=C['base'])

    W, H = 540, 610
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

    # Local Folder (entry + browse button side by side)
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

    def _browse():
        path = filedialog.askdirectory(
            title='Select Local Sync Folder',
            initialdir=folder_var.get() or str(Path.home()),
        )
        if path:
            folder_var.set(path)

    _make_button(folder_row, 'Browse…', _browse, accent=True, side='right')

    # Drive Folder Name
    _section_label(body, '☁   Google Drive Folder Name')
    drive_var = tk.StringVar(value=existing.get('drive_folder_name', 'Obsidian'))
    _make_entry(body, drive_var)

    # Sync Interval + Max Workers (two equal columns)
    cols = tk.Frame(body, bg=C['base'])
    cols.pack(fill='x', pady=(4, 0))
    cols.columnconfigure(0, weight=1, uniform='col')
    cols.columnconfigure(1, weight=1, uniform='col')

    left  = tk.Frame(cols, bg=C['base'])
    left.grid(row=0, column=0, sticky='nsew', padx=(0, 10))
    right = tk.Frame(cols, bg=C['base'])
    right.grid(row=0, column=1, sticky='nsew')

    _section_label(left,  '⏱   Sync Interval (s)')
    interval_var = tk.IntVar(value=existing.get('sync_interval', 300))
    _make_spinbox(left, interval_var, 30, 86400)

    _section_label(right, '⚡  Max Workers')
    workers_var = tk.IntVar(value=existing.get('max_workers', 10))
    _make_spinbox(right, workers_var, 1, 50)

    # Status message
    status_var = tk.StringVar()
    status_lbl = tk.Label(
        body, textvariable=status_var,
        font=FONT_SMALL, fg=C['green'], bg=C['base'],
    )
    status_lbl.pack(pady=(20, 0))

    def _set_status(msg: str, color: str) -> None:
        status_var.set(msg)
        status_lbl.config(fg=color)

    # ── Footer ────────────────────────────────────────────────
    tk.Frame(root, bg=C['surface1'], height=1).pack(fill='x')
    footer = tk.Frame(root, bg=C['mantle'], padx=36, pady=18)
    footer.pack(fill='x')

    def _on_cancel() -> None:
        root.destroy()

    def _on_save() -> None:
        local = folder_var.get().strip()
        drive = drive_var.get().strip()

        try:
            interval = int(interval_var.get())
            workers  = int(workers_var.get())
        except (tk.TclError, ValueError):
            _set_status('⚠  Interval and workers must be whole numbers.', C['red'])
            return

        if not local:
            _set_status('⚠  Local folder path cannot be empty.', C['red']); return
        if not os.path.isdir(local):
            _set_status(f'⚠  Folder not found: {local}', C['red']); return
        if not drive:
            _set_status('⚠  Drive folder name cannot be empty.', C['red']); return
        if not (30 <= interval <= 86400):
            _set_status('⚠  Interval must be between 30 and 86 400 seconds.', C['yellow']); return
        if not (1 <= workers <= 50):
            _set_status('⚠  Workers must be between 1 and 50.', C['yellow']); return

        cfg = {
            'local_folder':     local,
            'drive_folder_name': drive,
            'sync_interval':    interval,
            'max_workers':      workers,
        }
        save_config(cfg)
        result[0] = cfg
        _set_status('✓  Configuration saved.', C['green'])
        root.after(700, root.destroy)

    _make_button(footer, 'Cancel',            _on_cancel, accent=False, side='left')
    _make_button(footer, 'Save & Continue  →', _on_save,  accent=True,  side='right')

    root.bind('<Return>', lambda _e: _on_save())
    root.bind('<Escape>', lambda _e: _on_cancel())

    root.mainloop()
    return result[0]


if __name__ == '__main__':
    cfg = run_setup()
    if cfg:
        print('Saved config:', json.dumps(cfg, indent=2))
    else:
        print('Setup cancelled.')
