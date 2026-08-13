# Module Reference

## tawspom/cli.py

**Role**: CLI entry point. Parses args, wires components, dispatches to Manager.

**Key**: `main()` — creates `argparse` parser with subcommands, initializes `SpotifyClient` and `Manager`, routes to `manager.<command>()`.

**Playlist names**: Reads `ACTIVE_PLAYLIST_NAME` and `PHONE_PLAYLIST_NAME` from env (defaults: "My Active Music", "Phone Listening").

**User label**: `--user` flag (default: "default") controls which Spotify OAuth token cache is used.

---

## tawspom/core/manager.py

**Role**: Central business logic. All commands are methods on `Manager`.

**Constructor**: `Manager(sp_client: SpotifyClient, db_conn)`

### Key Methods

| Method | CLI Command | Description |
| -------- | ------------- | ------------- |
| `init_db()` | `initdb` | Populate DB from A-Z playlists on Spotify |
| `sync_storage()` | `sync` | Ingest liked songs + dedup + detect manual removals |
| `ingest_liked_songs()` | `ingest` | Move "Liked Songs" to A-Z storage |
| `deduplicate_storage()` | `deduplicate` | Binary dedup (same artist+name+duration) |
| `find_version_duplicates()` | `findversions` | Semantic dedup (strip remix/edit/feat suffixes) |
| `process_version_review()` | `processversions` | Finalize version review: delete rejected, allowlist kept |
| `add_artist_to_storage()` | `addartist` | Interactive artist album picker → A-Z storage |
| `create_artist_radio()` | `artistradio` | Scrape "Fans Also Like" → create radio playlist |
| `find_new_music()` | `findnew` | Check qualifying artists for new albums |
| `refill_active_playlist()` | `refill` | Core listening loop: oldest-first + artist spreading |
| `fix_duplicate_playlists()` | `fixplaylists` | Resolve duplicate-named playlists |
| `list_adds()` / `list_deletes()` | `listadd` / `listdelete` | List transactions |
| `show_add()` / `show_delete()` | `showadd` / `showdelete` | Show tracks in a transaction |
| `cancel_add()` / `cancel_delete()` | `canceladd` / `canceldelete` | Undo a transaction |
| `clean_database()` | `cleandatabase` | Permanently remove old deleted tracks |
| `whoami()` | `whoami` | Show authenticated Spotify user |

### Helper Methods

| Method | Description |
| -------- | ------------- |
| `_get_storage_letter(name)` | Map track name → A-Z letter (handles Finnish chars, accents) |
| `_format_duration(ms)` | Milliseconds → H:MM:SS string |
| `_fetch_all_storage_tracks()` | Read all tracks from A-Z playlists and upsert to DB |
| `_get_root_name(name)` | Strip version/remix suffixes for semantic dedup |
| `process_listened_tracks()` | Identify listened tracks in active playlist, mark as played |
| `flush_active_playlist()` | Clear active playlist (except current track), mark as played |
| `detect_manual_removals()` | Detect user-deleted tracks from Active, remove from storage |
| `_update_phone_listening()` | Append listened tracks to Phone History playlist |
| `_display_dashboard()` | Print library stats (coverage, momentum, queue health) |
| `_get_current_track_id()` | Get currently playing track ID |

---

## tawspom/core/spotify.py

**Role**: Spotify API wrapper with retry logic and playlist management.

**Constructor**: `SpotifyClient(user_label="default", scope=None, db_conn=None)`

### Key Methods

| Method | Description |
| -------- | ------------- |
| `_call_with_retry(func, *args)` | Retry wrapper (3 attempts, 2s delay, re-init on failure) |
| `get_storage_playlists()` | Fetch all A-Z playlists (single-char alpha name) |
| `search_artist(name)` | Search artists, includes top 3 tracks for disambiguation |
| `get_artist_albums(artist_id, types)` | Fetch albums/singles, skips live/deluxe/expanded |
| `create_playlist(name)` | Create a private playlist |
| `get_album_tracks(album_id, album_name)` | Fetch all tracks from an album |
| `get_playlist_tracks(playlist_id)` | Fetch all tracks from a playlist (as Track objects) |
| `get_playlist_tracks_ordered(playlist_id)` | Fetch track IDs in playlist order |
| `get_playlist_duration_ms(playlist_id)` | Efficient duration calc (field-filtered) |
| `get_liked_songs()` | Fetch all "Liked Songs" |
| `remove_liked_songs(track_ids)` | Remove tracks from "Liked Songs" (batch 20) |
| `get_active_playlist(name)` | Resolve active playlist: stored ID → name search → create |
| `get_current_playback()` | Get currently playing track and context |
| `remove_tracks_from_playlist(pl_id, track_ids)` | Remove tracks (batch 100) |
| `add_tracks_to_playlist(pl_id, track_ids)` | Add tracks (batch 100) |
| `is_playlist_id_valid(pl_id)` | Check if a playlist ID is valid on Spotify |

### Playlist ID Persistence

Active and Phone playlist IDs are stored in the `meta` table as `playlist_id_<name>`. This prevents duplicate playlist creation and enables fast lookup.

---

## tawspom/core/db.py

**Role**: SQLite database operations. Schema, migrations, CRUD.

**DB Path**: `~/.local/share/tawspom/tawspom.sqlite3` (configurable via `DB_PATH` env)

See [DATABASE.md](DATABASE.md) for full schema reference.

### Key Functions

| Function | Description |
| ---------- | ------------- |
| `init_db()` | Create tables + run migrations |
| `set_meta(key, value)` / `get_meta(key)` | Key-value store in `meta` table |
| `create_transaction(type, count, desc)` | Create ADD/DELETE transaction record |
| `upsert_tracks(tracks)` | Bulk INSERT OR REPLACE for tracks |
| `mark_tracks_deleted(ids, trans_id)` | Soft-delete tracks |
| `unmark_tracks_deleted(ids)` | Restore soft-deleted tracks |
| `mark_as_played(track_id, played_at)` | Set last_played_at, increment play_count |
| `get_least_recently_played_tracks(limit)` | Oldest-first candidates for refill |
| `get_all_active_tracks()` / `get_all_active_track_ids()` | All non-deleted tracks |
| `get_tracks_by_ids(ids)` / `get_tracks_by_transaction(trans_id)` | Batch fetch |
| `add_active_tracks(ids)` / `reset_active_tracks(ids)` / `remove_active_tracks(ids)` | Manage `active_tracks` table |
| `add_to_duplicate_allowlist(ids)` | Allow all pairs in list to coexist |
| `is_duplicate_allowed(id_a, id_b)` | Check if pair is allowlisted |
| `save_review_session()` / `get_review_session()` / `clear_review_session()` | Semantic dedup session state |
| `get_library_stats()` | Comprehensive stats for dashboard |
| `get_listened_artist_data(min_count)` | Artists with ≥ min_count listened tracks |
| `get_artist_last_check()` / `set_artist_checked()` | findnew cooldown tracking |
| `get_handled_albums()` / `add_to_ignored_albums()` | Album ignore list |
| `delete_tracks_permanently(older_than_days)` | Hard-delete old soft-deleted tracks |

---

## tawspom/core/scraper.py

**Role**: Playwright-based scraper for Spotify Web Player "Fans Also Like" section.

**Why**: Spotify API's `artist_related_artists` is rate-limited. Scraper bypasses this.

**Constructor**: `SpotifyScraper()`

### Key Method

| Method | Description |
|--------|-------------|
| `get_related_artists(artist_id)` | Launch headless Chromium, scrape `/artist/{id}/related`, extract artist IDs and names |

**Selector**: `a[href*="/artist/"]` — matches all artist links on the page.

---

## tawspom/core/lastfm.py

**Role**: Last.fm API client for similar artist discovery.

**Note**: Currently defined in Manager but **not actively used** in any command path (Manager initializes it but no method calls `self.lfm`). Available for future use.

**Constructor**: `LastFMClient()` — reads `LASTFM_API_KEY` from env.

### Key Method

| Method | Description |
|--------|-------------|
| `get_similar_artists(name, limit)` | Fetch similar artist names from Last.fm |

---

## tawspom/core/constants.py

**Role**: All configurable magic numbers as named constants.

See [ALGORITHMS.md](ALGORITHMS.md) for how each constant is used.

---

## tawspom/models.py

**Role**: Dataclass definitions.

| Class | Fields |
| ------- | -------- |
| `Track` | id, name, artist, album, duration_ms, storage_playlist_id, last_played_at, added_at, deleted_at, is_deleted, add_transaction_id, delete_transaction_id |
| `Transaction` | id, type ('ADD'/'DELETE'), timestamp, track_count, description, is_cancelled, cancels_id |
| `Playlist` | id, name, is_active |
