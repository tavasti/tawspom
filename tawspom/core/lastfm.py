import os
import requests
from typing import List, Dict
from dotenv import load_dotenv

load_dotenv()

class LastFMClient:
    def __init__(self):
        self.api_key = os.getenv("LASTFM_API_KEY")
        self.base_url = "http://ws.audioscrobbler.com/2.0/"

    def get_similar_artists(self, artist_name: str, limit: int = 50) -> List[str]:
        """Fetches a list of similar artist names from Last.fm."""
        params = {
            "method": "artist.getsimilar",
            "artist": artist_name,
            "api_key": self.api_key,
            "format": "json",
            "limit": limit
        }
        
        try:
            response = requests.get(self.base_url, params=params, timeout=10)
            if response.status_code != 200:
                print(f"Last.fm Error: {response.status_code}")
                return []
            
            data = response.json()
            artists_data = data.get("similarartists", {}).get("artist", [])
            
            # If only one artist is returned, it might not be a list
            if isinstance(artists_data, dict):
                artists_data = [artists_data]
                
            return [a["name"] for a in artists_data if "name" in a]
            
        except Exception as e:
            print(f"Last.fm API call failed: {e}")
            return []
