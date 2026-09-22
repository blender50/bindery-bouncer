"""
Unit tests for review_quarantine.py's pure logic (folder grouping, CSV
context-joining, decisions round-trip) -- no audio playback involved, since
that only runs interactively on macOS with a person listening.

Run with: python3 -m unittest tests.test_review_quarantine -v
"""
import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import review_quarantine as rq  # noqa: E402


class FindBookFoldersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="review_quarantine_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_groups_audio_files_by_immediate_parent(self):
        folder = os.path.join(self.tmp, "Author", "Book")
        os.makedirs(folder)
        open(os.path.join(folder, "01.mp3"), "w").close()
        open(os.path.join(folder, "02.mp3"), "w").close()
        open(os.path.join(folder, "cover.jpg"), "w").close()  # not audio -- excluded

        groups = rq.find_book_folders(self.tmp)
        self.assertEqual(list(groups.keys()), [folder])
        self.assertEqual(len(groups[folder]), 2)

    def test_folders_with_no_audio_are_absent(self):
        os.makedirs(os.path.join(self.tmp, "Empty"))
        groups = rq.find_book_folders(self.tmp)
        self.assertEqual(groups, {})


class LoadContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="review_quarantine_test_")
        self.csv_path = os.path.join(self.tmp, "report.csv")
        with open(self.csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["folder", "tier", "expected_author", "reasons"])
            w.writeheader()
            w.writerow({
                "folder": "/audiobooks/Andy McNab/Deep Black (2004)",
                "tier": "confirmed",
                "expected_author": "Andy McNab",
                "reasons": "embedded genre tag is a music genre ('Death Metal')",
            })

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_strips_library_prefix_to_match_quarantine_relative_paths(self):
        context = rq.load_context(self.csv_path, "/audiobooks")
        key = os.path.normpath("Andy McNab/Deep Black (2004)")
        self.assertIn(key, context)
        self.assertEqual(context[key]["expected_author"], "Andy McNab")

    def test_missing_report_csv_returns_empty_context(self):
        self.assertEqual(rq.load_context(None, "/audiobooks"), {})


class DecisionsRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="review_quarantine_test_")
        self.decisions_path = os.path.join(self.tmp, "decisions.csv")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_file_yet_means_nothing_decided(self):
        self.assertEqual(rq.load_existing_decisions(self.decisions_path), set())

    def test_appended_decisions_are_loaded_back_and_normalized(self):
        rq.append_decision(self.decisions_path, "Author/Book One", "audiobook")
        rq.append_decision(self.decisions_path, "Author/Book Two", "music")
        decided = rq.load_existing_decisions(self.decisions_path)
        self.assertEqual(decided, {os.path.normpath("Author/Book One"), os.path.normpath("Author/Book Two")})

    def test_second_run_skips_already_decided_folders(self):
        # Simulates resuming: append once, then confirm a fresh load sees it.
        rq.append_decision(self.decisions_path, "Author/Book", "music")
        self.assertIn(os.path.normpath("Author/Book"), rq.load_existing_decisions(self.decisions_path))
        with open(self.decisions_path, newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], "music")


if __name__ == "__main__":
    unittest.main()
