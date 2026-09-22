"""
Unit tests for assess_folder()'s tier logic in scorer.py -- specifically the
"confirmed" rule.

Regression test for a real bug found calibrating against Steve's live
~1700-book library: the module docstring says "two independent signals
agreeing is required for the 'confirmed' tier", but the code required
strong_audio to specifically be one of those two. On real data, the audio
DSP score never crossed the 0.68 default threshold even on since-confirmed
real misfilings (Andy McNab's "Deep Black" and "Exit Wound" scored
0.31-0.54 despite genuinely containing Death Metal/Hardcore tracks), which
made "confirmed" unreachable regardless of how strong the tag-genre and
metadata-mismatch signals were. Since those two signals are independently
derived (one from the embedded genre field, one from comparing embedded
artist/album tags against Bindery's catalogue), two of them agreeing is
real corroboration without needing audio's help.

Run with: python3 -m unittest tests.test_scorer -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bindery_bouncer.bindery_client import BookRecord  # noqa: E402
from bindery_bouncer.listen import AudioSignal  # noqa: E402
from bindery_bouncer.scorer import ScoringConfig, assess_folder  # noqa: E402
from bindery_bouncer.tags import TrackTags  # noqa: E402


def _track(title=None, artist=None, album=None, genre=None):
    return TrackTags(path="/fake/path.mp3", duration_s=60.0, title=title,
                      artist=artist, album=album, genre=genre)


def _weak_audio():
    # Below the 0.68 default threshold -- mirrors the real Deep Black/Exit
    # Wound scores (0.31-0.54) despite those being genuinely music.
    return [AudioSignal(path="/fake/path.mp3", music_score=0.45)]


class ConfirmedTierTest(unittest.TestCase):
    def setUp(self):
        self.expected = BookRecord(
            id=1, title="Deep Black", author="Andy McNab",
            asin=None, isbn=None, media_type="audiobook",
            paths=["/fake/path.mp3"],
        )

    def test_tag_and_metadata_agreeing_reach_confirmed_without_audio(self):
        # The actual real-world case: genre says music, artist is a clearly
        # different person than the book's author, and audio never crosses
        # threshold. This must be "confirmed", not "suspect".
        tags = [_track(title="Into The Deep Black", artist="Dark Redeemer",
                        album="Into The Deep Black", genre="Death Metal")]
        assessment = assess_folder("/fake", tags, _weak_audio(), self.expected)
        self.assertEqual(assessment.tier, "confirmed")

    def test_single_signal_alone_still_only_earns_suspect(self):
        # Genre says music but there's no catalogue record to compare
        # metadata against -- only one signal, should stay "suspect".
        tags = [_track(title="Some Track", artist="Some Band",
                        album="Some Album", genre="Techno")]
        assessment = assess_folder("/fake", tags, _weak_audio(), None)
        self.assertEqual(assessment.tier, "suspect")

    def test_audio_can_still_be_the_second_signal(self):
        # Audio + one other signal (the original path) must still work.
        strong_audio = [AudioSignal(path="/fake/path.mp3", music_score=0.9)]
        tags = [_track(title="Some Track", artist="Some Band",
                        album="Some Album", genre="Techno")]
        assessment = assess_folder("/fake", tags, strong_audio, None)
        self.assertEqual(assessment.tier, "confirmed")

    def test_audio_alone_never_reaches_confirmed(self):
        # Strong audio with no corroborating tag-genre or metadata signal at
        # all (unrecognized genre, no catalogue record to compare against)
        # -- corroborating == 0, so this must stay "suspect", not "confirmed".
        strong_audio = [AudioSignal(path="/fake/path.mp3", music_score=0.9)]
        tags = [_track(title="Some Track", artist="Some Band",
                        album="Some Album", genre="Some Made Up Genre")]
        assessment = assess_folder("/fake", tags, strong_audio, None)
        self.assertEqual(assessment.tier, "suspect")

    def test_no_signals_at_all_is_clean(self):
        # No genre tag, no catalogue record to compare against at all --
        # nothing for any signal to fire on.
        tags = [_track(title="Chapter 1")]
        assessment = assess_folder("/fake", tags, [], None)
        self.assertEqual(assessment.tier, "clean")


if __name__ == "__main__":
    unittest.main()
