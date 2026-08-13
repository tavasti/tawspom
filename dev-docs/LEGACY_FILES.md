# Legacy Files

These files exist in the project but are **NOT imported or used** by the active codebase (`main.py` → `cli.py` → `core/*`).

They are remnants from an earlier architecture and are kept for reference only.

---

## tawspom/auth.py

**Status**: Obsolete — replaced by `core/spotify.py`

Old standalone auth helper. Created a `spotipy.Spotify` client with `open_browser=True`.

The current `SpotifyClient` in `core/spotify.py` handles auth internally with `open_browser=False` and per-user token caching.

---

## tawspom/db.py

**Status**: Obsolete — replaced by `core/db.py`

Old DB schema with a simpler structure:

- `track` table had only `id`, `name`, `artist` (no album, duration, play_count, etc.)
- `playlist_track` table for playlist-position mapping
- Versioned migration system (`DB_VERSION`)

The current `core/db.py` has a richer schema with 8 tables and column-presence migrations.

---

## tawspom/playlist.py

**Status**: Obsolete — replaced by `core/spotify.py` + `core/manager.py`

Old playlist management logic. Functions like `fetch_and_store_playlists()`, `fetch_and_compare_my_music()`, `show_current_playback()`.

All functionality is now in `SpotifyClient` (API calls) and `Manager` (business logic).

---

## tawspom/spotify_api.py

**Status**: Obsolete — replaced by `core/spotify.py`

Old thin wrapper for `get_storage_playlists()` and `get_playlist_tracks()` without retry logic.

Current `SpotifyClient` wraps all calls with `_call_with_retry()` (3 attempts, 2s delay, re-init on failure).

---

## tawspom/play.py

**Status**: Empty file

Contains only a docstring. No code.

---

## whoami.py

**Status**: Obsolete — replaced by `cli.py whoami` command

Standalone auth test script. The `whoami` command is now a proper CLI subcommand.

---

## tests/

**Status**: Empty test files

- `tests/test_db.py` — Empty
- `tests/test_playlist_manager.py` — Empty

No active test suite exists.
