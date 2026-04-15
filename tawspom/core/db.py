import sqlite3
import os
from typing import List, Optional, Tuple, Set, Dict
from datetime import datetime, timedelta
from tawspom.models import Track, Transaction

DB_PATH = os.getenv("DB_PATH", os.path.expanduser("~/.local/share/tawspom/tawspom.sqlite3"))

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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL, 
            timestamp TEXT NOT NULL,
            track_count INTEGER NOT NULL,
            description TEXT,
            is_cancelled INTEGER DEFAULT 0,
            cancels_id INTEGER,
            FOREIGN KEY (cancels_id) REFERENCES transactions(id)
        )
    """)

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
            play_count INTEGER DEFAULT 0,
            FOREIGN KEY (add_transaction_id) REFERENCES transactions(id),
            FOREIGN KEY (delete_transaction_id) REFERENCES transactions(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS active_tracks (
            track_id TEXT PRIMARY KEY,
            added_at TEXT NOT NULL,
            FOREIGN KEY (track_id) REFERENCES track(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS duplicate_allowlist (
            track_id_a TEXT,
            track_id_b TEXT,
            PRIMARY KEY (track_id_a, track_id_b)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS duplicate_review_session (
            track_id TEXT PRIMARY KEY,
            group_key TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS artist_checks (
            artist_name TEXT PRIMARY KEY,
            last_checked_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS handled_albums (
            artist_name TEXT,
            album_name TEXT,
            status TEXT, -- 'IGNORED'
            PRIMARY KEY (artist_name, album_name)
        )
    """)

    # Migrations
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
    if 'play_count' not in cols:
        cur.execute("ALTER TABLE track ADD COLUMN play_count INTEGER DEFAULT 0")
        cur.execute("UPDATE track SET play_count = 1 WHERE last_played_at IS NOT NULL")

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

def upsert_tracks(conn, tracks: List[Track]):
    """Bulk upsert tracks for speed."""
    if not tracks: return
    cur = conn.cursor()
    data = []
    for t in tracks:
        data.append((
            t.id, t.name, t.artist, t.album, t.duration_ms, t.storage_playlist_id,
            t.last_played_at.isoformat() if t.last_played_at else None,
            t.added_at.isoformat() if t.added_at else None,
            0, t.add_transaction_id
        ))
    
    cur.executemany("""
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
    """, data)
    conn.commit()

def upsert_track(conn, track: Track):
    upsert_tracks(conn, [track])

def mark_tracks_deleted(conn, track_ids: List[str], transaction_id: int):
    if not track_ids: return
    cur = conn.cursor()
    now = datetime.now().isoformat()
    cur.execute(f"UPDATE track SET is_deleted = 1, deleted_at = ?, delete_transaction_id = ? WHERE id IN ({','.join(['?']*len(track_ids))})", 
                [now, transaction_id] + track_ids)
    conn.commit()

def unmark_tracks_deleted(conn, track_ids: List[str]):
    if not track_ids: return
    cur = conn.cursor()
    cur.execute(f"UPDATE track SET is_deleted = 0, deleted_at = NULL, delete_transaction_id = NULL WHERE id IN ({','.join(['?']*len(track_ids))})", track_ids)
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
        FROM track WHERE {col} = ?
    """, (trans_id,))
    
    tracks = []
    for row in cur.fetchall():
        tracks.append(Track(id=row[0], name=row[1], artist=row[2], album=row[3], duration_ms=row[4], storage_playlist_id=row[5],
            last_played_at=datetime.fromisoformat(row[6]) if row[6] else None,
            added_at=datetime.fromisoformat(row[7]) if row[7] else None,
            deleted_at=datetime.fromisoformat(row[8]) if row[8] else None,
            is_deleted=bool(row[9]), add_transaction_id=row[10], delete_transaction_id=row[11]))
    return tracks

def get_tracks_by_ids(conn, track_ids: List[str]) -> List[Track]:
    if not track_ids: return []
    cur = conn.cursor()
    placeholders = ','.join(['?'] * len(track_ids))
    cur.execute(f"SELECT id, name, artist, album, duration_ms, storage_playlist_id, last_played_at, added_at, deleted_at, is_deleted, add_transaction_id, delete_transaction_id FROM track WHERE id IN ({placeholders})", track_ids)
    
    tracks = []
    for row in cur.fetchall():
        tracks.append(Track(id=row[0], name=row[1], artist=row[2], album=row[3], duration_ms=row[4], storage_playlist_id=row[5],
            last_played_at=datetime.fromisoformat(row[6]) if row[6] else None,
            added_at=datetime.fromisoformat(row[7]) if row[7] else None,
            deleted_at=datetime.fromisoformat(row[8]) if row[8] else None,
            is_deleted=bool(row[9]), add_transaction_id=row[10], delete_transaction_id=row[11]))
    return tracks

def get_all_active_tracks(conn) -> List[Track]:
    cur = conn.cursor()
    cur.execute("SELECT id, name, artist, album, duration_ms, storage_playlist_id, last_played_at, added_at, deleted_at, is_deleted, add_transaction_id, delete_transaction_id FROM track WHERE is_deleted = 0")
    tracks = []
    for row in cur.fetchall():
        tracks.append(Track(id=row[0], name=row[1], artist=row[2], album=row[3], duration_ms=row[4], storage_playlist_id=row[5],
            last_played_at=datetime.fromisoformat(row[6]) if row[6] else None,
            added_at=datetime.fromisoformat(row[7]) if row[7] else None,
            deleted_at=datetime.fromisoformat(row[8]) if row[8] else None,
            is_deleted=bool(row[9]), add_transaction_id=row[10], delete_transaction_id=row[11]))
    return tracks

def get_all_active_track_ids(conn) -> List[str]:
    cur = conn.cursor()
    cur.execute("SELECT id FROM track WHERE is_deleted = 0")
    return [row[0] for row in cur.fetchall()]

def mark_as_played(conn, track_id: str, played_at: datetime):
    cur = conn.cursor()
    cur.execute("""
        UPDATE track 
        SET last_played_at = ?, play_count = play_count + 1 
        WHERE id = ?
    """, (played_at.isoformat(), track_id))
    conn.commit()

def get_least_recently_played_tracks(conn, limit: int = 1000) -> List[Track]:
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, artist, album, duration_ms, storage_playlist_id, last_played_at, play_count
        FROM track WHERE is_deleted = 0
        ORDER BY last_played_at ASC NULLS FIRST, RANDOM()
        LIMIT ?
    """, (limit,))
    tracks = []
    for row in cur.fetchall():
        last_played = datetime.fromisoformat(row[6]) if row[6] else None
        t = Track(row[0], row[1], row[2], row[3], row[4], row[5], last_played)
        t.play_count = row[7]
        tracks.append(t)
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
    if not track_ids: return
    cur = conn.cursor()
    cur.execute(f"DELETE FROM active_tracks WHERE track_id IN ({','.join(['?']*len(track_ids))})", track_ids)
    conn.commit()

def get_tracked_active_ids(conn) -> List[str]:
    cur = conn.cursor()
    cur.execute("SELECT track_id FROM active_tracks")
    return [row[0] for row in cur.fetchall()]

def get_playlist_track_count(conn, playlist_id: str) -> int:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM track WHERE storage_playlist_id = ? AND is_deleted = 0", (playlist_id,))
    return cur.fetchone()[0]

def add_to_duplicate_allowlist(conn, track_ids: List[str]):
    """Allow all pairs in the list to coexist."""
    cur = conn.cursor()
    for i in range(len(track_ids)):
        for j in range(i + 1, len(track_ids)):
            id_a, id_b = sorted([track_ids[i], track_ids[j]])
            cur.execute("INSERT OR IGNORE INTO duplicate_allowlist (track_id_a, track_id_b) VALUES (?, ?)", (id_a, id_b))
    conn.commit()

def is_duplicate_allowed(conn, id_a: str, id_b: str) -> bool:
    cur = conn.cursor()
    id_a, id_b = sorted([id_a, id_b])
    cur.execute("SELECT 1 FROM duplicate_allowlist WHERE track_id_a = ? AND track_id_b = ?", (id_a, id_b))
    return cur.fetchone() is not None

def save_review_session(conn, session_data: List[Tuple[str, str]]):
    """Saves (track_id, group_key) pairs."""
    cur = conn.cursor()
    cur.execute("DELETE FROM duplicate_review_session")
    cur.executemany("INSERT INTO duplicate_review_session (track_id, group_key) VALUES (?, ?)", session_data)
    conn.commit()

def get_review_session(conn) -> Dict[str, List[str]]:
    """Returns group_key -> [track_ids]."""
    cur = conn.cursor()
    cur.execute("SELECT track_id, group_key FROM duplicate_review_session")
    groups = {}
    for tid, key in cur.fetchall():
        if key not in groups:
            groups[key] = []
        groups[key].append(tid)
    return groups

def clear_review_session(conn):
    cur = conn.cursor()
    cur.execute("DELETE FROM duplicate_review_session")
    conn.commit()

def get_library_stats(conn) -> Dict:
    cur = conn.cursor()
    
    # Basic counts
    cur.execute("SELECT COUNT(*), SUM(duration_ms) FROM track WHERE is_deleted = 0")
    total_active, total_duration_ms = cur.fetchone()
    total_duration_ms = total_duration_ms or 0
    
    cur.execute("SELECT COUNT(*) FROM track WHERE is_deleted = 1")
    total_deleted = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM track WHERE last_played_at IS NULL AND is_deleted = 0")
    never_played = cur.fetchone()[0]
    
    # Play statistics (Active only)
    cur.execute("SELECT SUM(play_count), AVG(play_count), SUM(play_count * duration_ms) FROM track WHERE is_deleted = 0")
    total_plays, avg_plays, total_history_ms = cur.fetchone()
    total_plays = total_plays or 0
    avg_plays = avg_plays or 0
    total_history_ms = total_history_ms or 0
    
    # Full history (Including deleted tracks)
    cur.execute("SELECT SUM(play_count * duration_ms) FROM track")
    full_listening_time_ms = cur.fetchone()[0] or 0
    
    # Momentum (Last 7 days)
    seven_days_ago = (datetime.now() - timedelta(days=7)).isoformat()
    cur.execute("SELECT SUM(duration_ms) FROM track WHERE last_played_at > ? AND is_deleted = 0", (seven_days_ago,))
    momentum_ms = cur.fetchone()[0] or 0
    
    # Queue Lag
    cur.execute("SELECT MIN(last_played_at) FROM track WHERE last_played_at IS NOT NULL AND is_deleted = 0")
    oldest_waiting_at = cur.fetchone()[0]
    
    # Top Waiting Artists (from oldest 1000)
    cur.execute("""
        SELECT artist, COUNT(*) as c FROM (
            SELECT artist FROM track WHERE is_deleted = 0 ORDER BY last_played_at ASC NULLS FIRST LIMIT 1000
        ) GROUP BY artist ORDER BY c DESC LIMIT 3
    """)
    top_waiting_artists = cur.fetchall()
    
    return {
        "total_active": total_active,
        "total_duration_ms": total_duration_ms,
        "total_deleted": total_deleted,
        "never_played": never_played,
        "coverage_pct": (1 - never_played / total_active) * 100 if total_active > 0 else 0,
        "total_plays": total_plays,
        "avg_plays": avg_plays,
        "total_history_ms": total_history_ms,
        "full_listening_time_ms": full_listening_time_ms,
        "momentum_ms": momentum_ms,
        "oldest_waiting_at": datetime.fromisoformat(oldest_waiting_at) if oldest_waiting_at else None,
        "top_waiting_artists": top_waiting_artists
    }

def get_artist_last_check(conn, artist_name: str) -> Optional[datetime]:
    cur = conn.cursor()
    cur.execute("SELECT last_checked_at FROM artist_checks WHERE artist_name = ?", (artist_name,))
    row = cur.fetchone()
    return datetime.fromisoformat(row[0]) if row else None

def set_artist_checked(conn, artist_name: str):
    cur = conn.cursor()
    now = datetime.now().isoformat()
    cur.execute("INSERT OR REPLACE INTO artist_checks (artist_name, last_checked_at) VALUES (?, ?)", (artist_name, now))
    conn.commit()

def get_handled_albums(conn, artist_name: str) -> Set[str]:
    """Returns set of album names already handled (in DB or IGNORED)."""
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT album FROM track WHERE artist = ?", (artist_name,))
    albums = {row[0].lower() for row in cur.fetchall()}
    cur.execute("SELECT album_name FROM handled_albums WHERE artist_name = ? AND status = 'IGNORED'", (artist_name,))
    for row in cur.fetchall():
        albums.add(row[0].lower())
    return albums

def add_to_ignored_albums(conn, artist_name: str, album_name: str):
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO handled_albums (artist_name, album_name, status) VALUES (?, ?, 'IGNORED')", (artist_name, album_name))
    conn.commit()
