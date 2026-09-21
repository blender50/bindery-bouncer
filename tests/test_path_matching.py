"""
Regression test for remap_prefix() / build_path_index() -- the logic that
decides whether a local audiobook folder matches a record in Bindery's
catalogue.

Caught from a real, library-wide false negative: a live catalogue dump
confirmed Bindery reports audiobook files under a prefix like
/data/media/audiobooks/..., while bindery-bouncer itself always scans
under /audiobooks/... (its own container's mount point). Those two
prefixes never match as plain strings, so *every* local folder came back
"no matching Bindery catalogue record found" regardless of how many
books were actually indexed -- the fetch-side pagination fix (see
test_bindery_client.py) and the catalogue index itself were both working
correctly; the lookup step just never had a chance to succeed because no
--bindery-path-prefix was ever configured, and remap_prefix() silently
no-ops when either prefix is unset (the safe default for someone whose
mount points genuinely do line up, but silent, so this was easy to miss).

Run with: python3 -m unittest tests.test_path_matching -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bindery_bouncer.bindery_client import BookRecord, build_path_index, remap_prefix  # noqa: E402


class RemapPrefixTest(unittest.TestCase):
    def test_translates_local_path_to_bindery_path(self):
        # Steve's real setup: this tool sees /audiobooks, but Bindery's own
        # audiobookFilePath values are rooted at /data/media/audiobooks.
        local = "/audiobooks/Andy McNab/Deep Black (2004)"
        result = remap_prefix(local, "/audiobooks", "/data/media/audiobooks")
        self.assertEqual(result, "/data/media/audiobooks/Andy McNab/Deep Black (2004)")

    def test_no_prefix_configured_is_a_silent_no_op(self):
        # This is the actual failure mode: with no --bindery-path-prefix set
        # (the default), the path comes back completely unchanged, with no
        # error or warning -- it just never matches anything downstream.
        local = "/audiobooks/Andy McNab/Deep Black (2004)"
        self.assertEqual(remap_prefix(local, None, None), local)
        self.assertEqual(remap_prefix(local, "/audiobooks", None), local)
        self.assertEqual(remap_prefix(local, None, "/data/media/audiobooks"), local)

    def test_path_not_under_from_prefix_is_left_unchanged(self):
        self.assertEqual(
            remap_prefix("/some/other/path", "/audiobooks", "/data/media/audiobooks"),
            "/some/other/path",
        )


class PathIndexMatchingTest(unittest.TestCase):
    """
    End-to-end reproduction of the real bug and its fix, using the exact
    folder from calibration.
    """

    def setUp(self):
        self.rec = BookRecord(
            id=1,
            title="Deep Black",
            author="Andy McNab",
            asin=None,
            isbn=None,
            media_type="audiobook",
            paths=["/data/media/audiobooks/Andy McNab/Deep Black (2004)/file.mp3"],
        )
        self.index = build_path_index([self.rec])
        self.local_folder = "/audiobooks/Andy McNab/Deep Black (2004)"

    def test_unmapped_local_path_does_not_match_bindery_indexed_path(self):
        # This is the bug: without remapping, a real, present record in the
        # index is invisible to a lookup using the local (this tool's own)
        # view of the path.
        self.assertNotIn(os.path.normpath(self.local_folder), self.index)

    def test_remapped_local_path_matches_the_indexed_record(self):
        remapped = remap_prefix(self.local_folder, "/audiobooks", "/data/media/audiobooks")
        key = os.path.normpath(remapped)
        self.assertIn(key, self.index)
        self.assertIs(self.index[key], self.rec)


if __name__ == "__main__":
    unittest.main()
