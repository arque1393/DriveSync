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

**What happens in the code** (`_find_downloads`):
```python
if os.path.getmtime(local_path) == stored.get('mtime', 0):
    to_download.append(entry)   # only local changed → safe download
else:
    conflicts.append(rel_path)  # BOTH changed → conflict
```

**Resolution:** Local version wins.  Drive version is silently discarded.  
**Risk:** ⚠️ HIGH — Drive changes are permanently lost.  
**Console output:** `⚠️  Conflict: path/to/file.md (keeping local version)`

---

### Type 2 — New File Collision *(silent conflict)*

| Attribute | State |
|-----------|-------|
| File in metadata | **No** (never synced) |
| Local file | Exists |
| Drive file | Also exists at the same path |

**What happens in the code** (`_find_downloads`, `_find_uploads`):
```python
# _find_downloads — file exists locally but not in metadata:
else:
    skip_msgs.append(rel_path)   # skips download silently

# _find_uploads — local file with no stored mtime means mtime > 0:
if mtime > metadata['files'].get(rel, {}).get('mtime', 0):
    result.append(...)           # uploads, overwrites Drive
```

**Resolution:** Local version is uploaded and overwrites the Drive version.  
**Risk:** ⚠️ HIGH — Drive version is lost with no warning.  
**Console output:** `ℹ️  Skipping path/to/file.md: not in sync history (will upload)`

---

### Type 3 — Local Deletion vs Drive Existence

| Attribute | State |
|-----------|-------|
| File in metadata | Yes or No |
| Local file | **Deleted** |
| Drive file | Still exists |

**What happens in the code** (`_find_downloads`):
```python
if not os.path.exists(local_path):
    to_download.append(entry)   # always re-downloads — deletion is reversed
```

**Resolution:** Drive version wins.  Deleted local file is **restored**.  
**Risk:** 🔶 MEDIUM — intentional local deletions are silently undone every cycle.  
**Console output:** `📥 Downloaded: path/to/file.md` (no warning that a deletion was reversed)

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

The engine uses a single strategy across all detected conflicts:

```
LOCAL WINS — always.
```

| Scenario | Winner |
|----------|--------|
| Both modified (Type 1) | Local |
| New file collision (Type 2) | Local |
| Local deleted, Drive exists (Type 3) | Drive ← (only exception) |
| Drive deleted, local unchanged (Type 4) | Neither (orphaned) |
| Drive deleted, local modified (Type 5) | Local |
| Both deleted (Type 6) | Neither (correct) |
| Rename on one side (Type 7) | Both (duplicate) |

---

## Conflict types by risk level

| Risk | Type | Description | Data Loss? |
|------|------|-------------|------------|
| ⚠️ HIGH | 1 | Both sides modified | Drive version lost |
| ⚠️ HIGH | 2 | New file collision (silent) | Drive version lost |
| 🔶 MEDIUM | 3 | Local deletion reversed | Intentional delete undone |
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
