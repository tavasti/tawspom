from spotipy import Spotify

def get_storage_playlists(sp: Spotify) -> list[dict]:
    playlists = []
    offset = 0
    limit = 50

    while True:
        response = sp.current_user_playlists(limit=limit, offset=offset)
        items = response.get("items", [])
        short_named = [p for p in items if len(p["name"]) == 1]
        playlists.extend(short_named)
        #playlists.extend(items)

        if len(items) < limit:
            break
        offset += limit

    return playlists

def get_playlist_tracks(sp: Spotify, playlist_id: str) -> list[dict]:
    tracks = []
    offset = 0
    limit = 100

    while True:
        result = sp.playlist_items(playlist_id, limit=limit, offset=offset)
        items = result.get("items", [])
        for item in items:
            if item["track"]:
                tracks.append(item["track"])

        if len(items) < limit:
            break
        offset += limit

    return tracks

