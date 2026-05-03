# DriveSync — Mobile App Plan

> **Branch:** `mobile-app`  
> **Goal:** Android APK (and optionally iOS IPA) with all CLI features exposed
> through a native UI, automatic background sync, notification controls, and a
> visual conflict resolver.

---

## 1. Technology Decision

### Why Flet

| Framework | Language | Android APK | Background | Conflict UI | Verdict |
|-----------|----------|-------------|------------|-------------|---------|
| **Flet** | Python | `flet build apk` | Via thread + Android API | Rich widgets | ✅ Chosen |
| Kivy | Python | Buildozer | Python service | Custom widgets | ⚠️ Mature but ugly |
| BeeWare/Briefcase | Python | Briefcase | Limited | Native OS | ⚠️ Limited widgets |
| React Native + Python | JS + Python | Expo | Full support | Complex | ❌ Two languages |
| Flutter alone | Dart | Full support | Full support | Full support | ❌ Rewrite sync engine |

**Flet** is built on Flutter (Google's production UI framework) but written in
pure Python.  This means:
- Our entire sync engine (`drive_ops_async.py`, `google_drive_sync.py`) is
  reused **unchanged**.
- `flet build apk` produces a release APK with zero Dart/Java code.
- Flutter's Material Design 3 gives a polished, modern look out of the box.
- Flet is async-native — `asyncio.gather` in the sync engine works directly.

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Android APK                                  │
│                                                                     │
│  ┌───────────────────────┐     ┌──────────────────────────────┐     │
│  │   Flet UI Layer       │     │   Android System Layer       │     │
│  │  (Flutter renderer)   │     │                              │     │
│  │                       │     │  Foreground Service          │     │
│  │  HomeScreen           │◄───►│  (persistent notification)   │     │
│  │  SetupScreen          │     │                              │     │
│  │  SyncScreen           │     │  WorkManager                 │     │
│  │  ConflictScreen       │     │  (scheduled background sync) │     │
│  │  HistoryScreen        │     │                              │     │
│  │  SettingsScreen       │     │  Notification with actions   │     │
│  └────────┬──────────────┘     └──────────┬───────────────────┘     │
│           │                               │                         │
│           └──────────────┬────────────────┘                         │
│                          │                                          │
│              ┌───────────▼────────────┐                             │
│              │   Core Sync Engine     │   ← reused unchanged        │
│              │                        │                             │
│              │  google_drive_sync.py  │                             │
│              │  drive_ops_async.py    │                             │
│              │  drive_api.py          │                             │
│              │  auth.py (OAuth)       │                             │
│              │  metadata.py           │                             │
│              │  config.py             │                             │
│              └────────────────────────┘                             │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Project Structure

```
DriveSync/
├── app/                          ← NEW: all mobile-specific code
│   ├── main.py                   ← Flet entry point (replaces main.py for mobile)
│   ├── theme.py                  ← colours, fonts, Material Design tokens
│   ├── state.py                  ← shared app state (reactive)
│   │
│   ├── screens/
│   │   ├── home.py               ← dashboard + manual sync trigger
│   │   ├── setup.py              ← folder config (replaces sync_setup.py GUI)
│   │   ├── sync_progress.py      ← live upload/download progress
│   │   ├── conflict_resolver.py  ← visual diff + per-file resolution ← KEY SCREEN
│   │   ├── history.py            ← recent sync logs
│   │   └── settings.py           ← interval, concurrency, auth
│   │
│   ├── widgets/
│   │   ├── file_diff_view.py     ← side-by-side text diff widget
│   │   ├── sync_status_card.py   ← status chip + progress ring
│   │   ├── file_list_tile.py     ← file row with icon + status badge
│   │   └── notification_bar.py   ← in-app notification strip
│   │
│   ├── services/
│   │   ├── sync_service.py       ← wraps GoogleDriveSync for mobile
│   │   ├── background.py         ← Android Foreground Service bridge
│   │   └── conflict_store.py     ← holds pending conflicts between cycles
│   │
│   └── assets/
│       ├── icon.png
│       └── splash.png
│
├── buildozer.spec                ← Android build config (alternative path)
├── pyproject.toml                ← already exists, add flet dependency
│
├── google_drive_sync.py          ← unchanged
├── drive_ops_async.py            ← unchanged
├── drive_api.py                  ← unchanged
├── auth.py                       ← minor: add device-flow for headless mobile
├── config.py                     ← unchanged
├── metadata.py                   ← unchanged
└── local_ops.py                  ← unchanged
```

---

## 4. Screen Designs

### 4.1 Home Screen

```
┌─────────────────────────────────────────────┐
│  ☁ DriveSync              [⚙]  [👤]        │  ← AppBar
├────────────────────────────────────────────┤
│                                            │
│  ┌─────────────────────────────────────┐   │
│  │  ✅ Last sync:  2 min ago          │   │  ← Status Card
│  │  📂 /storage/GDrive/Obsidian       │   │
│  │  ☁  Obsidian  (853 files)          │   │
│  └─────────────────────────────────────┘   │
│                                            │
│  ┌──────────────┐  ┌──────────────────┐    │
│  │ 🔄 Sync Now │   │ 🔍 Preview      │    │  ← Action Buttons
│  └──────────────┘  └──────────────────┘    │
│                                            │
│  ── Background Sync ─────────────────────  │
│  Auto-sync every  [▼ 5 minutes]            │
│  [●─────────────────○] ON                  │  ← Toggle Switch
│                                            │
│  ── Last Cycle ──────────────────────────  │
│  ↑ 3 uploaded    ↓ 0 downloaded            │
│  ⚡ 1 conflict   ⏱ 6.2s                   │  ← Stats Row
│                                            │
│  ⚡ 1 unresolved conflict  [Resolve →]     │  ← Conflict Banner
│                                            │
└────────────────────────────────────────────┘
│  [🏠 Home] [📋 History] [⚙ Settings]      │  ← Bottom Nav
└─────────────────────────────────────────────┘
```

---

### 4.2 Sync Progress Screen

```
┌─────────────────────────────────────────────┐
│  ← Sync in Progress                         │
├─────────────────────────────────────────────┤
│                                             │
│  ┌─────────────────────────────────────┐    │
│  │ ☁ Scanning Drive...                │    │
│  │ ████████████░░░░░░  853 / 900       │    │
│  └─────────────────────────────────────┘    │
│                                             │
│  ── Uploading ───────────────────────────   │
│  ↑ Notes/2026-05-01.md      ████░░  80%     │
│  ↑ German/Vocabulary.md     ██░░░░  40%     │
│  ↑ Projects/plan.md         ░░░░░░   0%     │ 
│                                             │
│  ── Downloading ─────────────────────────   │
│  ✅ Books/Quantum.pdf        Complete      │
│  ↓ Configs/app.json          ██░░░░  35%    │
│                                             │
│  ─────────────────────────────────────────  │
│  [    ■ Cancel Sync    ]                    │
└─────────────────────────────────────────────┘
```

---

### 4.3 Conflict Resolver Screen  *(key feature)*

> **How conflicts work now (updated from asyncio branch):**
> The sync engine already keeps BOTH versions automatically:
> - `research.local.HOSTNAME.md` ← your local changes
> - `research.drive.md` ← Drive's changes
> A ghost entry in metadata prevents re-download loops.
> The resolver's job is to review those two files and pick a winner.

```
┌─────────────────────────────────────────────┐
│  ← Conflict Resolver      2 of 5            │  ← counter
├─────────────────────────────────────────────┤
│  ⚡ Notes/Research.md                       │  ← original path
│  Conflict resolved — review both copies     │
├─────────────────────────────────────────────┤
│  [LOCAL copy]     [DRIVE copy]              │  ← tab bar
├─────────────────────────────────────────────┤
│  research.local.DESKTOP-ABC123.md           │
│  Modified: Today 14:22  ·  4.2 KB           │
│  ─────────────────────────────────────────  │
│  # Research Notes 2026                      │
│  +Added quantum circuit notes               │  ← green = added
│  -removed draft section                     │  ← red   = removed
│                                             │
│  ════ vs ════════════════════════════════   │
│  research.drive.md                          │
│  Modified: Yesterday 09:15  ·  3.8 KB       │
│  ─────────────────────────────────────────  │
│  # Research Notes 2026                      │
│  +Added papers section                      │
│                                             │
└─────────────────────────────────────────────┤
│  [✓ Mine is final]  [↓ Theirs is final]    │
│  [≡ Keep both]      [⏭ Decide later]       │
└─────────────────────────────────────────────┘
```

**Resolution actions:**

| Button | What happens |
|--------|-------------|
| ✓ Mine is final | Delete `.drive` copy, rename `.local.DEVICE` → original, upload |
| ↓ Theirs is final | Delete `.local.DEVICE` copy, rename `.drive` → original, update metadata |
| ≡ Keep both | Remove ghost entry — both files stay as separate files |
| ⏭ Decide later | Dismiss for now — ghost entry stays, both copies remain |

```
  ── Binary / Image files ──────────────────
  (shows file metadata + size comparison, no diff)

  ── Conflict list view ────────────────────
  [✓ All mine]  [↓ All theirs]  [≡ Keep all]
```

---

### 4.4 History Screen

```
┌─────────────────────────────────────────────┐
│  ← Sync History                             │
├─────────────────────────────────────────────┤
│  ┌─────────────────────────────────────┐    │
│  │ ✅ 2026-05-02  14:22  —  6.2s      │    │
│  │    ↑ 3  ↓ 0  ⚡ 1  (resolved)      │    │
│  └─────────────────────────────────────┘    │
│  ┌─────────────────────────────────────┐    │
│  │ ✅ 2026-05-02  09:05  —  8.4s      │    │
│  │    ↑ 0  ↓ 12  ⚡ 0                 │    │
│  └─────────────────────────────────────┘    │
│  ┌─────────────────────────────────────┐    │
│  │ ❌ 2026-05-01  23:10  — Failed     │     │
│  │    SSL: Certificate verify failed   │    │
│  └─────────────────────────────────────┘    │
│  [ Load more... ]                           │
└─────────────────────────────────────────────┘
```

---

### 4.5 Setup / Config Screen

```
┌─────────────────────────────────────────────┐
│  ← Setup                                    │
├─────────────────────────────────────────────┤
│  📂 Local Folder                           │
│  ┌───────────────────────────────────────┐  │
│  │ /storage/emulated/0/GDrive/Obsidian   │  │
│  └───────────────────────────────────────┘  │
│  [Browse...]                                │
│                                             │
│  ☁ Drive Folder Name                       │
│  ┌───────────────────────────────────────┐  │
│  │ Obsidian                              │  │
│  └───────────────────────────────────────┘  │
│                                             │
│  🔑 Drive Folder ID                        │
│  ┌─────────────────────────────────────┐    │
│  │ 16DYu4feYq-TfYx86j6...              │    │
│  └─────────────────────────────────────┘    │
│  [Verify ✓]  [Browse Drive...]             │
│  ✅  "Obsidian" — access confirmed         │
│                                             │
│  🔒 Google Account                         │
│  [● Signed in as: aritra@accenture.com]     │
│  [Sign Out]                                 │
│                                             │
│          [  Save Configuration  ]           │
└─────────────────────────────────────────────┘
```

---

## 5. Notification Design

### Persistent Notification (Foreground Service)

```
╔══════════════════════════════════════════════╗
║  ☁ DriveSync                          [✕]   ║
║  Syncing... ████████░░░░  68%                ║
║  ↑ 5 files  ↓ 2 files  remaining             ║
║  [⏸ Pause]              [Open App]          ║
╚══════════════════════════════════════════════╝

When idle:
╔══════════════════════════════════════════════╗
║  ☁ DriveSync                          [✕]   ║
║  ✅ All synced · Last: 3 min ago            ║
║  Next sync in: 2:14                          ║
║  [🔄 Sync Now]          [Open App]          ║
╚══════════════════════════════════════════════╝

When conflict detected:
╔══════════════════════════════════════════════╗
║  ⚡ DriveSync — Conflict Detected      [✕]  ║
║  3 files need your attention                 ║
║  [Resolve Now]          [Skip for now]       ║
╚══════════════════════════════════════════════╝
```

### Notification Actions (controlled without opening app)

| Action button | Behaviour |
|---------------|-----------|
| **Sync Now** | Triggers immediate sync cycle |
| **Pause / Resume** | Suspends/resumes background auto-sync |
| **Resolve Now** | Opens app directly on Conflict Resolver screen |
| **Skip for now** | Dismisses conflict banner until next cycle |

---

## 6. Background Sync Service

### Android Foreground Service

Android kills background processes unless a **Foreground Service** runs with a
visible notification.  The service:

1. Starts when auto-sync is toggled ON.
2. Runs `asyncio.run(sync_engine.run())` on a dedicated thread.
3. Posts notification updates via Android's `NotificationManager`.
4. Listens for broadcast intents from notification buttons (Pause, Sync Now, etc.).
5. Stops gracefully when the user taps the × on the notification.

```python
# app/services/background.py  (simplified)

from jnius import autoclass   # bridges Python → Android Java API

Service     = autoclass('android.app.Service')
Notification = autoclass('android.app.Notification')
NotifManager = autoclass('android.app.NotificationManager')

class DrivesSyncForegroundService:
    """
    Wraps the sync engine in an Android Foreground Service.
    Keeps the process alive and shows the persistent notification.
    """
    def start(self): ...
    def stop(self): ...
    def update_notification(self, status: str, progress: float): ...
    def on_action(self, action: str): ...   # Pause / SyncNow / Resolve
```

### WorkManager (scheduled sync when app is closed)

For syncs that run even when the app is fully closed (e.g., nightly sync),
Android's `WorkManager` enqueues a `PeriodicWorkRequest`:

```
WorkManager
  └── DrivesSyncWorker (PeriodicWorkRequest, interval=user_setting)
        └── Starts a short-lived process → runs one sync cycle → exits
```

---

## 7. Authentication on Mobile

> **Updated:** Auth now uses OAuth user credentials (not service account).
> Service accounts were tried but have zero Drive storage quota —
> they can read but cannot create new files on personal Drive.

### Problem
The current OAuth flow calls `flow.run_local_server()` which opens a browser.
On Android, this needs a WebView or the Device Authorization flow.

### Solution: Android Custom Tab / WebView

```
1. App generates OAuth URL
2. Opens Chrome Custom Tab (or in-app WebView)
3. User approves on Google's sign-in page
4. Google redirects to app via custom URI scheme:
   drivesync://oauth/callback?code=<auth_code>
5. App intercepts redirect, exchanges code for token
6. Token saved to app's private storage (encrypted)
```

### Fallback: Device Authorization Grant (headless)
If WebView is unavailable, show a 6-character code and URL:
```
Visit: accounts.google.com/device
Enter code: ABC-XYZ
```

---

## 8. CLI Feature → Mobile UI Mapping

| CLI flag | Mobile equivalent |
|----------|-------------------|
| `--run` | Home screen → Background sync toggle ON |
| `--sync-once` | Home screen → "Sync Now" button |
| `--dry-run` | Home screen → "Preview" button → Preview screen |
| `--setup` | Bottom nav → Setup screen |
| `--interval N` | Settings screen → "Sync every N minutes" slider |
| `--help` | Settings screen → About / Help section |
| Conflict warning | Home screen → conflict banner + Conflict Resolver screen |
| Progress output | Sync Progress screen (live) |
| Error messages | History screen + in-app snackbar |

---

## 9. Visual Conflict Resolver — Detailed Design

### For text files (.md, .txt, .json, .py, etc.)

Uses a **unified diff** displayed in a scrollable split pane.

```
Colour coding:
  🟢 Green background  — lines added in this version
  🔴 Red background    — lines removed from this version
  ⬜ White             — unchanged lines
  🔵 Blue border       — currently selected hunk
```

**Resolution options per file:**

| Button | Action |
|--------|--------|
| ✓ Keep Local | Upload local → Drive, discard Drive version |
| ↓ Take Drive | Download Drive → local, discard local version |
| ✎ Merge (text only) | Open text editor with both versions |
| ⏭ Skip | Defer this file to the next cycle |

### For binary files (.pdf, .png, .xlsx, etc.)

Shows metadata side-by-side:

```
┌────────────────────┬────────────────────┐
│ LOCAL              │ DRIVE              │
├────────────────────┼────────────────────┤
│ 📄 book.pdf        │ 📄 book.pdf       │
│ Size: 4.2 MB       │ Size: 4.1 MB       │
│ Modified: today    │ Modified: yesterday│
│ [🔍 Preview]       │ [🔍 Preview]      │
└────────────────────┴────────────────────┘
```

### Conflict queue management

All conflicts detected during a sync cycle are stored in
`app/services/conflict_store.py` and persist until resolved.
A badge on the home screen and notification shows the unresolved count.

---

## 10. Implementation Phases

### Phase 1 — Foundation (Week 1–2)

- [ ] Add Flet dependency (`uv add flet`)
- [ ] Create `app/main.py` entry point with bottom navigation
- [ ] `HomeScreen` — static layout, no sync yet
- [ ] `SetupScreen` — replaces Tkinter `sync_setup.py`
- [ ] `SettingsScreen` — config reader/writer
- [ ] Wire `config.py` → screen fields (bidirectional)
- [ ] Test on desktop: `flet run app/main.py`

### Phase 2 — Core Sync Integration (Week 3–4)

- [ ] `SyncService` wrapping `GoogleDriveSync`
- [ ] Live progress events from sync engine → UI updates
- [ ] `SyncProgressScreen` with real-time bars
- [ ] `HistoryScreen` reading from sync log
- [ ] Manual sync trigger ("Sync Now") wired end-to-end
- [ ] Dry-run / Preview screen wired

### Phase 3 — Conflict Resolver (Week 5–6)

- [ ] `conflict_store.py` — captures conflicts during sync
- [ ] `ConflictResolverScreen` — file list with status badges
- [ ] `FileDiffView` widget — unified diff for text files
- [ ] Binary file metadata view
- [ ] Resolution actions: Keep Local / Take Drive / Skip
- [ ] Bulk resolve actions
- [ ] Re-run upload/download after resolution

### Phase 4 — Background + Notifications (Week 7–8)

- [ ] Android Foreground Service via `jnius`
- [ ] Persistent notification with progress
- [ ] Notification action buttons (Pause / Sync Now / Resolve)
- [ ] WorkManager periodic sync (works when app is closed)
- [ ] Battery optimisation handling

### Phase 5 — Auth + Polish (Week 9–10)

- [ ] OAuth WebView flow for first-time sign-in
- [ ] Token storage in Android Keystore (encrypted)
- [ ] App icon + splash screen
- [ ] Dark / Light theme toggle
- [ ] Accessibility (font scaling, screen reader labels)
- [ ] Error handling + user-friendly error screens

### Phase 6 — Build & Release (Week 11–12)

- [ ] `flet build apk --release`
- [ ] Test on physical device
- [ ] APK signing with keystore
- [ ] (Optional) iOS: `flet build ipa` on macOS

---

## 11. Steps to Build the APK

### Prerequisites

```bash
# 1. Install Flutter SDK
# https://flutter.dev/docs/get-started/install

# 2. Install Android SDK + NDK (via Android Studio or sdkmanager)
# Minimum: Android SDK 21 (Android 5.0)

# 3. Install Flet CLI
uv add flet[cli]

# 4. Verify setup
flet doctor
flutter doctor
```

### Build Commands

```bash
# Development — runs on desktop to test UI
flet run app/main.py

# Build debug APK (faster, no optimisation)
flet build apk --project DriveSync

# Build release APK (optimised, signed)
flet build apk --release \
    --project DriveSync \
    --org com.drivesync.app \
    --signing-key keystore.jks

# Output: build/apk/app-release.apk
# Install on device:
adb install build/apk/app-release.apk
```

### APK Signing (one-time)

```bash
# Generate keystore (keep this file safe — losing it means can't update the app)
keytool -genkey -v \
  -keystore drivesync-release-key.jks \
  -keyalg RSA -keysize 2048 -validity 10000 \
  -alias drivesync
```

---

## 12. Key Technical Challenges

| Challenge | Solution |
|-----------|----------|
| Android kills background processes | Foreground Service with persistent notification |
| OAuth browser flow on mobile | Chrome Custom Tab + custom URI scheme redirect |
| Token storage security | Android Keystore API via `jnius` |
| File system access on Android | `READ_EXTERNAL_STORAGE` + `WRITE_EXTERNAL_STORAGE` permissions (or Scoped Storage API on Android 11+) |
| Text diff rendering performance | Diff computed off main thread, rendered lazily |
| `asyncio` event loop + Flet UI | Flet runs its own event loop; sync engine runs on a separate thread via `asyncio.run()` |
| iOS background sync | `BackgroundFetch` API + `BGTaskScheduler` (15-min minimum interval) |
| Corporate Zscaler SSL (Accenture) | `truststore` already in `auth.py` — same fix applies on mobile |

---

## 13. File Permissions Required

### Android `AndroidManifest.xml`

```xml
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
<uses-permission android:name="android.permission.RECEIVE_BOOT_COMPLETED" />
<uses-permission android:name="android.permission.READ_EXTERNAL_STORAGE" />
<uses-permission android:name="android.permission.WRITE_EXTERNAL_STORAGE" />
<uses-permission android:name="android.permission.POST_NOTIFICATIONS" />
<uses-permission android:name="android.permission.WAKE_LOCK" />
```

---

## 14. Dependencies to Add

```toml
# pyproject.toml additions
flet          = ">=0.24"      # UI framework
jnius         = ">=1.5"       # Python → Android Java bridge
plyer         = ">=2.1"       # cross-platform notifications (fallback)
diff-match-patch = ">=20230430"  # text diff for conflict resolver
```

---

*Plan created: 2026-05-02 · Branch: `mobile-app`*
