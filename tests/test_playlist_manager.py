"""Tests for duplicate prevention and sync safety.

Covers the fixes for:
- Bug 1: ingest must not create a duplicate storage playlist when the DB
  already has tracks for the letter (_resolve_storage_playlist).
- Bug 2: sync must not mass-delete tracks when a playlist read collapsed
  (partial API glitch) — suspicious playlists are detected and the user
  decides (_find_suspicious_playlists).
- Bug 3a: no add path may add a track that is already in the target
  playlist (add_tracks_to_playlist_unique).
- Bug 3b: 'recreate' rebuilds one storage playlist from the DB with
  pre-clear verification, rescue of misplaced tracks, and post-refill
  verification (recreate_storage_playlist).
- Rate limiting: 429 handling must wait bounded times and stop cleanly
  beyond the cap, so interrupted commands can be re-run to resume
  (_call_with_retry).

Run with:  python -m unittest tests.test_playlist_manager -v

Mock-heavy test file: pyright cannot see through Mock() assignments to
SpotifyClient.sp / Manager.sp, so attribute-access and general type
compatibility checks are disabled for this file only.
"""
# pyright: reportGeneralTypeIssues = false
# pyright: reportAttributeAccessIssue = false
import unittest
from unittest.mock import Mock, call, patch

from spotipy.exceptions import SpotifyException

from tawspom.core.manager import Manager
from tawspom.core.spotify import SpotifyClient, SpotifyRateLimitError
from tawspom.models import Playlist, Track


def make_sp_client():
    """Builds a SpotifyClient without running __init__ (no auth needed)."""
    client = SpotifyClient.__new__(SpotifyClient)
    client.sp = Mock()
    client.db = None
    return client


def make_manager():
    """Builds a Manager without running __init__ (no auth/DB needed)."""
    mgr = Manager.__new__(Manager)
    mgr.sp = Mock()
    mgr.db = Mock()
    return mgr


def make_track(tid, name, storage_playlist_id="p1", is_deleted=False):
    """Builds a minimal Track for tests."""
    return Track(id=tid, name=name, artist="Artist", album="Album",
                 duration_ms=200000, storage_playlist_id=storage_playlist_id,
                 is_deleted=is_deleted)


class TestAddTracksUnique(unittest.TestCase):
    """Bug 3a: central dedup guard used by all add paths."""

    def test_skips_tracks_already_in_playlist(self):
        client = make_sp_client()
        client.get_playlist_tracks_ordered = lambda playlist_id: ["t1", "t2"]
        client.add_tracks_to_playlist = Mock()

        added, skipped = client.add_tracks_to_playlist_unique("pl", ["t1", "t3"])

        self.assertEqual(added, ["t3"])
        self.assertEqual(skipped, ["t1"])
        client.add_tracks_to_playlist.assert_called_once_with("pl", ["t3"])

    def test_all_new_tracks_are_added(self):
        client = make_sp_client()
        client.get_playlist_tracks_ordered = lambda playlist_id: ["t1"]
        client.add_tracks_to_playlist = Mock()

        added, skipped = client.add_tracks_to_playlist_unique("pl", ["t2", "t3"])

        self.assertEqual(added, ["t2", "t3"])
        self.assertEqual(skipped, [])
        client.add_tracks_to_playlist.assert_called_once_with("pl", ["t2", "t3"])

    def test_empty_input_does_nothing(self):
        client = make_sp_client()
        client.get_playlist_tracks_ordered = Mock()
        client.add_tracks_to_playlist = Mock()

        added, skipped = client.add_tracks_to_playlist_unique("pl", [])

        self.assertEqual((added, skipped), ([], []))
        client.get_playlist_tracks_ordered.assert_not_called()
        client.add_tracks_to_playlist.assert_not_called()

    def test_all_already_present_adds_nothing(self):
        client = make_sp_client()
        client.get_playlist_tracks_ordered = lambda playlist_id: ["t1", "t2"]
        client.add_tracks_to_playlist = Mock()

        added, skipped = client.add_tracks_to_playlist_unique("pl", ["t1", "t2"])

        self.assertEqual(added, [])
        self.assertEqual(skipped, ["t1", "t2"])
        client.add_tracks_to_playlist.assert_not_called()


class TestRecreateStoragePlaylist(unittest.TestCase):
    """Bug 3b: 'recreate' rebuilds one storage playlist from the DB, safely."""

    def test_invalid_letter_rejected(self):
        for bad in ("", "AB", "1", "Ä"):
            with self.subTest(bad=bad):
                mgr = make_manager()
                mgr.recreate_storage_playlist(bad)
                mgr.sp.get_storage_playlists.assert_not_called()

    def test_duplicate_target_playlists_abort(self):
        mgr = make_manager()
        mgr.sp.get_storage_playlists.return_value = [Playlist("x1", "A"), Playlist("x2", "A")]
        mgr.recreate_storage_playlist("A")
        mgr.sp.clear_playlist.assert_not_called()
        mgr.sp.add_tracks_to_playlist.assert_not_called()

    def test_missing_playlist_declined(self):
        mgr = make_manager()
        mgr.sp.get_storage_playlists.return_value = []
        with patch("builtins.input", return_value="n"):
            mgr.recreate_storage_playlist("A")
        mgr.sp.create_playlist.assert_not_called()
        mgr.sp.clear_playlist.assert_not_called()

    def test_happy_path_with_dup_misplaced_and_missing(self):
        """Dups removed, misplaced track rescued, missing track added, DB normalized."""
        mgr = make_manager()
        mgr.sp.get_storage_playlists.return_value = [Playlist("p1", "A"), Playlist("p2", "B")]
        # reads: target 'A' content, rescue target 'B' content, final verification
        mgr.sp.get_playlist_tracks_ordered.side_effect = [
            ["a1", "a2", "b1", "a1"],
            ["b2"],
            ["a1", "a2", "a3"],
        ]
        tracks = [make_track("a1", "Alpha"), make_track("a2", "Act"),
                  make_track("a3", "Apple"), make_track("b1", "Beta", "p2")]
        with patch("tawspom.core.manager.get_all_active_tracks", return_value=tracks), \
             patch("tawspom.core.manager.get_tracks_by_ids", return_value=tracks), \
             patch("tawspom.core.manager.update_storage_playlist_id") as upd, \
             patch("builtins.input", return_value="y"):
            mgr.recreate_storage_playlist("A")

        mgr.sp.clear_playlist.assert_called_once_with("p1")
        add_calls = mgr.sp.add_tracks_to_playlist.call_args_list
        self.assertIn(call("p2", ["b1"]), add_calls)            # rescue
        self.assertIn(call("p1", ["a1", "a2", "a3"]), add_calls)  # refill
        upd.assert_any_call(mgr.db, ["a1", "a2", "a3"], "p1")
        upd.assert_any_call(mgr.db, ["b1"], "p2")

    def test_unknown_track_aborts_before_clear(self):
        mgr = make_manager()
        mgr.sp.get_storage_playlists.return_value = [Playlist("p1", "A")]
        mgr.sp.get_playlist_tracks_ordered.return_value = ["a1", "ghost"]
        tracks = [make_track("a1", "Alpha")]
        with patch("tawspom.core.manager.get_all_active_tracks", return_value=tracks), \
             patch("tawspom.core.manager.get_tracks_by_ids", return_value=tracks):
            mgr.recreate_storage_playlist("A")
        mgr.sp.clear_playlist.assert_not_called()
        mgr.sp.add_tracks_to_playlist.assert_not_called()

    def test_deleted_track_aborts_before_clear(self):
        mgr = make_manager()
        mgr.sp.get_storage_playlists.return_value = [Playlist("p1", "A")]
        mgr.sp.get_playlist_tracks_ordered.return_value = ["a1", "gone"]
        a1 = make_track("a1", "Alpha")
        gone = make_track("gone", "Gone", is_deleted=True)
        with patch("tawspom.core.manager.get_all_active_tracks", return_value=[a1]), \
             patch("tawspom.core.manager.get_tracks_by_ids", return_value=[a1, gone]):
            mgr.recreate_storage_playlist("A")
        mgr.sp.clear_playlist.assert_not_called()

    def test_rescue_target_unresolvable_aborts(self):
        """Misplaced track whose own letter has no playlist: abort, don't lose it."""
        mgr = make_manager()
        mgr.sp.get_storage_playlists.return_value = [Playlist("p1", "A")]  # no 'B'
        mgr.sp.get_playlist_tracks_ordered.return_value = ["a1", "b1"]
        tracks = [make_track("a1", "Alpha"), make_track("b1", "Beta", "p2")]
        with patch("tawspom.core.manager.get_all_active_tracks", return_value=tracks), \
             patch("tawspom.core.manager.get_tracks_by_ids", return_value=tracks):
            mgr.recreate_storage_playlist("A")
        mgr.sp.clear_playlist.assert_not_called()
        mgr.sp.add_tracks_to_playlist.assert_not_called()

    def test_declined_confirmation_changes_nothing(self):
        mgr = make_manager()
        mgr.sp.get_storage_playlists.return_value = [Playlist("p1", "A")]
        mgr.sp.get_playlist_tracks_ordered.return_value = ["a1"]
        tracks = [make_track("a1", "Alpha")]
        with patch("tawspom.core.manager.get_all_active_tracks", return_value=tracks), \
             patch("tawspom.core.manager.get_tracks_by_ids", return_value=tracks), \
             patch("builtins.input", return_value="n"):
            mgr.recreate_storage_playlist("A")
        mgr.sp.clear_playlist.assert_not_called()
        mgr.sp.add_tracks_to_playlist.assert_not_called()

    def test_missing_playlist_created_and_refilled(self):
        mgr = make_manager()
        mgr.sp.get_storage_playlists.return_value = []
        mgr.sp.create_playlist.return_value = Playlist("new1", "A")
        # reads: new playlist content (empty), final verification
        mgr.sp.get_playlist_tracks_ordered.side_effect = [[], ["a1"]]
        tracks = [make_track("a1", "Alpha")]
        with patch("tawspom.core.manager.get_all_active_tracks", return_value=tracks), \
             patch("tawspom.core.manager.get_tracks_by_ids", return_value=[]), \
             patch("tawspom.core.manager.update_storage_playlist_id") as upd, \
             patch("builtins.input", side_effect=["y", "y"]):
            mgr.recreate_storage_playlist("a")  # lowercase is normalized

        mgr.sp.create_playlist.assert_called_once_with("A")
        mgr.sp.clear_playlist.assert_called_once_with("new1")
        mgr.sp.add_tracks_to_playlist.assert_called_once_with("new1", ["a1"])
        upd.assert_called_once_with(mgr.db, ["a1"], "new1")

    def test_empty_playlist_and_empty_db_noop(self):
        mgr = make_manager()
        mgr.sp.get_storage_playlists.return_value = [Playlist("p1", "A")]
        mgr.sp.get_playlist_tracks_ordered.return_value = []
        with patch("tawspom.core.manager.get_all_active_tracks", return_value=[]):
            mgr.recreate_storage_playlist("A")
        mgr.sp.clear_playlist.assert_not_called()
        mgr.sp.add_tracks_to_playlist.assert_not_called()


class TestFindSuspiciousPlaylists(unittest.TestCase):
    """Bug 2: sync guard for collapsed playlist reads (partial API glitches)."""

    def test_collapsed_read_is_suspicious(self):
        mgr = make_manager()
        stats = [
            {"id": "p1", "name": "A", "count": 10, "duration": 0},
            {"id": "p2", "name": "B", "count": 90, "duration": 0},
        ]
        with patch("tawspom.core.manager.get_playlist_track_count",
                   side_effect=lambda db, pl_id: {"p1": 100, "p2": 100}[pl_id]):
            ids, detail = mgr._find_suspicious_playlists(stats)
        self.assertEqual(ids, {"p1"})
        self.assertEqual(detail, [("A", 10, 100)])

    def test_full_read_not_suspicious(self):
        mgr = make_manager()
        stats = [{"id": "p1", "name": "A", "count": 100, "duration": 0}]
        with patch("tawspom.core.manager.get_playlist_track_count", return_value=100):
            ids, detail = mgr._find_suspicious_playlists(stats)
        self.assertEqual((ids, detail), (set(), []))

    def test_empty_db_not_suspicious(self):
        """A playlist the DB knows nothing about cannot 'collapse'."""
        mgr = make_manager()
        stats = [{"id": "p1", "name": "A", "count": 0, "duration": 0}]
        with patch("tawspom.core.manager.get_playlist_track_count", return_value=0):
            ids, detail = mgr._find_suspicious_playlists(stats)
        self.assertEqual((ids, detail), (set(), []))

    def test_boundary_half_not_suspicious(self):
        """Exactly half the DB count is still a trustworthy read."""
        mgr = make_manager()
        stats = [{"id": "p1", "name": "A", "count": 50, "duration": 0}]
        with patch("tawspom.core.manager.get_playlist_track_count", return_value=100):
            ids, detail = mgr._find_suspicious_playlists(stats)
        self.assertEqual((ids, detail), (set(), []))


class TestRateLimitHandling(unittest.TestCase):
    """429 handling: bounded waits, clean stop beyond the cap, resumable."""

    def test_large_retry_after_stops_cleanly(self):
        """A multi-hour Retry-After must not be sat through — stop cleanly."""
        client = make_sp_client()
        exc = SpotifyException(429, 0, "rate limited", headers={"Retry-After": "999999"})
        client.sp.api_call = Mock(side_effect=exc)
        with patch("tawspom.core.spotify.time.sleep") as sl, \
             self.assertRaises(SpotifyRateLimitError):
            client._call_with_retry(client.sp.api_call)
        self.assertEqual(client.sp.api_call.call_count, 1)
        sl.assert_not_called()

    def test_small_retry_after_waits_and_recovers(self):
        client = make_sp_client()
        exc = SpotifyException(429, 0, "rate limited", headers={"Retry-After": "2"})
        ok = {"ok": True}
        client.sp.api_call = Mock(side_effect=[exc, ok])
        with patch("tawspom.core.spotify.time.sleep") as sl:
            result = client._call_with_retry(client.sp.api_call)
        self.assertEqual(result, ok)
        sl.assert_called_once_with(2.0)

    def test_missing_retry_after_uses_default(self):
        client = make_sp_client()
        exc = SpotifyException(429, 0, "rate limited")
        client.sp.api_call = Mock(side_effect=[exc, "ok"])
        with patch("tawspom.core.spotify.time.sleep") as sl:
            result = client._call_with_retry(client.sp.api_call)
        self.assertEqual(result, "ok")
        sl.assert_called_once_with(30.0)

    def test_repeated_429_beyond_retry_budget_stops(self):
        client = make_sp_client()
        exc = SpotifyException(429, 0, "rate limited", headers={"Retry-After": "2"})
        client.sp.api_call = Mock(side_effect=exc)
        with patch("tawspom.core.spotify.time.sleep") as sl, \
             self.assertRaises(SpotifyRateLimitError):
            client._call_with_retry(client.sp.api_call)
        # SPOTIFY_MAX_RETRIES=3: two waits, then a clean stop on the last iteration
        self.assertEqual(sl.call_count, 2)

    def test_non_429_raises_immediately(self):
        client = make_sp_client()
        exc = SpotifyException(500, 1, "server error")
        client.sp.api_call = Mock(side_effect=exc)
        with self.assertRaises(SpotifyException):
            client._call_with_retry(client.sp.api_call)
        self.assertEqual(client.sp.api_call.call_count, 1)


class TestResolveStoragePlaylist(unittest.TestCase):
    """Bug 1: never create a duplicate playlist when the DB already has tracks."""

    def _manager(self, spotify_playlists, db_pl_id):
        mgr = make_manager()
        mgr.sp.get_storage_playlists.return_value = spotify_playlists
        # Shadow the DB lookup with a fixed answer
        mgr._get_db_playlist_id_for_letter = lambda letter: db_pl_id
        return mgr

    def test_retry_fetch_finds_playlist(self):
        mgr = self._manager([Playlist("x1", "A")], db_pl_id=None)
        self.assertEqual(mgr._resolve_storage_playlist("A"), "x1")
        mgr.sp.create_playlist.assert_not_called()

    def test_db_stored_id_still_valid_on_spotify(self):
        mgr = self._manager([], db_pl_id="db1")
        mgr.sp.get_playlist_name.return_value = "A"

        self.assertEqual(mgr._resolve_storage_playlist("A"), "db1")
        mgr.sp.create_playlist.assert_not_called()

    def test_no_db_tracks_genuinely_new_creates_playlist(self):
        mgr = self._manager([], db_pl_id=None)
        mgr.sp.create_playlist.return_value = Playlist("new1", "A")

        self.assertEqual(mgr._resolve_storage_playlist("A"), "new1")
        mgr.sp.create_playlist.assert_called_once_with("A")

    def test_unverifiable_with_db_tracks_does_not_create(self):
        """The critical bug-1 case: DB has tracks for 'A' but Spotify lookup
        fails -> must return None (abort), never create a second 'A'."""
        mgr = self._manager([], db_pl_id="db1")
        mgr.sp.get_playlist_name.return_value = "Something Else"

        self.assertIsNone(mgr._resolve_storage_playlist("A"))
        mgr.sp.create_playlist.assert_not_called()

    def test_unverifiable_db_id_returns_none(self):
        mgr = self._manager([], db_pl_id="db1")
        mgr.sp.get_playlist_name.return_value = None

        self.assertIsNone(mgr._resolve_storage_playlist("A"))
        mgr.sp.create_playlist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
