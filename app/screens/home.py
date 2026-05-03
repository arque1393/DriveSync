"""app/screens/home.py — Dashboard screen."""
import flet as ft
from app import theme as T
from app.state import AppState
from app.services.sync_service import SyncService


def build(page: ft.Page, state: AppState, svc: SyncService) -> ft.Control:

    # ── Status card ───────────────────────────────────────────────────────────
    status_icon  = ft.Icon(ft.icons.CHECK_CIRCLE, color=T.GREEN, size=28)
    status_text  = T.title_medium(state.status_line, T.TEXT)
    local_lbl    = T.caption("", T.SUBTEXT0)
    drive_lbl    = T.caption("", T.SUBTEXT0)

    def _refresh_status():
        if state.is_syncing:
            status_icon.name  = ft.icons.SYNC
            status_icon.color = T.BLUE
            status_text.value = state.current_progress or "Syncing…"
        elif state.last_stats and state.last_stats.error:
            status_icon.name  = ft.icons.ERROR_OUTLINE
            status_icon.color = T.RED
            status_text.value = f"Failed: {state.last_stats.error[:55]}"
        else:
            status_icon.name  = ft.icons.CHECK_CIRCLE
            status_icon.color = T.GREEN
            status_text.value = state.status_line

        try:
            from config import LOCAL_FOLDER, DRIVE_FOLDER_NAME
            local_lbl.value = f"📂  {LOCAL_FOLDER}"
            drive_lbl.value = f"☁   {DRIVE_FOLDER_NAME}"
        except Exception:
            pass

    _refresh_status()

    status_card = T.card(
        ft.Column([
            ft.Row([status_icon, status_text], spacing=10),
            ft.Divider(height=8, color=T.BORDER),
            local_lbl,
            drive_lbl,
        ], spacing=6),
    )

    # ── Action buttons ────────────────────────────────────────────────────────
    def on_sync_now(_):
        if not state.is_syncing:
            svc.sync_once()

    def on_preview(_):
        page.go("/preview")

    action_row = ft.Row([
        ft.ElevatedButton(
            "Sync Now", icon=ft.icons.SYNC,
            on_click=on_sync_now,
            bgcolor=T.BLUE, color=T.CRUST,
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=10)),
            expand=True,
        ),
        ft.OutlinedButton(
            "Preview", icon=ft.icons.PREVIEW,
            on_click=on_preview,
            style=ft.ButtonStyle(
                color=T.TEXT,
                side=ft.BorderSide(1, T.SURFACE2),
                shape=ft.RoundedRectangleBorder(radius=10),
            ),
            expand=True,
        ),
    ], spacing=12)

    # ── Auto-sync toggle ──────────────────────────────────────────────────────
    auto_toggle = ft.Switch(
        value=state.auto_sync,
        active_color=T.BLUE,
        on_change=lambda e: _toggle_auto(e.control.value),
    )

    def _toggle_auto(enabled: bool):
        if enabled:
            svc.start_background()
        else:
            svc.stop_background()
        auto_toggle.value = state.auto_sync
        page.update()

    try:
        from config import SYNC_INTERVAL
        interval_label = f"every {SYNC_INTERVAL // 60} min"
    except Exception:
        interval_label = "every 5 min"

    auto_card = T.card(
        ft.Column([
            T.label("Background Sync"),
            ft.Row([
                ft.Text(f"Auto-sync {interval_label}", color=T.SUBTEXT0, size=13),
                auto_toggle,
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ], spacing=8),
    )

    # ── Last cycle stats ──────────────────────────────────────────────────────
    stats_up   = ft.Text("↑ —", color=T.GREEN,   size=13)
    stats_down = ft.Text("↓ —", color=T.BLUE,    size=13)
    stats_conf = ft.Text("⚡ —", color=T.YELLOW,  size=13)
    stats_time = ft.Text("⏱ —", color=T.SUBTEXT0, size=13)

    def _refresh_stats():
        s = state.last_stats
        if s:
            stats_up.value   = f"↑ {s.uploaded} uploaded"
            stats_down.value = f"↓ {s.downloaded} downloaded"
            stats_conf.value = f"⚡ {s.conflicts} conflicts"
            stats_time.value = f"⏱ {s.duration:.1f}s"

    _refresh_stats()

    stats_card = T.card(
        ft.Column([
            T.label("Last Cycle"),
            ft.Row([stats_up, stats_down], spacing=16),
            ft.Row([stats_conf, stats_time], spacing=16),
        ], spacing=8),
    )

    # ── Conflict banner ───────────────────────────────────────────────────────
    conflict_banner = ft.Container(visible=False)

    def _refresh_conflict_banner():
        n = state.conflict_count
        if n > 0:
            conflict_banner.visible = True
            conflict_banner.content = ft.Container(
                content=ft.Row([
                    ft.Icon(ft.icons.WARNING_AMBER_ROUNDED, color=T.YELLOW, size=20),
                    ft.Text(
                        f"{n} unresolved conflict{'s' if n != 1 else ''}",
                        color=T.YELLOW, size=13, expand=True,
                    ),
                    ft.TextButton(
                        "Resolve →",
                        style=ft.ButtonStyle(color=T.BLUE),
                        on_click=lambda _: page.go("/conflicts"),
                    ),
                ], spacing=8),
                bgcolor=ft.colors.with_opacity(0.15, T.YELLOW),
                border=ft.border.all(1, T.YELLOW),
                border_radius=10,
                padding=12,
            )
        else:
            conflict_banner.visible = False

    _refresh_conflict_banner()

    # ── Progress bar ──────────────────────────────────────────────────────────
    progress_bar = ft.ProgressBar(
        visible=False,
        color=T.BLUE,
        bgcolor=T.SURFACE0,
    )
    progress_lbl = ft.Text("", color=T.SUBTEXT0, size=12, visible=False)

    # ── Subscribe to state changes ────────────────────────────────────────────
    def _on_state_change():
        _refresh_status()
        _refresh_stats()
        _refresh_conflict_banner()
        auto_toggle.value  = state.auto_sync
        progress_bar.visible = state.is_syncing
        progress_lbl.visible = state.is_syncing
        progress_lbl.value   = state.current_progress or ""
        try:
            page.update()
        except Exception:
            pass

    state.subscribe(_on_state_change)

    # ── Assemble ──────────────────────────────────────────────────────────────
    return ft.Column(
        controls=[
            ft.Container(height=8),
            status_card,
            ft.Container(height=12),
            action_row,
            ft.Container(height=12),
            progress_bar,
            progress_lbl,
            ft.Container(height=4),
            auto_card,
            ft.Container(height=12),
            stats_card,
            ft.Container(height=12),
            conflict_banner,
        ],
        scroll=ft.ScrollMode.AUTO,
        expand=True,
        spacing=0,
    )
