"""
Unit tests for actions.py's filesystem safety rails, focused on
restore_folder() -- the "this was actually a real audiobook" half of the
manual review workflow (see --apply-decisions / review_quarantine.py).

Run with: python3 -m unittest tests.test_actions -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bindery_bouncer import actions  # noqa: E402


class RestoreFolderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bindery_bouncer_actions_test_")
        self.library_root = self.tmp
        self.quarantine_dir = os.path.join(self.library_root, "_bindery_bouncer_quarantine")
        self.quarantined_folder = os.path.join(self.quarantine_dir, "Some Author", "Some Book")
        os.makedirs(self.quarantined_folder)
        with open(os.path.join(self.quarantined_folder, "01.mp3"), "wb") as f:
            f.write(b"fake audio")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_restores_to_original_relative_path(self):
        original = os.path.join(self.library_root, "Some Author", "Some Book")
        actions.restore_folder(self.quarantined_folder, original, self.library_root)
        self.assertTrue(os.path.isfile(os.path.join(original, "01.mp3")))
        self.assertFalse(os.path.exists(self.quarantined_folder))

    def test_refuses_to_clobber_an_existing_path(self):
        original = os.path.join(self.library_root, "Some Author", "Some Book")
        os.makedirs(original)
        with open(os.path.join(original, "already-here.mp3"), "wb") as f:
            f.write(b"something else re-imported here since quarantine")
        with self.assertRaises(actions.UnsafePathError):
            actions.restore_folder(self.quarantined_folder, original, self.library_root)
        # Nothing should have moved.
        self.assertTrue(os.path.exists(self.quarantined_folder))
        self.assertTrue(os.path.exists(os.path.join(original, "already-here.mp3")))

    def test_refuses_to_restore_outside_library_root(self):
        outside = os.path.join(os.path.dirname(self.library_root), "escaped")
        with self.assertRaises(actions.UnsafePathError):
            actions.restore_folder(self.quarantined_folder, outside, self.library_root)

    def test_prunes_empty_quarantine_parent_after_restore(self):
        original = os.path.join(self.library_root, "Some Author", "Some Book")
        actions.restore_folder(self.quarantined_folder, original, self.library_root)
        # "Some Author" under quarantine is now empty -- should be pruned.
        self.assertFalse(os.path.exists(os.path.join(self.quarantine_dir, "Some Author")))


if __name__ == "__main__":
    unittest.main()
