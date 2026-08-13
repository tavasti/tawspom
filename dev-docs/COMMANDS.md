# CLI Commands Reference

**Entry**: `python main.py [--user <label>] <command> [options]`

`--user <label>` controls which Spotify OAuth token cache is used (default: "default").

---

## Core Commands

### `sync`

Full storage sync: ingest liked songs → deduplicate → detect manual removals from A-Z playlists.

### `initdb`

Initialize DB schema and populate from existing A-Z playlists on Spotify. Run once on first setup.

### `whoami`

Check currently authenticated Spotify user.

---

## Ingestion

### `ingest`

Move all "Liked Songs" to A-Z storage playlists. Tracks already in storage are skipped but still removed from Liked Songs.

---

## Active Playlist

### `refill [--hours H] [--flush | --add]`

Refill the Active playlist.

| Option | Default | Description |
| -------- | --------- | ------------- |
| `--hours` | 15.0 | Target duration in hours |
| `--flush` | — | Clear playlist (except current track) and refill from scratch |
| `--add` | — | Only add tracks, don't remove listened ones |

Modes are mutually exclusive. Default mode processes listened tracks from current playback position.

---

## Deduplication

### `deduplicate`

Find and remove binary duplicates (same artist + name + duration within 2s). Interactive confirmation.

### `findversions`

Find semantic duplicates (remixes, live versions, etc.) and populate "Duplicate Review" playlist. Limit: 20 groups per session.

### `processversions`

Process the "Duplicate Review" playlist: delete rejected tracks from storage, allowlist kept versions.

---

## Discovery

### `findnew`

Find new albums from artists you've listened to (≥8 tracks). Respects 180-day cooldown per artist.

**Artist-level choices**: `[y]es` (review albums), `[n]o` (skip), `[a]ll` (bulk add), `[d]on't ask 6mo`, `[q]uit`

**Album-level choices**: `[y]es` (add), `[n]o` (skip), `[l]ist` (show tracks), `[d]on't ask again`, `[q]uit`

### `artistradio <name> [--main-count N] [--per-artist N] [--filter big|small|both]`

Create a radio playlist using scraped "Fans Also Like" data.

| Option | Default | Description |
| -------- | --------- | ------------- |
| `--main-count` | 10 | Tracks from the main artist |
| `--per-artist` | 5 | Tracks from each discovered peer |
| `--filter` | both | `big` (popular peers), `small` (niche peers), `both` (all) |

### `addartist <name> [--album | --single | --all]`

Interactively select and add an artist's albums/singles to A-Z storage.

---

## Transaction Management

### `listadd`

List all ADD and INGEST transactions (newest first).

### `listdelete`

List all DELETE transactions.

### `showadd <transaction_id>`

Show tracks added in a transaction.

### `showdelete <transaction_id>`

Show tracks deleted in a transaction.

### `canceladd <transaction_id>`

Undo an ADD/INGEST: remove tracks from Spotify storage, soft-delete in DB.

### `canceldelete <transaction_id>`

Undo a DELETE: restore tracks to Spotify storage, unmark in DB.

---

## Maintenance

### `fixplaylists`

Interactively resolve duplicate playlists with the same name (Active Music, Phone Listening). Establish primary ID, delete duplicates, optionally resync DB.

### `cleandatabase <threshold>`

Permanently remove soft-deleted tracks older than threshold.

Format: `<number><unit>` where unit is `m` (months, ×30 days) or `y` (years, ×365 days). Example: `3m`, `1y`.
