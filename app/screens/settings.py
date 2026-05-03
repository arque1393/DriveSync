"""app/screens/settings.py — Advanced settings (concurrency, intervals, about)."""
import flet as ft
from app import theme as T
from app.state import AppState


def build(page: ft.Page, state: AppState) -> ft.Control:
    import json, os
    USER_CONFIG_FILE = "user_config.json"

    def _load():
        if os.path.exists(USER_CONFIG_FILE):
            try:
                with open(USER_CONFIG_FILE, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save(cfg):
        with open(USER_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)

    cfg = _load()

    def _int_field(label, key, default, min_val, max_val):
        tf = ft.TextField(
            label=label,
            value=str(cfg.get(key, default)),
            keyboard_type=ft.KeyboardType.NUMBER,
            border_color=T.SURFACE2,
            focused_border_color=T.BLUE,
            label_style=ft.TextStyle(color=T.SUBTEXT1),
            color=T.TEXT,
            bgcolor=T.SURFACE0,
            border_radius=10,
            hint_text=f"{min_val}–{max_val}",
        )
        return tf, key

    scan_f,  sk = _int_field("Scan concurrency",     "scan_concurrency",     30, 5,  100)
    up_f,    uk = _int_field("Upload concurrency",   "upload_concurrency",   5,  1,  20)
    down_f,  dk = _int_field("Download concurrency", "download_concurrency", 5,  1,  20)
    retry_f, rk = _int_field("Download retries",     "download_retries",     4,  0,  10)

    save_lbl = ft.Text("", size=12)

    def on_save(_):
        updated = _load()
        for field, key in [(scan_f, sk), (up_f, uk), (down_f, dk), (retry_f, rk)]:
            try:
                updated[key] = int(field.value.strip())
            except ValueError:
                save_lbl.value = f"⚠️  {key} must be a number."
                save_lbl.color = T.RED
                page.update()
                return
        _save(updated)
        save_lbl.value = "✅  Saved — restart to apply."
        save_lbl.color = T.GREEN
        page.update()

    # ── About ─────────────────────────────────────────────────────────────────
    try:
        import flet
        flet_ver = flet.__version__
    except Exception:
        flet_ver = "unknown"

    about = T.card(ft.Column([
        T.label("About"),
        ft.Divider(height=8, color=T.BORDER),
        ft.Row([T.caption("Engine", T.SUBTEXT0), T.caption("asyncio + aiohttp", T.TEXT)],
               alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.Row([T.caption("UI framework", T.SUBTEXT0), T.caption(f"Flet {flet_ver}", T.TEXT)],
               alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.Row([T.caption("Branch", T.SUBTEXT0), T.caption("mobile-app", T.BLUE)],
               alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
    ], spacing=8))

    return ft.Column([
        ft.Container(height=8),
        T.card(ft.Column([
            T.label("⚡  Concurrency"),
            T.caption("Higher values = faster sync; lower = fewer errors on slow networks",
                      T.OVERLAY0),
            ft.Container(height=4),
            scan_f, up_f, down_f, retry_f,
        ], spacing=10)),
        ft.Container(height=12),
        ft.ElevatedButton(
            "Save Settings",
            icon=ft.icons.SAVE,
            on_click=on_save,
            bgcolor=T.BLUE, color=T.CRUST,
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=10)),
            width=float("inf"),
        ),
        save_lbl,
        ft.Container(height=16),
        about,
        ft.Container(height=16),
    ], scroll=ft.ScrollMode.AUTO, expand=True, spacing=0)
