"""app/screens/setup.py — Folder & credentials configuration screen."""
import json
import os

import flet as ft
from app import theme as T
from app.state import AppState

USER_CONFIG_FILE = "user_config.json"


def _load() -> dict:
    if os.path.exists(USER_CONFIG_FILE):
        try:
            with open(USER_CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save(cfg: dict) -> None:
    with open(USER_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def build(page: ft.Page, state: AppState) -> ft.Control:
    cfg = _load()

    # ── Fields ────────────────────────────────────────────────────────────────
    local_field = ft.TextField(
        label="Local Folder",
        value=cfg.get("local_folder", ""),
        hint_text="/storage/emulated/0/GDrive/Obsidian",
        border_color=T.SURFACE2,
        focused_border_color=T.BLUE,
        label_style=ft.TextStyle(color=T.SUBTEXT1),
        color=T.TEXT,
        bgcolor=T.SURFACE0,
        border_radius=10,
    )

    drive_name_field = ft.TextField(
        label="Google Drive Folder Name",
        value=cfg.get("drive_folder_name", "Obsidian"),
        border_color=T.SURFACE2,
        focused_border_color=T.BLUE,
        label_style=ft.TextStyle(color=T.SUBTEXT1),
        color=T.TEXT,
        bgcolor=T.SURFACE0,
        border_radius=10,
    )

    folder_id_field = ft.TextField(
        label="Drive Folder ID  (paste from URL)",
        value=cfg.get("drive_folder_id", ""),
        hint_text="16DYu4feYq-...",
        border_color=T.SURFACE2,
        focused_border_color=T.BLUE,
        label_style=ft.TextStyle(color=T.SUBTEXT1),
        color=T.TEXT,
        bgcolor=T.SURFACE0,
        border_radius=10,
    )

    interval_field = ft.TextField(
        label="Sync Interval (seconds)",
        value=str(cfg.get("sync_interval", 300)),
        keyboard_type=ft.KeyboardType.NUMBER,
        border_color=T.SURFACE2,
        focused_border_color=T.BLUE,
        label_style=ft.TextStyle(color=T.SUBTEXT1),
        color=T.TEXT,
        bgcolor=T.SURFACE0,
        border_radius=10,
    )

    # ── Verify folder ID ──────────────────────────────────────────────────────
    verify_result = ft.Text("", size=12, color=T.SUBTEXT0)

    def on_verify(_):
        fid = folder_id_field.value.strip()
        if not fid:
            verify_result.value = "⚠️  Paste a folder ID first."
            verify_result.color = T.YELLOW
            page.update()
            return

        verify_result.value = "⏳  Checking…"
        verify_result.color = T.SUBTEXT0
        page.update()

        import threading
        def _check():
            try:
                from auth import get_credentials
                from googleapiclient.discovery import build
                creds = get_credentials()
                svc   = build("drive", "v3", credentials=creds)
                meta  = svc.files().get(
                    fileId=fid, fields="name,mimeType",
                    supportsAllDrives=True,
                ).execute()
                name = meta.get("name", "?")
                if meta.get("mimeType") == "application/vnd.google-apps.folder":
                    verify_result.value = f"✅  Found: \"{name}\" — access confirmed"
                    verify_result.color = T.GREEN
                    drive_name_field.value = name
                else:
                    verify_result.value = f"⚠️  \"{name}\" is a file, not a folder."
                    verify_result.color = T.YELLOW
            except Exception as exc:
                msg = str(exc)
                if "404" in msg or "notFound" in msg:
                    msg = "Not found — check the ID or share the folder first."
                elif "403" in msg:
                    msg = "Access denied — share with your Google account."
                verify_result.value = f"✗  {msg[:80]}"
                verify_result.color = T.RED
            finally:
                page.update()

        threading.Thread(target=_check, daemon=True).start()

    # ── Auth status ───────────────────────────────────────────────────────────
    from config import TOKEN_FILE
    token_exists = os.path.exists(TOKEN_FILE)
    auth_status  = ft.Text(
        "✅  Signed in (token cached)" if token_exists
        else "⚠️  Not signed in — first run will open a browser",
        color=T.GREEN if token_exists else T.YELLOW,
        size=13,
    )

    def on_sign_out(_):
        try:
            if os.path.exists(TOKEN_FILE):
                os.unlink(TOKEN_FILE)
            auth_status.value = "⚠️  Signed out — next sync will need browser login"
            auth_status.color = T.YELLOW
        except OSError as e:
            auth_status.value = f"Error: {e}"
            auth_status.color = T.RED
        page.update()

    # ── Save ──────────────────────────────────────────────────────────────────
    save_result = ft.Text("", size=12)

    def on_save(_):
        local  = local_field.value.strip()
        dname  = drive_name_field.value.strip()
        fid    = folder_id_field.value.strip()

        try:
            interval = int(interval_field.value.strip())
        except ValueError:
            save_result.value = "⚠️  Interval must be a number."
            save_result.color = T.RED
            page.update()
            return

        if not local:
            save_result.value = "⚠️  Local folder cannot be empty."
            save_result.color = T.RED
            page.update()
            return
        if not dname:
            save_result.value = "⚠️  Drive folder name cannot be empty."
            save_result.color = T.RED
            page.update()
            return

        _save({
            "local_folder":      local,
            "drive_folder_name": dname,
            "drive_folder_id":   fid,
            "sync_interval":     interval,
            "scan_concurrency":  cfg.get("scan_concurrency", 30),
            "upload_concurrency": cfg.get("upload_concurrency", 5),
            "download_concurrency": cfg.get("download_concurrency", 5),
        })
        save_result.value = "✅  Saved — restart to apply changes."
        save_result.color = T.GREEN
        page.update()

    # ── Assemble ──────────────────────────────────────────────────────────────
    return ft.Column(
        controls=[
            ft.Container(height=4),
            T.card(ft.Column([
                T.label("📂  Local Folder"),
                local_field,
            ], spacing=8)),
            ft.Container(height=10),
            T.card(ft.Column([
                T.label("☁   Google Drive"),
                drive_name_field,
                ft.Container(height=6),
                T.label("🔑  Drive Folder ID"),
                T.caption(
                    "drive.google.com/drive/folders/  ← copy the part after /",
                    T.OVERLAY0,
                ),
                folder_id_field,
                verify_result,
                ft.Container(height=4),
                ft.Row([
                    ft.TextButton(
                        "Verify",
                        on_click=on_verify,
                        style=ft.ButtonStyle(color=T.BLUE),
                    ),
                ]),
            ], spacing=6)),
            ft.Container(height=10),
            T.card(ft.Column([
                T.label("⏱   Sync Interval"),
                interval_field,
            ], spacing=8)),
            ft.Container(height=10),
            T.card(ft.Column([
                T.label("🔒  Google Account"),
                auth_status,
                ft.Container(height=6),
                ft.TextButton(
                    "Sign Out",
                    icon=ft.icons.LOGOUT,
                    on_click=on_sign_out,
                    style=ft.ButtonStyle(color=T.RED),
                ),
            ], spacing=6)),
            ft.Container(height=16),
            ft.ElevatedButton(
                "Save Configuration",
                icon=ft.icons.SAVE,
                on_click=on_save,
                bgcolor=T.BLUE,
                color=T.CRUST,
                style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=10)),
                width=float("inf"),
            ),
            save_result,
            ft.Container(height=16),
        ],
        scroll=ft.ScrollMode.AUTO,
        expand=True,
        spacing=0,
    )
