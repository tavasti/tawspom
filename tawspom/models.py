from dataclasses import dataclass
from typing import Optional
from datetime import datetime

@dataclass
class Track:
    id: str
    name: str
    artist: str
    album: str
    duration_ms: int
    storage_playlist_id: str
    last_played_at: Optional[datetime] = None
    added_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    is_deleted: bool = False
    add_transaction_id: Optional[int] = None
    delete_transaction_id: Optional[int] = None

@dataclass
class Transaction:
    id: int
    type: str # 'ADD' or 'DELETE'
    timestamp: datetime
    track_count: int
    description: str
    is_cancelled: bool = False
    cancels_id: Optional[int] = None

@dataclass
class Playlist:
    id: str
    name: str
    is_active: bool = False
