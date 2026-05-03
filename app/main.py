"""app/main.py — DriveSync mobile app entry point.

Run on desktop to develop/test:
    flet run app/main.py

Build Android APK:
    flet build apk --project DriveSync

Routing
───────
  /            Home dashboard
  /conflicts   Conflict list
  /conflicts/N Conflict detail for index N
  /history     Sync history
  /setup       Folder & auth config
  /settings    Advanced settings
"""
import sys
import os

# Ensure the project root is on sys.path so core modules are importable
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import flet as ft
from app import theme as T
from app.state import AppState
from app.services.sync_service import SyncService

import app.screens.home             as home_screen
import app.screens.setup            as setup_screen
import app.screens.conflict_resolver as conflict_screen
import app.screens.history          as history_screen
import app.screens.settings         as settings_screen


# ── Tab index ─────────────────────────────────────────────────────────────────
_NAV_ROUTES = ["/", "/history", "/settings"]
_NAV_IDX    = {r: i for i, r in enumerate(_NAV_ROUTES)}


def main(page: ft.Page) -> None:
    # ── Page setup ────────────────────────────────────────────────────────────
    page.title      = "DriveSync"
    page.theme_mode = ft.ThemeMode.DARK
    page.theme      = T.DARK_THEME
    page.bgcolor    = T.BASE
    page.padding    = 0
    page.fonts      = {}   # system fonts

    # ── Shared state and service ──────────────────────────────────────────────
    state = AppState()
    svc   = SyncService(state, page.update)

    # ── Bottom navigation bar (shown on root tabs only) ───────────────────────
    def _nav_change(e):
        idx   = e.control.selected_index
        route = _NAV_ROUTES[idx]
        page.go(route)

    nav_bar = ft.NavigationBar(
        selected_index=0,
        bgcolor=T.MANTLE,
        indicator_color=ft.colors.with_opacity(0.2, T.BLUE),
        destinations=[
            ft.NavigationBarDestination(
                icon=ft.icons.HOME_OUTLINED,
                selected_icon=ft.icons.HOME,
                label="Home",
            ),
            ft.NavigationBarDestination(
                icon=ft.icons.HISTORY_OUTLINED,
                selected_icon=ft.icons.HISTORY,
                label="History",
            ),
            ft.NavigationBarDestination(
                icon=ft.icons.SETTINGS_OUTLINED,
                selected_icon=ft.icons.SETTINGS,
                label="Settings",
            ),
        ],
        on_change=_nav_change,
    )

    # ── AppBar factory ────────────────────────────────────────────────────────
    def _appbar(title: str, show_back: bool = False) -> ft.AppBar:
        actions = []
        if title == "Home":
            def _goto_setup(_):
                page.go("/setup")
            actions.append(ft.IconButton(
                icon=ft.icons.SETTINGS_OUTLINED,
                icon_color=T.SUBTEXT0,
                on_click=_goto_setup,
                tooltip="Setup",
            ))
            # Conflict badge
            if state.conflict_count > 0:
                actions.append(ft.IconButton(
                    icon=ft.icons.WARNING_AMBER_ROUNDED,
                    icon_color=T.YELLOW,
                    on_click=lambda _: page.go("/conflicts"),
                    tooltip=f"{state.conflict_count} conflict(s)",
                ))

        return ft.AppBar(
            title=ft.Text(title, color=T.TEXT, size=18, weight=ft.FontWeight.W_600),
            bgcolor=T.MANTLE,
            color=T.TEXT,
            leading=ft.IconButton(
                icon=ft.icons.ARROW_BACK_IOS_NEW,
                icon_color=T.SUBTEXT0,
                on_click=lambda _: page.views.pop() or page.update(),
                visible=show_back,
            ) if show_back else None,
            actions=actions,
            elevation=0,
            center_title=False,
        )

    # ── Route handler ─────────────────────────────────────────────────────────
    def route_change(e: ft.RouteChangeEvent) -> None:
        route = page.route
        page.views.clear()

        # Determine whether to show the bottom nav
        is_root = route in _NAV_ROUTES
        nb      = nav_bar if is_root else None
        if is_root:
            nav_bar.selected_index = _NAV_IDX.get(route, 0)

        if route == "/":
            page.views.append(ft.View(
                "/",
                controls=[
                    _appbar("DriveSync"),
                    ft.Container(
                        content=home_screen.build(page, state, svc),
                        expand=True,
                        padding=ft.padding.symmetric(horizontal=16),
                    ),
                ],
                navigation_bar=nb,
                bgcolor=T.BASE,
                padding=0,
            ))

        elif route == "/history":
            page.views.append(ft.View(
                "/history",
                controls=[
                    _appbar("Sync History"),
                    ft.Container(
                        content=history_screen.build(page, state),
                        expand=True,
                        padding=ft.padding.symmetric(horizontal=16),
                    ),
                ],
                navigation_bar=nb,
                bgcolor=T.BASE,
                padding=0,
            ))

        elif route == "/settings":
            page.views.append(ft.View(
                "/settings",
                controls=[
                    _appbar("Settings"),
                    ft.Container(
                        content=settings_screen.build(page, state),
                        expand=True,
                        padding=ft.padding.symmetric(horizontal=16),
                    ),
                ],
                navigation_bar=nb,
                bgcolor=T.BASE,
                padding=0,
            ))

        elif route == "/setup":
            page.views.append(ft.View(
                "/setup",
                controls=[
                    _appbar("Setup", show_back=True),
                    ft.Container(
                        content=setup_screen.build(page, state),
                        expand=True,
                        padding=ft.padding.symmetric(horizontal=16),
                    ),
                ],
                bgcolor=T.BASE,
                padding=0,
            ))

        elif route == "/conflicts":
            page.views.append(ft.View(
                "/conflicts",
                controls=[
                    _appbar("Conflicts", show_back=True),
                    ft.Container(
                        content=conflict_screen.build(page, state),
                        expand=True,
                        padding=ft.padding.symmetric(horizontal=16),
                    ),
                ],
                bgcolor=T.BASE,
                padding=0,
            ))

        elif route.startswith("/conflicts/"):
            try:
                idx   = int(route.split("/")[-1])
                items = state.pending_conflicts
                if 0 <= idx < len(items):
                    def _on_resolved(item):
                        if item in state.pending_conflicts:
                            state.pending_conflicts.remove(item)
                        state.notify()
                        page.go("/conflicts")

                    detail = conflict_screen.build_item(
                        page, state, items[idx], idx, len(items), _on_resolved
                    )
                    page.views.append(ft.View(
                        route,
                        controls=[
                            _appbar(f"Conflict {idx+1}/{len(items)}", show_back=True),
                            ft.Container(content=detail, expand=True, padding=0),
                        ],
                        bgcolor=T.BASE,
                        padding=0,
                    ))
                else:
                    page.go("/conflicts")
                    return
            except (ValueError, IndexError):
                page.go("/conflicts")
                return

        else:
            page.go("/")
            return

        page.update()

    def view_pop(e: ft.ViewPopEvent) -> None:
        if len(page.views) > 1:
            page.views.pop()
            top = page.views[-1]
            page.go(top.route)

    page.on_route_change = route_change
    page.on_view_pop     = view_pop
    page.go("/")


if __name__ == "__main__":
    ft.app(target=main)
