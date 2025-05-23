import sqlite3
from pathlib import Path

DB_PATH = Path.home() / ".tawspom" / "tawspom.db"

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                id TEXT PRIMARY KEY,
                name TEXT,
                artist TEXT,
                added_at TEXT,
                last_played TEXT,
                duplicate_checked INTEGER DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id TEXT,
                action_type TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

def get_connection():
    return sqlite3.connect(DB_PATH)

