"""app/screens/history.py — Sync history log."""
from datetime import datetime

import flet as ft
from app import theme as T
from app.state import AppState


def build(page: ft.Page, state: AppState) -> ft.Control:
    list_col = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO, expand=True)

    def _refresh():
        list_col.controls.clear()
        history = AppState.load_history()

        if not history:
            list_col.controls.append(ft.Container(
                content=ft.Column([
                    ft.Icon(ft.icons.HISTORY, size=48, color=T.SURFACE2),
                    ft.Text("No sync history yet", color=T.SUBTEXT0, size=15),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
                alignment=ft.alignment.center,
                expand=True,
                padding=40,
            ))
            return

        for entry in history:
            error     = entry.get("error")
            success   = error is None
            ts_str    = entry.get("timestamp", "")
            try:
                ts_str = datetime.fromisoformat(ts_str).strftime("%Y-%m-%d  %H:%M")
            except (ValueError, TypeError):
                pass

            icon  = ft.Icon(
                ft.icons.CHECK_CIRCLE if success else ft.icons.ERROR_OUTLINE,
                color=T.GREEN if success else T.RED, size=18,
            )
            dur   = f"{entry.get('duration', 0):.1f}s"
            stats = (
                f"↑ {entry.get('uploaded',0)}  "
                f"↓ {entry.get('downloaded',0)}  "
                f"⚡ {entry.get('conflicts',0)}"
            ) if success else error[:70]

            tile = T.card(ft.Column([
                ft.Row([
                    icon,
                    ft.Text(ts_str, color=T.TEXT, size=13, expand=True),
                    T.caption(dur, T.SUBTEXT0),
                ], spacing=8),
                T.caption(stats, T.GREEN if success else T.RED),
            ], spacing=4))

            list_col.controls.append(tile)

    state.subscribe(_refresh)
    _refresh()

    return ft.Column([
        ft.Container(height=8),
        ft.Container(
            content=list_col, expand=True,
            padding=ft.padding.symmetric(horizontal=0),
        ),
    ], expand=True, spacing=0)
