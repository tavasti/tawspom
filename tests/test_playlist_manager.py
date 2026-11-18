"""Tests for the duplicate-prevention and duplicate-purge logic.

Covers the fixes for:
- Bug 1: ingest must not create a duplicate storage playlist when the DB
  already has tracks for the letter (_resolve_storage_playlist).
- Bug 3a: no add path may add a track that is already in the target
  playlist (add_tracks_to_playlist_unique).
- Bug 3b: purging duplicates must keep exactly one copy of every track
  and never remove the last copy (remove_playlist_duplicates,
  purge_storage_duplicates).

Run with:  python -m unittest tests.test_playlist_manager -v

Mock-heavy test file: pyright cannot see through Mock() assignments to
SpotifyClient.sp / Manager.sp, so attribute-access and general type
compatibility checks are disabled for this file only.
"""
# pyright: reportGeneralTypeIssues = false
# pyright: reportAttributeAccessIssue = false
import unittest
from unittest.mock import Mock, call, patch

from tawspom.core.manager import Manager
from tawspom.core.spotify import SpotifyClient
from tawspom.models import Playlist


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


class TestRemovePlaylistDuplicates(unittest.TestCase):
    """Bug 3b: purge engine - keeps the first copy, never removes the last copy."""

    def _client_with_tracks(self, tracks):
        """tracks: the playlist contents as a list of track ids (single read)."""
        client = make_sp_client()
        client.get_playlist_tracks_ordered = Mock(return_value=list(tracks))
        client.sp.playlist_remove_all_occurrences_of_items.return_value = {}
        client.sp.playlist_add_items.return_value = {}
        return client

    def test_keeps_first_copy_removes_extras(self):
        # a@0,2  b@1,3 -> keep a@0, b@1; remove the extras.
        client = self._client_with_tracks(["a", "b", "a", "b"])

        removed = client.remove_playlist_duplicates("pl")

        self.assertEqual(removed, 2)
        # Each duplicated track is fully removed, then re-added once at its first position.
        removed_uris = [c[0][1] for c in client.sp.playlist_remove_all_occurrences_of_items.call_args_list]
        self.assertEqual(removed_uris, [["spotify:track:a"], ["spotify:track:b"]])
        readds = client.sp.playlist_add_items.call_args_list
        self.assertEqual(readds[0], call("pl", ["spotify:track:a"], position=0))
        self.assertEqual(readds[1], call("pl", ["spotify:track:b"], position=1))

    def test_no_duplicates_no_removal(self):
        client = self._client_with_tracks(["a", "b"])

        removed = client.remove_playlist_duplicates("pl")

        self.assertEqual(removed, 0)
        client.sp.playlist_remove_all_occurrences_of_items.assert_not_called()
        client.sp.playlist_add_items.assert_not_called()
        self.assertEqual(client.get_playlist_tracks_ordered.call_count, 1)

    def test_empty_playlist(self):
        client = self._client_with_tracks([])
        self.assertEqual(client.remove_playlist_duplicates("pl"), 0)
        client.sp.playlist_remove_all_occurrences_of_items.assert_not_called()

    def test_single_track_never_removed(self):
        """A track that appears exactly once must never be removed."""
        client = self._client_with_tracks(["only", "dup", "dup"])

        removed = client.remove_playlist_duplicates("pl")

        self.assertEqual(removed, 1)
        removed_uris = [c[0][1] for c in client.sp.playlist_remove_all_occurrences_of_items.call_args_list]
        self.assertEqual(removed_uris, [["spotify:track:dup"]])
        self.assertEqual(client.sp.playlist_add_items.call_args_list,
                         [call("pl", ["spotify:track:dup"], position=1)])

    def test_api_failure_stops_early(self):
        client = self._client_with_tracks(["a", "a"])
        client.sp.playlist_remove_all_occurrences_of_items.return_value = None

        removed = client.remove_playlist_duplicates("pl")

        self.assertEqual(removed, 0)  # nothing confirmed removed
        # Must not loop forever after a failed call
        self.assertEqual(client.sp.playlist_remove_all_occurrences_of_items.call_count, 1)
        client.sp.playlist_add_items.assert_not_called()

    def test_readd_fallback_appends_on_positioned_failure(self):
        """If the positioned re-add fails, append at the end so the track is not lost."""
        client = self._client_with_tracks(["a", "a"])
        # First add (with position) fails; the fallback add (no position) succeeds.
        client.sp.playlist_add_items.side_effect = [None, {}]

        removed = client.remove_playlist_duplicates("pl")

        self.assertEqual(removed, 1)
        self.assertEqual(client.sp.playlist_add_items.call_count, 2)
        self.assertEqual(client.sp.playlist_add_items.call_args_list[1],
                         call("pl", ["spotify:track:a"]))


class TestPurgeStorageDuplicates(unittest.TestCase):
    """Bug 3b: manager-level purge with plan/confirm/verify."""

    def _manager_with_playlists(self, playlists, items_by_id):
        mgr = make_manager()
        mgr.sp.get_storage_playlists.return_value = playlists
        mgr.sp.get_playlist_items_with_positions.side_effect = (
            lambda pl_id: items_by_id[pl_id]
        )
        mgr.sp.remove_playlist_duplicates.return_value = 0
        return mgr

    def test_no_duplicates_no_removal(self):
        mgr = self._manager_with_playlists(
            [Playlist("p1", "A")],
            {"p1": [(0, "a"), (1, "b")]},
        )

        mgr.purge_storage_duplicates()

        mgr.sp.remove_playlist_duplicates.assert_not_called()

    def test_no_storage_playlists(self):
        mgr = make_manager()
        mgr.sp.get_storage_playlists.return_value = []
        mgr.purge_storage_duplicates()
        mgr.sp.remove_playlist_duplicates.assert_not_called()

    def test_cancelled_by_user(self):
        mgr = self._manager_with_playlists(
            [Playlist("p1", "A")],
            {"p1": [(0, "a"), (1, "a")]},
        )
        with patch("builtins.input", return_value="n"):
            mgr.purge_storage_duplicates()
        mgr.sp.remove_playlist_duplicates.assert_not_called()

    def test_confirmed_removes_from_each_playlist_with_dups(self):
        mgr = self._manager_with_playlists(
            [Playlist("p1", "A"), Playlist("p2", "B")],
            {
                "p1": [(0, "a"), (1, "a"), (2, "b")],  # 1 dup
                "p2": [(0, "c")],                      # no dups
            },
        )
        mgr.sp.remove_playlist_duplicates.side_effect = lambda pl_id: 1 if pl_id == "p1" else 0

        with patch("builtins.input", return_value="y"):
            mgr.purge_storage_duplicates()

        # Only the playlist that actually had duplicates is purged
        mgr.sp.remove_playlist_duplicates.assert_called_once_with("p1")


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
