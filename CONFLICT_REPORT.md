# Conflict Report — DriveSync

> **What is a conflict?**  
> A conflict arises when the same file has been independently changed on both
> Local and Google Drive since the last successful sync.  The sync engine
> cannot know which version the user intends to keep, so it must make a choice.

---

## How conflict detection works

Every successfully synced file is recorded in `sync_metadata.json`:

```json
"Daily Notes/2026-05-01.md": {
  "mtime":       1746096000.0,       ← local file's modification time
  "drive_id":    "1abc...xyz",        ← Drive file ID
  "drive_mtime": "2026-05-01T10:00:00Z"  ← Drive's modifiedTime
}
```

On every sync cycle the engine compares the **current** timestamps against
these **stored** values to classify each file.

```
              stored mtime         stored drive_mtime
                   │                       │
     current       ▼      current          ▼
     local mtime ──┤       drive mtime ────┤
                   │                       │
         changed?  │           changed?    │
          YES/NO   │            YES/NO     │
                   └───────────┬───────────┘
                               │
          ┌────────────────────┼─────────────────────────────┐
          │                    │                             │
      local only          drive only                      BOTH
       → upload            → download               → CONFLICT
```

---

## All possible conflict types

### Type 1 — Bidirectional Modification *(true conflict)*

| Attribute | State |
|-----------|-------|
| File in metadata | Yes |
| Local mtime | **changed** since last sync |
| Drive mtime | **changed** since last sync |

**What happens in the code** (`_find_downloads` → `_resolve_conflicts`):
```python
# Detection in _find_downloads:
local_new, drive_new = _conflict_paths(rel, device_name)
conflicts.append((rel, drive_id, name, drive_mtime, local_new, drive_new, 'type1'))

# Resolution in _resolve_conflicts:
shutil.move(original_path, local_new_path)          # rename local copy
metadata['files'][original_rel] = {'mtime': None, ...}  # ghost entry
drive_downloads.append((drive_id, name, drive_new_path, drive_mtime))  # queue Drive copy
```

**Resolution:** **Both versions kept.**
- Local copy renamed to `filename.local.HOSTNAME.ext`
- Drive copy downloaded as `filename.drive.ext`
- A **ghost entry** (`mtime: null`) is inserted for the original path so it
  is not re-downloaded next cycle unless Drive changes it again.
- The renamed local copy has **no metadata entry** → `sync_up` will upload
  it to Drive on the very next cycle, ensuring the user's work is preserved.

**Risk:** ✅ No data loss — both versions survive.  
**Console output:**
```
⚡ 1 conflict(s) detected — keeping both versions:
   [both modified]  Notes/research.md
      ├─ local → Notes/research.local.DESKTOP-ABC123.md
      └─ drive → Notes/research.drive.md
```

---

### Type 2 — New File Collision *(previously silent)*

| Attribute | State |
|-----------|-------|
| File in metadata | **No** (never synced) |
| Local file | Exists |
| Drive file | Also exists at the same path |

**What happens in the code** (`_find_downloads` → `_resolve_conflicts`):
```python
# Detection: file not in metadata, exists locally, Drive also has it
local_new, drive_new = _conflict_paths(rel, device_name)
conflicts.append((rel, drive_id, name, drive_mtime, local_new, drive_new, 'type2'))

# Resolution: identical to Type 1
```

**Resolution:** **Both versions kept** — same mechanism as Type 1.
- Local copy renamed to `filename.local.HOSTNAME.ext`
- Drive copy downloaded as `filename.drive.ext`
- Ghost entry inserted for original path.

**Risk:** ✅ No data loss — previously Drive version was silently overwritten.  
**Console output:**
```
⚡ 1 conflict(s) detected — keeping both versions:
   [new file collision]  Notes/shared-note.md
      ├─ local → Notes/shared-note.local.DESKTOP-ABC123.md
      └─ drive → Notes/shared-note.drive.md
```

---

### Type 3 — Local Deletion vs Drive Existence

| Attribute | State |
|-----------|-------|
| File in metadata | Yes |
| Local file | **Deleted** |
| Drive file | Unchanged since last sync |

**What happens in the code** (`_find_downloads`):
```python
# stored entry exists, Drive mtime == stored drive_mtime → Drive unchanged
if info['mtime'] == stored.get('drive_mtime'):
    pass   # respects the local deletion — file stays deleted
```

When Drive **has changed** since last sync AND local was deleted:
```python
# Drive changed, local deleted → restore Drive version
to_download.append((info['id'], info['name'], local_path, info['mtime']))
```

**Resolution:**
- Drive **unchanged** → local deletion is **respected** (file stays gone).
- Drive **changed** → Drive version is downloaded (user gets the update).

**Risk:** 🟢 LOW — intentional deletions are now respected when Drive hasn't changed.
*(Previously: always re-downloaded regardless.)*  
**Console output:** `📥 Downloaded: path/to/file.md` (only when Drive also changed)

---

### Type 4 — Drive Deletion vs Local File

| Attribute | State |
|-----------|-------|
| File in metadata | Yes |
| Local file | Exists (unmodified) |
| Drive file | **Deleted** |

**What happens in the code** (`_find_downloads`, `_find_uploads`):
```python
# Drive file absent from drive_files → never reaches _find_downloads
# _find_uploads: mtime == stored mtime (unchanged) → NOT uploaded
```

**Resolution:** Local file survives but the Drive deletion is **not propagated**.  
The file becomes an orphan — it is tracked in metadata but no longer on Drive.  
On the next cycle it will *not* be re-uploaded (mtime unchanged) and will *not* be
deleted locally.  It silently de-syncs.  
**Risk:** 🔵 LOW — no data loss, but metadata drifts.  
**Console output:** None — this case is invisible.

---

### Type 5 — Drive Deletion vs Local Modification

| Attribute | State |
|-----------|-------|
| File in metadata | Yes |
| Local file | **Modified** after Drive deleted it |
| Drive file | **Deleted** |

**What happens in the code** (`_find_uploads`):
```python
if mtime > stored.get('mtime', 0):   # local mtime advanced
    result.append(...)               # re-uploads, recreates on Drive
```

**Resolution:** Local modification wins.  File is recreated on Drive.  
**Risk:** 🟢 LOW — generally the desired outcome.  
**Console output:** `📤 Uploaded: path/to/file.md`

---

### Type 6 — Both Deleted

| Attribute | State |
|-----------|-------|
| File in metadata | Yes |
| Local file | **Deleted** |
| Drive file | **Deleted** |

**What happens in the code:**  
File appears in neither `local_files` nor `drive_files`.  Neither `_find_uploads`
nor `_find_downloads` touches it.  Stale entry remains in `sync_metadata.json`.

**Resolution:** Nothing — effectively correct.  Metadata accumulates dead entries.  
**Risk:** 🟢 VERY LOW — harmless, but `sync_metadata.json` grows over time.  
**Console output:** None.

---

### Type 7 — Rename / Move on One Side *(unhandled)*

| Attribute | State |
|-----------|-------|
| Original path | Disappears from one side |
| New path | Appears on same side |

Neither the local scan nor the Drive scan understands renames.
A rename looks like a **delete + create**.

**Example — file renamed locally (`notes.md` → `notes-v2.md`):**

| Path | Local | Drive | Action |
|------|-------|-------|--------|
| `notes.md` | deleted | exists | → re-downloaded (restores old name) |
| `notes-v2.md` | exists | absent | → uploaded (new file created on Drive) |

**Result:** Both `notes.md` AND `notes-v2.md` exist on Drive.  Duplicate created.  
**Risk:** 🔶 MEDIUM — silent content duplication on every sync until one is removed.  
**Console output:** None — looks like a normal download + upload.

---

## Resolution strategy (current)

| Scenario | Strategy | Data loss? |
|----------|----------|------------|
| **Both modified (Type 1)** | **Keep both** — `.local.DEVICE` + `.drive` | ✅ None |
| **New file collision (Type 2)** | **Keep both** — `.local.DEVICE` + `.drive` | ✅ None |
| Local deleted, Drive unchanged (Type 3) | Respect deletion | ✅ None |
| Local deleted, Drive changed (Type 3b) | Download Drive version | ✅ None |
| Drive deleted, local unchanged (Type 4) | Orphaned (local stays) | 🔵 Metadata drift |
| Drive deleted, local modified (Type 5) | Local wins (re-uploads) | ✅ None |
| Both deleted (Type 6) | Ghost entry cleaned up | ✅ None |
| Rename on one side (Type 7) | Duplicate created | 🔶 Medium |

### How "keep both" works

```
Conflict on: research.md
                │
                ├── shutil.move(research.md → research.local.DESKTOP-ABC123.md)
                │       └── No metadata entry → sync_up uploads it next cycle
                │
                ├── download(Drive → research.drive.md)
                │       └── metadata records it normally
                │
                └── ghost entry for research.md: {mtime: null, drive_mtime: <current>}
                        └── prevents re-download unless Drive changes it again
```

Ghost entry lifecycle:
- Inserted when conflict is resolved
- Suppresses re-download of the original path while Drive is unchanged
- Removed and replaced by a real entry if Drive later updates the file

---

## Conflict types by risk level

| Risk | Type | Description | Data Loss? |
|------|------|-------------|------------|
| ✅ RESOLVED | 1 | Both sides modified → **keep both** | None |
| ✅ RESOLVED | 2 | New file collision → **keep both** | None |
| 🟢 FIXED | 3 | Local deletion now **respected** when Drive unchanged | None |
| 🔶 MEDIUM | 7 | Rename creates duplicate | Content duplicated |
| 🔵 LOW | 4 | Drive deletion ignored | Metadata drift |
| 🟢 LOW | 5 | Drive deleted, local modified | None |
| 🟢 VERY LOW | 6 | Both deleted | None |

---

## What is NOT implemented (known gaps)

| Feature | Impact |
|---------|--------|
| Deletion propagation | Local deletes never mirror to Drive; Drive deletes never mirror locally |
| Rename / move tracking | Renames always create duplicates |
| Content-based conflict | Only timestamps compared — identical-content "conflicts" are still flagged |
| Conflict backup | Losing side is discarded with no copy saved |
| Three-way merge | No base version is kept; merge is impossible |
| Per-file conflict policy | Only one global policy (local wins); no per-extension or per-folder rules |

---

## Detection accuracy

The conflict detector relies entirely on **file modification timestamps (mtime)**.

| Scenario | Correctly detected? |
|----------|-------------------|
| File edited in a text editor | ✅ Yes — editor updates mtime |
| File copied in (same content, new mtime) | ✅ Yes — treated as modified |
| File touched (`touch file`) | ✅ Yes — mtime advances |
| File content changed, mtime not updated (rare) | ❌ No — looks unchanged |
| Clock drift between machines | ❌ Possible false conflicts |
| FAT32 filesystem (2-second mtime granularity) | ❌ Possible false matches |
| Google Drive mtime vs. local mtime timezone | ✅ Handled — Drive uses ISO 8601 UTC, local uses Unix epoch |

---

*Generated from codebase analysis of `google_drive_sync.py` · `drive_ops_async.py` · `sync_metadata.json` schema*
