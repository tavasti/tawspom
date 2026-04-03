from spotipy import Spotify
from tawspom.db import insert_playlist, insert_playlist_tracks, get_known_playlist_ids, update_my_music, get_my_music_tracks
import difflib

def fetch_and_store_playlists(sp: Spotify, conn, update=False):
    results = sp.current_user_playlists()
    known_ids = get_known_playlist_ids(conn)

    for item in results['items']:
        name = item['name']
        playlist_id = item['id']
        if len(name) == 1:
            if playlist_id not in known_ids:
                print(f"Inserting playlist {name} ({playlist_id})")
                insert_playlist(conn, playlist_id, name)
                tracks = fetch_all_tracks(sp, playlist_id)
                insert_playlist_tracks(conn, playlist_id, tracks)
            elif update:
                # If update flag given, confirm name matches
                try:
                    insert_playlist(conn, playlist_id, name)
                except ValueError as e:
                    print(f"WARNING: {e}")

def fetch_all_tracks(sp: Spotify, playlist_id: str) -> list:
    results = sp.playlist_items(playlist_id, limit=100)
    tracks = results['items']
    while results['next']:
        results = sp.next(results)
        tracks.extend(results['items'])
    return [item['track'] for item in tracks if item['track']]

def fetch_and_compare_my_music(sp: Spotify, conn):
    results = sp.current_user_saved_tracks(limit=50)
    tracks = results['items']
    while results['next']:
        results = sp.next(results)
        tracks.extend(results['items'])
    new_tracks = [item['track'] for item in tracks]

    old_ids = set(get_my_music_tracks(conn))
    new_ids = set(track['id'] for track in new_tracks)

    added = new_ids - old_ids
    removed = old_ids - new_ids

    if added:
        print("ADDED tracks to My Music:")
        for track in new_tracks:
            if track['id'] in added:
                print(f"  + {track['name']} - {', '.join(a['name'] for a in track['artists'])}")
    if removed:
        print("REMOVED tracks from My Music:")
        for track_id in removed:
            print(f"  - {track_id}")

    update_my_music(conn, new_tracks)

def show_current_playback(sp: Spotify):
    current = sp.current_playback()
    if current and current.get("item"):
        track = current["item"]
        print(f"Currently playing: {track['name']} by {', '.join(a['name'] for a in track['artists'])}")
    else:
        print("No track currently playing.")

