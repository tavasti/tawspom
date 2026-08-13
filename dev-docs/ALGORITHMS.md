# Key Algorithms

## 1. Refill (Time-Queue with Spreading)

**File**: `core/manager.py` → `refill_active_playlist()`

**Goal**: Fill the Active playlist to `DEFAULT_REFILL_HOURS` (15h) with the oldest-unlistened tracks, while enforcing artist variety.

### Steps

```
1. Process listened tracks (unless --flush or --add):
   a. Get current playback position in playlist
   b. All tracks BEFORE current position = "listened"
   c. Mark them as played in DB (increment play_count, set last_played_at)
   d. Remove them from Spotify playlist
   e. Remove from active_tracks table
   f. Append to Phone Listening history

2. Detect manual removals:
   a. Compare DB's active_tracks vs current Spotify playlist
   b. If >50% appear removed AND >10 tracks → mass removal safety prompt
   c. Otherwise, delete removed tracks from storage too

3. Calculate needed duration:
   needed_ms = (target_hours * 3600000) - current_playlist_duration_ms

4. Fetch candidate pool:
   - DB query: oldest last_played_at first, NULLs first
   - Limit: REFILL_CANDIDATE_POOL_SIZE (2000)

5. Initialize artist locks:
   - Look at last REFILL_ARTIST_LOCK_WINDOW (5) tracks in current playlist
   - Lock each artist for remaining distance from end

6. Select tracks (oldest-first with artist lock):
   FOR each track in pool (oldest first):
     - Skip if already in playlist
     - Skip if any artist is locked (lock index > current fill index)
     - ADD track → lock its artists for next REFILL_ARTIST_LOCK_WINDOW tracks
     - Stop when added_duration >= needed_ms

7. Add selected tracks to Spotify playlist
8. Update active_tracks table
9. Display dashboard + play count stats
```

### Constants

| Constant | Value | Meaning |
| ---------- | ------- | --------- |
| `DEFAULT_REFILL_HOURS` | 15.0 | Target playlist duration |
| `REFILL_ARTIST_LOCK_WINDOW` | 5 | Tracks to lock an artist after appearance |
| `REFILL_CANDIDATE_POOL_SIZE` | 2000 | Max candidates to scan |
| `DASHBOARD_OLD_POOL_SIZE` | 20 | Pool for "Top Waiting Artists" |

### Modes

| Mode | Flag | Behavior |
| ------ | ------ | ---------- |
| Default | (none) | Process listened tracks from current position |
| Flush | `--flush` | Clear entire playlist (except current track), refill from scratch |
| Add | `--add` | Only add tracks, don't remove any |

---

## 2. Binary Deduplication

**File**: `core/manager.py` → `deduplicate_storage()`

**Goal**: Find tracks with identical Artist + Name + similar duration.

### Algorithm

```
1. Fetch all active tracks from DB
2. Group by (artist.lower(), name.lower())
3. For each group with >1 track:
   a. Sort by duration_ms
   b. Walk sorted list: if |duration[i] - duration[i-1]| <= BINARY_DEDUPE_THRESHOLD_MS (2000ms) → same subgroup
   c. For each subgroup with >1 track:
      - Sort by play history (has last_played_at > doesn't), then oldest added_at
      - KEEP first, mark rest for deletion
4. Show KEEP/DEL pairs to user for confirmation
5. On confirm: remove from Spotify playlists, soft-delete in DB, create DELETE transaction
```

---

## 3. Semantic Deduplication (findversions / processversions)

**File**: `core/manager.py` → `find_version_duplicates()` and `process_version_review()`

**Goal**: Find different versions of the same song (remixes, live versions, etc.) and let the user decide.

### findversions

```
1. Fetch all active tracks from DB
2. For each track:
   a. Extract lead artist (split on ',', take first)
   b. Extract root name: strip suffixes matching ROOT_NAME_FORBIDDEN_KEYWORDS
3. Group by (lead_artist, root_name)
4. For groups with >1 track:
   a. Remove tracks already in duplicate_allowlist (pairwise check)
   b. If still >1 unique track → potential duplicate group
5. Limit to VERSION_REVIEW_GROUP_LIMIT (20) groups
6. Populate "Duplicate Review" playlist with all tracks from selected groups
7. Save session to duplicate_review_session table
```

### processversions

```
1. Load session from duplicate_review_session table
2. Get current tracks in "Duplicate Review" playlist on Spotify
3. For each group:
   a. kept = tracks still in playlist, removed = tracks user deleted
   b. If kept is EMPTY → restore all (safety: user deleted everything by mistake)
   c. If removed > 0 → mark for deletion from storage
   d. If kept > 1 → user wants multiple versions → add to allowlist
4. On confirm: delete removed tracks from storage, allowlist kept groups
5. Clear session (unless groups were restored)
```

### Root Name Extraction

Strips patterns matching `ROOT_NAME_FORBIDDEN_KEYWORDS`:

```
remaster|edit|remix|mix|radio|live|feat|acoustic|version|original|mono|stereo|bonus|20\d\d|single|video
```

Handles `[Remaster 2020]`, `- Live Version`, `(Radio Edit)`, etc.

---

## 4. Deep Discovery (findnew)

**File**: `core/manager.py` → `find_new_music()`

**Goal**: Find new albums from artists the user already likes.

### Algorithm

```
1. Query DB: artists where user has listened to >= FINDNEW_QUALIFY_MIN_TRACKS (8) tracks
2. For each qualifying artist:
   a. Skip if checked within FINDNEW_ARTIST_COOLDOWN_DAYS (180)
   b. Resolve true Spotify Artist ID from an existing track in DB (avoids name collision)
   c. Fetch artist's albums from Spotify (skips live/deluxe/expanded)
   d. Filter out already-handled albums (in DB or IGNORED list)
   e. For remaining albums → interactive prompt:
      - [y] Review one by one
      - [n] Skip artist
      - [a] Bulk add all new albums
      - [d] Don't ask for 6 months
      - [q] Quit
   f. For each album reviewed:
      - [y] Add all tracks to A-Z storage
      - [n] Skip
      - [l] List tracks first
      - [d] Permanently ignore
      - Check similarity to existing albums (FINDNEW_ALBUM_SIMILARITY_THRESHOLD = 0.8)
```

---

## 5. Artist Radio

**File**: `core/manager.py` → `create_artist_radio()`

**Goal**: Create a radio playlist from a seed artist using scraped "Fans Also Like" data.

### Algorithm

```
1. Search for seed artist, disambiguate if multiple matches
2. Get top tracks from seed artist (RADIO_DEFAULT_MAIN_COUNT = 10)
3. Launch Playwright scraper → get_related_artists(artist_id)
   - Opens Spotify Web Player /artist/{id}/related
   - Scrapes all <a href="/artist/..."> links
4. Fetch peer details in batches of 50 (Spotify API limit)
5. Calculate midpoint popularity of peers
6. Filter by --filter: big (≥midpoint), small (<midpoint), both (all)
7. Shuffle filtered peers → take RADIO_DEFAULT_PER_ARTIST (5) tracks from each
8. Create/clear "Tawspom Radio: {artist} ({filter})" playlist
9. Shuffle all tracks and add to playlist
```

---

## 6. Storage Letter Mapping

**File**: `core/manager.py` → `_get_storage_letter(name)`

Maps a track name to an A-Z letter for storage playlist assignment.

```
1. Uppercase the name
2. Replace Finnish chars: Ä→A, Å→A, Ö→O
3. NFKD normalize → strip combining marks (accents)
4. Take first character
5. If A-Z → return it
6. Otherwise → DEFAULT_STORAGE_LETTER ("A")
```
