import os
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()

def get_spotify_client(scope=None) -> spotipy.Spotify:
    return spotipy.Spotify(auth_manager=SpotifyOAuth(
        scope=scope or "user-library-read user-library-modify user-read-playback-state",
        client_id=os.getenv("SPOTIPY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIPY_CLIENT_SECRET"),
        redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI"),
        cache_path=os.path.expanduser("~/.tawspom/spotify_token.json"),
        open_browser=True
    ))

