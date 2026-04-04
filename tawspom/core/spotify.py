import os
from typing import List, Optional
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from tawspom.models import Track, Playlist

load_dotenv()

class SpotifyClient:
    def __init__(self, user_label: str = "default", scope=None):
        cache_path = os.path.expanduser(f"~/.tawspom/token_{user_label}.json")
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        
        self.sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
            scope=scope or "user-library-read user-library-modify user-read-playback-state playlist-read-private playlist-modify-private playlist-modify-public",
            client_id=os.getenv("SPOTIPY_CLIENT_ID"),
            client_secret=os.getenv("SPOTIPY_CLIENT_SECRET"),
            redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI"),
            cache_path=cache_path,
            open_browser=False
        ))

    def get_storage_playlists(self) -> List[Playlist]:
        """Fetches all playlists that have a single-character name (A-Z)."""
        playlists = []
        offset = 0
        limit = 50

        while True:
            response = self.sp.current_user_playlists(limit=limit, offset=offset)
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
        results = self.sp.search(q=f"artist:{name}", type="artist", limit=30)
        items = results.get("artists", {}).get("items", [])
        
        artists = []
        for item in items:
            top_tracks_res = self.sp.artist_top_tracks(item["id"])
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

    def get_artist_albums(self, artist_id: str) -> List[dict]:
        """Fetches studio albums, filtering out live and deluxe versions where possible."""
        albums = []
        offset = 0
        while True:
            result = self.sp.artist_albums(artist_id, album_type="album", limit=50, offset=offset)
            albums.extend(result["items"])
            if not result["next"]:
                break
            offset += 50
        
        filtered = {}
        skip_keywords = ["live", "deluxe", "expanded", "bonus", "remaster", "edition", "super"]
        albums.sort(key=lambda x: x.get("release_date", "0000"), reverse=True)

        for album in albums:
            name_lower = album["name"].lower()
            if any(k in name_lower for k in skip_keywords):
                continue
            if album["name"] not in filtered:
                filtered[album["name"]] = album
        
        return list(filtered.values())

    def create_playlist(self, name: str) -> Playlist:
        """Creates a new private playlist."""
        user_id = self.sp.current_user()["id"]
        res = self.sp.user_playlist_create(user_id, name, public=False)
        return Playlist(res["id"], res["name"])

    def get_album_tracks(self, album_id: str, album_name: str = "") -> List[Track]:
        """Fetches all tracks for a specific album."""
        tracks = []
        offset = 0
        while True:
            result = self.sp.album_tracks(album_id, limit=50, offset=offset)
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
            result = self.sp.playlist_items(playlist_id, limit=limit, offset=offset)
            items = result.get("items", [])
            for item in items:
                t = item.get("track")
                if t:
                    artist_names = ", ".join([a["name"] for a in t["artists"]])
                    album_name = t.get("album", {}).get("name", "")
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

    def get_liked_songs(self) -> List[Track]:
        """Fetches all tracks from 'Liked Songs'."""
        tracks = []
        offset = 0
        limit = 50
        while True:
            result = self.sp.current_user_saved_tracks(limit=limit, offset=offset)
            items = result.get("items", [])
            for item in items:
                t = item.get("track")
                if t:
                    artist_names = ", ".join([a["name"] for a in t["artists"]])
                    album_name = t.get("album", {}).get("name", "")
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
        """Removes tracks from 'Liked Songs'. Batches of 50."""
        if not track_ids: return
        for i in range(0, len(track_ids), 50):
            # spotipy's current_user_saved_tracks_delete takes a list of IDs
            self.sp.current_user_saved_tracks_delete(tracks=track_ids[i:i+50])

    def get_active_playlist(self, name: str) -> Optional[Playlist]:
        """Finds or creates an 'Active' playlist by name."""
        offset = 0
        limit = 50
        while True:
            response = self.sp.current_user_playlists(limit=limit, offset=offset)
            items = response.get("items", [])
            for item in items:
                if item["name"] == name:
                    return Playlist(item["id"], item["name"], is_active=True)
            if len(items) < limit:
                break
            offset += limit
        
        user_id = self.sp.current_user()["id"]
        new_pl = self.sp.user_playlist_create(user_id, name, public=False)
        return Playlist(new_pl["id"], new_pl["name"], is_active=True)

    def get_current_playback(self) -> Optional[dict]:
        """Returns the currently playing track ID and context."""
        return self.sp.current_playback()

    def remove_tracks_from_playlist(self, playlist_id: str, track_ids: List[str]):
        """Removes a list of tracks from a playlist."""
        if not track_ids:
            return
        for i in range(0, len(track_ids), 100):
            self.sp.playlist_remove_all_occurrences_of_items(playlist_id, track_ids[i:i+100])

    def add_tracks_to_playlist(self, playlist_id: str, track_ids: List[str]):
        """Adds a list of tracks to a playlist."""
        if not track_ids:
            return
        for i in range(0, len(track_ids), 100):
            self.sp.playlist_add_items(playlist_id, track_ids[i:i+100])

    def get_playlist_tracks_ordered(self, playlist_id: str) -> List[str]:
        """Returns track IDs in the order they appear in the playlist."""
        track_ids = []
        offset = 0
        limit = 100
        while True:
            result = self.sp.playlist_items(playlist_id, fields="items(track(id))", limit=limit, offset=offset)
            items = result.get("items", [])
            track_ids.extend([item["track"]["id"] for item in items if item.get("track")])
            if len(items) < limit:
                break
            offset += limit
        return track_ids
