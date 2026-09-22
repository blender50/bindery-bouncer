"""
End-to-end test: builds a tiny fake library (one folder that's genuinely a
music album mistagged as a book -- the exact bug this tool exists for --
and one folder that's a correctly tagged audiobook), serves a mock Bindery
API describing what each folder *should* contain, runs the real CLI
pipeline against it, and asserts the verdicts come out right.

Run with: python3 -m pytest tests/ -v
(or just: python3 tests/test_integration.py)
"""
import csv
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bindery_bouncer.cli import run  # noqa: E402

SR = 22050
DUR = 60.0


def _synthetic_music(n, rng, bpm=120):
    t = np.arange(n) / SR
    beat_period = 60.0 / bpm
    click = np.zeros(n)
    click_len = int(SR * 0.08)
    env = np.exp(-np.linspace(0, 8, click_len))
    for bt in np.arange(0, DUR, beat_period):
        idx = int(bt * SR)
        end = min(idx + click_len, n)
        click[idx:end] += env[: end - idx] * 0.9
    tone = 0.15 * np.sin(2 * np.pi * 220 * t) + 0.1 * np.sin(2 * np.pi * 330 * t)
    mix = click + tone
    return (mix / (np.max(np.abs(mix)) + 1e-9) * 0.9).astype(np.float32)


def _synthetic_speech(n, rng):
    out = np.zeros(n)
    pos = 0.0
    while pos < DUR:
        seg_len = rng.uniform(0.08, 0.35)
        idx0, idx1 = int(pos * SR), int(min(pos + seg_len, DUR) * SR)
        seg_n = idx1 - idx0
        if seg_n > 0:
            tt = np.arange(seg_n) / SR
            if rng.random() < 0.65:
                f0 = rng.uniform(100, 190) + 15 * np.sin(2 * np.pi * 3 * tt)
                phase = 2 * np.pi * np.cumsum(f0) / SR
                seg = np.sin(phase) + 0.5 * np.sin(2 * phase) + 0.25 * np.sin(3 * phase)
            else:
                seg = rng.standard_normal(seg_n)
            out[idx0:idx1] += seg * (np.hanning(seg_n) ** 0.5)
        pos += seg_len + rng.uniform(0.03, 0.35)
    return (out / (np.max(np.abs(out)) + 1e-9) * 0.9).astype(np.float32)


class MockBinderyHandler(BaseHTTPRequestHandler):
    books = []
    history = []  # list of history entries, each with an "id" and a "path"
    calls = []    # every mutating call this mock received: (method, path, body)

    def do_GET(self):
        if self.path.startswith("/api/v1/book"):
            self._json(200, self.books)
        elif self.path.startswith("/api/v1/history"):
            self._json(200, self.history)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        body = self._read_body()
        self.calls.append(("POST", self.path, body))
        self._json(200, {"ok": True})

    def do_PUT(self):
        body = self._read_body()
        self.calls.append(("PUT", self.path, body))
        self._json(200, {"ok": True})

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return None

    def _json(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *a):
        pass


class BounceIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bindery_bouncer_test_")
        rng = np.random.default_rng(1)
        n = int(SR * DUR)

        self.bad_dir = os.path.join(self.tmp, "Harry Turtledove", "Brothers in Arms")
        self.good_dir = os.path.join(self.tmp, "Some Author", "Correct Book")
        os.makedirs(self.bad_dir)
        os.makedirs(self.good_dir)

        self.bad_file = os.path.join(self.bad_dir, "01 - Track.flac")
        self.good_file = os.path.join(self.good_dir, "01 - Chapter 1.flac")

        sf.write(self.bad_file, _synthetic_music(n, rng), SR, format="FLAC")
        sf.write(self.good_file, _synthetic_speech(n, rng), SR, format="FLAC")

        bad_tags = FLAC(self.bad_file)
        bad_tags.update(title="Money for Nothing", artist="Dire Straits",
                         album="Brothers in Arms", genre="Rock")
        bad_tags.save()

        good_tags = FLAC(self.good_file)
        good_tags.update(title="Chapter 1", artist="Some Author",
                          album="Correct Book", genre="Audiobook")
        good_tags.save()

        MockBinderyHandler.books = [
            {"id": 1, "title": "Brothers in Arms", "author": {"name": "Harry Turtledove"},
             "mediaType": "audiobook", "editions": [{"formats": [{"path": self.bad_file}]}]},
            {"id": 2, "title": "Correct Book", "author": {"name": "Some Author"},
             "mediaType": "audiobook", "editions": [{"formats": [{"path": self.good_file}]}]},
        ]
        MockBinderyHandler.history = [
            {"id": 501, "bookId": 1, "path": self.bad_file, "event": "imported"},
        ]
        MockBinderyHandler.calls = []
        self.server = HTTPServer(("127.0.0.1", 0), MockBinderyHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        self.report_path = os.path.join(self.tmp, "report.csv")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, extra_args=None):
        args = [
            "--library-path", self.tmp,
            "--bindery-url", f"http://127.0.0.1:{self.port}/api/v1",
            "--bindery-api-key", "testkey",
            "--report-csv", self.report_path,
        ] + (extra_args or [])
        return run(args)

    def _read_report(self):
        with open(self.report_path, newline="") as f:
            return {row["folder"]: row for row in csv.DictReader(f)}

    def test_dry_run_confirms_the_music_and_leaves_the_book_alone(self):
        rc = self._run()
        self.assertEqual(rc, 0)
        report = self._read_report()
        self.assertEqual(report[self.bad_dir]["tier"], "confirmed")
        self.assertEqual(report[self.good_dir]["tier"], "clean")
        # Dry run: nothing on disk should have changed.
        self.assertTrue(os.path.exists(self.bad_file))
        self.assertTrue(os.path.exists(self.good_file))

    def test_metadata_mismatch_is_driven_by_author_not_coincidental_title_match(self):
        # Regression test: the album title tag ("Brothers in Arms") coincidentally
        # matches the expected book title exactly, even though the artist is
        # completely wrong. The mismatch score must not be fooled by that.
        self._run()
        report = self._read_report()
        mismatch = float(report[self.bad_dir]["metadata_mismatch_score"])
        self.assertGreater(mismatch, 0.5, "author mismatch should dominate a coincidental title match")

    def test_execute_deletes_confirmed_and_prunes_empty_parent(self):
        rc = self._run(["--execute"])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.bad_file))
        self.assertFalse(os.path.exists(self.bad_dir))
        self.assertFalse(os.path.exists(os.path.dirname(self.bad_dir)))  # pruned empty parent
        self.assertTrue(os.path.exists(self.good_file))  # untouched

    def test_execute_quarantine_preserves_relative_path(self):
        rc = self._run(["--execute", "--confirmed-action", "quarantine"])
        self.assertEqual(rc, 0)
        quarantined = os.path.join(self.tmp, "_bindery_bouncer_quarantine",
                                    "Harry Turtledove", "Brothers in Arms", "01 - Track.flac")
        self.assertTrue(os.path.exists(quarantined))
        self.assertTrue(os.path.exists(self.good_file))

    def test_execute_delete_closes_the_loop_with_bindery(self):
        # On an actual delete: blocklist the offending history entry, re-want
        # the book, and trigger an immediate re-search (all default-on).
        rc = self._run(["--execute"])
        self.assertEqual(rc, 0)
        methods_paths = [(m, p) for m, p, _ in MockBinderyHandler.calls]
        self.assertIn(("POST", "/api/v1/history/501/blocklist"), methods_paths)
        self.assertIn(("PUT", "/api/v1/book/1"), methods_paths)
        self.assertIn(("POST", "/api/v1/book/1/search"), methods_paths)
        # And never touches book 2 (the correctly-tagged, untouched audiobook).
        self.assertFalse(any(p.startswith("/api/v1/book/2") for _, p in methods_paths))

    def test_execute_quarantine_never_closes_the_loop(self):
        # Quarantine leaves the file in place -- the book isn't missing, so
        # none of the blocklist/re-want/search calls should ever fire.
        rc = self._run(["--execute", "--confirmed-action", "quarantine"])
        self.assertEqual(rc, 0)
        self.assertEqual(MockBinderyHandler.calls, [])

    def test_no_close_loop_flag_suppresses_it(self):
        rc = self._run(["--execute", "--no-close-loop"])
        self.assertEqual(rc, 0)
        self.assertEqual(MockBinderyHandler.calls, [])
        # The delete itself should still have happened.
        self.assertFalse(os.path.exists(self.bad_file))

    def test_no_search_after_blocklist_flag(self):
        rc = self._run(["--execute", "--no-search-after-blocklist"])
        self.assertEqual(rc, 0)
        methods_paths = [(m, p) for m, p, _ in MockBinderyHandler.calls]
        self.assertIn(("POST", "/api/v1/history/501/blocklist"), methods_paths)
        self.assertIn(("PUT", "/api/v1/book/1"), methods_paths)
        self.assertNotIn(("POST", "/api/v1/book/1/search"), methods_paths)


class NewOnlyModeTest(unittest.TestCase):
    """
    --new-only + --state-file: the scheduled/cron-friendly mode. Runs entirely
    tag-only (--no-bindery --skip-audio) since these tests are about mtime
    quiescence and state persistence, not the detection signals themselves
    (those are covered above).
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bindery_bouncer_newonly_")
        rng = np.random.default_rng(2)
        # _synthetic_music/_synthetic_speech both loop out to the module-level
        # DUR regardless of n, so n has to match that -- content doesn't
        # actually matter here since these tests run with --skip-audio.
        n = int(SR * DUR)

        self.bad_dir = os.path.join(self.tmp, "Harry Turtledove", "Brothers in Arms")
        self.good_dir = os.path.join(self.tmp, "Some Author", "Correct Book")
        os.makedirs(self.bad_dir)
        os.makedirs(self.good_dir)
        self.bad_file = os.path.join(self.bad_dir, "01 - Track.flac")
        self.good_file = os.path.join(self.good_dir, "01 - Chapter 1.flac")
        sf.write(self.bad_file, _synthetic_music(n, rng), SR, format="FLAC")
        sf.write(self.good_file, _synthetic_speech(n, rng), SR, format="FLAC")

        bad_tags = FLAC(self.bad_file)
        bad_tags.update(title="Money for Nothing", artist="Dire Straits",
                         album="Brothers in Arms", genre="Rock")
        bad_tags.save()
        good_tags = FLAC(self.good_file)
        good_tags.update(title="Chapter 1", artist="Some Author",
                          album="Correct Book", genre="Audiobook")
        good_tags.save()

        self.state_path = os.path.join(self.tmp, "state.json")
        self.report_path = os.path.join(self.tmp, "report.csv")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _backdate(self, seconds):
        then = time.time() - seconds
        for f in (self.bad_file, self.good_file):
            os.utime(f, (then, then))

    def _run(self, extra_args=None):
        if os.path.exists(self.report_path):
            os.remove(self.report_path)
        args = [
            "--library-path", self.tmp,
            "--no-bindery", "--skip-audio",
            "--new-only", "--min-quiet-seconds", "3600",
            "--state-file", self.state_path,
            "--report-csv", self.report_path,
        ] + (extra_args or [])
        return run(args)

    def _report_folders(self):
        if not os.path.exists(self.report_path):
            return set()
        with open(self.report_path, newline="") as f:
            return {row["folder"] for row in csv.DictReader(f)}

    def test_recently_modified_folders_are_skipped(self):
        # Files were just written -- mtime is "now", well inside the 1h window.
        rc = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(self._report_folders(), set())

    def test_quiet_clean_folder_is_processed_then_skipped_next_tick(self):
        self._backdate(7200)
        rc = self._run()
        self.assertEqual(rc, 0)
        self.assertIn(self.good_dir, self._report_folders())
        # Nothing changed about the clean folder since -- state should skip
        # it on the next tick (bad_dir reappears regardless -- it's still
        # unresolved dry-run output, covered by the test below).
        rc = self._run()
        self.assertEqual(rc, 0)
        self.assertNotIn(self.good_dir, self._report_folders())

    def test_dry_run_flag_is_not_swallowed_by_state(self):
        self._backdate(7200)
        self._run()  # dry run: bad_dir flagged suspect (tag genre alone), nothing done
        self.assertIn(self.bad_dir, self._report_folders())
        # Unresolved (dry run never executes) -- must be reconsidered, not skipped.
        self._run()
        self.assertIn(self.bad_dir, self._report_folders())

    def test_execute_resolves_and_state_then_skips_next_tick(self):
        self._backdate(7200)
        rc = self._run(["--execute", "--confirmed-action", "quarantine", "--suspect-action", "quarantine"])
        self.assertEqual(rc, 0)
        self.assertIn(self.bad_dir, self._report_folders())
        self.assertFalse(os.path.exists(self.bad_file))  # quarantined out of the library
        # Everything that was still present (the clean folder) is now resolved
        # in state too -- a repeat tick with no changes is a no-op.
        rc = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(self._report_folders(), set())


class ApplyDecisionsTest(unittest.TestCase):
    """
    --apply-decisions: replays a human's audiobook/music verdicts (from
    review_quarantine.py) for folders already sitting in quarantine --
    the other half of the suspect-tier workflow.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bindery_bouncer_apply_test_")
        self.quarantine_dir = os.path.join(self.tmp, "_bindery_bouncer_quarantine")

        # A folder that was quarantined but turns out, on a human listen, to
        # really be the correct audiobook (single misleading signal, e.g. an
        # oddly-tagged genre) -- should get RESTORED.
        self.audiobook_rel = os.path.join("Some Author", "Actually Fine Book")
        self.audiobook_quarantined = os.path.join(self.quarantine_dir, self.audiobook_rel)
        os.makedirs(self.audiobook_quarantined)
        with open(os.path.join(self.audiobook_quarantined, "01.mp3"), "wb") as f:
            f.write(b"fake narration")

        # A folder that a human listen confirms really is misfiled music --
        # should get DELETED, with the loop closed with Bindery.
        self.music_rel = os.path.join("Harry Turtledove", "Brothers in Arms")
        self.music_quarantined = os.path.join(self.quarantine_dir, self.music_rel)
        os.makedirs(self.music_quarantined)
        self.music_file = os.path.join(self.music_quarantined, "01 - Track.mp3")
        with open(self.music_file, "wb") as f:
            f.write(b"fake music")

        # Bindery's catalogue only ever knows about the folder's *original*
        # location -- it has no idea this tool later quarantined it -- so
        # the book/history records point there, not into quarantine.
        original_music_path = os.path.join(self.tmp, self.music_rel, "01 - Track.mp3")
        MockBinderyHandler.books = [
            {"id": 1, "title": "Brothers in Arms", "author": {"name": "Harry Turtledove"},
             "mediaType": "audiobook", "editions": [{"formats": [{"path": original_music_path}]}]},
        ]
        MockBinderyHandler.history = [
            {"id": 501, "bookId": 1, "path": original_music_path, "event": "imported"},
        ]
        MockBinderyHandler.calls = []
        self.server = HTTPServer(("127.0.0.1", 0), MockBinderyHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        self.decisions_path = os.path.join(self.tmp, "decisions.csv")
        with open(self.decisions_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["relative_path", "decision"])
            w.writerow([self.audiobook_rel, "audiobook"])
            w.writerow([self.music_rel, "music"])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, extra_args=None):
        args = [
            "--library-path", self.tmp,
            "--bindery-url", f"http://127.0.0.1:{self.port}/api/v1",
            "--bindery-api-key", "testkey",
            "--apply-decisions", self.decisions_path,
        ] + (extra_args or [])
        return run(args)

    def test_dry_run_touches_nothing(self):
        rc = self._run()
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self.audiobook_quarantined))
        self.assertTrue(os.path.exists(self.music_quarantined))
        self.assertEqual(MockBinderyHandler.calls, [])

    def test_execute_restores_audiobook_and_deletes_music(self):
        rc = self._run(["--execute"])
        self.assertEqual(rc, 0)
        restored = os.path.join(self.tmp, self.audiobook_rel, "01.mp3")
        self.assertTrue(os.path.exists(restored))
        self.assertFalse(os.path.exists(self.audiobook_quarantined))
        self.assertFalse(os.path.exists(self.music_quarantined))

    def test_execute_closes_the_loop_for_the_music_verdict_only(self):
        rc = self._run(["--execute"])
        self.assertEqual(rc, 0)
        methods_paths = [(m, p) for m, p, _ in MockBinderyHandler.calls]
        self.assertIn(("POST", "/api/v1/history/501/blocklist"), methods_paths)
        self.assertIn(("PUT", "/api/v1/book/1"), methods_paths)
        self.assertIn(("POST", "/api/v1/book/1/search"), methods_paths)

    def test_no_close_loop_flag_is_honored(self):
        rc = self._run(["--execute", "--no-close-loop"])
        self.assertEqual(rc, 0)
        self.assertEqual(MockBinderyHandler.calls, [])
        self.assertFalse(os.path.exists(self.music_quarantined))  # delete itself still happens

    def test_missing_quarantine_folder_is_reported_not_crashed(self):
        # Simulates re-running apply-decisions after it already ran once.
        shutil.rmtree(self.audiobook_quarantined)
        rc = self._run(["--execute"])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.music_quarantined))  # the other row still applies fine


if __name__ == "__main__":
    unittest.main()
