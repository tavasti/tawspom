import argparse
import sys
import os
from tawspom.core.db import init_db
from tawspom.core.spotify import SpotifyClient
from tawspom.core.manager import Manager
from tawspom.core.constants import (
    DEFAULT_REFILL_HOURS, RADIO_DEFAULT_MAIN_COUNT, RADIO_DEFAULT_PER_ARTIST,
    FINDNEW_QUALIFY_MIN_TRACKS
)

# Configurable playlist names with defaults
ACTIVE_PLAYLIST_NAME = os.getenv("ACTIVE_PLAYLIST_NAME", "My Active Music")
PHONE_PLAYLIST_NAME = os.getenv("PHONE_PLAYLIST_NAME", "Phone Listening")

def main():
    parser = argparse.ArgumentParser(description="Tawspom: Spotify Playlist Manager")
    parser.add_argument("--user", default="default", help="User profile/label (e.g., 'test' or 'main')")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Basic Commands
    subparsers.add_parser("sync", help="Sync storage playlists (A-Z) with DB and detect manual removals")
    subparsers.add_parser("initdb", help="Initialize database from A-Z playlists")
    subparsers.add_parser("whoami", help="Check currently authenticated user")

    # Ingestion
    subparsers.add_parser("ingest", help="Move all 'Liked Songs' to A-Z storage playlists")

    # Maintenance
    subparsers.add_parser("deduplicate", help="Find and remove binary duplicates from storage")
    subparsers.add_parser("findversions", help="Find potential versions of songs and put in 'Duplicate Review' playlist")
    subparsers.add_parser("processversions", help="Process the 'Duplicate Review' playlist and delete removed tracks")
    subparsers.add_parser("findnew", help=f"Find new albums from artists you have listened to (>= {FINDNEW_QUALIFY_MIN_TRACKS} songs)")
    subparsers.add_parser("fixplaylists", help="Interactively resolve and clean up duplicate playlists")

    # Radio
    radio_parser = subparsers.add_parser("artistradio", help="Create a custom artist radio playlist")
    radio_parser.add_argument("name", help="Name of the artist")
    radio_parser.add_argument("--main-count", type=int, default=RADIO_DEFAULT_MAIN_COUNT, help=f"Number of songs from the main artist (default: {RADIO_DEFAULT_MAIN_COUNT})")
    radio_parser.add_argument("--per-artist", type=int, default=RADIO_DEFAULT_PER_ARTIST, help=f"Number of songs to take from EACH discovered related artist (default: {RADIO_DEFAULT_PER_ARTIST})")
    radio_parser.add_argument("--filter", choices=["big", "small", "both"], default="both", 
                              help="Filter related artists by size (big = top 50%, small = bottom 50%, both = all)")

    # Add Artist
    add_artist_parser = subparsers.add_parser("addartist", help="Add studio tracks of an artist to storage")
    add_artist_parser.add_argument("name", help="Name of the artist")
    
    artist_type_group = add_artist_parser.add_mutually_exclusive_group()
    artist_type_group.add_argument("--album", action="store_true", default=True, help="Fetch albums only (default)")
    artist_type_group.add_argument("--single", action="store_true", help="Fetch singles only")
    artist_type_group.add_argument("--all", action="store_true", help="Fetch both albums and singles")

    # Transaction Management
    subparsers.add_parser("listadd", help="List all addition (ADD/INGEST) transactions")
    subparsers.add_parser("listdelete", help="List all deletion transactions")

    showadd_parser = subparsers.add_parser("showadd", help="Show tracks added/ingested in a transaction")
    showadd_parser.add_argument("id", type=int, help="Transaction ID")

    showdelete_parser = subparsers.add_parser("showdelete", help="Show tracks deleted in a transaction")
    showdelete_parser.add_argument("id", type=int, help="Transaction ID")

    canceladd_parser = subparsers.add_parser("canceladd", help="Undo an addition/ingest transaction")
    canceladd_parser.add_argument("id", type=int, help="Transaction ID")

    canceldelete_parser = subparsers.add_parser("canceldelete", help="Undo a deletion transaction")
    canceldelete_parser.add_argument("id", type=int, help="Transaction ID")

    # Cleanup
    clean_parser = subparsers.add_parser("cleandatabase", help="Permanently remove old deleted tracks")
    clean_parser.add_argument("older_than", help="Age threshold (e.g., '3m', '1y')")

    # Refill active playlist
    refill_parser = subparsers.add_parser("refill", help="Refill the active playlist")
    refill_parser.add_argument("--hours", type=float, default=DEFAULT_REFILL_HOURS, help=f"Target duration in hours (default: {DEFAULT_REFILL_HOURS})")
    
    refill_mode = refill_parser.add_mutually_exclusive_group()
    refill_mode.add_argument("--flush", action="store_true", help="Flush current playlist and refill from scratch")
    refill_mode.add_argument("--add", action="store_true", help="Just add more tracks without removing listened ones")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Initialize components
    conn = init_db()
    sp_client = SpotifyClient(user_label=args.user, db_conn=conn)
    manager = Manager(sp_client, conn)

    if args.command == "sync":
        manager.sync_storage()
    elif args.command == "initdb":
        manager.init_db()
    elif args.command == "whoami":
        manager.whoami()
    elif args.command == "ingest":
        manager.ingest_liked_songs()
    elif args.command == "deduplicate":
        manager.deduplicate_storage()
    elif args.command == "findversions":
        manager.find_version_duplicates()
    elif args.command == "processversions":
        manager.process_version_review()
    elif args.command == "findnew":
        manager.find_new_music()
    elif args.command == "fixplaylists":
        manager.fix_duplicate_playlists([ACTIVE_PLAYLIST_NAME, PHONE_PLAYLIST_NAME])
    elif args.command == "artistradio":
        manager.create_artist_radio(args.name, args.main_count, args.filter, args.per_artist)
    elif args.command == "addartist":
        types = ["album"]
        if args.single:
            types = ["single"]
        elif args.all:
            types = ["album", "single"]
        manager.add_artist_to_storage(args.name, types=types)
    elif args.command == "listadd":
        manager.list_adds()
    elif args.command == "listdelete":
        manager.list_deletes()
    elif args.command == "showadd":
        manager.show_add(args.id)
    elif args.command == "showdelete":
        manager.show_delete(args.id)
    elif args.command == "canceladd":
        manager.cancel_add(args.id)
    elif args.command == "canceldelete":
        manager.cancel_delete(args.id)
    elif args.command == "cleandatabase":
        manager.clean_database(args.older_than)
    elif args.command == "refill":
        mode = "default"
        if args.flush:
            mode = "flush"
        elif args.add:
            mode = "add"
        manager.refill_active_playlist(ACTIVE_PLAYLIST_NAME, args.hours, mode=mode, phone_playlist_name=PHONE_PLAYLIST_NAME)

if __name__ == "__main__":
    main()
