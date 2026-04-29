import os

# --- Library Architecture ---
# Default letter for numbers/symbols in storage
DEFAULT_STORAGE_LETTER = "A"

# --- Refill Logic ---
# Default target duration for the active playlist (hours)
DEFAULT_REFILL_HOURS = 15.0
# Number of tracks to lock an artist after they appear in refill
REFILL_ARTIST_LOCK_WINDOW = 5
# Size of the pool to fetch from DB for refill selection (Oldest first)
REFILL_CANDIDATE_POOL_SIZE = 2000
# Window for calculating momentum statistics (days)
MOMENTUM_WINDOW_DAYS = 7
# Pool size for identifying "Top Waiting Artists" in dashboard
DASHBOARD_OLD_POOL_SIZE = 20

# --- Phone History ---
# Capacity limit for the Phone Listening history playlist (hours)
PHONE_PLAYLIST_CAPACITY_HOURS = 100

# --- Deduplication ---
# Threshold for binary duplicate detection (milliseconds)
BINARY_DEDUPE_THRESHOLD_MS = 2000
# Suffixes to strip for Root Name extraction
ROOT_NAME_FORBIDDEN_KEYWORDS = r'remaster|edit|remix|mix|radio|live|feat|acoustic|version|original|mono|stereo|bonus|20\d\d|single|video'
# Maximum number of song groups to include in a single version review session
VERSION_REVIEW_GROUP_LIMIT = 20

# --- Discovery (findnew) ---
# Minimum number of tracks listened to qualify an artist for discovery
FINDNEW_QUALIFY_MIN_TRACKS = 8
# Cooldown period between checking the same artist for new albums (days)
FINDNEW_ARTIST_COOLDOWN_DAYS = 180
# Similarity threshold for flagging existing albums in findnew (0.0 to 1.0)
FINDNEW_ALBUM_SIMILARITY_THRESHOLD = 0.8

# --- Artist Radio ---
# Default number of tracks from the main artist
RADIO_DEFAULT_MAIN_COUNT = 10
# Default tracks per related artist
RADIO_DEFAULT_PER_ARTIST = 5

# --- Spotify API Constraints ---
# Maximum items per playlist edit (Spotify limit: 100)
SPOTIFY_PLAYLIST_BATCH_SIZE = 100
# Maximum items per 'Liked Songs' removal (Avoids 400 errors)
SPOTIFY_LIKED_SONGS_BATCH_SIZE = 20
# Maximum artists per profile batch fetch (Spotify limit: 50)
SPOTIFY_ARTIST_PROFILE_BATCH_SIZE = 50
# API Request Timeout (seconds)
SPOTIFY_API_TIMEOUT = 10
# Connection retry attempts
SPOTIFY_MAX_RETRIES = 3
# Delay between retries (seconds)
SPOTIFY_RETRY_DELAY = 2

# Keywords to skip when fetching artist albums (unless specifically requested)
ALBUM_SKIP_KEYWORDS = ["live", "deluxe", "expanded", "bonus", "edition", "super"]
# Search result limit for artists
ARTIST_SEARCH_LIMIT = 30
