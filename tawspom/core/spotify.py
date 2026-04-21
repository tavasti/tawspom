import os
import time
from typing import List, Optional, Tuple
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from requests.exceptions import ConnectionError
from urllib3.exceptions import ProtocolError
from tawspom.models import Track, Playlist
from tawspom.core.db import get_meta, set_meta

load_dotenv()

class SpotifyClient:
    def __init__(self, user_label: str = "default", scope=None, db_conn=None):
        self.user_label = user_label
        self.scope = scope or "user-library-read user-library-modify user-read-playback-state playlist-read-private playlist-modify-private playlist-modify-public"
        self.db = db_conn
        self._init_sp()

    def _init_sp(self):
        """Initializes or re-initializes the Spotify client."""
        cache_path = os.path.expanduser(f"~/.tawspom/token_{self.user_label}.json")
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        
        self.sp = spotipy.Spotify(
            auth_manager=SpotifyOAuth(
                scope=self.scope,
                client_id=os.getenv("SPOTIPY_CLIENT_ID"),
                client_secret=os.getenv("SPOTIPY_CLIENT_SECRET"),
                redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI"),
                cache_path=cache_path,
                open_browser=False
            ),
            requests_timeout=10
        )

    def _call_with_retry(self, func, *args, **kwargs):
        """Wraps a Spotify API call with a retry mechanism for connection errors."""
        max_retries = 3
        for i in range(max_retries):
            try:
                return func(*args, **kwargs)
            except (ConnectionError, ProtocolError) as e:
                if i == max_retries - 1:
                    raise
                print(f"\nConnection error: {e}. Retrying in 2 seconds... (Attempt {i+1}/{max_retries})")
                time.sleep(2)
                self._init_sp()
        return None

    def get_storage_playlists(self) -> List[Playlist]:
        """Fetches all playlists that have a single-character name (A-Z)."""
        playlists = []
        offset = 0
        limit = 50

        while True:
            response = self._call_with_retry(self.sp.current_user_playlists, limit=limit, offset=offset)
            items = response.get("items", [])
            for item in items:
                if len(item["name"]) == 1 and item["name"].isalpha():
                    playlists.append(Playlist(item["id"], item["name"]))
            
            if len(items) < limit:
                break
            offset += limit

        return playlists

    def search_artist(self, name: str) -> List[dict]:
        """Returns a list of the top 30 artists found matching the name, including top tracks."""
        results = self._call_with_retry(self.sp.search, q=f"artist:{name}", type="artist", limit=30)
        items = results.get("artists", {}).get("items", [])
        
        artists = []
        for item in items:
            top_tracks_res = self._call_with_retry(self.sp.artist_top_tracks, item["id"])
            top_tracks = top_tracks_res.get("tracks", [])[:3]
            top_songs_summary = ", ".join([t["name"][:20] for t in top_tracks])
            
            artists.append({
                "id": item["id"],
                "name": item["name"],
                "genres": item["genres"],
                "popularity": item["popularity"],
                "top_songs": top_songs_summary
            })
        return artists

    def get_artist_albums(self, artist_id: str, types: List[str] = ["album"]) -> List[dict]:
        """Fetches albums/singles, including release date."""
        albums = []
        offset = 0
        album_types = ",".join(types)
        while True:
            result = self._call_with_retry(self.sp.artist_albums, artist_id, album_type=album_types, limit=50, offset=offset)
            albums.extend(result["items"])
            if not result["next"]:
                break
            offset += 50
        
        filtered = {}
        skip_keywords = ["live", "deluxe", "expanded", "bonus", "remaster", "edition", "super"]
        albums.sort(key=lambda x: x.get("release_date", "0000"), reverse=True)

        for album in albums:
            name_lower = album["name"].lower()
            if album["album_type"] != "single" and any(k in name_lower for k in skip_keywords):
                continue
            
            if album["name"] not in filtered:
                filtered[album["name"]] = album
        
        return list(filtered.values())

    def create_playlist(self, name: str) -> Playlist:
        """Creates a new private playlist."""
        user = self._call_with_retry(self.sp.current_user)
        user_id = user["id"]
        res = self._call_with_retry(self.sp.user_playlist_create, user_id, name, public=False)
        return Playlist(res["id"], res["name"])

    def get_album_tracks(self, album_id: str, album_name: str = "") -> List[Track]:
        """Fetches all tracks for a specific album."""
        tracks = []
        offset = 0
        while True:
            result = self._call_with_retry(self.sp.album_tracks, album_id, limit=50, offset=offset)
            items = result.get("items", [])
            for t in items:
                artist_names = ", ".join([a["name"] for a in t["artists"]])
                tracks.append(Track(
                    id=t["id"],
                    name=t["name"],
                    artist=artist_names,
                    album=album_name,
                    duration_ms=t["duration_ms"],
                    storage_playlist_id="" 
                ))
            if not result["next"]:
                break
            offset += 50
        return tracks

    def get_playlist_tracks(self, playlist_id: str) -> List[Track]:
        """Fetches all tracks from a given playlist."""
        tracks = []
        offset = 0
        limit = 100

        while True:
            result = self._call_with_retry(self.sp.playlist_items, playlist_id, limit=limit, offset=offset)
            items = result.get("items", [])
            for item in items:
                t = item.get("track")
                if t:
                    artist_names = ", ".join([a["name"] for a in t["artists"]])
                    album_obj = t.get("album")
                    album_name = album_obj.get("name", "") if album_obj else ""
                    tracks.append(Track(
                        id=t["id"],
                        name=t["name"],
                        artist=artist_names,
                        album=album_name,
                        duration_ms=t["duration_ms"],
                        storage_playlist_id=playlist_id
                    ))

            if len(items) < limit:
                break
            offset += limit

        return tracks

    def get_playlist_duration_ms(self, playlist_id: str) -> int:
        """Calculates total duration of a playlist efficiently by only fetching duration fields."""
        total_ms = 0
        offset = 0
        limit = 100
        while True:
            result = self._call_with_retry(
                self.sp.playlist_items, 
                playlist_id, 
                fields="items(track(duration_ms)),next", 
                limit=limit, 
                offset=offset
            )
            if not result:
                break
            items = result.get("items", [])
            for item in items:
                t = item.get("track")
                if t:
                    total_ms += t.get("duration_ms", 0)
            
            if not result.get("next"):
                break
            offset += limit
        return total_ms

    def get_liked_songs(self) -> List[Track]:
        """Fetches all tracks from 'Liked Songs'."""
        tracks = []
        offset = 0
        limit = 50
        while True:
            result = self._call_with_retry(self.sp.current_user_saved_tracks, limit=limit, offset=offset)
            items = result.get("items", [])
            for item in items:
                t = item.get("track")
                if t:
                    artist_names = ", ".join([a["name"] for a in t["artists"]])
                    album_obj = t.get("album")
                    album_name = album_obj.get("name", "") if album_obj else ""
                    tracks.append(Track(
                        id=t["id"],
                        name=t["name"],
                        artist=artist_names,
                        album=album_name,
                        duration_ms=t["duration_ms"],
                        storage_playlist_id="LIKED"
                    ))
            if len(items) < limit:
                break
            offset += limit
        return tracks

    def remove_liked_songs(self, track_ids: List[str]):
        """Removes tracks from 'Liked Songs'. Using safe batch size of 20."""
        if not track_ids: return
        for i in range(0, len(track_ids), 20):
            self._call_with_retry(self.sp.current_user_saved_tracks_delete, tracks=track_ids[i:i+20])

    def _get_stored_playlist_id(self, name: str) -> Optional[str]:
        if not self.db: return None
        return get_meta(self.db, f"playlist_id_{name}")

    def _save_stored_playlist_id(self, name: str, pl_id: str):
        if not self.db: return
        set_meta(self.db, f"playlist_id_{name}", pl_id)

    def is_playlist_id_valid(self, pl_id: str) -> bool:
        try:
            self._call_with_retry(self.sp.playlist, pl_id, fields="id")
            return True
        except Exception:
            return False

    def get_active_playlist(self, name: str) -> Optional[Playlist]:
        """Finds or creates an 'Active' playlist by name, prioritizing the established ID or currently playing."""
        # 1. Check if we have an established ID in DB
        stored_id = self._get_stored_playlist_id(name)
        if stored_id and self.is_playlist_id_valid(stored_id):
            return Playlist(stored_id, name, is_active=True)

        # 2. Not stored or invalid, search by name
        matches = []
        offset = 0
        limit = 50
        while True:
            response = self._call_with_retry(self.sp.current_user_playlists, limit=limit, offset=offset)
            items = response.get("items", [])
            for item in items:
                if item["name"] == name:
                    track_count = item.get("tracks", {}).get("total", 0)
                    matches.append((Playlist(item["id"], item["name"], is_active=True), track_count))
            if len(items) < limit:
                break
            offset += limit
        
        if not matches:
            print(f"\n⚠️  WARNING: Playlist '{name}' not found on Spotify.")
            confirm = input(f"Would you like to create a new private playlist named '{name}'? [y/N]: ").lower().strip()
            if confirm == 'y':
                user = self._call_with_retry(self.sp.current_user)
                user_id = user["id"]
                new_pl = self._call_with_retry(self.sp.user_playlist_create, user_id, name, public=False)
                self._save_stored_playlist_id(name, new_pl["id"])
                return Playlist(new_pl["id"], new_pl["name"], is_active=True)
            else:
                print("Operation aborted. Please create the playlist manually or check your settings.")
                return None

        # 3. Handle multiple matches
        selected_pl = None
        if len(matches) > 1:
            print(f"\n⚠️  WARNING: Found {len(matches)} playlists named '{name}'.")
            
            playback = self.get_current_playback()
            if playback and playback.get("context") and playback["context"].get("type") == "playlist":
                current_uri = playback["context"].get("uri", "")
                for m, count in matches:
                    if m.id in current_uri:
                        print(f"  Note: Using currently playing playlist '{name}' ({m.id})")
                        selected_pl = m
                        break
            
            if not selected_pl:
                print("Please select which one to establish as the official playlist:")
                for idx, (m, count) in enumerate(matches, 1):
                    print(f"  [{idx}] ID: {m.id} | Tracks: {count}")
                
                try:
                    choice = int(input(f"Select 1-{len(matches)}: "))
                    selected_pl = matches[choice - 1][0]
                except (ValueError, IndexError):
                    selected_pl = matches[0][0]
        else:
            selected_pl = matches[0][0]
        
        # 4. Establish the winner!
        self._save_stored_playlist_id(name, selected_pl.id)
        return selected_pl

    def get_current_playback(self) -> Optional[dict]:
        """Returns the currently playing track ID and context."""
        return self._call_with_retry(self.sp.current_playback)

    def remove_tracks_from_playlist(self, playlist_id: str, track_ids: List[str]):
        """Removes a list of tracks from a playlist."""
        if not track_ids:
            return
        for i in range(0, len(track_ids), 100):
            self._call_with_retry(self.sp.playlist_remove_all_occurrences_of_items, playlist_id, track_ids[i:i+100])

    def add_tracks_to_playlist(self, playlist_id: str, track_ids: List[str]):
        """Adds a list of tracks to a playlist."""
        if not track_ids:
            return
        for i in range(0, len(track_ids), 100):
            self._call_with_retry(self.sp.playlist_add_items, playlist_id, track_ids[i:i+100])

    def get_playlist_tracks_ordered(self, playlist_id: str) -> List[str]:
        """Returns track IDs in the order they appear in the playlist."""
        track_ids = []
        offset = 0
        limit = 100
        while True:
            result = self._call_with_retry(self.sp.playlist_items, playlist_id, fields="items(track(id))", limit=limit, offset=offset)
            items = result.get("items", [])
            track_ids.extend([item["track"]["id"] for item in items if item.get("track")])
            if len(items) < limit:
                break
            offset += limit
        return track_ids

    def search_artists_by_genre(self, genre: str, limit: int = 50) -> List[dict]:
        """Discovers artists by searching for a specific genre."""
        query = f"genre:\"{genre}\""
        res = self._call_with_retry(self.sp.search, q=query, type="artist", limit=limit)
        return res.get("artists", {}).get("items", []) if res else []

    def get_artist_top_tracks(self, artist_id: str, country: str = 'US') -> List[dict]:
        """Fetches top tracks for an artist."""
        res = self._call_with_retry(self.sp.artist_top_tracks, artist_id, country=country)
        return res.get("tracks", []) if res else []
