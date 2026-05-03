"""app/screens/conflict_resolver.py — Visual diff + resolution for conflict pairs.

Each conflict was already resolved by the sync engine into two copies:
  filename.local.HOSTNAME.ext  ← user's local version
  filename.drive.ext           ← Drive's version

This screen lets the user review both and pick a final outcome.
"""
import os
from pathlib import Path
from typing import List

import flet as ft
from app import theme as T
from app.state import AppState, ConflictItem
from app.services import conflict_store as cs

TEXT_EXTS = {
    ".md", ".txt", ".py", ".json", ".yaml", ".yml", ".toml",
    ".csv", ".html", ".css", ".js", ".ts", ".sh", ".bat",
    ".rst", ".tex", ".xml",
}


def _read(path: str, max_bytes: int = 32_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_bytes)
    except OSError:
        return ""


def _file_size(path: str) -> str:
    try:
        b = os.path.getsize(path)
        if b < 1024:
            return f"{b} B"
        if b < 1024 ** 2:
            return f"{b/1024:.1f} KB"
        return f"{b/1024**2:.1f} MB"
    except OSError:
        return "?"


def _mtime_str(path: str) -> str:
    try:
        import datetime
        ts = os.path.getmtime(path)
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d  %H:%M")
    except OSError:
        return "unknown"


def _build_diff_view(text_a: str, text_b: str) -> ft.Control:
    """Render a unified diff using diff-match-patch with colour coding."""
    try:
        from diff_match_patch import diff_match_patch
        dmp   = diff_match_patch()
        diffs = dmp.diff_main(text_a, text_b)
        dmp.diff_cleanupSemantic(diffs)

        spans: List[ft.TextSpan] = []
        for op, data in diffs:
            if op == 0:      # equal
                spans.append(ft.TextSpan(data, style=ft.TextStyle(color=T.TEXT)))
            elif op == 1:    # insert (in Drive version)
                spans.append(ft.TextSpan(
                    data,
                    style=ft.TextStyle(
                        color=T.GREEN,
                        bgcolor=ft.colors.with_opacity(0.2, T.GREEN),
                    ),
                ))
            elif op == -1:   # delete (was in Local version)
                spans.append(ft.TextSpan(
                    data,
                    style=ft.TextStyle(
                        color=T.RED,
                        decoration=ft.TextDecoration.LINE_THROUGH,
                        bgcolor=ft.colors.with_opacity(0.15, T.RED),
                    ),
                ))

        return ft.Container(
            content=ft.Text(
                spans=spans,
                selectable=True,
                size=12,
                font_family="monospace",
            ),
            bgcolor=T.SURFACE0,
            border_radius=8,
            padding=12,
        )
    except Exception as exc:
        return ft.Text(f"Diff unavailable: {exc}", color=T.RED, size=12)


def _build_meta_row(path: str, label: str, color: str) -> ft.Control:
    return ft.Container(
        content=ft.Column([
            ft.Text(label, color=color, size=13, weight=ft.FontWeight.W_600),
            ft.Text(Path(path).name, color=T.SUBTEXT0, size=11),
            ft.Row([
                ft.Icon(ft.icons.SCHEDULE, size=12, color=T.OVERLAY0),
                ft.Text(_mtime_str(path), color=T.OVERLAY0, size=11),
                ft.Container(width=8),
                ft.Icon(ft.icons.DATA_USAGE, size=12, color=T.OVERLAY0),
                ft.Text(_file_size(path), color=T.OVERLAY0, size=11),
            ], spacing=4),
        ], spacing=2),
        bgcolor=ft.colors.with_opacity(0.08, color),
        border=ft.border.all(1, ft.colors.with_opacity(0.3, color)),
        border_radius=8,
        padding=10,
        expand=True,
    )


def build_item(
    page: ft.Page,
    state: AppState,
    item: ConflictItem,
    idx: int,
    total: int,
    on_resolved,
) -> ft.Control:
    """Build the detail view for one conflict item."""

    ext      = Path(item.original).suffix.lower()
    is_text  = ext in TEXT_EXTS

    # ── Header ────────────────────────────────────────────────────────────────
    header = ft.Container(
        content=ft.Column([
            ft.Row([
                ft.Icon(ft.icons.WARNING_AMBER_ROUNDED, color=T.YELLOW, size=20),
                ft.Text(
                    item.original, color=T.TEXT, size=14,
                    weight=ft.FontWeight.W_600, expand=True,
                ),
                T.caption(f"{idx+1} / {total}", T.SUBTEXT0),
            ], spacing=8),
            T.caption(
                "both modified" if item.kind == "type1" else "new file collision",
                T.YELLOW,
            ),
        ], spacing=4),
        bgcolor=T.MANTLE,
        padding=ft.padding.symmetric(horizontal=16, vertical=12),
    )

    # ── Meta row ──────────────────────────────────────────────────────────────
    meta_row = ft.Row([
        _build_meta_row(item.local_path, "LOCAL", T.BLUE)
        if item.local_exists else ft.Text("local copy missing", color=T.RED, size=12),
        _build_meta_row(item.drive_path, "DRIVE", T.GREEN)
        if item.drive_exists else ft.Text("drive copy missing", color=T.RED, size=12),
    ], spacing=8)

    # ── Content area (diff or binary info) ───────────────────────────────────
    content_area: ft.Control
    if is_text and item.local_exists and item.drive_exists:
        local_text = _read(item.local_path)
        drive_text = _read(item.drive_path)
        tabs = ft.Tabs(
            selected_index=0,
            indicator_color=T.BLUE,
            label_color=T.TEXT,
            unselected_label_color=T.SUBTEXT0,
            tabs=[
                ft.Tab(
                    text="Diff (LOCAL→DRIVE)",
                    content=ft.Container(
                        content=_build_diff_view(local_text, drive_text),
                        padding=ft.padding.only(top=8),
                    ),
                ),
                ft.Tab(
                    text="LOCAL",
                    content=ft.Container(
                        content=ft.Container(
                            content=ft.Text(
                                local_text, size=12,
                                font_family="monospace", selectable=True,
                                color=T.TEXT,
                            ),
                            bgcolor=T.SURFACE0, border_radius=8, padding=12,
                        ),
                        padding=ft.padding.only(top=8),
                    ),
                ),
                ft.Tab(
                    text="DRIVE",
                    content=ft.Container(
                        content=ft.Container(
                            content=ft.Text(
                                drive_text, size=12,
                                font_family="monospace", selectable=True,
                                color=T.TEXT,
                            ),
                            bgcolor=T.SURFACE0, border_radius=8, padding=12,
                        ),
                        padding=ft.padding.only(top=8),
                    ),
                ),
            ],
        )
        content_area = ft.Container(content=tabs, expand=True)
    else:
        content_area = ft.Container(
            content=ft.Text(
                "Binary file — compare sizes and dates above.",
                color=T.SUBTEXT0, size=13, italic=True,
            ),
            padding=ft.padding.symmetric(vertical=20),
        )

    # ── Resolution buttons ────────────────────────────────────────────────────
    def _resolve(action: str):
        try:
            from metadata import load_metadata, save_metadata
            from config import LOCAL_FOLDER
            meta = load_metadata()
            if action == "mine":
                cs.resolve_keep_mine(item, meta, LOCAL_FOLDER)
            elif action == "theirs":
                cs.resolve_keep_theirs(item, meta, LOCAL_FOLDER)
            elif action == "both":
                cs.resolve_keep_both(item, meta)
            else:
                cs.resolve_decide_later(item)
            save_metadata(meta)
        except Exception as exc:
            page.snack_bar = ft.SnackBar(ft.Text(f"Error: {exc}"), bgcolor=T.RED)
            page.snack_bar.open = True
        finally:
            on_resolved(item)
            page.update()

    buttons = ft.Row([
        ft.ElevatedButton(
            "✓ Mine is final",
            on_click=lambda _: _resolve("mine"),
            bgcolor=T.BLUE, color=T.CRUST,
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
            expand=True,
        ),
        ft.ElevatedButton(
            "↓ Theirs is final",
            on_click=lambda _: _resolve("theirs"),
            bgcolor=T.GREEN, color=T.CRUST,
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
            expand=True,
        ),
    ], spacing=8)

    buttons2 = ft.Row([
        ft.OutlinedButton(
            "≡ Keep both",
            on_click=lambda _: _resolve("both"),
            style=ft.ButtonStyle(
                color=T.TEXT, side=ft.BorderSide(1, T.SURFACE2),
                shape=ft.RoundedRectangleBorder(radius=8),
            ),
            expand=True,
        ),
        ft.OutlinedButton(
            "⏭ Decide later",
            on_click=lambda _: _resolve("later"),
            style=ft.ButtonStyle(
                color=T.SUBTEXT0, side=ft.BorderSide(1, T.SURFACE1),
                shape=ft.RoundedRectangleBorder(radius=8),
            ),
            expand=True,
        ),
    ], spacing=8)

    return ft.Column([
        header,
        ft.Container(
            content=ft.Column([
                ft.Container(height=4),
                meta_row,
                ft.Container(height=8),
                content_area,
                ft.Container(height=12),
                buttons,
                ft.Container(height=8),
                buttons2,
                ft.Container(height=16),
            ], scroll=ft.ScrollMode.AUTO, expand=True, spacing=0),
            expand=True,
            padding=ft.padding.symmetric(horizontal=16),
        ),
    ], expand=True, spacing=0)


def build(page: ft.Page, state: AppState) -> ft.Control:
    """Conflict list view — tap a conflict to open its detail view."""

    items_col = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO, expand=True)

    def _refresh():
        items_col.controls.clear()
        if not state.pending_conflicts:
            items_col.controls.append(ft.Container(
                content=ft.Column([
                    ft.Icon(ft.icons.CHECK_CIRCLE_OUTLINE, size=48, color=T.GREEN),
                    ft.Text("No conflicts", color=T.GREEN, size=16,
                            weight=ft.FontWeight.W_600),
                    T.caption("All conflicts resolved.", T.SUBTEXT0),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
                alignment=ft.alignment.center,
                expand=True,
                padding=40,
            ))
            return

        n = len(state.pending_conflicts)
        items_col.controls.append(T.caption(
            f"{n} conflict pair{'s' if n != 1 else ''} waiting for review", T.YELLOW
        ))
        items_col.controls.append(ft.Container(height=4))

        for i, item in enumerate(state.pending_conflicts):
            def _open(e, _item=item, _i=i):
                page.go(f"/conflicts/{_i}")

            tile = T.card(ft.Column([
                ft.Row([
                    ft.Icon(ft.icons.WARNING_AMBER_ROUNDED, color=T.YELLOW, size=18),
                    ft.Text(item.original, color=T.TEXT, size=13, expand=True),
                    T.status_badge(
                        "type1" if item.kind == "type1" else "type2", T.YELLOW
                    ),
                ], spacing=8),
                ft.Row([
                    T.caption(
                        f"local: {Path(item.local_copy).name}" if item.local_exists
                        else "local copy missing", T.BLUE,
                    ),
                    T.caption(
                        f"drive: {Path(item.drive_copy).name}" if item.drive_exists
                        else "drive copy missing", T.GREEN,
                    ),
                ], spacing=12),
            ], spacing=6))

            tile_btn = ft.GestureDetector(content=tile, on_tap=_open)
            items_col.controls.append(tile_btn)

    state.subscribe(_refresh)
    _refresh()

    return ft.Column([
        ft.Container(height=8),
        ft.Container(
            content=items_col,
            expand=True,
            padding=ft.padding.symmetric(horizontal=0),
        ),
    ], expand=True, spacing=0)
