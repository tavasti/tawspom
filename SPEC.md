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
    - Numbers and Symbols: Default to storage playlist `A`.
- **Sync Process**:
    1. **Ingest**: Move all "Liked Songs" to appropriate A-Z playlists.
    2. **Deduplicate**: Remove exact binary duplicates.
    3. **Cleanup**: Remote tracks from "Liked Songs" after successful storage.
    4. **Manual Removal Tracking**: If a track is deleted from an A-Z playlist on Spotify, it is marked as `is_deleted` in the local DB during the next sync.

### 2.2 Track Statistics
- Each track in the database includes a `play_count` column.
- **Increment Logic**: Every time a track is identified as "listened" during a `refill` run, its `play_count` is incremented in the local DB.
- **Initialization**: Tracks already marked as played before this feature was added start with a `play_count` of 1; all others start at 0.

---

## 3. Listening Experience
### 3.1 The "Active" Playlist (Refill)
The primary listening source is a configurable rolling window (default 12 hours).
- **Configuration**: Playlist name is set via `ACTIVE_PLAYLIST_NAME` in `.env` (default: "My Active Music").
- **Refill Logic**:
    - **Fair Lead-Artist Round-Robin**: The system picks one track per artist in a rotating loop to ensure maximum variety.
    - **Artist Locking**: When a track is picked, all credited artists on that track are "locked" for the remainder of that round-robin cycle.
    - **Least Recently Played**: Priority is given to tracks that haven't been played in the longest time.
- **Preservation**:
    - Never remove the currently playing song.
    - If playback position cannot be determined (and playlist isn't empty), the refill aborts to prevent losing the user's place.
- **Manual Removal Detection**: If a user deletes a track directly from the Active playlist, the system detects this during refill and deletes that track from the permanent A-Z storage as well (interpreting it as "I don't like this song anymore").

### 3.2 Phone History (Phone Listening)
- On every `refill` run, tracks identified as "listened" are appended to a secondary history playlist.
- **Configuration**: Playlist name is set via `PHONE_PLAYLIST_NAME` in `.env` (default: "Phone Listening").
- **Capacity**: This addition only occurs if the playlist is currently **shorter than 100 hours**.
- **Note**: This history is one-way and is not tracked in the local database.

---

## 4. Deduplication
### 4.1 Binary Deduplication
- Identifies tracks with the same Artist + Name and a duration within **2 seconds** of each other.
- Automatically keeps the version with the most play history or the oldest `added_at` date.

### 4.2 Semantic Deduplication (findversions)
- **Root Name Logic**: Uses regex to strip suffixes like `(Remastered)`, `[Live]`, `- Edit`, `feat. X`.
- **Bridge Workflow**:
    1. Potential duplicates are moved to a "Duplicate Review" playlist.
    2. The user listens and deletes the versions they don't want.
    3. `processversions` deletes the missing tracks from storage and adds the remaining ones to a `duplicate_allowlist` so they aren't flagged again.
    4. **Safety**: If a user accidentally deletes ALL versions of a song in the review playlist, the system restores them all to the playlist for a second chance.

---

## 5. Discovery Tools
### 5.1 Artist Radio (artistradio)
- **Discovery Engine**: Uses a **Headless Robot (Playwright)** to scrape the "Fans Also Like" section from the Spotify Web Player.
- **Filtering**:
    - Calculates the midpoint popularity of the discovered group.
    - Supports `--filter`: `big` (above midpoint), `small` (below midpoint), or `both`.
- **Scalability**: No total song limit. Takes `--per-artist` tracks from **every** peer found.
- **Selection**: Uses Round-Robin to ensure every discovered artist is represented before taking a second song from any one artist.

### 5.2 Deep Discovery (findnew)
- **Targeting**: Checks artists where the user has listened to at least **15 songs**.
- **Cooldown**: Artists are only checked once every **6 months** (stored in `artist_checks`).
- **Precision**: 
    - Resolves the exact Spotify Artist ID using existing tracks from the DB to prevent name collisions.
    - **Fuzzy Matching**: Flags new albums if their name is highly similar (>80% match) to albums already in the catalog.
- **Interface**:
    - Displays existing catalog for context.
    - Interactive options: 
        - `[y]es`: Add all tracks from the album to A-Z storage.
        - `[n]o`: Skip for now.
        - `[l]ist`: Show tracks within the album before deciding.
        - `[d]on't ask again`: Permanently ignore this album.
        - `[q]uit`: Exit the command.

---

## 6. Technical Specifications
- **Database**: SQLite3 with tables for `track`, `transactions`, `active_tracks`, `duplicate_allowlist`, `artist_checks`, and `handled_albums`.
- **Batching**: All Spotify removals and additions are batched (Playlists: 100, Liked Songs: 20) to prevent `400 Bad Request` errors.
- **API Wrapper**: A centralized `_call_with_retry` function handles `ConnectionResetError` and Spotify Rate Limits with exponential backoff.
- **Environment**: Configuration via `.env` file.
    - `SPOTIPY_CLIENT_ID`
    - `SPOTIPY_CLIENT_SECRET`
    - `LASTFM_API_KEY`
    - `ACTIVE_PLAYLIST_NAME` (Optional)
    - `PHONE_PLAYLIST_NAME` (Optional)
