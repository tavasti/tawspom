from typing import List, Optional, Dict, Set, Tuple
from datetime import datetime
import re
import sys
import unicodedata
import random
from tawspom.core.spotify import SpotifyClient
from tawspom.core.db import (
    init_db as db_init_db,
    upsert_track, mark_as_played, get_least_recently_played_tracks,
    create_transaction, mark_tracks_deleted, unmark_tracks_deleted,
    get_all_active_track_ids, list_transactions, get_tracks_by_transaction,
    get_transaction, delete_tracks_permanently, set_transaction_cancelled_status,
    add_active_tracks, remove_active_tracks, get_tracked_active_ids,
    get_tracks_by_ids, get_all_active_tracks
)
from tawspom.models import Track, Playlist, Transaction

class Manager:
    def __init__(self, sp_client: SpotifyClient, db_conn):
        self.sp = sp_client
        self.db = db_conn

    def _get_storage_letter(self, name: str) -> str:
        """Maps a track name to a single A-Z letter, handling accents and special characters."""
        if not name:
            return "A"
        
        name = name.upper()
        name = name.replace('Ä', 'A').replace('Å', 'A').replace('Ö', 'O')
        
        nfkd_form = unicodedata.normalize('NFKD', name)
        only_ascii = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
        
        first_char = only_ascii[0] if only_ascii else "A"
        
        if first_char.isalpha() and 'A' <= first_char <= 'Z':
            return first_char
        return "A" # Default for numbers/symbols

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
        """Initializes the database schema and populates it from A-Z playlists.
        Does NOT ingest or detect removals."""
        print("Initializing local database schema...")
        db_init_db()
        
        print("\nPopulating database from Spotify A-Z playlists...")
        spotify_ids, stats = self._fetch_all_storage_tracks()
        
        total_songs = sum(s['count'] for s in stats)
        print(f"\nDatabase initialized and populated with {total_songs} tracks.")

    def _fetch_all_storage_tracks(self) -> Tuple[Set[str], List[dict]]:
        """Internal helper to read all tracks from A-Z playlists and upsert to DB.
        Returns a set of all seen Spotify IDs and a list of per-playlist stats.
        """
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
                "name": pl.name,
                "count": pl_count,
                "duration": pl_duration_ms
            })
            
            for track in tracks:
                track.storage_playlist_id = pl.id
                upsert_track(self.db, track)
                spotify_track_ids.add(track.id)
        
        print(f"  Finished reading {len(playlists)} playlists.        ")
        return spotify_track_ids, playlist_stats

    def sync_storage(self):
        """Main sync loop: 
        1. Ingest new Liked Songs into storage.
        2. Fetch A-Z playlists and update DB.
        3. Detect manual removals from storage.
        4. Report summary.
        """
        # Step 1: Ingest new likes
        self.ingest_liked_songs()

        # Step 2: Fetch storage
        print("\nFetching current storage state from Spotify...")
        spotify_track_ids, playlist_stats = self._fetch_all_storage_tracks()

        # Step 3: Detect manual removals
        db_track_ids = set(get_all_active_track_ids(self.db))
        missing_ids = list(db_track_ids - spotify_track_ids)
        
        if missing_ids:
            print(f"\nDetected {len(missing_ids)} tracks removed manually from Spotify storage playlists:")
            missing_tracks = get_tracks_by_ids(self.db, missing_ids)
            self._print_tracks(missing_tracks)
            
            trans_id = create_transaction(self.db, "DELETE", len(missing_ids), "Sync: Manual removal from storage")
            mark_tracks_deleted(self.db, missing_ids, trans_id)
            print(f"Recorded DELETE transaction #{trans_id}\n")

        # Step 4: Report
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
        print("\nStorage synced successfully.")

    def ingest_liked_songs(self):
        """Moves tracks from 'Liked Songs' to storage playlists, avoiding duplicates."""
        print("Checking 'Liked Songs' for new tracks...")
        liked_tracks = self.sp.get_liked_songs()
        if not liked_tracks:
            print("No new liked songs to ingest.")
            return

        # Get current storage IDs from DB to avoid duplicates
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

        if not to_move:
            print("No new tracks to add to A-Z playlists.")
        else:
            print(f"Moving {len(to_move)} new songs to A-Z storage...")
            # Determine destinations
            tracks_by_letter: Dict[str, List[Track]] = {}
            for track in to_move:
                letter = self._get_storage_letter(track.name)
                if letter not in tracks_by_letter:
                    tracks_by_letter[letter] = []
                tracks_by_letter[letter].append(track)
                
            trans_id = create_transaction(self.db, "INGEST", len(to_move), f"Ingest from Liked Songs")
            current_playlists = {p.name: p.id for p in self.sp.get_storage_playlists()}
            now = datetime.now()

            for letter in sorted(tracks_by_letter.keys()):
                playlist_id = current_playlists.get(letter)
                if not playlist_id:
                    print(f"Creating storage playlist '{letter}'...")
                    new_pl = self.sp.create_playlist(letter)
                    playlist_id = new_pl.id
                
                tracks = tracks_by_letter[letter]
                track_ids = [t.id for t in tracks]
                
                print(f"  Adding {len(track_ids)} tracks to playlist '{letter}'...")
                self.sp.add_tracks_to_playlist(playlist_id, track_ids)
                
                for track in tracks:
                    track.storage_playlist_id = playlist_id
                    track.add_transaction_id = trans_id
                    track.added_at = now
                    upsert_track(self.db, track)
            print(f"New tracks recorded in DB (Transaction #{trans_id})")

        print(f"Cleaning up 'Liked Songs' ({len(liked_tracks)} tracks)...")
        self.sp.remove_liked_songs([t.id for t in liked_tracks])
        print("Ingest complete.")

    def deduplicate_storage(self):
        """Identifies and removes duplicate tracks (same artist/name/duration)."""
        print("Searching for duplicates in local database...")
        all_tracks = get_all_active_tracks(self.db)
        
        # Group by (Artist, Name) - case insensitive
        groups: Dict[Tuple[str, str], List[Track]] = {}
        for track in all_tracks:
            key = (track.artist.lower(), track.name.lower())
            if key not in groups:
                groups[key] = []
            groups[key].append(track)
            
        duplicate_groups = [g for g in groups.values() if len(g) > 1]
        
        to_delete = []
        for group in duplicate_groups:
            # Further refine by duration (within 2 seconds)
            # We sort by duration to easily find close ones
            group.sort(key=lambda x: x.duration_ms)
            
            subgroups: List[List[Track]] = []
            if group:
                current_subgroup = [group[0]]
                for i in range(1, len(group)):
                    if abs(group[i].duration_ms - group[i-1].duration_ms) <= 2000:
                        current_subgroup.append(group[i])
                    else:
                        subgroups.append(current_subgroup)
                        current_subgroup = [group[i]]
                subgroups.append(current_subgroup)
                
            for sg in subgroups:
                if len(sg) > 1:
                    # We have actual duplicates!
                    # Logic to pick the one to KEEP:
                    # 1. Prefer the one with a last_played_at
                    # 2. Prefer the one added first
                    sg.sort(key=lambda x: (x.last_played_at is None, x.added_at or datetime.max))
                    keep = sg[0]
                    others = sg[1:]
                    
                    print(f"\nFound duplicate group for '{keep.artist} - {keep.name}':")
                    print(f"  [KEEP] {keep.id} (Added: {keep.added_at}, Played: {keep.last_played_at})")
                    for other in others:
                        print(f"  [DEL ] {other.id} (Added: {other.added_at}, Played: {other.last_played_at})")
                        to_delete.append(other)

        if not to_delete:
            print("No simple duplicates found.")
            return

        print(f"\nProceed with removing {len(to_delete)} duplicate tracks from Spotify and DB? [y/N]: ")
        confirm = input().lower()
        if confirm != 'y':
            print("Operation cancelled.")
            return

        print(f"Removing {len(to_delete)} tracks...")
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

    def add_artist_to_storage(self, artist_name: str):
        """Finds artist, studio albums, and adds all tracks to A-Z storage playlists."""
        artists = self.sp.search_artist(artist_name)
        if not artists:
            print(f"Artist '{artist_name}' not found.")
            return

        selected_artist = None
        if len(artists) == 1:
            selected_artist = artists[0]
        else:
            print(f"\nMultiple artists found for '{artist_name}':")
            for idx, artist in enumerate(artists, 1):
                genres = ", ".join(artist["genres"])
                top_songs = artist["top_songs"]
                print(f"  [{idx}] {artist['name']} (Pop: {artist['popularity']}, Genres: {genres})")
                print(f"      Top Songs: {top_songs}")
            
            try:
                choice = int(input(f"\nSelect artist [1-{len(artists)}]: "))
                selected_artist = artists[choice - 1]
            except (ValueError, IndexError):
                print("Invalid choice. Operation cancelled.")
                return

        artist_id = selected_artist["id"]
        print(f"\nFetching albums for {selected_artist['name']} (ID: {artist_id})...")
        albums = self.sp.get_artist_albums(artist_id)
        
        if not albums:
            print(f"No studio albums found for {selected_artist['name']}.")
            return

        tracks_by_letter: Dict[str, List[Track]] = {}
        total_tracks = 0
        
        for album in albums:
            print(f"  Fetching tracks for album: {album['name']}")
            tracks = self.sp.get_album_tracks(album["id"], album["name"])
            for track in tracks:
                letter = self._get_storage_letter(track.name)
                if letter not in tracks_by_letter:
                    tracks_by_letter[letter] = []
                tracks_by_letter[letter].append(track)
                total_tracks += 1

        if total_tracks == 0:
            print(f"No tracks found in the {len(albums)} albums.")
            return

        print(f"\nSummary for {selected_artist['name']}:")
        print(f"  Total studio albums: {len(albums)}")
        print(f"  Total tracks to add: {total_tracks}")
        
        confirm = input("\nProceed with adding these tracks to A-Z playlists? [y/N]: ").lower()
        if confirm != 'y':
            print("Operation cancelled.")
            return

        trans_id = create_transaction(self.db, "ADD", total_tracks, f"addartist: {selected_artist['name']}")
        current_playlists = {p.name: p.id for p in self.sp.get_storage_playlists()}
        now = datetime.now()

        for letter in sorted(tracks_by_letter.keys()):
            playlist_id = current_playlists.get(letter)
            if not playlist_id:
                print(f"Creating storage playlist '{letter}'...")
                new_pl = self.sp.create_playlist(letter)
                playlist_id = new_pl.id
            
            tracks = tracks_by_letter[letter]
            track_ids = list(set(t.id for t in tracks))
            
            print(f"Adding {len(track_ids)} tracks to playlist '{letter}'...")
            self.sp.add_tracks_to_playlist(playlist_id, track_ids)
            
            for track in tracks:
                track.storage_playlist_id = playlist_id
                track.add_transaction_id = trans_id
                track.added_at = now
                upsert_track(self.db, track)
        
        print(f"Artist '{selected_artist['name']}' added to storage. Transaction #{trans_id}")

    def _get_current_track_id(self) -> Optional[str]:
        playback = self.sp.get_current_playback()
        if playback and playback.get("item"):
            return playback["item"]["id"]
        return None

    def process_listened_tracks(self, active_playlist_name: str) -> bool:
        """Identifies listened tracks and marks them in DB."""
        active_playlist = self.sp.get_active_playlist(active_playlist_name)
        if not active_playlist: return False

        playback = self.sp.get_current_playback()
        if not playback or not playback.get("item"):
            print("Error: Nothing is currently playing.")
            return False

        current_track_id = playback["item"]["id"]
        context = playback.get("context")
        if not context or context.get("type") != "playlist" or active_playlist.id not in context.get("uri", ""):
            print(f"Error: Currently playing from a different source (not {active_playlist_name}).")
            return False

        track_ids = self.sp.get_playlist_tracks_ordered(active_playlist.id)
        try:
            current_index = track_ids.index(current_track_id)
        except ValueError:
            print("Error: Currently playing track not found in the active playlist.")
            return False

        listened_ids = track_ids[:current_index]
        if not listened_ids:
            print("No new tracks have been listened to yet.")
            return True

        print(f"Marking {len(listened_ids)} tracks as played.")
        now = datetime.now()
        for tid in listened_ids:
            mark_as_played(self.db, tid, now)

        self.sp.remove_tracks_from_playlist(active_playlist.id, listened_ids)
        remove_active_tracks(self.db, listened_ids)
        return True

    def flush_active_playlist(self, active_playlist_name: str):
        """Marks all tracks (except currently playing) as played and clears them."""
        active_playlist = self.sp.get_active_playlist(active_playlist_name)
        if not active_playlist: return

        track_ids = self.sp.get_playlist_tracks_ordered(active_playlist.id)
        if not track_ids: return

        current_track_id = self._get_current_track_id()
        # Keep current track if it's in the list
        to_remove = [tid for tid in track_ids if tid != current_track_id]
        
        if not to_remove:
            print("Only the currently playing track remains. Skipping flush.")
            return

        print(f"Flushing {len(to_remove)} tracks from '{active_playlist_name}'.")
        now = datetime.now()
        for tid in to_remove:
            mark_as_played(self.db, tid, now)
        
        self.sp.remove_tracks_from_playlist(active_playlist.id, to_remove)
        remove_active_tracks(self.db, to_remove)

    def detect_manual_removals(self, current_active_ids: List[str]):
        """Detects if user manually removed tracks from 'Active Music' and removes from storage."""
        tracked_ids = set(get_tracked_active_ids(self.db))
        current_set = set(current_active_ids)
        
        removed_ids = list(tracked_ids - current_set)
        if not removed_ids:
            return

        print(f"\nDetected {len(removed_ids)} tracks manually removed from Active Music:")
        removed_tracks = get_tracks_by_ids(self.db, removed_ids)
        self._print_tracks(removed_tracks)
        
        ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        trans_id = create_transaction(self.db, "DELETE", len(removed_ids), f"Refill run removal {ts_str}")
        
        # Group by storage playlist for efficient removal
        by_playlist = {}
        for track in removed_tracks:
            if track.storage_playlist_id not in by_playlist:
                by_playlist[track.storage_playlist_id] = []
            by_playlist[track.storage_playlist_id].append(track.id)
            
        for pl_id, tids in by_playlist.items():
            print(f"Removing {len(tids)} tracks from storage playlist {pl_id}...")
            self.sp.remove_tracks_from_playlist(pl_id, tids)
            
        mark_tracks_deleted(self.db, removed_ids, trans_id)
        remove_active_tracks(self.db, removed_ids)
        print(f"Manual removals processed. Transaction #{trans_id}")

    def refill_active_playlist(self, active_playlist_name: str, target_hours: float, mode: str = "default"):
        if mode == "flush":
            self.flush_active_playlist(active_playlist_name)
        elif mode == "add":
            print("Mode 'add': Just adding more tracks.")
        else: # default
            if not self.process_listened_tracks(active_playlist_name):
                print("Aborting refill due to missing playback context.")
                return

        active_playlist = self.sp.get_active_playlist(active_playlist_name)
        target_ms = int(target_hours * 3600 * 1000)

        current_tracks = self.sp.get_playlist_tracks(active_playlist.id)
        current_duration_ms = sum(t.duration_ms for t in current_tracks)
        current_track_ids_set = set(t.id for t in current_tracks)

        # Detect manual removals
        if mode != "add":
            self.detect_manual_removals([t.id for t in current_tracks])

        needed_ms = target_ms - current_duration_ms
        if needed_ms <= 0:
            print(f"Active playlist already has {current_duration_ms / 3600000:.1f} hours.")
            return

        print(f"Adding music to reach {target_hours} hours. Currently at {current_duration_ms / 3600000:.1f} hours.")
        
        pool = get_least_recently_played_tracks(self.db, limit=1000)
        candidates = [t for t in pool if t.id not in current_track_ids_set]
        random.shuffle(candidates)
        
        to_add = []
        added_duration = 0
        last_artist = current_tracks[-1].artist if current_tracks else ""
        
        while candidates and added_duration < needed_ms:
            found_idx = -1
            for i, track in enumerate(candidates):
                if track.artist != last_artist:
                    found_idx = i
                    break
            
            if found_idx == -1:
                track = candidates.pop(0)
            else:
                track = candidates.pop(found_idx)
                
            to_add.append(track.id)
            added_duration += track.duration_ms
            last_artist = track.artist

        if to_add:
            print(f"Adding {len(to_add)} tracks to '{active_playlist_name}'.")
            self.sp.add_tracks_to_playlist(active_playlist.id, to_add)
            add_active_tracks(self.db, to_add)

    def list_adds(self):
        # We also want to show INGEST transactions here as they are additions
        transactions = [t for t in list_transactions(self.db, "ADD")]
        transactions.extend(list_transactions(self.db, "INGEST"))
        transactions.sort(key=lambda x: x.timestamp, reverse=True)
        self._print_transactions(transactions)

    def list_deletes(self):
        transactions = list_transactions(self.db, "DELETE")
        self._print_transactions(transactions)

    def _print_transactions(self, transactions: List[Transaction]):
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
        # Try both ADD and INGEST
        tracks = get_tracks_by_transaction(self.db, trans_id, "ADD")
        if not tracks:
            tracks = get_tracks_by_transaction(self.db, trans_id, "INGEST")
        self._print_tracks(tracks)

    def show_delete(self, trans_id: int):
        tracks = get_tracks_by_transaction(self.db, trans_id, "DELETE")
        self._print_tracks(tracks)

    def _print_tracks(self, tracks: List[Track]):
        if not tracks:
            print("No tracks found.")
            return
        print(f"{'Artist':<25} {'Album':<30} {'Name'}")
        print("-" * 90)
        for t in tracks:
            print(f"{t.artist[:23]:<25} {t.album[:28]:<30} {t.name}")

    def cancel_add(self, trans_id: int):
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
        
        for pl_id, track_ids in by_playlist.items():
            self.sp.add_tracks_to_playlist(pl_id, track_ids)

        unmark_tracks_deleted(self.db, [t.id for t in tracks])
        set_transaction_cancelled_status(self.db, trans_id, True)
        
        if trans.cancels_id:
            set_transaction_cancelled_status(self.db, trans.cancels_id, False)
            print(f"Original transaction #{trans.cancels_id} is now active again.")

        print(f"Tracks restored in DB.")

    def clean_database(self, older_than: str):
        match = re.match(r"(\d+)([my])", older_than.lower())
        if not match:
            print("Invalid format. Use e.g., '3m' for 3 months or '1y' for 1 year.")
            return
        amount, unit = int(match.group(1)), match.group(2)
        days = amount * 30 if unit == "m" else amount * 365
        count = delete_tracks_permanently(self.db, days)
        print(f"Permanently removed {count} tracks from database deleted over {older_than} ago.")

    def whoami(self):
        try:
            user = self.sp.sp.current_user()
            print(f"\nSuccessfully authenticated!")
            print(f"User Display Name: {user['display_name']}")
            print(f"User ID: {user['id']}")
        except Exception as e:
            print(f"Authentication failed: {e}")
