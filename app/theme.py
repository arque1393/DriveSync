"""app/theme.py — Catppuccin Mocha palette + Material Design 3 tokens.

Matches the desktop Tkinter GUI colour scheme so the two UIs feel cohesive.
"""
import flet as ft

# ── Catppuccin Mocha ──────────────────────────────────────────────────────────
BASE     = "#1e1e2e"
MANTLE   = "#181825"
CRUST    = "#11111b"
SURFACE0 = "#313244"
SURFACE1 = "#45475a"
SURFACE2 = "#585b70"
OVERLAY0 = "#6c7086"
OVERLAY1 = "#7f849c"
TEXT     = "#cdd6f4"
SUBTEXT0 = "#a6adc8"
SUBTEXT1 = "#bac2de"
BLUE     = "#89b4fa"
SAPPHIRE = "#74c7ec"
SKY      = "#89dceb"
TEAL     = "#94e2d5"
GREEN    = "#a6e3a1"
YELLOW   = "#f9e2af"
PEACH    = "#fab387"
RED      = "#f38ba8"
MAROON   = "#eba0ac"
MAUVE    = "#cba6f7"
PINK     = "#f5c2e7"

# ── Semantic aliases ──────────────────────────────────────────────────────────
BG          = BASE
CARD_BG     = SURFACE0
BORDER      = SURFACE1
ACCENT      = BLUE
ACCENT_DARK = SAPPHIRE
SUCCESS     = GREEN
ERROR       = RED
WARNING     = YELLOW
MUTED       = SUBTEXT0

# ── Flet ColorScheme ──────────────────────────────────────────────────────────
COLOR_SCHEME = ft.ColorScheme(
    primary=BLUE,
    primary_container=SURFACE1,
    on_primary=CRUST,
    secondary=MAUVE,
    secondary_container=SURFACE0,
    on_secondary=CRUST,
    surface=SURFACE0,
    on_surface=TEXT,
    error=RED,
    on_error=CRUST,
    outline=SURFACE2,
)

DARK_THEME = ft.Theme(
    color_scheme=COLOR_SCHEME,
    color_scheme_seed=BLUE,
    visual_density=ft.VisualDensity.COMFORTABLE,
)

# ── Text styles ───────────────────────────────────────────────────────────────
def title_large(text: str, color: str = TEXT) -> ft.Text:
    return ft.Text(text, size=22, weight=ft.FontWeight.BOLD, color=color)

def title_medium(text: str, color: str = TEXT) -> ft.Text:
    return ft.Text(text, size=16, weight=ft.FontWeight.W_600, color=color)

def body(text: str, color: str = TEXT) -> ft.Text:
    return ft.Text(text, size=14, color=color)

def caption(text: str, color: str = SUBTEXT0) -> ft.Text:
    return ft.Text(text, size=12, color=color)

def label(text: str, color: str = SUBTEXT1) -> ft.Text:
    return ft.Text(text, size=13, weight=ft.FontWeight.W_500, color=color)

# ── Button helpers ────────────────────────────────────────────────────────────
def primary_btn(text: str, on_click=None, icon=None) -> ft.ElevatedButton:
    return ft.ElevatedButton(
        text=text,
        icon=icon,
        on_click=on_click,
        bgcolor=BLUE,
        color=CRUST,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=10)),
    )

def secondary_btn(text: str, on_click=None, icon=None) -> ft.OutlinedButton:
    return ft.OutlinedButton(
        text=text,
        icon=icon,
        on_click=on_click,
        style=ft.ButtonStyle(
            color=TEXT,
            side=ft.BorderSide(1, SURFACE2),
            shape=ft.RoundedRectangleBorder(radius=10),
        ),
    )

def danger_btn(text: str, on_click=None, icon=None) -> ft.ElevatedButton:
    return ft.ElevatedButton(
        text=text,
        icon=icon,
        on_click=on_click,
        bgcolor=RED,
        color=CRUST,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=10)),
    )

# ── Card helper ───────────────────────────────────────────────────────────────
def card(content: ft.Control, padding: int = 16) -> ft.Container:
    return ft.Container(
        content=content,
        bgcolor=CARD_BG,
        border_radius=12,
        padding=padding,
        border=ft.border.all(1, BORDER),
    )

# ── Divider ───────────────────────────────────────────────────────────────────
def divider() -> ft.Divider:
    return ft.Divider(height=1, color=BORDER)

# ── Status badge ──────────────────────────────────────────────────────────────
def status_badge(text: str, color: str) -> ft.Container:
    return ft.Container(
        content=ft.Text(text, size=11, weight=ft.FontWeight.W_600, color=CRUST),
        bgcolor=color,
        border_radius=20,
        padding=ft.padding.symmetric(horizontal=10, vertical=4),
    )
