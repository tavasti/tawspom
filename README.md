# Tawspom: The Advanced Spotify Playlist Manager

**Tawspom** is a professional-grade music library management tool designed for Spotify power users who maintain massive collections (20k+ tracks). It automates the organization of your library into A-Z storage, maintains a perfectly diverse rolling listening experience, and provides high-precision discovery tools that bypass standard API limitations.

---

## ⚠️ CRITICAL WARNING
**Tawspom uses your "Liked Songs" (My Music) as an INBOX.**
When you run the `sync` or `ingest` commands, **all songs in your "Liked Songs" will be MOVED** to permanent A-Z storage playlists and **REMOVED** from your "Liked Songs" list. This is by design to keep your library organized and searchable. 

---

## 🚀 Key Features

### 1. Automated A-Z Organization
Tawspom organizes your permanent collection into public playlists named **A through Z**. It handles character normalization (e.g., mapping `Ä` to `A`) and ensures your library is structured and easy to navigate even with tens of thousands of tracks.

### 2. The 12-Hour "Active" Window
Tawspom maintains a configurable Active playlist (default: **"My Active Music"**) which serves as your primary listening source.
- **Fair Round-Robin**: Ensures maximum variety by rotating through lead artists.
- **Artist Locking**: Collaborators on a track are "locked" for the round to prevent artist clusters.
- **Least Recently Played**: Prioritizes music you haven't heard in a long time.
- **Manual Removal Sync**: If you delete a track from your "Active" list, Tawspom interprets this as "I don't like this anymore" and removes it from your permanent A-Z storage as well.

### 3. Phone History
On every refill, your listened tracks are appended to a secondary history playlist (default: **"Phone Listening"**). This continues until the playlist reaches 100 hours in duration, giving you a massive rolling history on your device.

### 4. High-Precision Discovery
- **Robot Discovery (`artistradio`)**: Uses a headless browser robot to scrape "Fans Also Like" data directly from the Spotify Web Player, providing far more accurate related artists than the restricted official API.
- **Deep Catalog Search (`findnew`)**: Identifies artists you listen to heavily (15+ plays) and interactively suggests new albums they've released that aren't yet in your collection.

### 5. Advanced Deduplication
- **Binary Dedupe**: Automatically removes exact duplicates (same artist/name/duration).
- **Semantic Dedupe (`findversions`)**: Uses regex and fuzzy matching to identify different versions of the same song (Remasters, Live, Edits) and lets you review them via a temporary "Duplicate Review" playlist.

---

## 🛠 Setup

### 1. Prerequisites
- Python 3.10+
- A Spotify Developer account (create an app at [developer.spotify.com](https://developer.spotify.com))
- (Optional) A Last.fm API Key

### 2. Environment Configuration
Create a `.env` file in the project root:
```env
SPOTIPY_CLIENT_ID='your_client_id'
SPOTIPY_CLIENT_SECRET='your_client_secret'
SPOTIPY_REDIRECT_URI='http://localhost:8080'

# Optional Configuration
LASTFM_API_KEY='your_lastfm_key'
ACTIVE_PLAYLIST_NAME='My Active Music'
PHONE_PLAYLIST_NAME='Phone Listening'
```

### 3. Installation
```bash
pip install -r requirements.txt
playwright install chromium
```

### 4. First Run
Initialize your local database from your existing A-Z playlists:
```bash
python main.py initdb
```

---

## 📖 Usage Guide

### Regular Maintenance
- **`python main.py sync`**: Ingest Liked Songs, deduplicate, and sync manual storage removals.
- **`python main.py refill`**: Process listened tracks and refill the Active playlist to 12 hours.

### Library Growth
- **`python main.py addartist "Artist Name"`**: Interactively select and add an artist's entire discography.
- **`python main.py findnew`**: Check for new album releases from your favorite artists.

### Discovery
- **`python main.py artistradio "Artist Name" --filter small`**: Create a massive discovery playlist based on a specific artist's "Fans Also Like" profile (focusing on niche/smaller artists).

### Clean Up
- **`python main.py findversions`**: Identify potential remasters/live versions for review.
- **`python main.py processversions`**: Finalize your version review after deleting tracks from the "Duplicate Review" playlist.

---

## 🗄 Storage Philosophy
Tawspom is built on the belief that **Playlists > Liked Songs**. By moving your music into A-Z playlists, you gain better control over shuffle logic, easier library portability, and the ability to use Tawspom's advanced statistical and variety-based algorithms to keep your music experience fresh.
