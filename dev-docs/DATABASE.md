# Database Schema Reference

**Engine**: SQLite3
**Path**: `~/.local/share/tawspom/tawspom.sqlite3` (configurable via `DB_PATH` env)

Schema and migrations are in `tawspom/core/db.py` function `init_db()`.

---

## Tables

### `meta`

Key-value store for app state (playlist IDs, etc.).

| Column | Type | Description |
|--------|------|-------------|
| `key` | TEXT (PK) | Key name (e.g., `playlist_id_My Active Music`) |
| `value` | TEXT | Stored value |

**Usage**: `set_meta(conn, key, value)` / `get_meta(conn, key)`

---

### `track`

Core table. One row per Spotify track in the library.

| Column | Type | Description |
| -------- | ------ | ------------- |
| `id` | TEXT (PK) | Spotify track ID |
| `name` | TEXT | Track name |
| `artist` | TEXT | Artist name(s), comma-separated |
| `album` | TEXT | Album name |
| `duration_ms` | INTEGER | Track duration in milliseconds |
| `storage_playlist_id` | TEXT | Spotify playlist ID of the A-Z storage playlist |
| `last_played_at` | TEXT (ISO) | When track was last identified as "listened" |
| `added_at` | TEXT (ISO) | When track was added to storage |
| `deleted_at` | TEXT (ISO) | When track was soft-deleted |
| `is_deleted` | INTEGER (0/1) | Soft-delete flag |
| `add_transaction_id` | INTEGER | FK → transactions.id (which ADD/INGEST added this) |
| `delete_transaction_id` | INTEGER | FK → transactions.id (which DELETE removed this) |
| `play_count` | INTEGER | Number of times identified as "listened" |

**Key queries**:

- Active tracks: `WHERE is_deleted = 0`
- Oldest-first refill: `ORDER BY last_played_at ASC NULLS FIRST`
- Library stats: aggregates on `play_count`, `duration_ms`, `last_played_at`

---

### `transactions`

Audit log for all add/delete operations. Enables undo.

| Column | Type | Description |
| -------- | ------ | ------------- |
| `id` | INTEGER (PK, AUTO) | Transaction ID |
| `type` | TEXT | `'ADD'`, `'INGEST'`, or `'DELETE'` |
| `timestamp` | TEXT (ISO) | When the transaction occurred |
| `track_count` | INTEGER | Number of tracks in this transaction |
| `description` | TEXT | Human-readable description |
| `is_cancelled` | INTEGER (0/1) | Was this transaction undone? |
| `cancels_id` | INTEGER | FK → transactions.id (which transaction does this cancel) |

**Workflow**:

1. `canceladd <id>` → creates a new DELETE transaction with `cancels_id = <id>`, marks original as cancelled
2. `canceldelete <id>` → restores tracks, marks DELETE as cancelled, un-cancels original if it had one

---

### `active_tracks`

Tracks currently in the Active playlist. Used for manual removal detection.

| Column | Type | Description |
|--------|------|-------------|
| `track_id` | TEXT (PK) | FK → track.id |
| `added_at` | TEXT (ISO) | When added to active playlist |

**Operations**: `add_active_tracks()`, `reset_active_tracks()` (clear + replace), `remove_active_tracks()`

---

### `duplicate_allowlist`

Pairs of tracks that are allowed to coexist (different versions).

| Column | Type | Description |
| -------- | ------ | ------------- |
| `track_id_a` | TEXT | First track ID (sorted) |
| `track_id_b` | TEXT | Second track ID (sorted) |
| **PK** | `(track_id_a, track_id_b)` | |

**Usage**: After `processversions`, all kept versions of a song are pairwise allowlisted so `findversions` won't flag them again.

---

### `duplicate_review_session`

Temporary state for semantic dedup review (findversions → processversions).

| Column | Type | Description |
|--------|------|-------------|
| `track_id` | TEXT (PK) | Track being reviewed |
| `group_key` | TEXT | `"artist\|root_name"` grouping key |

**Lifecycle**: `save_review_session()` on findversions → `get_review_session()` on processversions → `clear_review_session()` when done.

---

### `artist_checks`

Cooldown tracking for findnew.

| Column | Type | Description |
|--------|------|-------------|
| `artist_name` | TEXT (PK) | Artist name |
| `last_checked_at` | TEXT (ISO) | Last time this artist was checked for new albums |

**Rule**: Artist is skipped if `last_checked_at` is within `FINDNEW_ARTIST_COOLDOWN_DAYS` (180 days).

---

### `handled_albums`

Permanently ignored albums for findnew.

| Column | Type | Description |
| -------- | ------ | ------------- |
| `artist_name` | TEXT | Artist name |
| `album_name` | TEXT | Album name |
| `status` | TEXT | `'IGNORED'` |
| **PK** | `(artist_name, album_name)` | |

**Usage**: When user selects `[d]on't ask again` on an album in findnew.

---

## Migration Strategy

Migrations are **column-presence checks** in `init_db()`. Pattern:

```python
cur.execute("PRAGMA table_info(track)")
cols = [col[1] for col in cur.fetchall()]
if 'some_column' not in cols:
    cur.execute("ALTER TABLE track ADD COLUMN some_column ...")
```

**Important**: The project is in active use. Any schema change MUST use this pattern — never truncate or drop tables. See AGENTS.md §4.

---

## Access Patterns

| Pattern | Function | Used By |
| --------- | ---------- | --------- |
| Bulk upsert tracks | `upsert_tracks()` | ingest, addartist, findnew |
| Mark as played | `mark_as_played()` | refill (process_listened_tracks) |
| Get refill candidates | `get_least_recently_played_tracks()` | refill |
| Soft-delete batch | `mark_tracks_deleted()` | dedup, manual removal, processversions |
| Dashboard stats | `get_library_stats()` | refill (post-run) |
| Transaction CRUD | `create_transaction()`, `get_transaction()`, `list_transactions()` | All commands |
