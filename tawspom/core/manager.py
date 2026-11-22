from typing import List, Optional, Dict, Set, Tuple
from datetime import datetime, timedelta
import re
import sys
import unicodedata
import random
import difflib
import statistics
from spotipy.exceptions import SpotifyException
from tawspom.core.spotify import SpotifyClient
from tawspom.core.lastfm import LastFMClient
from tawspom.core.scraper import SpotifyScraper
from tawspom.core.db import (
    init_db as db_init_db,
    upsert_track, upsert_tracks, mark_as_played, get_least_recently_played_tracks,
    create_transaction, mark_tracks_deleted, unmark_tracks_deleted,
    get_all_active_track_ids, list_transactions, get_tracks_by_transaction,
    get_transaction, delete_tracks_permanently, set_transaction_cancelled_status,
    add_active_tracks, remove_active_tracks, get_tracked_active_ids,
    get_tracks_by_ids, get_all_active_tracks, get_playlist_track_count,
    add_to_duplicate_allowlist, is_duplicate_allowed, save_review_session,
    get_review_session, clear_review_session, get_library_stats,
    get_artist_last_check, set_artist_checked, get_handled_albums, add_to_ignored_albums,
    reset_active_tracks, set_meta
)
from tawspom.models import Track, Playlist, Transaction
from tawspom.core.constants import (
    DEFAULT_STORAGE_LETTER, REFILL_ARTIST_LOCK_WINDOW, REFILL_CANDIDATE_POOL_SIZE,
    BINARY_DEDUPE_THRESHOLD_MS, ROOT_NAME_FORBIDDEN_KEYWORDS, VERSION_REVIEW_GROUP_LIMIT,
    FINDNEW_QUALIFY_MIN_TRACKS, FINDNEW_ARTIST_COOLDOWN_DAYS, FINDNEW_ALBUM_SIMILARITY_THRESHOLD,
    MOMENTUM_WINDOW_DAYS, PHONE_PLAYLIST_CAPACITY_HOURS
)

class Manager:
    def __init__(self, sp_client: SpotifyClient, db_conn):
        self.sp = sp_client
        self.db = db_conn
        self.lfm = LastFMClient()
        self.scraper = SpotifyScraper()

    def _get_storage_letter(self, name: str) -> str:
        """Maps a track name to a single A-Z letter, handling accents and special characters."""
        if not name:
            return DEFAULT_STORAGE_LETTER
        
        name = name.upper()
        name = name.replace('Ä', 'A').replace('Å', 'A').replace('Ö', 'O')
        
        nfkd_form = unicodedata.normalize('NFKD', name)
        only_ascii = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
        
        first_char = only_ascii[0] if only_ascii else DEFAULT_STORAGE_LETTER
        
        if first_char.isalpha() and 'A' <= first_char <= 'Z':
            return first_char
        return DEFAULT_STORAGE_LETTER

    def _format_duration(self, ms: int) -> str:
        """Formats milliseconds into H:MM:SS."""
        seconds = int(ms / 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours}:{minutes:02}:{seconds:02}"
        else:
            return f"{minutes:02}:{seconds:02}"

    def init_db(self):
        """Initializes the database schema and populates it from A-Z playlists."""
        print("Initializing local database schema...")
        db_init_db()
        
        print("\nPopulating database from Spotify A-Z playlists...")
        spotify_ids, stats = self._fetch_all_storage_tracks(skip_synced=True)
        
        total_songs = sum(s['count'] for s in stats)
        print(f"\nDatabase initialized and populated with {total_songs} tracks.")

    def _fetch_all_storage_tracks(self, skip_synced: bool = False) -> Tuple[Set[str], List[dict]]:
        """Internal helper to read all tracks from A-Z playlists and upsert to DB."""
        playlists = self.sp.get_storage_playlists()
        spotify_track_ids = set()
        playlist_stats = []

        for pl in playlists:
            print(f"  Reading {pl.name}...", end="\r")
            sys.stdout.flush()
            
            tracks = self.sp.get_playlist_tracks(pl.id)
            pl_count = len(tracks)
            pl_duration_ms = sum(t.duration_ms for t in tracks)
            
            playlist_stats.append({
                "id": pl.id,
                "name": pl.name,
                "count": pl_count,
                "duration": pl_duration_ms
            })
            
            # Save tracks to DB
            upsert_tracks(self.db, tracks)
            for t in tracks:
                spotify_track_ids.add(t.id)
        
        print(f"  Finished reading {len(playlists)} playlists.        ")
        return spotify_track_ids, playlist_stats

    def _find_suspicious_playlists(self, playlist_stats: list[dict]) -> tuple[set[str], list[tuple]]:
        """Finds playlists whose read track count collapsed vs the DB's count for them.

        Such a collapse usually means a partial read (transient API glitch), not a
        real removal, so 'missing' tracks mapped to those playlists must not be
        silently marked deleted. Returns (suspicious playlist IDs, details).
        """
        suspicious_ids: set[str] = set()
        detail: list[tuple] = []
        for stat in playlist_stats:
            db_count = get_playlist_track_count(self.db, stat["id"])
            if db_count > 0 and stat["count"] < 0.5 * db_count:
                suspicious_ids.add(stat["id"])
                detail.append((stat["name"], stat["count"], db_count))
        return suspicious_ids, detail

    def _ask_suspicious_action(self, suspicious_detail: list[tuple], suspicious_missing: int) -> str:
        """Prompts about missing tracks mapped to short-read playlists. Returns 'y', 'n' or 'r'."""
        print("\n⚠️  SAFETY WARNING: some playlists returned far fewer tracks than the database holds for them:")
        for name, read_count, db_count in suspicious_detail:
            print(f"   playlist '{name}': read {read_count} tracks, DB has {db_count}")
        print(f"   {suspicious_missing} missing tracks are mapped to these playlists.")
        print("   This usually means a partial Spotify read, not a real removal.")
        print("  Action: [y]es (mark missing as deleted) / [n]o (skip) / [r]esync (re-fetch from Spotify)")
        return input("  >> ").lower().strip()

    def sync_storage(self):
        """Main sync loop: Ingests new music, dedupes, and detects manual removals from Spotify."""
        self.ingest_liked_songs()
        self.deduplicate_storage()

        print("\nSyncing storage state...")
        spotify_track_ids, playlist_stats = self._fetch_all_storage_tracks()

        # Compare Spotify state with DB state to find manual removals
        db_track_ids = set(get_all_active_track_ids(self.db))
        deletion_skipped = False

        # Safety: an empty read from Spotify is never evidence of mass removal
        if not playlist_stats or not spotify_track_ids:
            print("\n⚠️  SAFETY ABORT: no storage playlists (or no tracks) could be read from Spotify.")
            print("   This is usually a temporary API problem. Deletion detection is SKIPPED")
            print("   to protect your library. Re-run sync when Spotify is reachable.")
            deletion_skipped = True
        else:
            suspicious_ids, suspicious_detail = self._find_suspicious_playlists(playlist_stats)
            while True:
                missing_ids = list(db_track_ids - spotify_track_ids)
                if not missing_ids:
                    break

                # Per-playlist short-read safety: a playlist whose read count
                # collapsed vs the DB is treated as a partial read, not as
                # evidence of removal, until the user confirms or a re-fetch agrees.
                if suspicious_ids:
                    suspicious_missing = sum(
                        1 for t in get_tracks_by_ids(self.db, missing_ids)
                        if t.storage_playlist_id in suspicious_ids
                    )
                    if suspicious_missing:
                        action = self._ask_suspicious_action(suspicious_detail, suspicious_missing)
                        if action == 'r':
                            print("Re-fetching storage playlists from Spotify...")
                            spotify_track_ids, playlist_stats = self._fetch_all_storage_tracks()
                            if not playlist_stats or not spotify_track_ids:
                                print("⚠️  Re-fetch also returned no data. Deletion detection is SKIPPED "
                                      "to protect your library.")
                                deletion_skipped = True
                                break
                            suspicious_ids, suspicious_detail = self._find_suspicious_playlists(playlist_stats)
                            continue
                        if action != 'y':
                            print("Skipping deletion detection. No tracks marked as deleted.")
                            deletion_skipped = True
                            break
                        # 'y': user confirmed; the global mass check below still applies

                # Mass-removal safety: same pattern as refill
                is_mass = len(db_track_ids) > 10 and len(missing_ids) / len(db_track_ids) > 0.5
                if is_mass:
                    print(f"\n⚠️  SAFETY WARNING: {len(missing_ids)} out of {len(db_track_ids)} tracks "
                          f"({len(missing_ids) / len(db_track_ids):.1%})")
                    print("appear to be missing from your storage playlists.")
                    print("This could be a Spotify API sync error or a deliberate mass removal.")
                    print("  Action: [y]es (mark as deleted) / [n]o (skip) / [r]esync (re-fetch from Spotify)")
                    confirm = input("  >> ").lower().strip()
                    if confirm == 'r':
                        print("Re-fetching storage playlists from Spotify...")
                        spotify_track_ids, playlist_stats = self._fetch_all_storage_tracks()
                        if not playlist_stats or not spotify_track_ids:
                            print("⚠️  Re-fetch also returned no data. Deletion detection is SKIPPED "
                                  "to protect your library.")
                            deletion_skipped = True
                            break
                        continue
                    elif confirm != 'y':
                        print("Skipping deletion detection. No tracks marked as deleted.")
                        deletion_skipped = True
                        break

                print(f"\nDetected {len(missing_ids)} tracks removed manually from Spotify storage playlists:")
                missing_tracks = get_tracks_by_ids(self.db, missing_ids)
                self._print_tracks(missing_tracks)

                trans_id = create_transaction(self.db, "DELETE", len(missing_ids), "Sync: Manual removal from storage")
                mark_tracks_deleted(self.db, missing_ids, trans_id)
                print(f"Recorded DELETE transaction #{trans_id}\n")
                break

        # Print summary table
        total_songs = 0
        total_duration_ms = 0
        print(f"{'Playlist':<10} {'Songs':<8} {'Duration':<12}")
        print("-" * 30)
        for stat in sorted(playlist_stats, key=lambda x: x["name"]):
            print(f"{stat['name']:<10} {stat['count']:<8} {self._format_duration(stat['duration']):<12}")
            total_songs += stat['count']
            total_duration_ms += stat['duration']
        
        print("-" * 30)
        print(f"{'TOTAL':<10} {total_songs:<8} {self._format_duration(total_duration_ms):<12}")
        if deletion_skipped:
            print("\nSync finished with warnings — deletion detection was skipped.")
        else:
            print("\nStorage synced successfully.")

    def ingest_liked_songs(self):
        """Moves tracks from 'Liked Songs' to storage playlists, avoiding duplicates."""
        print("Checking 'Liked Songs' for new tracks...")
        liked_tracks = self.sp.get_liked_songs()
        if not liked_tracks:
            print("No new liked songs to ingest.")
            return

        active_storage_ids = set(get_all_active_track_ids(self.db))
        
        to_move = []
        already_there_count = 0
        for track in liked_tracks:
            if track.id in active_storage_ids:
                already_there_count += 1
            else:
                to_move.append(track)

        if already_there_count > 0:
            print(f"Note: {already_there_count} tracks were already in storage. They will only be removed from 'Liked Songs'.")

        skipped_tracks: list[Track] = []

        if to_move:
            print(f"Moving {len(to_move)} new songs to A-Z storage...")
            tracks_by_letter: dict[str, list[Track]] = {}
            for track in to_move:
                letter = self._get_storage_letter(track.name)
                if letter not in tracks_by_letter:
                    tracks_by_letter[letter] = []
                tracks_by_letter[letter].append(track)
                
            current_playlists = {p.name: p.id for p in self.sp.get_storage_playlists()}
            now = datetime.now()
            added_tracks: list[Track] = []

            for letter in sorted(tracks_by_letter.keys()):
                playlist_id = current_playlists.get(letter)
                if not playlist_id:
                    # Double-check before ever creating a playlist (prevents duplicates)
                    playlist_id = self._resolve_storage_playlist(letter)
                    if playlist_id is None:
                        # Playlist could not be verified; keep these tracks in 'Liked Songs'
                        skipped_tracks.extend(tracks_by_letter[letter])
                        continue
                    current_playlists[letter] = playlist_id
                
                tracks = tracks_by_letter[letter]
                track_ids = [t.id for t in tracks]
                
                print(f"  Adding {len(track_ids)} tracks to playlist '{letter}'...")
                _added, skipped = self.sp.add_tracks_to_playlist_unique(playlist_id, track_ids)
                if skipped:
                    print(f"  Note: {len(skipped)} tracks were already in playlist '{letter}' and not added again.")
                
                # Update tracks with storage info before DB upsert
                for track in tracks:
                    track.storage_playlist_id = playlist_id
                    track.added_at = now
                added_tracks.extend(tracks)
            
            if added_tracks:
                trans_id = create_transaction(self.db, "INGEST", len(added_tracks), "Ingest from Liked Songs")
                for track in added_tracks:
                    track.add_transaction_id = trans_id
                upsert_tracks(self.db, added_tracks)
                print(f"New tracks recorded in DB (Transaction #{trans_id})")

            if skipped_tracks:
                print(f"\n⚠️  WARNING: {len(skipped_tracks)} tracks were NOT ingested because their storage")
                print("   playlist could not be verified on Spotify. They remain in 'Liked Songs'.")
                print("   Check your playlists (or run fixplaylists) and re-run sync.")

        # Clear only the processed tracks from Liked Songs (skipped ones stay for the next run)
        skipped_ids = {t.id for t in skipped_tracks}
        to_remove = [t.id for t in liked_tracks if t.id not in skipped_ids]
        if to_remove:
            print(f"Cleaning up 'Liked Songs' ({len(to_remove)} tracks)...")
            self.sp.remove_liked_songs(to_remove)
        print("Ingest complete.")

    def _get_db_playlist_id_for_letter(self, letter: str) -> str | None:
        """Returns the storage playlist ID that the DB's active tracks for `letter` point to.
        Returns None if the DB has no active tracks for this letter."""
        cur = self.db.cursor()
        cur.execute("SELECT name, storage_playlist_id FROM track WHERE is_deleted = 0 AND storage_playlist_id != ''")
        counts: dict[str, int] = {}
        for name, pl_id in cur.fetchall():
            if self._get_storage_letter(name) == letter:
                counts[pl_id] = counts.get(pl_id, 0) + 1
        if not counts:
            return None
        # If tracks point to several playlists (e.g. after a duplicate-creation incident),
        # prefer the one holding the most tracks
        return max(counts.items(), key=lambda kv: kv[1])[0]

    def _resolve_storage_playlist(self, letter: str) -> str | None:
        """Resolves the storage playlist ID for `letter` without ever creating a duplicate.
        Called when the initial playlist fetch did not include `letter`.
        Retries the fetch, then verifies the playlist ID stored in the DB.
        Only creates a new playlist when the DB has no tracks for this letter at all.
        Returns None when the playlist cannot be verified - the caller must NOT create one."""
        # 1. Retry the playlist fetch (the initial one may have been incomplete)
        print(f"  Playlist '{letter}' not found in initial fetch. Re-checking Spotify...")
        for p in self.sp.get_storage_playlists():
            if p.name == letter:
                print(f"  Found playlist '{letter}' on retry (ID: {p.id}).")
                return p.id

        # 2. Verify the playlist ID stored in the DB for this letter
        db_pl_id = self._get_db_playlist_id_for_letter(letter)
        if db_pl_id:
            if self.sp.get_playlist_name(db_pl_id) == letter:
                print(f"  Verified DB-stored playlist '{letter}' (ID: {db_pl_id}) still exists on Spotify.")
                return db_pl_id
            print(f"  DB points to playlist ID {db_pl_id}, which is not available on Spotify as '{letter}'.")

        # 3. No DB tracks for this letter at all -> genuinely new, safe to create
        if db_pl_id is None:
            print(f"  No existing tracks for '{letter}' in DB. Creating storage playlist '{letter}'...")
            return self.sp.create_playlist(letter).id

        # 4. DB has tracks for this letter but the playlist cannot be verified -> do NOT create
        print(f"  ⚠️  Could not verify storage playlist '{letter}' on Spotify, but the DB has tracks for it.")
        print(f"  NOT creating a new playlist to avoid duplicates.")
        return None

    def deduplicate_storage(self):
        """Identifies and removes duplicate tracks (same artist/name/duration)."""
        print("Checking for binary duplicates...")
        all_tracks = get_all_active_tracks(self.db)
        
        # Group by Lead Artist + Song Name
        groups: Dict[Tuple[str, str], List[Track]] = {}
        for track in all_tracks:
            key = (track.artist.lower(), track.name.lower())
            if key not in groups:
                groups[key] = []
            groups[key].append(track)
            
        duplicate_groups = [g for g in groups.values() if len(g) > 1]
        
        to_delete = []
        to_show = [] # Tracks identified for review
        
        for group in duplicate_groups:
            # Sort by duration to group similar lengths
            group.sort(key=lambda x: x.duration_ms)
            subgroups: List[List[Track]] = []
            if group:
                current_subgroup = [group[0]]
                for i in range(1, len(group)):
                    # Within defined threshold (e.g. 2s) is considered same audio
                    if abs(group[i].duration_ms - group[i-1].duration_ms) <= BINARY_DEDUPE_THRESHOLD_MS:
                        current_subgroup.append(group[i])
                    else:
                        subgroups.append(current_subgroup)
                        current_subgroup = [group[i]]
                subgroups.append(current_subgroup)
                
            for sg in subgroups:
                if len(sg) > 1:
                    # Preference: Most play history, then oldest added_at
                    sg.sort(key=lambda x: (x.last_played_at is None, x.added_at or datetime.max))
                    keep = sg[0]
                    others = sg[1:]
                    to_show.append((keep, others))
                    to_delete.extend(others)

        if not to_delete:
            print("No binary duplicates found.")
            return

        # List identified duplicates for user confirmation
        print(f"\nFound {len(to_delete)} duplicate tracks across {len(to_show)} song groups:")
        for keep, others in to_show:
            print(f"\n  [KEEP] {keep.artist} - {keep.name} ({self._format_duration(keep.duration_ms)})")
            print(f"         ID: {keep.id} | Added: {keep.added_at} | Played: {keep.last_played_at}")
            for other in others:
                print(f"  [DEL ] {other.artist} - {other.name} ({self._format_duration(other.duration_ms)})")
                print(f"         ID: {other.id} | Added: {other.added_at} | Played: {other.last_played_at}")

        print(f"\nProceed with removing these {len(to_delete)} duplicates? [y/N]: ")
        confirm = input().lower()
        if confirm != 'y':
            print("Operation cancelled.")
            return

        print(f"Removing {len(to_delete)} duplicate tracks...")
        trans_id = create_transaction(self.db, "DELETE", len(to_delete), "Deduplication run")
        
        by_playlist = {}
        for track in to_delete:
            if track.storage_playlist_id not in by_playlist:
                by_playlist[track.storage_playlist_id] = []
            by_playlist[track.storage_playlist_id].append(track.id)
            
        for pl_id, tids in by_playlist.items():
            self.sp.remove_tracks_from_playlist(pl_id, tids)
            
        mark_tracks_deleted(self.db, [t.id for t in to_delete], trans_id)
        print(f"Deduplication complete. Recorded DELETE transaction #{trans_id}")

    def recreate_storage_playlist(self, letter: str):
        """Rebuilds one storage playlist so it exactly matches the DB mapping.

        The DB is the source of truth: after the run, the playlist contains
        exactly the active tracks the DB maps to it, once each. Extra copies
        are removed, extra tracks are removed (tracks the DB maps to another
        playlist are first guaranteed present there), and missing tracks are
        added. Zero DB writes; is_deleted is never touched. One confirmation.
        """
        letter = (letter or "").upper()
        if len(letter) != 1 or not letter.isascii() or not letter.isalpha():
            print(f"Invalid storage letter: '{letter}'. Use a single A-Z letter.")
            return

        print(f"Recreating storage playlist '{letter}' from the database...")
        print("NOTE: this trusts the DB as the source of truth. Run 'sync' first if you are not sure it is current.\n")

        # 1) Resolve the target playlist on the current account (never create)
        playlists = self.sp.get_storage_playlists()
        by_name: dict[str, list[Playlist]] = {}
        for pl in playlists:
            by_name.setdefault(pl.name.upper(), []).append(pl)

        matches = by_name.get(letter, [])
        if len(matches) > 1:
            print(f"⚠️  Found {len(matches)} playlists named '{letter}':")
            for m in matches:
                print(f"   ID: {m.id}")
            print("Resolve the duplicates first (e.g. 'fixplaylists'), then re-run.")
            return
        if not matches:
            print(f"⚠️  Playlist '{letter}' not found on this account. Nothing was done.")
            return
        target = matches[0]
        pl_id_to_name = {pl.id: pl.name for pl in playlists}

        # 2) Refill set: the active tracks the DB maps to this exact playlist
        all_tracks = get_all_active_tracks(self.db)
        refill_tracks = [t for t in all_tracks if t.storage_playlist_id == target.id]
        refill_ids = [t.id for t in refill_tracks]
        refill_set = set(refill_ids)

        # 3) Read the current playlist content
        current_ids = self.sp.get_playlist_tracks_ordered(target.id)
        current_unique = list(dict.fromkeys(current_ids))
        dup_copies = len(current_ids) - len(current_unique)

        # 4) Classify tracks that are in the playlist but not in the refill set
        extra_ids = [tid for tid in current_unique if tid not in refill_set]
        extra_rows = {t.id: t for t in get_tracks_by_ids(self.db, extra_ids)} if extra_ids else {}
        move_by_pl: dict[str, list[str]] = {}    # (a) DB maps to another existing playlist
        deleted_extras: list[str] = []           # (b) DB marks deleted
        unknown_extras: list[str] = []           # (c) unknown to the DB
        unresolvable: list[tuple[str, str]] = []  # (a') DB maps to a playlist not on this account
        for tid in extra_ids:
            row = extra_rows.get(tid)
            if row is None:
                unknown_extras.append(tid)
            elif row.is_deleted:
                deleted_extras.append(tid)
            elif row.storage_playlist_id in pl_id_to_name:
                move_by_pl.setdefault(row.storage_playlist_id, []).append(tid)
            else:
                unresolvable.append((tid, row.storage_playlist_id))

        missing_ids = [tid for tid in refill_ids if tid not in set(current_unique)]

        # 5) Never remove a track we cannot guarantee in its DB-mapped playlist
        if unresolvable:
            print(f"\n⚠️  SAFETY ABORT: {len(unresolvable)} track(s) in playlist '{letter}' are mapped by the DB")
            print("   to playlists that do not exist on this account, so they cannot be guaranteed elsewhere:")
            for tid, pl_id in unresolvable[:20]:
                row = extra_rows[tid]
                print(f"   {row.artist} - {row.name} (ID {tid}) → playlist ID {pl_id}")
            if len(unresolvable) > 20:
                print(f"   ... and {len(unresolvable) - 20} more")
            print("Fix the DB mapping or the account playlists, then re-run.")
            return

        # 6) Nothing to do?
        if not dup_copies and not extra_ids and not missing_ids:
            if not refill_ids and not current_ids:
                print(f"Nothing to do: playlist '{letter}' is empty and the DB maps no active tracks to it.")
            else:
                print(f"Nothing to do: playlist '{letter}' already contains exactly the {len(refill_ids)} database tracks, once each.")
            return

        # 7) Preview + single confirmation
        track_by_id = {t.id: t for t in all_tracks}
        unknown_labels = self.sp.get_track_summaries(unknown_extras) if unknown_extras else {}
        print(f"Playlist '{letter}': currently {len(current_ids)} entries ({len(current_unique)} unique).")
        print(f"Database maps {len(refill_ids)} active tracks to this playlist.")
        print(f"After recreate: exactly those {len(refill_ids)} tracks, once each.")
        if dup_copies:
            print(f"  - {dup_copies} duplicate copies removed")
        if extra_ids:
            print(f"  - {len(extra_ids)} extra tracks removed:")
            for pl_id, tids in sorted(move_by_pl.items(), key=lambda kv: pl_id_to_name[kv[0]]):
                for tid in tids:
                    row = extra_rows[tid]
                    print(f"      {row.artist} - {row.name}        → in DB: playlist '{pl_id_to_name[pl_id]}'")
            for tid in deleted_extras:
                row = extra_rows[tid]
                print(f"      {row.artist} - {row.name}        → deleted in database")
            for tid in unknown_extras:
                print(f"      {unknown_labels.get(tid, tid)}        → not in database")
        if missing_ids:
            print(f"  - {len(missing_ids)} missing tracks added:")
            for tid in missing_ids:
                row = track_by_id[tid]
                print(f"      {row.artist} - {row.name}")
        print("\nProceed? [y/N]: ")
        confirm = input().lower()
        if confirm != 'y':
            print("Operation cancelled. Nothing was changed.")
            return

        # 8) Guarantee DB-mapped destinations BEFORE touching the target
        for pl_id, tids in sorted(move_by_pl.items(), key=lambda kv: pl_id_to_name[kv[0]]):
            print(f"Ensuring {len(tids)} track(s) are in playlist '{pl_id_to_name[pl_id]}'...")
            try:
                added, skipped = self.sp.add_tracks_to_playlist_unique(pl_id, tids)
            except SpotifyException as e:
                print(f"\n⚠️  SAFETY ABORT: could not guarantee tracks in playlist '{pl_id_to_name[pl_id]}' ({e}).")
                print("   Nothing was changed. Fix the problem and re-run.")
                return
            if len(added) + len(skipped) != len(tids):
                print(f"\n⚠️  SAFETY ABORT: {len(tids) - len(added) - len(skipped)} track(s) could not be guaranteed")
                print(f"   in playlist '{pl_id_to_name[pl_id]}'. Nothing was changed.")
                return
            if skipped:
                print(f"  {len(skipped)} already present, {len(added)} added.")

        # 9) Clear + refill (same playlist ID: the DB references it by ID)
        print(f"Clearing playlist '{letter}'...")
        self.sp.clear_playlist(target.id)
        print(f"Adding {len(refill_ids)} tracks...")
        self.sp.add_tracks_to_playlist(target.id, refill_ids)

        # 10) Verify the result
        print("Verifying...")
        final_ids = self.sp.get_playlist_tracks_ordered(target.id)
        final_set = set(final_ids)
        missing = [tid for tid in refill_ids if tid not in final_set]
        extra = [tid for tid in final_ids if tid not in refill_set]
        if missing or extra or len(final_ids) != len(refill_ids):
            print(f"  ⚠️  Verification: {len(missing)} tracks missing, {len(extra)} unexpected tracks present, "
                  f"{len(final_ids)} entries total (expected {len(refill_ids)}). Re-run recreate to self-heal.")
        else:
            print(f"  OK: playlist '{letter}' now contains exactly the {len(refill_ids)} database tracks, once each.")

    def _get_root_name(self, name: str) -> str:
        """Strips common version/remix/feat suffixes to find the 'root' song name."""
        # Using centralized regex keywords for consistency
        name = re.sub(fr'\s*[\[\(]({ROOT_NAME_FORBIDDEN_KEYWORDS}).*?[\]\)]', '', name, flags=re.IGNORECASE)
        name = re.sub(fr'\s*-\s*({ROOT_NAME_FORBIDDEN_KEYWORDS}).*$', '', name, flags=re.IGNORECASE)
        return name.strip().lower()

    def find_version_duplicates(self):
        """Identifies potential semantic duplicates and puts them in 'Duplicate Review' playlist."""
        print("Searching for different versions of the same songs...")
        all_tracks = get_all_active_tracks(self.db)
        
        groups: Dict[Tuple[str, str], List[Track]] = {}
        for track in all_tracks:
            # Group by Lead Artist + Root Song Name
            lead_artist = track.artist.split(',')[0].strip().lower()
            root_name = self._get_root_name(track.name)
            if not root_name: continue
            
            key = (lead_artist, root_name)
            if key not in groups:
                groups[key] = []
            groups[key].append(track)
            
        potential_groups = {}
        for key, tracks in groups.items():
            if len(tracks) < 2: continue
            
            # Filter out pairs already in the allowlist
            filtered_tracks = []
            for i in range(len(tracks)):
                is_new = True
                for j in range(len(tracks)):
                    if i == j: continue
                    if is_duplicate_allowed(self.db, tracks[i].id, tracks[j].id):
                        is_new = False
                        break
                if is_new:
                    filtered_tracks.append(tracks[i])
            
            if len(filtered_tracks) > 1:
                potential_groups[key] = filtered_tracks

        if not potential_groups:
            print("No potential versions found to review.")
            return

        # Prepare a manageable review session
        review_keys = list(potential_groups.keys())[:VERSION_REVIEW_GROUP_LIMIT]
        tracks_to_review = []
        session_data = []
        
        print(f"\nPreparing review session for {len(review_keys)} song groups...")
        for key in review_keys:
            tracks = potential_groups[key]
            for t in tracks:
                tracks_to_review.append(t.id)
                session_data.append((t.id, f"{key[0]}|{key[1]}"))

        review_pl = self.sp.get_active_playlist("Duplicate Review")
        if review_pl is None:
            print("Aborted: 'Duplicate Review' playlist is not available.")
            return
        print(f"Clearing and populating '{review_pl.name}'...")
        existing_ids = self.sp.get_playlist_tracks_ordered(review_pl.id)
        self.sp.remove_tracks_from_playlist(review_pl.id, existing_ids)
        self.sp.add_tracks_to_playlist_unique(review_pl.id, tracks_to_review)
        
        save_review_session(self.db, session_data)
        
        print("\nReview session ready!")
        print("1. Open Spotify and listen to the 'Duplicate Review' playlist.")
        print("2. DELETE any tracks you don't want to keep.")
        print("3. LEAVE the tracks you want to keep.")
        print("4. When done, run: python main.py processversions")

    def process_version_review(self):
        """Finalizes the review session: deletes missing tracks, allows remaining ones."""
        session_groups = get_review_session(self.db)
        if not session_groups:
            print("No active review session found. Run 'findversions' first.")
            return

        review_pl = self.sp.get_active_playlist("Duplicate Review")
        if review_pl is None:
            print("Aborted: 'Duplicate Review' playlist is not available.")
            return
        current_ids = set(self.sp.get_playlist_tracks_ordered(review_pl.id))
        
        tracks_to_delete = []
        kept_duplicate_groups = []
        restored_groups = []
        
        print("Processing your review...")
        for key, expected_ids in session_groups.items():
            kept_in_group = [tid for tid in expected_ids if tid in current_ids]
            removed_in_group = [tid for tid in expected_ids if tid not in current_ids]
            
            if not kept_in_group:
                # Safety: If all versions deleted, restore them for a second look
                restored_groups.append(expected_ids)
            else:
                if removed_in_group:
                    tracks_to_delete.extend(removed_in_group)
                
                if len(kept_in_group) > 1:
                    kept_duplicate_groups.append(kept_in_group)

        if restored_groups:
            print(f"\nWarning: You removed ALL versions for {len(restored_groups)} songs.")
            for group in restored_groups:
                tracks = get_tracks_by_ids(self.db, group)
                print(f"  - {tracks[0].artist}: {tracks[0].name} ({len(tracks)} versions)")
            
            print("These will be restored to the 'Duplicate Review' playlist for you to try again.")
            restore_ids = [tid for g in restored_groups for tid in g]
            self.sp.add_tracks_to_playlist_unique(review_pl.id, restore_ids)

        if kept_duplicate_groups:
            print(f"\nYou have kept multiple versions for {len(kept_duplicate_groups)} songs:")
            for group in kept_duplicate_groups:
                tracks = get_tracks_by_ids(self.db, group)
                print(f"  - {tracks[0].artist}: {tracks[0].name} ({len(tracks)} versions)")
            
            confirm = input("\nIs it okay to allow these duplicates in the future? [y/N]: ").lower()
            if confirm != 'y':
                print("Operation aborted. No changes made.")
                return
            
            for group in kept_duplicate_groups:
                add_to_duplicate_allowlist(self.db, group)
            print("Allowed duplicates added to database.")

        if tracks_to_delete:
            print(f"\nDeleting {len(tracks_to_delete)} rejected tracks from storage...")
            trans_id = create_transaction(self.db, "DELETE", len(tracks_to_delete), "Semantic Deduplication Review")
            removed_tracks_full = get_tracks_by_ids(self.db, tracks_to_delete)
            by_playlist = {}
            for track in removed_tracks_full:
                if track.storage_playlist_id not in by_playlist:
                    by_playlist[track.storage_playlist_id] = []
                by_playlist[track.storage_playlist_id].append(track.id)
                
            for pl_id, tids in by_playlist.items():
                self.sp.remove_tracks_from_playlist(pl_id, tids)
                
            mark_tracks_deleted(self.db, tracks_to_delete, trans_id)
            print(f"Recorded DELETE transaction #{trans_id}")

        if not restored_groups:
            clear_review_session(self.db)
            self.sp.remove_tracks_from_playlist(review_pl.id, self.sp.get_playlist_tracks_ordered(review_pl.id))
            print("\nReview complete. 'Duplicate Review' playlist cleared.")
        else:
            print("\nReview partially complete. Group(s) were restored to 'Duplicate Review' playlist for another try.")

    def add_artist_to_storage(self, artist_name: str, types: List[str] = ["album"]):
        """Finds artist and interactively lets user select albums/singles to add."""
        artists = self.sp.search_artist(artist_name)
        if not artists:
            print(f"Artist '{artist_name}' not found.")
            return

        selected_artist = artists[0] if len(artists) == 1 else None
        if not selected_artist:
            print(f"\nMultiple artists found for '{artist_name}':")
            for idx, artist in enumerate(artists, 1):
                print(f"  [{idx}] {artist['name']} (Pop: {artist['popularity']}, Genres: {', '.join(artist['genres'])})")
                print(f"      Top Songs: {artist['top_songs']}")
            
            try:
                choice = int(input(f"\nSelect artist [1-{len(artists)}]: "))
                selected_artist = artists[choice - 1]
            except (ValueError, IndexError):
                print("Invalid choice. Operation cancelled.")
                return

        artist_id = selected_artist["id"]
        print(f"\nFetching releases for {selected_artist['name']}...")
        all_releases = self.sp.get_artist_albums(artist_id, types=types)
        
        if not all_releases:
            print(f"No releases found matching criteria ({'/'.join(types)}).")
            return

        selected_indices = set()

        # Multi-select album picker
        while True:
            print(f"\n--- {selected_artist['name']} Releases ---")
            for idx, album in enumerate(all_releases, 1):
                year = album.get("release_date", "0000")[:4]
                status = "[X]" if (idx-1) in selected_indices else "[ ]"
                print(f"  {status} [{idx:2}] ({year}) {album['name']}")
            
            print(f"\nSelected: {len(selected_indices)} releases.")
            print("Add [a]ll / [s]ome / [n]one / [c]ancel / [ok] to proceed")
            choice = input("> ").lower().strip()
            
            if choice == 'a':
                selected_indices = set(range(len(all_releases)))
                break
            elif choice == 'n':
                selected_indices = set()
                print("Selection cleared.")
            elif choice == 'c':
                print("Operation cancelled.")
                return
            elif choice == 'ok':
                if not selected_indices:
                    print("Nothing selected. Use 'c' to cancel or select some releases first.")
                    continue
                break
            elif choice == 's' or choice.isdigit() or choice == 'all' or any(c in choice for c in ', ;'):
                if choice == 'all':
                    selected_indices = set(range(len(all_releases)))
                    continue

                if choice == 's':
                    print("Enter numbers to toggle (or 'all' to select all, 'ok' to proceed):")
                    nums_str = input(">> ").lower().strip()
                else:
                    nums_str = choice
                
                if nums_str == 'all':
                    selected_indices = set(range(len(all_releases)))
                    continue
                elif nums_str == 'ok':
                    break

                nums = re.split(r'[ ,;]+', nums_str)
                for n in nums:
                    try:
                        val = int(n)
                        idx = val - 1
                        if 0 <= idx < len(all_releases):
                            if idx in selected_indices:
                                selected_indices.remove(idx)
                            else:
                                selected_indices.add(idx)
                    except ValueError:
                        continue
            else:
                print("Invalid choice.")

        selected_albums = [all_releases[i] for i in sorted(list(selected_indices))]

        active_storage_ids = set(get_all_active_track_ids(self.db))
        tracks_by_letter: Dict[str, List[Track]] = {}
        total_tracks_to_add = 0
        already_there_count = 0
        
        print("\nFetching tracks for selected releases...")
        for album in selected_albums:
            tracks = self.sp.get_album_tracks(album["id"], album["name"])
            for track in tracks:
                if track.id in active_storage_ids:
                    already_there_count += 1
                    continue
                    
                letter = self._get_storage_letter(track.name)
                if letter not in tracks_by_letter:
                    tracks_by_letter[letter] = []
                tracks_by_letter[letter].append(track)
                total_tracks_to_add += 1

        print(f"\nFinal Summary for {selected_artist['name']}:")
        print(f"  Releases selected: {len(selected_albums)}")
        print(f"  New tracks to add: {total_tracks_to_add}")
        print(f"  Tracks already in storage: {already_there_count}")
        
        if total_tracks_to_add == 0:
            print("All tracks are already in your storage. Nothing to add.")
            return

        confirm = input("\nProceed with adding new tracks to A-Z playlists? [y/N]: ").lower()
        if confirm != 'y':
            print("Operation cancelled.")
            return

        current_playlists = {p.name: p.id for p in self.sp.get_storage_playlists()}
        now = datetime.now()
        added_tracks: List[Track] = []

        for letter in sorted(tracks_by_letter.keys()):
            playlist_id = current_playlists.get(letter)
            if not playlist_id:
                # Double-check before ever creating a playlist (prevents duplicates)
                playlist_id = self._resolve_storage_playlist(letter)
                if playlist_id is None:
                    print(f"  ⚠️  Skipping tracks for '{letter}' (playlist could not be verified).")
                    continue
                current_playlists[letter] = playlist_id
            
            tracks = tracks_by_letter[letter]
            track_ids = [t.id for t in tracks]
            
            print(f"  Adding {len(track_ids)} tracks to playlist '{letter}'...")
            _added, skipped = self.sp.add_tracks_to_playlist_unique(playlist_id, track_ids)
            if skipped:
                print(f"  Note: {len(skipped)} tracks were already in playlist '{letter}' and not added again.")
            
            # Update tracks with storage info before DB upsert
            for track in tracks:
                track.storage_playlist_id = playlist_id
                track.added_at = now
            added_tracks.extend(tracks)
        
        if added_tracks:
            trans_id = create_transaction(self.db, "ADD", len(added_tracks), f"addartist: {selected_artist['name']}")
            for track in added_tracks:
                track.add_transaction_id = trans_id
            upsert_tracks(self.db, added_tracks)
            print(f"Artist '{selected_artist['name']}' added to storage. Transaction #{trans_id}")
        else:
            print("No tracks added (all storage playlists failed verification).")

    def create_artist_radio(self, artist_name: str, main_count: int, size_filter: str, per_artist: int):
        """Creates a custom artist radio using the robot scraper for discovery with interactive selection."""
        artists = self.sp.search_artist(artist_name)
        if not artists:
            print(f"Artist '{artist_name}' not found on Spotify.")
            return

        selected_artist = artists[0] if len(artists) == 1 else None
        if not selected_artist:
            print(f"\nMultiple artists found for '{artist_name}':")
            for idx, artist in enumerate(artists, 1):
                print(f"  [{idx}] {artist['name']} (Pop: {artist['popularity']}, Genres: {', '.join(artist['genres'])})")
                print(f"      Top Songs: {artist['top_songs']}")
            
            try:
                choice = int(input(f"\nSelect artist [1-{len(artists)}]: "))
                selected_artist = artists[choice - 1]
            except (ValueError, IndexError):
                print("Invalid choice. Operation cancelled.")
                return

        artist_id = selected_artist["id"]
        print(f"Creating radio for {selected_artist['name']}...")
        
        main_tracks = self.sp.get_artist_top_tracks(artist_id)
        selected_track_ids = [t["id"] for t in main_tracks[:main_count]]
        
        # Discover Peers via ROBOT SCRAPER (Bypass API limits)
        print("Discovering peers via Spotify Web Player robot...")
        scraped_peers = self.scraper.get_related_artists(artist_id)
        peer_ids = [p["id"] for p in scraped_peers]
        
        if not peer_ids:
            print("Robot found no peers on the Spotify web player. Aborting.")
            return

        peer_details = []
        for i in range(0, len(peer_ids), 50):
            batch = self.sp.sp.artists(peer_ids[i:i+50])
            peer_details.extend(batch.get("artists", []) if batch else [])

        if not peer_details:
            print("Failed to fetch peer details. Aborting.")
            return

        # Midpoint popularity filter
        peer_details.sort(key=lambda x: x["popularity"], reverse=True)
        midpoint_pop = statistics.mean(a['popularity'] for a in peer_details)
        
        if size_filter == "big":
            filtered = [a for a in peer_details if a['popularity'] >= midpoint_pop]
        elif size_filter == "small":
            filtered = [a for a in peer_details if a['popularity'] < midpoint_pop]
        else: 
            filtered = peer_details
            
        if not filtered:
            print(f"No peers matched the filter '{size_filter}'. Aborting.")
            return

        print(f"Found {len(filtered)} peers matching filter '{size_filter}' (Midpoint: {midpoint_pop:.1f}).")
        
        # Fair selection from peers
        random.shuffle(filtered)
        added_related = 0
        for peer in filtered:
            try:
                r_tracks = self.sp.get_artist_top_tracks(peer["id"])
                if r_tracks:
                    to_take = min(per_artist, len(r_tracks))
                    selected_track_ids.extend([t["id"] for t in r_tracks[:to_take]])
                    added_related += to_take
                    print(f"  Added {to_take} tracks from {peer['name']}")
            except SpotifyException:
                continue

        if added_related == 0:
            print("No tracks found from discovered peers. Aborting.")
            return

        radio_name = f"Tawspom Radio: {selected_artist['name']} ({size_filter})"
        radio_pl = self.sp.get_active_playlist(radio_name)
        if radio_pl is None:
            print(f"Aborted: radio playlist '{radio_name}' is not available.")
            return
        existing_ids = self.sp.get_playlist_tracks_ordered(radio_pl.id)
        self.sp.remove_tracks_from_playlist(radio_pl.id, existing_ids)
        
        random.shuffle(selected_track_ids)
        self.sp.add_tracks_to_playlist_unique(radio_pl.id, selected_track_ids)
        
        print(f"\nRadio playlist '{radio_name}' created with {len(selected_track_ids)} tracks.")

    def find_new_music(self):
        """Discovers new albums from artists the user already likes (>= qualifying min tracks)."""
        print(f"Searching for artists you like (>= {FINDNEW_QUALIFY_MIN_TRACKS} listened songs)...")
        cur = self.db.cursor()
        cur.execute("""
            SELECT artist, id FROM track 
            WHERE last_played_at IS NOT NULL AND is_deleted = 0
            GROUP BY artist
            HAVING COUNT(*) >= ?
        """, (FINDNEW_QUALIFY_MIN_TRACKS,))
        qualifying_rows = cur.fetchall()
        
        if not qualifying_rows:
            print(f"No artists found with at least {FINDNEW_QUALIFY_MIN_TRACKS} listened tracks.")
            return

        print(f"Found {len(qualifying_rows)} artists to check.")
        
        current_playlists = {p.name: p.id for p in self.sp.get_storage_playlists()}
        now = datetime.now()
        cooldown_threshold = now - timedelta(days=FINDNEW_ARTIST_COOLDOWN_DAYS)

        for artist_name, sample_track_id in qualifying_rows:
            last_check = get_artist_last_check(self.db, artist_name)
            if last_check and last_check > cooldown_threshold:
                continue

            print(f"\n--- Checking artist: {artist_name} ---")
            
            # Resolve accurate Artist ID using an existing track ID from our DB
            try:
                track_info = self.sp.sp.track(sample_track_id)
                if not track_info:
                    continue
                artist_id = track_info['artists'][0]['id']
                real_artist_name = track_info['artists'][0]['name']
            except Exception as e:
                print(f"  Error resolving artist ID: {e}")
                continue
            
            all_albums = self.sp.get_artist_albums(artist_id, types=["album"])
            if not all_albums:
                set_artist_checked(self.db, artist_name)
                continue

            handled_album_names = get_handled_albums(self.db, artist_name)
            
            new_albums = []
            for album in all_albums:
                if album["name"].lower() not in handled_album_names:
                    new_albums.append(album)

            if not new_albums:
                set_artist_checked(self.db, artist_name)
                continue

            print(f"  Found {len(new_albums)} potentially new albums.")

            # High-level artist confirmation
            skip_artist = False
            while True:
                print(f"  Process {real_artist_name}? [y]es (one by one) / [n]o (skip now) / [a]ll (add {len(new_albums)} albums) / [d]on't ask for 6mo / [q]uit")
                artist_choice = input("  >> ").lower().strip()
                if artist_choice == 'y':
                    break
                elif artist_choice == 'n':
                    skip_artist = True
                    break
                elif artist_choice == 'a':
                    print(f"    Bulk adding {len(new_albums)} albums for {real_artist_name}...")
                    all_tracks_to_add = []
                    for album in new_albums:
                        album_tracks = self.sp.get_album_tracks(album["id"], album["name"])
                        all_tracks_to_add.extend(album_tracks)
                    
                    if all_tracks_to_add:
                        trans_id = create_transaction(self.db, "ADD", len(all_tracks_to_add), f"findnew-bulk: {real_artist_name}")
                        tracks_by_letter = {}
                        for t in all_tracks_to_add:
                            letter = self._get_storage_letter(t.name)
                            if letter not in tracks_by_letter: tracks_by_letter[letter] = []
                            tracks_by_letter[letter].append(t)
                        
                        for letter, letter_tracks in tracks_by_letter.items():
                            playlist_id = current_playlists.get(letter)
                            if not playlist_id:
                                playlist_id = self._resolve_storage_playlist(letter)
                                if playlist_id is None:
                                    print(f"    ⚠️  Skipping tracks for '{letter}' (playlist could not be verified).")
                                    continue
                                current_playlists[letter] = playlist_id
                            
                            tids = [t.id for t in letter_tracks]
                            _added, skipped = self.sp.add_tracks_to_playlist_unique(playlist_id, tids)
                            if skipped:
                                print(f"    Note: {len(skipped)} tracks were already in playlist '{letter}' and not added again.")
                            for t in letter_tracks:
                                t.storage_playlist_id = playlist_id
                                t.add_transaction_id = trans_id
                                t.added_at = now
                            upsert_tracks(self.db, letter_tracks)
                        print(f"    Bulk add complete. Transaction #{trans_id}")
                    skip_artist = True # We are done with this artist
                    break
                elif artist_choice == 'd':
                    set_artist_checked(self.db, artist_name)
                    skip_artist = True
                    break
                elif artist_choice == 'q':
                    print("Quitting findnew.")
                    return
                else:
                    print("  Invalid choice.")

            if skip_artist:
                continue

            for album in new_albums:
                print(f"\n  [NEW ALBUM] ({album.get('release_date', '0000')[:4]}) {album['name']}")
                
                # Check for high similarity to existing catalog
                for handled in handled_album_names:
                    similarity = difflib.SequenceMatcher(None, album["name"].lower(), handled.lower()).ratio()
                    if similarity > FINDNEW_ALBUM_SIMILARITY_THRESHOLD:
                        print(f"  WARNING: Name is very similar to existing album: '{handled}' (Similarity: {similarity:.2f})")

                while True:
                    print("  Action: [y]es (add) / [n]o (skip) / [l]ist tracks / [d]on't ask again / [q]uit command")
                    choice = input("  > ").lower().strip()
                    
                    if choice == 'l':
                        tracks = self.sp.get_album_tracks(album["id"], album["name"])
                        print(f"    Tracks in '{album['name']}':")
                        for idx, t in enumerate(tracks, 1):
                            print(f"      {idx:2}. {t.name}")
                        continue
                    elif choice == 'y':
                        # Ingest the entire album
                        tracks = self.sp.get_album_tracks(album["id"], album["name"])
                        if tracks:
                            print(f"    Adding {len(tracks)} tracks to storage...")
                            trans_id = create_transaction(self.db, "ADD", len(tracks), f"findnew: {real_artist_name} - {album['name']}")
                            
                            tracks_by_letter = {}
                            for t in tracks:
                                letter = self._get_storage_letter(t.name)
                                if letter not in tracks_by_letter: tracks_by_letter[letter] = []
                                tracks_by_letter[letter].append(t)
                            
                            for letter, letter_tracks in tracks_by_letter.items():
                                playlist_id = current_playlists.get(letter)
                                if not playlist_id:
                                    playlist_id = self._resolve_storage_playlist(letter)
                                    if playlist_id is None:
                                        print(f"    ⚠️  Skipping tracks for '{letter}' (playlist could not be verified).")
                                        continue
                                    current_playlists[letter] = playlist_id
                                
                                tids = [t.id for t in letter_tracks]
                                _added, skipped = self.sp.add_tracks_to_playlist_unique(playlist_id, tids)
                                if skipped:
                                    print(f"    Note: {len(skipped)} tracks were already in playlist '{letter}' and not added again.")
                                for t in letter_tracks:
                                    t.storage_playlist_id = playlist_id
                                    t.add_transaction_id = trans_id
                                    t.added_at = now
                                upsert_tracks(self.db, letter_tracks)
                            print(f"    Added. Transaction #{trans_id}")
                        break
                    elif choice == 'n':
                        print("    Skipped for now.")
                        break
                    elif choice == 'd':
                        print("    Will not ask about this album again.")
                        add_to_ignored_albums(self.db, artist_name, album["name"])
                        break
                    elif choice == 'q':
                        print("Quitting findnew.")
                        return
                    else:
                        print("  Invalid choice.")

            set_artist_checked(self.db, artist_name)

        print("\nAll qualifying artists checked.")

    def _get_current_track_id(self) -> Optional[str]:
        playback = self.sp.get_current_playback()
        if playback and playback.get("item"):
            return playback["item"]["id"]
        return None

    def process_listened_tracks(self, active_playlist_name: str) -> tuple[list[str], Playlist | None]:
        """Identifies listened tracks, marks them in DB, and returns them along with the active playlist used."""
        active_playlist = self.sp.get_active_playlist(active_playlist_name)
        if not active_playlist: return [], None

        track_ids = self.sp.get_playlist_tracks_ordered(active_playlist.id)
        
        playback = self.sp.get_current_playback()
        if not playback or not playback.get("item"):
            print(f"Error: Nothing is currently playing. Start playing '{active_playlist_name}' first, or use --flush.")
            return [], active_playlist

        current_track_id = playback["item"]["id"]
        context = playback.get("context")
        context_uri = context.get("uri", "") if context else ""
        
        if not context or context.get("type") != "playlist" or active_playlist.id not in context_uri:
            # Check for name match fallback
            if context and context.get("type") == "playlist":
                curr_pl_id = context_uri.split(':')[-1]
                try:
                    curr_pl = self.sp.sp.playlist(curr_pl_id, fields="name")
                    if curr_pl and curr_pl.get("name") == active_playlist_name:
                        print(f"  Note: State reconciliation triggered. Switched to '{active_playlist_name}' ({curr_pl_id})")
                        active_playlist = Playlist(curr_pl_id, active_playlist_name, is_active=True)
                        track_ids = self.sp.get_playlist_tracks_ordered(active_playlist.id)
                        
                        # Immediately sync the DB's active_tracks table to this new playlist
                        reset_active_tracks(self.db, track_ids)
                        
                        # Update established ID in DB permanently
                        set_meta(self.db, f"playlist_id_{active_playlist_name}", curr_pl_id)
                        
                        # Validate library content
                        db_all_ids = set(get_all_active_track_ids(self.db))
                        unknown_tracks = [tid for tid in track_ids if tid not in db_all_ids]
                        if unknown_tracks:
                            print(f"  ⚠️  WARNING: {len(unknown_tracks)} tracks in this playlist are NOT in your library.")
                    else:
                        print(f"Error: Currently playing from a different source.")
                        print(f"  Expected playlist: {active_playlist_name} ({active_playlist.id})")
                        print(f"  Current source: {curr_pl.get('name') if curr_pl else 'Unknown'} ({curr_pl_id})")
                        return [], active_playlist
                except Exception:
                    return [], active_playlist
            else:
                print(f"Error: Currently playing from a different source.")
                return [], active_playlist

        try:
            current_index = track_ids.index(current_track_id)
        except ValueError:
            print(f"Error: Currently playing track not found in '{active_playlist_name}'. Use --flush if you want to reset it.")
            return [], active_playlist

        listened_ids = track_ids[:current_index]
        if not listened_ids:
            return [], active_playlist

        print(f"Marking {len(listened_ids)} tracks as played.")
        now = datetime.now()
        for tid in listened_ids:
            mark_as_played(self.db, tid, now)

        self.sp.remove_tracks_from_playlist(active_playlist.id, listened_ids)
        remove_active_tracks(self.db, listened_ids)
        return listened_ids, active_playlist

    def flush_active_playlist(self, active_playlist_name: str) -> List[str]:
        """Marks all tracks (except currently playing) as played, clears them, and returns them."""
        active_playlist = self.sp.get_active_playlist(active_playlist_name)
        if not active_playlist: return []

        track_ids = self.sp.get_playlist_tracks_ordered(active_playlist.id)
        if not track_ids: return []

        current_track_id = self._get_current_track_id()
        to_remove = [tid for tid in track_ids if tid != current_track_id]
        
        if not to_remove:
            print("Only the currently playing track remains. Skipping flush.")
            return []

        print(f"Flushing {len(to_remove)} tracks from '{active_playlist_name}'.")
        now = datetime.now()
        for tid in to_remove:
            mark_as_played(self.db, tid, now)
        
        self.sp.remove_tracks_from_playlist(active_playlist.id, to_remove)
        remove_active_tracks(self.db, to_remove)
        return to_remove

    def detect_manual_removals(self, current_active_ids: List[str]):
        """Detects if user manually removed tracks from 'Active Music' and removes from storage."""
        tracked_ids = list(get_tracked_active_ids(self.db))
        if not tracked_ids:
            return

        current_set = set(current_active_ids)
        removed_ids = [tid for tid in tracked_ids if tid not in current_set]
        
        if not removed_ids:
            return

        removal_ratio = len(removed_ids) / len(tracked_ids)
        if removal_ratio > 0.5 and len(tracked_ids) > 10:
            print(f"\n⚠️  SAFETY WARNING: {len(removed_ids)} out of {len(tracked_ids)} tracks ({removal_ratio:.1%}) ")
            print(f"appear to have been removed from your active playlist.")
            print("This could be a Spotify API sync error or a deliberate mass removal.")
            print("  Action: [y]es (delete from storage) / [n]o (skip) / [r]esync DB (use Spotify tracks as correct state)")
            confirm = input("  >> ").lower().strip()
            
            if confirm == 'r':
                print("Resyncing database to match Spotify playlist tracks...")
                reset_active_tracks(self.db, current_active_ids)
                print("DB resynced. Ghost tracks cleared.")
                return
            elif confirm != 'y':
                print("Aborting removal sync. No tracks deleted from storage.")
                return

        print(f"\nDetected {len(removed_ids)} tracks manually removed from Active Music:")
        removed_tracks = get_tracks_by_ids(self.db, removed_ids)
        self._print_tracks(removed_tracks)
        
        ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        trans_id = create_transaction(self.db, "DELETE", len(removed_ids), f"Refill run removal {ts_str}")
        
        by_playlist = {}
        for track in removed_tracks:
            if track.storage_playlist_id not in by_playlist:
                by_playlist[track.storage_playlist_id] = []
            by_playlist[track.storage_playlist_id].append(track.id)
            
        for pl_id, tids in by_playlist.items():
            self.sp.remove_tracks_from_playlist(pl_id, tids)
            
        mark_tracks_deleted(self.db, removed_ids, trans_id)
        remove_active_tracks(self.db, removed_ids)
        print(f"Manual removals processed. Transaction #{trans_id}")

    def _display_dashboard(self):
        """Displays the comprehensive library dashboard."""
        stats = get_library_stats(self.db)
        print("\n" + "="*30 + " LIBRARY STATUS " + "="*30)
        print(f"Tracks: {stats['total_active']:,} Active ({self._format_duration(stats['total_duration_ms'])}) | "
              f"{stats['total_deleted']} Deleted | "
              f"{stats['never_played']:,} Never Listened ({stats['coverage_pct']:.1f}% Coverage)")
        
        print(f"History: Library listening time: {self._format_duration(stats['total_history_ms'])}, "
              f"with deleted songs {self._format_duration(stats['full_listening_time_ms'])} | "
              f"Avg plays: {stats['avg_plays']:.2f}")
        
        print(f"Momentum: {self._format_duration(stats['momentum_ms'])} listened in last {MOMENTUM_WINDOW_DAYS} days.")
        
        if stats['oldest_waiting_at']:
            print(f"Queue: Oldest song waiting since {stats['oldest_waiting_at'].strftime('%Y-%m-%d %H:%M')}.")
        
        if stats['top_waiting_artists']:
            top_arr = [f"{a} ({c})" for a, c in stats['top_waiting_artists']]
            print(f"Top Waiting Artists: {', '.join(top_arr)}")
        print("="*76 + "\n")

    def refill_active_playlist(self, active_playlist_name: str, target_hours: float, mode: str = "default", phone_playlist_name: str = "Phone Listening"):
        """Refills the active playlist with oldest music, enforcing artist variety."""
        listened_ids = []
        
        if mode == "flush":
            active_playlist = self.sp.get_active_playlist(active_playlist_name)
            listened_ids = self.flush_active_playlist(active_playlist_name)
        elif mode == "add":
            active_playlist = self.sp.get_active_playlist(active_playlist_name)
            print("Mode 'add': Just adding more tracks.")
        else: # default
            listened_ids, active_playlist = self.process_listened_tracks(active_playlist_name)
            if not active_playlist: return 
            
            if not listened_ids:
                current_count = len(self.sp.get_playlist_tracks_ordered(active_playlist.id))
                if current_count > 0 and self._get_current_track_id() is None:
                    return

        if active_playlist is None:
            print("No active playlist found. Aborting refill.")
            return

        if listened_ids:
            self._update_phone_listening(listened_ids, phone_playlist_name)

        target_ms = int(target_hours * 3600 * 1000)

        print("Fetching current active playlist state...")
        current_tracks_on_spotify = self.sp.get_playlist_tracks(active_playlist.id)
        current_track_ids_set = set(t.id for t in current_tracks_on_spotify)

        if mode != "add":
            self.detect_manual_removals([t.id for t in current_tracks_on_spotify])

        current_duration_ms = sum(t.duration_ms for t in current_tracks_on_spotify)
        needed_ms = target_ms - current_duration_ms
        if needed_ms <= 0:
            print(f"Active playlist already has {current_duration_ms / 3600000:.1f} hours.")
            self._display_dashboard()
            return

        print(f"Adding music to reach {target_hours} hours. Currently at {current_duration_ms / 3600000:.1f} hours.")
        pool = get_least_recently_played_tracks(self.db, limit=REFILL_CANDIDATE_POOL_SIZE)
        
        artist_locks = {} 
        current_fill_index = 0
        
        # Initial locks based on what's currently playing/waiting
        tail_window = current_tracks_on_spotify[-REFILL_ARTIST_LOCK_WINDOW:]
        for i, t in enumerate(tail_window):
            dist_from_end = len(tail_window) - i
            lock_remaining = REFILL_ARTIST_LOCK_WINDOW - dist_from_end
            if lock_remaining > 0:
                artists = [a.strip() for a in t.artist.split(',')]
                for a in artists:
                    artist_locks[a] = max(artist_locks.get(a, 0), lock_remaining)

        to_add_ids = []
        added_play_counts = []
        added_duration = 0
        unique_artists_added = set()
        skipped_locked_count = 0
        
        print(f"Selecting tracks using Time-Queue with Spreading ({REFILL_ARTIST_LOCK_WINDOW}-song window)...")
        for track in pool:
            if added_duration >= needed_ms:
                break
            
            if track.id in current_track_ids_set:
                continue
                
            track_artists = [a.strip() for a in track.artist.split(',')]
            is_locked = any(artist_locks.get(a, 0) > current_fill_index for a in track_artists)
            
            if is_locked:
                skipped_locked_count += 1
                continue
                
            to_add_ids.append(track.id)
            added_play_counts.append(track.play_count)
            added_duration += track.duration_ms
            
            current_fill_index += 1
            for a in track_artists:
                artist_locks[a] = current_fill_index + REFILL_ARTIST_LOCK_WINDOW
                unique_artists_added.add(a)

        if to_add_ids:
            print(f"Selected {len(to_add_ids)} tracks from {len(unique_artists_added)} different artists. (Skipped {skipped_locked_count} locked tracks)")
            
            if added_play_counts:
                low = min(added_play_counts)
                high = max(added_play_counts)
                avg = sum(added_play_counts) / len(added_play_counts)
                med = statistics.median(added_play_counts)
                print(f"Play count stats for added songs: Low: {low}, Avg: {avg:.1f}, Median: {med}, High: {high}")

            print(f"Adding selected tracks to '{active_playlist.name}'...")
            _added, skipped = self.sp.add_tracks_to_playlist_unique(active_playlist.id, to_add_ids)
            if skipped:
                print(f"  Note: {len(skipped)} tracks were already in the playlist and not added again.")
            add_active_tracks(self.db, to_add_ids)
            print("Refill complete.")
        else:
            print(f"Warning: Could not find any tracks to add. (Processed {len(pool)} candidates, skipped {skipped_locked_count} due to locks)")

        self._display_dashboard()

    def fix_duplicate_playlists(self, target_names: List[str]):
        """Interactively finds and resolves duplicate playlists for given names."""
        print("\nScanning for duplicate playlists...")
        all_matches = {}
        offset = 0
        limit = 50
        while True:
            response = self.sp._call_with_retry(self.sp.sp.current_user_playlists, limit=limit, offset=offset)
            items = response.get("items", [])
            for item in items:
                if item["name"] in target_names:
                    if item["name"] not in all_matches:
                        all_matches[item["name"]] = []
                    track_count = item.get("tracks", {}).get("total", 0)
                    all_matches[item["name"]].append({
                        "id": item["id"],
                        "name": item["name"],
                        "tracks": track_count
                    })
            if len(items) < limit:
                break
            offset += limit

        for name in target_names:
            matches = all_matches.get(name, [])
            if not matches:
                print(f"No playlists found with name '{name}'.")
                continue
            
            print(f"\n--- Resolving duplicates for '{name}' ---")
            
            # Offer resync even for single matches to clean up orphans
            if len(matches) == 1:
                print(f"Only one playlist found ({matches[0]['id']}). Setting as official.")
                set_meta(self.db, f"playlist_id_{name}", matches[0]['id'])
                
                confirm_sync = input(f"Would you like to sync the DB state to the tracks in this playlist? [y/N]: ").lower()
                if confirm_sync == 'y':
                    track_ids = self.sp.get_playlist_tracks_ordered(matches[0]['id'])
                    reset_active_tracks(self.db, track_ids)
                    print(f"DB resynced with {len(track_ids)} tracks.")
                continue

            print(f"Found {len(matches)} playlists named '{name}':")
            for idx, m in enumerate(matches, 1):
                print(f"  [{idx}] ID: {m['id']} | Tracks: {m['tracks']}")
            
            try:
                choice = int(input(f"Select which one to KEEP [1-{len(matches)}]: "))
                keep = matches[choice - 1]
                others = [m for i, m in enumerate(matches) if i != (choice - 1)]
                
                # Establish the winner
                set_meta(self.db, f"playlist_id_{name}", keep['id'])
                print(f"Established '{name}' ({keep['id']}) as the official playlist.")

                # Option to cleanup others
                confirm_del = input(f"Would you like to DELETE the {len(others)} other duplicate playlists from Spotify? [y/N]: ").lower()
                if confirm_del == 'y':
                    for other in others:
                        print(f"  Removing duplicate {other['id']}...")
                        self.sp._call_with_retry(self.sp.sp.current_user_unfollow_playlist, other['id'])
                    print("Duplicates removed.")

                # Option to resync DB state
                confirm_sync = input(f"Would you like to sync the DB state to the tracks in the established playlist? [y/N]: ").lower()
                if confirm_sync == 'y':
                    track_ids = self.sp.get_playlist_tracks_ordered(keep['id'])
                    reset_active_tracks(self.db, track_ids)
                    print(f"DB resynced with {len(track_ids)} tracks.")

            except (ValueError, IndexError):
                print("Invalid choice. Skipping cleanup for this name.")

    def _update_phone_listening(self, track_ids: List[str], phone_playlist_name: str):
        """Adds tracks to the specified phone history playlist if it's shorter than configured capacity."""
        pl = self.sp.get_active_playlist(phone_playlist_name)
        if pl is None:
            print(f"  Playlist '{phone_playlist_name}' not found. Skipping history update.")
            return

        print(f"Checking '{phone_playlist_name}' capacity...")
        current_ms = self.sp.get_playlist_duration_ms(pl.id)
        
        limit_ms = int(PHONE_PLAYLIST_CAPACITY_HOURS * 3600 * 1000)
        if current_ms < limit_ms:
            print(f"  Adding {len(track_ids)} tracks to history...")
            _added, skipped = self.sp.add_tracks_to_playlist_unique(pl.id, track_ids)
            if skipped:
                print(f"  Note: {len(skipped)} tracks were already in history and not added again.")
        else:
            print(f"  '{phone_playlist_name}' is full (>= {PHONE_PLAYLIST_CAPACITY_HOURS} hours). Skipping history update.")

    def list_adds(self):
        """Lists addition and ingestion transactions."""
        transactions = [t for t in list_transactions(self.db, "ADD")]
        transactions.extend(list_transactions(self.db, "INGEST"))
        transactions.sort(key=lambda x: x.timestamp, reverse=True)
        self._print_transactions(transactions)

    def list_deletes(self):
        """Lists deletion transactions."""
        transactions = list_transactions(self.db, "DELETE")
        self._print_transactions(transactions)

    def _print_transactions(self, transactions: List[Transaction]):
        """Prints a table of transactions."""
        if not transactions:
            print("No transactions found.")
            return
        print(f"{'ID':<5} {'Timestamp':<20} {'Type':<8} {'Count':<6} {'Status':<12} {'Description'}")
        print("-" * 90)
        for t in transactions:
            ts = t.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            status = "[CANCELLED]" if t.is_cancelled else ""
            print(f"{t.id:<5} {ts:<20} {t.type:<8} {t.track_count:<6} {status:<12} {t.description}")

    def show_add(self, trans_id: int):
        """Show tracks added/ingested in a transaction."""
        tracks = get_tracks_by_transaction(self.db, trans_id, "ADD")
        if not tracks:
            tracks = get_tracks_by_transaction(self.db, trans_id, "INGEST")
        self._print_tracks(tracks)

    def show_delete(self, trans_id: int):
        """Show tracks deleted in a transaction."""
        tracks = get_tracks_by_transaction(self.db, trans_id, "DELETE")
        self._print_tracks(tracks)

    def _print_tracks(self, tracks: List[Track]):
        """Prints a summary table of tracks."""
        if not tracks:
            print("No tracks found.")
            return
        print(f"{'Artist':<25} {'Album':<30} {'Name'}")
        print("-" * 90)
        for t in tracks:
            print(f"{t.artist[:23]:<25} {t.album[:28]:<30} {t.name}")

    def cancel_add(self, trans_id: int):
        """Reverts an addition transaction by removing tracks from storage."""
        trans = get_transaction(self.db, trans_id)
        if not trans or trans.type not in ["ADD", "INGEST"]:
            print(f"Transaction #{trans_id} (ADD/INGEST) not found.")
            return

        if trans.is_cancelled:
            print(f"Transaction #{trans_id} is already cancelled.")
            return

        tracks = get_tracks_by_transaction(self.db, trans_id, trans.type)
        if not tracks:
            print("No tracks to cancel.")
            return

        print(f"Canceling transaction #{trans_id}: {trans.description}")
        print(f"Removing {len(tracks)} tracks from Spotify storage...")
        
        by_playlist = {}
        for t in tracks:
            if t.storage_playlist_id not in by_playlist:
                by_playlist[t.storage_playlist_id] = []
            by_playlist[t.storage_playlist_id].append(t.id)
        
        for pl_id, track_ids in by_playlist.items():
            self.sp.remove_tracks_from_playlist(pl_id, track_ids)

        new_trans_id = create_transaction(self.db, "DELETE", len(tracks), f"Cancel {trans.type} #{trans_id} ({trans.description})", cancels_id=trans_id)
        mark_tracks_deleted(self.db, [t.id for t in tracks], new_trans_id)
        set_transaction_cancelled_status(self.db, trans_id, True)
        print(f"Tracks marked as deleted in DB. Recorded DELETE transaction #{new_trans_id}")

    def cancel_delete(self, trans_id: int):
        """Reverts a deletion transaction by restoring tracks to storage."""
        trans = get_transaction(self.db, trans_id)
        if not trans or trans.type != "DELETE":
            print(f"Transaction #{trans_id} (DELETE) not found.")
            return

        if trans.is_cancelled:
            print(f"Transaction #{trans_id} is already cancelled.")
            return

        tracks = get_tracks_by_transaction(self.db, trans_id, "DELETE")
        if not tracks:
            print("No tracks to restore.")
            return

        print(f"Restoring transaction #{trans_id}: {trans.description}")
        
        by_playlist = {}
        for t in tracks:
            if t.storage_playlist_id not in by_playlist:
                by_playlist[t.storage_playlist_id] = []
            by_playlist[t.storage_playlist_id].append(t.id)
        
        total_added = 0
        total_skipped = 0
        for pl_id, track_ids in by_playlist.items():
            added, skipped = self.sp.add_tracks_to_playlist_unique(pl_id, track_ids)
            total_added += len(added)
            total_skipped += len(skipped)
        if total_skipped:
            print(f"  Note: {total_skipped} tracks were already in their playlists and not added again.")
        print(f"  Restored {total_added} tracks to Spotify storage.")

        unmark_tracks_deleted(self.db, [t.id for t in tracks])
        set_transaction_cancelled_status(self.db, trans_id, True)
        
        if trans.cancels_id:
            set_transaction_cancelled_status(self.db, trans.cancels_id, False)
            print(f"Original transaction #{trans.cancels_id} is now active again.")

        print(f"Tracks restored in DB.")

    def clean_database(self, older_than: str):
        """Permanently removes tracks deleted from storage longer ago than threshold."""
        match = re.match(r"(\d+)([my])", older_than.lower())
        if not match:
            print("Invalid format. Use e.g., '3m' for 3 months or '1y' for 1 year.")
            return
        amount, unit = int(match.group(1)), match.group(2)
        days = amount * 30 if unit == "m" else amount * 365
        count = delete_tracks_permanently(self.db, days)
        print(f"Permanently removed {count} tracks from database deleted over {older_than} ago.")

    def whoami(self):
        """Displays currently authenticated Spotify user."""
        try:
            user = self.sp.sp.current_user()
            if not user:
                raise RuntimeError("Spotify current_user() returned no user")
            print(f"\nSuccessfully authenticated!")
            print(f"User Display Name: {user['display_name']}")
            print(f"User ID: {user['id']}")
        except Exception as e:
            print(f"Authentication failed: {e}")
