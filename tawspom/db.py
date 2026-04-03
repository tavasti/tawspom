import sqlite3
import os

DB_PATH = os.path.expanduser("~/.local/share/tawspom/tawspom.sqlite3")
DB_VERSION = 1

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute("SELECT value FROM meta WHERE key = 'version'")
    row = cur.fetchone()
    current_version = int(row[0]) if row else 0

    if current_version < 1:
        cur.execute("""
            CREATE TABLE track (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                artist TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE playlist_track (
                playlist_id TEXT,
                track_id TEXT,
                position INTEGER,
                PRIMARY KEY (playlist_id, track_id),
                FOREIGN KEY (track_id) REFERENCES track(id)
            )
        """)
        current_version = DB_VERSION

    if current_version < DB_VERSION:
        # Future migration step(s) here
        current_version = DB_VERSION

    cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('version', ?)", (str(DB_VERSION),))
    conn.commit()
    return conn

def insert_playlist_tracks(conn, playlist_id: str, tracks: list[dict]):
    cur = conn.cursor()
    for pos, track in enumerate(tracks):
        track_id = track["id"]
        name = track["name"]
        artist = ", ".join([a["name"] for a in track["artists"]])

        cur.execute("INSERT OR IGNORE INTO track (id, name, artist) VALUES (?, ?, ?)",
                    (track_id, name, artist))
        cur.execute("INSERT OR REPLACE INTO playlist_track (playlist_id, track_id, position) VALUES (?, ?, ?)",
                    (playlist_id, track_id, pos))
    conn.commit()

