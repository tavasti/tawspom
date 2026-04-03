import sqlite3
import os
from typing import List, Optional, Tuple
from datetime import datetime, timedelta
from tawspom.models import Track, Transaction

DB_PATH = os.path.expanduser("~/.local/share/tawspom/tawspom.sqlite3")

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

    # Transaction table with cancels_id
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL, -- 'ADD', 'DELETE', or 'INGEST'
            timestamp TEXT NOT NULL,
            track_count INTEGER NOT NULL,
            description TEXT,
            is_cancelled INTEGER DEFAULT 0,
            cancels_id INTEGER,
            FOREIGN KEY (cancels_id) REFERENCES transactions(id)
        )
    """)

    # Track table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS track (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            artist TEXT NOT NULL,
            album TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER NOT NULL,
            storage_playlist_id TEXT NOT NULL,
            last_played_at TEXT,
            added_at TEXT,
            deleted_at TEXT,
            is_deleted INTEGER DEFAULT 0,
            add_transaction_id INTEGER,
            delete_transaction_id INTEGER,
            FOREIGN KEY (add_transaction_id) REFERENCES transactions(id),
            FOREIGN KEY (delete_transaction_id) REFERENCES transactions(id)
        )
    """)

    # Active tracks table (to detect manual removals)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS active_tracks (
            track_id TEXT PRIMARY KEY,
            added_at TEXT NOT NULL,
            FOREIGN KEY (track_id) REFERENCES track(id)
        )
    """)

    # Migration check for existing installations
    cur.execute("PRAGMA table_info(track)")
    cols = [col[1] for col in cur.fetchall()]
    if 'album' not in cols:
        cur.execute("ALTER TABLE track ADD COLUMN album TEXT NOT NULL DEFAULT ''")
    if 'added_at' not in cols:
        cur.execute("ALTER TABLE track ADD COLUMN added_at TEXT")
        cur.execute("ALTER TABLE track ADD COLUMN deleted_at TEXT")
        cur.execute("ALTER TABLE track ADD COLUMN is_deleted INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE track ADD COLUMN add_transaction_id INTEGER")
        cur.execute("ALTER TABLE track ADD COLUMN delete_transaction_id INTEGER")

    cur.execute("PRAGMA table_info(transactions)")
    t_cols = [col[1] for col in cur.fetchall()]
    if 'is_cancelled' not in t_cols:
        cur.execute("ALTER TABLE transactions ADD COLUMN is_cancelled INTEGER DEFAULT 0")
    if 'cancels_id' not in t_cols:
        cur.execute("ALTER TABLE transactions ADD COLUMN cancels_id INTEGER")

    conn.commit()
    return conn

def create_transaction(conn, trans_type: str, count: int, description: str, cancels_id: Optional[int] = None) -> int:
    cur = conn.cursor()
    now = datetime.now().isoformat()
    cur.execute("""
        INSERT INTO transactions (type, timestamp, track_count, description, is_cancelled, cancels_id)
        VALUES (?, ?, ?, ?, 0, ?)
    """, (trans_type, now, count, description, cancels_id))
    conn.commit()
    return cur.lastrowid

def set_transaction_cancelled_status(conn, trans_id: int, cancelled: bool):
    cur = conn.cursor()
    cur.execute("UPDATE transactions SET is_cancelled = ? WHERE id = ?", (1 if cancelled else 0, trans_id))
    conn.commit()

def upsert_track(conn, track: Track):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO track (id, name, artist, album, duration_ms, storage_playlist_id, last_played_at, 
                          added_at, is_deleted, add_transaction_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,
            artist=excluded.artist,
            album=excluded.album,
            duration_ms=excluded.duration_ms,
            storage_playlist_id=excluded.storage_playlist_id,
            added_at=COALESCE(excluded.added_at, track.added_at),
            is_deleted=0,
            deleted_at=NULL,
            add_transaction_id=COALESCE(excluded.add_transaction_id, track.add_transaction_id)
    """, (track.id, track.name, track.artist, track.album, track.duration_ms, track.storage_playlist_id, 
          track.last_played_at.isoformat() if track.last_played_at else None,
          track.added_at.isoformat() if track.added_at else None,
          0, track.add_transaction_id))
    conn.commit()

def mark_tracks_deleted(conn, track_ids: List[str], transaction_id: int):
    cur = conn.cursor()
    now = datetime.now().isoformat()
    for tid in track_ids:
        cur.execute("""
            UPDATE track SET 
                is_deleted = 1, 
                deleted_at = ?, 
                delete_transaction_id = ? 
            WHERE id = ?
        """, (now, transaction_id, tid))
    conn.commit()

def unmark_tracks_deleted(conn, track_ids: List[str]):
    cur = conn.cursor()
    for tid in track_ids:
        cur.execute("""
            UPDATE track SET 
                is_deleted = 0, 
                deleted_at = NULL, 
                delete_transaction_id = NULL 
            WHERE id = ?
        """, (tid,))
    conn.commit()

def get_transaction(conn, trans_id: int) -> Optional[Transaction]:
    cur = conn.cursor()
    cur.execute("SELECT id, type, timestamp, track_count, description, is_cancelled, cancels_id FROM transactions WHERE id = ?", (trans_id,))
    row = cur.fetchone()
    if row:
        return Transaction(row[0], row[1], datetime.fromisoformat(row[2]), row[3], row[4], bool(row[5]), row[6])
    return None

def list_transactions(conn, trans_type: str) -> List[Transaction]:
    cur = conn.cursor()
    cur.execute("""
        SELECT id, type, timestamp, track_count, description, is_cancelled, cancels_id 
        FROM transactions 
        WHERE type = ? 
        ORDER BY timestamp DESC
    """, (trans_type,))
    return [Transaction(row[0], row[1], datetime.fromisoformat(row[2]), row[3], row[4], bool(row[5]), row[6]) for row in cur.fetchall()]

def get_tracks_by_transaction(conn, trans_id: int, trans_type: str) -> List[Track]:
    cur = conn.cursor()
    col = "add_transaction_id" if trans_type in ["ADD", "INGEST"] else "delete_transaction_id"
    cur.execute(f"""
        SELECT id, name, artist, album, duration_ms, storage_playlist_id, last_played_at, added_at, deleted_at, is_deleted, add_transaction_id, delete_transaction_id
        FROM track 
        WHERE {col} = ?
    """, (trans_id,))
    
    tracks = []
    for row in cur.fetchall():
        tracks.append(Track(
            id=row[0], name=row[1], artist=row[2], album=row[3], duration_ms=row[4], storage_playlist_id=row[5],
            last_played_at=datetime.fromisoformat(row[6]) if row[6] else None,
            added_at=datetime.fromisoformat(row[7]) if row[7] else None,
            deleted_at=datetime.fromisoformat(row[8]) if row[8] else None,
            is_deleted=bool(row[9]),
            add_transaction_id=row[10],
            delete_transaction_id=row[11]
        ))
    return tracks

def get_tracks_by_ids(conn, track_ids: List[str]) -> List[Track]:
    if not track_ids: return []
    cur = conn.cursor()
    placeholders = ','.join(['?'] * len(track_ids))
    cur.execute(f"""
        SELECT id, name, artist, album, duration_ms, storage_playlist_id, last_played_at, added_at, deleted_at, is_deleted, add_transaction_id, delete_transaction_id
        FROM track WHERE id IN ({placeholders})
    """, track_ids)
    
    tracks = []
    for row in cur.fetchall():
        tracks.append(Track(
            id=row[0], name=row[1], artist=row[2], album=row[3], duration_ms=row[4], storage_playlist_id=row[5],
            last_played_at=datetime.fromisoformat(row[6]) if row[6] else None,
            added_at=datetime.fromisoformat(row[7]) if row[7] else None,
            deleted_at=datetime.fromisoformat(row[8]) if row[8] else None,
            is_deleted=bool(row[9]),
            add_transaction_id=row[10],
            delete_transaction_id=row[11]
        ))
    return tracks

def get_all_active_track_ids(conn) -> List[str]:
    cur = conn.cursor()
    cur.execute("SELECT id FROM track WHERE is_deleted = 0")
    return [row[0] for row in cur.fetchall()]

def mark_as_played(conn, track_id: str, played_at: datetime):
    cur = conn.cursor()
    cur.execute("UPDATE track SET last_played_at = ? WHERE id = ?", 
                (played_at.isoformat(), track_id))
    conn.commit()

def get_least_recently_played_tracks(conn, limit: int = 1000) -> List[Track]:
    """Returns a randomized pool of the oldest tracks."""
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, artist, album, duration_ms, storage_playlist_id, last_played_at 
        FROM track 
        WHERE is_deleted = 0
        ORDER BY last_played_at ASC NULLS FIRST, RANDOM()
        LIMIT ?
    """, (limit,))
    
    tracks = []
    for row in cur.fetchall():
        last_played = datetime.fromisoformat(row[6]) if row[6] else None
        tracks.append(Track(row[0], row[1], row[2], row[3], row[4], row[5], last_played))
        
    return tracks

def delete_tracks_permanently(conn, older_than_days: int):
    cur = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat()
    cur.execute("DELETE FROM track WHERE is_deleted = 1 AND deleted_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount

def add_active_tracks(conn, track_ids: List[str]):
    cur = conn.cursor()
    now = datetime.now().isoformat()
    for tid in track_ids:
        cur.execute("INSERT OR IGNORE INTO active_tracks (track_id, added_at) VALUES (?, ?)", (tid, now))
    conn.commit()

def remove_active_tracks(conn, track_ids: List[str]):
    cur = conn.cursor()
    for tid in track_ids:
        cur.execute("DELETE FROM active_tracks WHERE track_id = ?", (tid,))
    conn.commit()

def get_tracked_active_ids(conn) -> List[str]:
    cur = conn.cursor()
    cur.execute("SELECT track_id FROM active_tracks")
    return [row[0] for row in cur.fetchall()]
