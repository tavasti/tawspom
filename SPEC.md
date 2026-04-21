# Tawspom: The Advanced Spotify Playlist Manager

## 1. Project Philosophy
Tawspom is a tool for power users who maintain massive Spotify libraries (20k+ tracks). It treats "Liked Songs" as a temporary inbox and organizes the permanent collection into A-Z storage playlists. It automates the maintenance of a diverse "Active" listening experience and provides high-precision discovery tools that bypass the limitations of the official Spotify API.

---

## 2. Library Architecture & Storage
### 2.1 A-Z Storage
- All tracks are stored in public playlists named with a single character (**A-Z**).
- **Mapping Logic**:
    - Finnish characters: `Ä`, `Å` -> `A`; `Ö` -> `O`.
    - Accents: `È` -> `E`, `Ŝ` -> `S`, etc. (Normalized to base ASCII).
    - Numbers and Symbols: Default to storage playlist defined by `DEFAULT_STORAGE_LETTER` (Default: "A").
- **Sync Process**:
    1. **Ingest**: Move all "Liked Songs" to appropriate A-Z playlists.
    2. **Deduplicate**: Identify and list potential duplicates for user review.
    3. **Cleanup**: Remote tracks from "Liked Songs" after successful storage.
    4. **Manual Removal Tracking**: If a track is deleted from an A-Z playlist on Spotify, it is marked as `is_deleted` in the local DB during the next sync.

### 2.2 Track Statistics
- Each track in the database includes a `play_count` column.
- **Increment Logic**: Every time a track is identified as "listened" during a `refill` run, its `play_count` is incremented in the local DB.
- **Initialization**: Tracks already marked as played before this feature was added start with a `play_count` of 1; all others start at 0.

---

## 3. Listening Experience
### 3.1 The "Active" Playlist (Refill)
The primary listening source is a configurable rolling window defined by `DEFAULT_REFILL_HOURS` (Default: 15.0 hours).
- **Configuration**: Playlist name is set via `ACTIVE_PLAYLIST_NAME` in `.env` (default: "My Active Music").
- **Established ID Persistence**: To prevent the creation of duplicate playlists, the script "locks in" the specific Spotify ID of the active playlist and stores it in the database `meta` table.
- **Creation Safety**: If the established playlist ID is missing or invalid, the script will **ask for user permission** before creating a new one on Spotify.
- **Library Dashboard**: At the end of every refill, a dashboard displays:
    - **Counts**: Total active tracks, total duration, and deleted tracks.
    - **Coverage**: Number of "Never Listened" tracks and overall Library Coverage %.
    - **History**: 
        - **Library listening time**: Cumulative listening for current (active) songs.
        - **with deleted songs**: Absolute lifetime listening including removed tracks.
        - **Avg plays**: Average play count per track.
    - **Momentum**: Total time listened in the window defined by `MOMENTUM_WINDOW_DAYS` (Default: 7 days).
    - **Queue Health**: Date of the oldest song waiting in the queue and top 3 artists currently at the front of the queue (identified from a pool of size `DASHBOARD_OLD_POOL_SIZE`, Default: 1000).
- **Refill Logic (Time-Queue with Spreading)**:
    - **Oldest First**: Candidate tracks are fetched in absolute order of their `last_played_at` date (ascending).
    - **Spreading Lock**: Once an artist is added to the playlist, they (and their collaborators) are locked for the next `REFILL_ARTIST_LOCK_WINDOW` tracks (Default: 5). 
    - **Exhaustive Search**: The system scans up to `REFILL_CANDIDATE_POOL_SIZE` oldest candidates to find enough tracks that satisfy the variety window (Default: 2000).
- **Statistical Reporting**: At the end of each refill, the system reports the play count distribution of the newly added tracks (**Low, Average, Median, High**).
- **Preservation & State Reconciliation**:
    - Never remove the currently playing song.
    - **Smart Playback Detection**: If the currently playing playlist name matches but the ID is different, the system automatically switches its "Established ID" to the playing one and triggers **State Reconciliation**.
    - **State Reconciliation**: When switching to a new playlist ID, the system immediately resyncs the DB's `active_tracks` table to match the current tracks on Spotify, preventing accidental mass-deletion detections.
- **Manual Removal Detection**:
    - If a user deletes a track directly from the Active playlist, the system detects this during refill and deletes that track from the permanent A-Z storage as well. 
    - **Mass Removal Safety**: If more than 50% of the active tracks (minimum 10) appear to be removed, the system prompts for action:
        - `[y]es`: Delete the missing tracks from permanent storage.
        - `[n]o`: Skip deletion for this run.
        - `[r]esync`: Use the current Spotify playlist as the "correct" state and update the database (clears "ghost" tracks).

### 3.2 Phone History (Phone Listening)
- On every `refill` run, tracks identified as "listened" are appended to a secondary history playlist.
- **Configuration**: Playlist name is set via `PHONE_PLAYLIST_NAME` in `.env` (default: "Phone Listening").
- **Persistence**: Like the Active playlist, the Phone playlist's Spotify ID is persisted in the database.
- **Capacity**: This addition only occurs if the playlist is currently shorter than `PHONE_PLAYLIST_CAPACITY_HOURS` (Default: 100 hours).
- **Optimization**: Duration checks use field-filtering to minimize API payload and prevent timeouts.

---

## 4. Deduplication
### 4.1 Binary Deduplication
- Identifies tracks with the same Artist + Name and a duration within `BINARY_DEDUPE_THRESHOLD_MS` (Default: 2000ms).
- **Interactive Review**: Always lists the "KEEP" and "DELETE" versions for user review before proceeding.
- **Selection Logic**: Automatically proposes keeping the version with more play history or the oldest `added_at` date.

### 4.2 Semantic Deduplication (findversions)
- **Root Name Logic**: Uses regex to strip suffixes matching `ROOT_NAME_FORBIDDEN_KEYWORDS`.
- **Bridge Workflow**:
    1. Potential duplicates are moved to a "Duplicate Review" playlist.
    2. The user listens and deletes the versions they don't want.
    3. `processversions` deletes the missing tracks from storage and adds the remaining ones to a `duplicate_allowlist` so they aren't flagged again.
    4. **Safety**: If a user accidentally deletes ALL versions of a song in the review playlist, the system restores them all to the playlist for a second chance.
- **Review Session Limit**: Limits the number of groups in a session to `VERSION_REVIEW_GROUP_LIMIT` (Default: 20).

---

## 5. Discovery Tools
### 5.1 Artist Radio (artistradio)
- **Discovery Engine**: Uses a **Headless Robot (Playwright)** to scrape the "Fans Also Like" section from the Spotify Web Player.
- **Filtering**:
    - Calculates the midpoint popularity of the discovered group.
    - Supports `--filter`: `big` (above midpoint), `small` (below midpoint), or `both`.
- **Scalability**: No total song limit. Takes up to `RADIO_DEFAULT_PER_ARTIST` (Default: 5) tracks from **every** peer found.
- **Selection**: Uses Round-Robin to ensure every discovered artist is represented before taking a second song from any one artist.

### 5.2 Deep Discovery (findnew)
- **Targeting**: Checks artists where the user has listened to at least `FINDNEW_QUALIFY_MIN_TRACKS` tracks (Default: 8).
- **Cooldown**: Artists are only checked once every `FINDNEW_ARTIST_COOLDOWN_DAYS` (Default: 180 days).
- **Precision**: 
    - Resolves the exact Spotify Artist ID using existing tracks from the DB to prevent name collisions.
    - **Fuzzy Matching**: Flags new albums if their name is highly similar (exceeding `FINDNEW_ALBUM_SIMILARITY_THRESHOLD`, Default: 0.8) to albums already in the catalog.
- **Interface**:
    - **Artist Level**: For artists with new albums, asks for high-level decision: `[y]es` (review individually), `[n]o` (skip artist), `[a]ll` (ingest all new albums), `[d]on't ask for 6mo`, or `[q]uit`.
    - **Album Level**: 
        - `[y]es`: Add all tracks from the album to A-Z storage.
        - `[n]o`: Skip for now.
        - `[l]ist`: Show tracks within the album before deciding.
        - `[d]on't ask again`: Permanently ignore this album.
        - `[q]uit`: Exit the command.

---

## 6. Maintenance Tools
### 6.1 Playlist Cleanup (fixplaylists)
- Interactively scans for duplicate playlists with the same name.
- Allows the user to establish a primary ID and delete redundant duplicates from Spotify.
- Provides an option to resync the local database state to the tracks in the established playlist.

---

## 7. Technical Specifications
- **Database**: SQLite3 with tables for `track`, `transactions`, `active_tracks`, `duplicate_allowlist`, `artist_checks`, and `handled_albums`.
- **Batching**: 
    - Playlists: `SPOTIFY_PLAYLIST_BATCH_SIZE` (Default: 100).
    - Liked Songs: `SPOTIFY_LIKED_SONGS_BATCH_SIZE` (Default: 20).
- **API Wrapper**: A centralized `_call_with_retry` function handles connection errors and rate limits using `SPOTIFY_MAX_RETRIES` (Attempt 3) and `SPOTIFY_RETRY_DELAY` (Delay 2s).
- **API Timeout**: Configured via `SPOTIFY_API_TIMEOUT` (Default: 10s).
- **Environment**: Configuration via `.env` file.
    - `SPOTIPY_CLIENT_ID`
    - `SPOTIPY_CLIENT_SECRET`
    - `LASTFM_API_KEY`
    - `ACTIVE_PLAYLIST_NAME` (Optional)
    - `PHONE_PLAYLIST_NAME` (Optional)
    - `DB_PATH` (Optional)
