# Architecture Overview

## High-Level Design

Tawspom is a **CLI tool** for managing a massive Spotify library (20k+ tracks). It treats "Liked Songs" as an inbox and organizes the permanent collection into **A-Z storage playlists**.

```
┌─────────────────────────────────────────────────────────┐
│                    main.py (entry)                      │
│                imports tawspom.cli.main()               │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│                     cli.py                               │
│  - argparse: defines all CLI commands                   │
│  - Creates SpotifyClient, Manager, passes DB conn       │
│  - Routes command → Manager.method()                    │
└──────────────┬──────────────────┬───────────────────────┘
               │                  │
       ┌───────▼──────┐  ┌───────▼──────────┐
       │ SpotifyClient │  │    Manager       │
       │ (core/spotify)│  │  (core/manager)  │
       │               │  │                  │
       │ - spotipy     │  │ - All business   │
       │ - retry logic │  │   logic          │
       │ - playlist    │  │ - User prompts   │
       │   CRUD        │  │ - Algorithm      │
       └───────────────┘  │   orchestration  │
                          └──┬────────┬──────┘
                             │        │
                    ┌────────▼──┐ ┌───▼──────────┐
                    │  core/db  │ │ core/scraper │
                    │  (SQLite) │ │ (Playwright) │
                    └───────────┘ └──────────────┘
                             │
                    ┌────────▼──┐
                    │ core/lastfm│
                    │  (discovery)│
                    └───────────┘
```

## Module Layers

| Layer | Files | Responsibility |
| ------- | ------- | ---------------- |
| **Entry** | `main.py` | Entry point — delegates to `cli.main()` |
| **CLI** | `tawspom/cli.py` | Argument parsing, component wiring, command dispatch |
| **Business Logic** | `tawspom/core/manager.py` | All commands, algorithms, user interaction |
| **Spotify API** | `tawspom/core/spotify.py` | spotipy wrapper with retry, playlist CRUD |
| **Database** | `tawspom/core/db.py` | SQLite schema, migrations, all DB operations |
| **Scraper** | `tawspom/core/scraper.py` | Playwright-based Spotify Web Player scraping |
| **Last.fm** | `tawspom/core/lastfm.py` | Last.fm API for similar artists |
| **Config** | `tawspom/core/constants.py` | All magic numbers as named constants |
| **Models** | `tawspom/models.py` | Dataclass definitions (Track, Transaction, Playlist) |

## Legacy Files (NOT used by main.py)

Files in `tawspom/` root that are **not imported** by the active codebase:

- `tawspom/auth.py` — Old auth helper (replaced by `core/spotify.py`)
- `tawspom/db.py` — Old DB schema (replaced by `core/db.py`)
- `tawspom/playlist.py` — Old playlist logic (replaced by `core/spotify.py` + `core/manager.py`)
- `tawspom/spotify_api.py` — Old Spotify API helpers (replaced by `core/spotify.py`)
- `tawspom/play.py` — Empty file
- `whoami.py` — Standalone auth test (replaced by `cli.py whoami` command)

See [LEGACY_FILES.md](LEGACY_FILES.md) for details.

## Data Flow: Typical Command (e.g., `refill`)

```
1. cli.py parses args → creates SpotifyClient + Manager
2. Manager.refill_active_playlist() orchestrates:
   a. SpotifyClient.get_active_playlist() — resolve playlist ID
   b. SpotifyClient.get_current_playback() — find current track
   c. SpotifyClient.get_playlist_tracks_ordered() — get playlist state
   d. core/db.py — mark listened tracks, get candidates
   e. Manager applies refill algorithm (artist lock, oldest-first)
   f. SpotifyClient.add_tracks_to_playlist() — write to Spotify
   g. core/db.py — update active_tracks table
   h. core/db.py — get_library_stats() for dashboard
```

## Key Design Principles

1. **Manager is the brain**: `core/manager.py` contains all business logic. CLI is thin.
2. **DB is authoritative**: Spotify state is reconciled against the local DB.
3. **Retry on failure**: All Spotify API calls go through `_call_with_retry()` (3 attempts, 2s delay).
4. **Constants over magic numbers**: All configurable values in `core/constants.py`.
5. **Transaction logging**: Every add/delete is recorded in the `transactions` table for audit/undo.
6. **Safety first**: Mass operations (>50% removal) require user confirmation.
