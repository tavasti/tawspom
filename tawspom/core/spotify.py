import os
import time
from collections import deque
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse, quote as urllib_parse_quote
from dotenv import load_dotenv
import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.exceptions import SpotifyException, SpotifyOauthError
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError
from urllib3.exceptions import ProtocolError
from urllib3.util.retry import Retry
from tawspom.models import Track, Playlist
from tawspom.core.db import get_meta, set_meta
from tawspom.core.constants import (
    SPOTIFY_API_TIMEOUT, SPOTIFY_MAX_RETRIES, SPOTIFY_RETRY_DELAY,
    SPOTIFY_RATE_WINDOW_SECONDS, SPOTIFY_RATE_MAX_REQUESTS,
    SPOTIFY_MAX_RETRY_AFTER_SECONDS, SPOTIFY_RETRY_AFTER_DEFAULT_SECONDS,
    SPOTIFY_MAX_RATE_LIMIT_WAITS,
)

load_dotenv()

class SpotifyRateLimitError(SpotifyException):
    """Raised when Spotify's rate limit demands a wait beyond our cap.
    The in-progress operation is safe to re-run: it resumes from the current state."""
    def __init__(self, msg: str):
        super().__init__(429, -1, msg)

class _SlidingWindowRateLimiter:
    """Paces calls so at most `max_requests` happen within any `window` seconds.

    The Spotify Web API applies a rolling ~30s rate limit whose ceiling is not
    published (dev-mode apps are low). Pacing client-side keeps us under the
    limit, so 429s (and their long pauses) should not happen in the first place.
    """
    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window = window_seconds
        self._calls: deque = deque()

    def wait(self):
        now = time.monotonic()
        # Drop calls that have fallen out of the window
        while self._calls and now - self._calls[0] >= self.window:
            self._calls.popleft()
        if len(self._calls) >= self.max_requests:
            # Wait until the oldest call in the window falls out
            time.sleep(self.window - (now - self._calls[0]))
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= self.window:
                self._calls.popleft()
        self._calls.append(time.monotonic())

class SpotifyClient:
    def __init__(self, user_label: str = "default", scope=None, db_conn=None):
        self.user_label = user_label
        self.scope = scope or "user-library-read user-library-modify user-read-playback-state playlist-read-private playlist-modify-private playlist-modify-public"
        self.db = db_conn
        self._init_sp()

    def _init_sp(self):
        """Initializes or re-initializes the Spotify client."""
        self._cache_path = os.path.expanduser(f"~/.tawspom/token_{self.user_label}.json")
        os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)

        self.sp = spotipy.Spotify(
            auth_manager=SpotifyOAuth(
                scope=self.scope,
                client_id=os.getenv("SPOTIPY_CLIENT_ID"),
                client_secret=os.getenv("SPOTIPY_CLIENT_SECRET"),
                redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI"),
                cache_path=self._cache_path,
                open_browser=False
            ),
            requests_timeout=SPOTIFY_API_TIMEOUT,
            # spotipy's stub mis-annotates this as bool; it accepts a requests.Session
            requests_session=self._build_session()  # type: ignore[arg-type]
        )

    def _build_session(self) -> requests.Session:
        """Builds a requests session that does NOT auto-sleep on 429.

        spotipy's default urllib3 Retry treats 429 as retryable and sleeps for
        the full Retry-After (which can be hours). We handle 429 ourselves in
        _call_with_retry (bounded wait or clean stop), so 429 must raise
        immediately; 5xx keep the usual short retries.
        """
        session = requests.Session()
        retry = Retry(
            total=3,
            status=3,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE"]),
            backoff_factor=0.3,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _throttle(self):
        """Paces API calls to stay under Spotify's rolling rate limit.

        The limiter state is created lazily so test clients built without
        __init__ keep working.
        """
        limiter = getattr(self, "_rate_limiter", None)
        if limiter is None:
            limiter = self._rate_limiter = _SlidingWindowRateLimiter(
                SPOTIFY_RATE_MAX_REQUESTS, SPOTIFY_RATE_WINDOW_SECONDS)
        limiter.wait()

    def _retry_after_seconds(self, exc: SpotifyException) -> float:
        """Extracts the Retry-After header (seconds) from a 429 exception."""
        raw = exc.headers.get("Retry-After") if exc.headers else None
        if raw is None:
            return float(SPOTIFY_RETRY_AFTER_DEFAULT_SECONDS)
        try:
            return max(1.0, float(raw))
        except ValueError:
            return float(SPOTIFY_RETRY_AFTER_DEFAULT_SECONDS)

    def _call_with_retry(self, func, *args, **kwargs) -> Any:
        """Wraps a Spotify API call with a retry mechanism for connection, auth and rate-limit errors."""
        max_retries = SPOTIFY_MAX_RETRIES
        reauth_done = False
        rate_waits = 0
        for i in range(max_retries):
            try:
                self._throttle()
                return func(*args, **kwargs)
            except SpotifyOauthError as e:
                # Handle expired refresh token (invalid_grant)
                # Spotify deprecated one-time refresh tokens; they now expire after 6 months.
                # Per Spotify policy: discard the token and re-authenticate the user.
                if e.error == "invalid_grant" and not reauth_done:
                    reauth_done = True
                    self._handle_expired_token()
                    # Retry the original call with fresh token (one more attempt)
                    continue
                raise
            except SpotifyException as e:
                if e.http_status != 429:
                    raise
                # Rate limit: honor Spotify's Retry-After, but never sit through a
                # multi-hour pause — the command is resumable, so stopping cleanly
                # is better than hanging.
                retry_after = self._retry_after_seconds(e)
                rate_waits += 1
                if retry_after > SPOTIFY_MAX_RETRY_AFTER_SECONDS or rate_waits > SPOTIFY_MAX_RATE_LIMIT_WAITS:
                    raise SpotifyRateLimitError(
                        f"Spotify rate limit reached (Retry-After: {retry_after:.0f}s). "
                        "Nothing was lost — re-run the command later and it will continue "
                        "from the current state."
                    ) from e
                if i == max_retries - 1:
                    # No retry iteration left; stop cleanly instead of returning None
                    raise SpotifyRateLimitError(
                        "Spotify rate limit reached repeatedly; stopping. Nothing was lost — "
                        "re-run the command later."
                    ) from e
                print(f"\n⏳ Spotify rate limit reached; waiting {retry_after:.0f}s before retrying "
                      f"({rate_waits}/{SPOTIFY_MAX_RATE_LIMIT_WAITS})...")
                time.sleep(retry_after)
                continue
            except (ConnectionError, ProtocolError) as e:
                if i == max_retries - 1:
                    raise
                print(f"\nConnection error: {e}. Retrying in {SPOTIFY_RETRY_DELAY} seconds... (Attempt {i+1}/{max_retries})")
                time.sleep(SPOTIFY_RETRY_DELAY)
                self._init_sp()
        return None

    def _handle_expired_token(self):
        """Handle expired refresh token: delete stale cache and re-authenticate user."""
        print("\n" + "=" * 60)
        print("⚠️  YOUR SPOTIFY SESSION HAS EXPIRED")
        print("=" * 60)
        print("\nSpotify refresh tokens expire after 6 months.")
        print("You need to authorize tawspom again.\n")

        # Delete the stale cache file so spotipy doesn't retry the dead token
        if os.path.exists(self._cache_path):
            os.remove(self._cache_path)
            print(f"Removed expired token cache: {self._cache_path}\n")

        # Build and display the authorization URL
        redirect_uri = os.getenv("SPOTIPY_REDIRECT_URI", "")
        client_id = os.getenv("SPOTIPY_CLIENT_ID", "")
        auth_url = (
            f"https://accounts.spotify.com/authorize"
            f"?client_id={urllib_parse_quote(client_id)}"
            f"&response_type=code"
            f"&redirect_uri={urllib_parse_quote(redirect_uri)}"
            f"&scope={urllib_parse_quote(self.scope, safe=' ')}"
        )

        print(f"1. Open this URL in your browser:\n\n   {auth_url}\n")
        print("2. Log in to Spotify and authorize tawspom")
        print("3. Paste the redirect URL below:\n")

        # Get the redirect URL from user
        redirect_url = input("> ").strip()

        # Extract the authorization code from the redirect URL
        parsed = urlparse(redirect_url)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]

        if not code:
            # Error redirect? Check for error param
            error = params.get("error", [None])[0]
            if error:
                print(f"\nAuthorization denied: {error}")
            else:
                print("\n❌ Could not extract authorization code from the URL.")
                print("Make sure you pasted the full redirect URL.")
            raise SpotifyOauthError("Authorization failed")

        # Exchange the authorization code for a new token (spotipy caches it automatically)
        self._init_sp()
        assert self.sp.auth_manager is not None  # always set by _init_sp
        self.sp.auth_manager.get_access_token(code)
        print("\n✅ Authorization successful!\n")

        # Re-initialize so the cached token is loaded properly
        self._init_sp()

    def get_storage_playlists(self) -> list[Playlist]:
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

    def search_artist(self, name: str) -> list[dict]:
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

    def get_artist_albums(self, artist_id: str, types: list[str] | None = None) -> list[dict]:
        """Fetches albums/singles, including release date."""
        if types is None:
            types = ["album"]
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

    def get_album_tracks(self, album_id: str, album_name: str = "") -> list[Track]:
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

    def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
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

    def get_liked_songs(self) -> list[Track]:
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

    def remove_liked_songs(self, track_ids: list[str]):
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

    def get_playlist_name(self, pl_id: str) -> str | None:
        """Fetches a playlist's name. Returns None if the playlist is unavailable."""
        try:
            pl = self._call_with_retry(self.sp.playlist, pl_id, fields="id,name")
            return pl.get("name") if pl else None
        except (SpotifyException, ConnectionError, ProtocolError):
            return None

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

    def remove_tracks_from_playlist(self, playlist_id: str, track_ids: list[str]):
        """Removes a list of tracks from a playlist."""
        if not track_ids:
            return
        for i in range(0, len(track_ids), 100):
            self._call_with_retry(self.sp.playlist_remove_all_occurrences_of_items, playlist_id, track_ids[i:i+100])

    def add_tracks_to_playlist(self, playlist_id: str, track_ids: list[str]):
        """Adds a list of tracks to a playlist."""
        if not track_ids:
            return
        for i in range(0, len(track_ids), 100):
            self._call_with_retry(self.sp.playlist_add_items, playlist_id, track_ids[i:i+100])

    def add_tracks_to_playlist_unique(self, playlist_id: str, track_ids: list[str]) -> tuple[list[str], list[str]]:
        """Adds tracks to a playlist, skipping any that are already present.
        Central guard against Spotify-side duplicates on all add paths.
        Returns (added_ids, skipped_ids) so callers can report avoided duplicates."""
        if not track_ids:
            return [], []
        existing_ids = set(self.get_playlist_tracks_ordered(playlist_id))
        to_add = [tid for tid in track_ids if tid not in existing_ids]
        skipped = [tid for tid in track_ids if tid in existing_ids]
        if to_add:
            self.add_tracks_to_playlist(playlist_id, to_add)
        return to_add, skipped

    def get_playlist_tracks_ordered(self, playlist_id: str) -> list[str]:
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

    def clear_playlist(self, playlist_id: str):
        """Removes all items from a playlist (replaces its contents with an empty list)."""
        self._call_with_retry(self.sp.playlist_replace_items, playlist_id, [])

    def search_artists_by_genre(self, genre: str, limit: int = 50) -> list[dict]:
        """Discovers artists by searching for a specific genre."""
        query = f"genre:\"{genre}\""
        res = self._call_with_retry(self.sp.search, q=query, type="artist", limit=limit)
        return res.get("artists", {}).get("items", []) if res else []

    def get_artist_top_tracks(self, artist_id: str, country: str = 'US') -> list[dict]:
        """Fetches top tracks for an artist."""
        res = self._call_with_retry(self.sp.artist_top_tracks, artist_id, country=country)
        return res.get("tracks", []) if res else []
