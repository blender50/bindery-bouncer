"""
Regression test for TrackTags.genre_is_music (see tags.py's KNOWN_MUSIC_GENRES).

Caught from a real false negative during calibration against a live
~1700-book library: two folders genuinely containing metal albums
(genre tags "Death Metal" and "Hardcore") produced no genre signal at all,
because those subgenres weren't on the original, much shorter whitelist --
an unrecognized genre silently falls through to "no signal" rather than
"music", which is the safe default for a genuinely odd tag but the wrong
one for an ordinary, common subgenre nobody had listed yet.

This doesn't try to enumerate every possible genre (that whitelist can
never be complete -- see the comment above it in tags.py), just the
specific tags that were caught missing, plus a representative sample
across each genre family that was added, so a future edit that
accidentally shrinks the list gets caught here instead of on a real scan.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bindery_bouncer.tags import TrackTags  # noqa: E402


def _genre_is_music(genre):
    return TrackTags(path="x", genre=genre).genre_is_music


class GenreClassificationTest(unittest.TestCase):
    def test_the_actual_missed_tags_are_now_recognized_as_music(self):
        # These are the exact genre strings from the two real folders that
        # were wrongly marked "clean" during calibration.
        self.assertTrue(_genre_is_music("Death Metal"))
        self.assertTrue(_genre_is_music("Hardcore"))

    def test_case_and_whitespace_insensitive(self):
        self.assertTrue(_genre_is_music("  death metal  "))
        self.assertTrue(_genre_is_music("DEATH METAL"))

    def test_representative_sample_across_added_genre_families(self):
        for genre in (
            "Black Metal", "Doom Metal", "Metalcore", "Deathcore",  # metal
            "Hardcore Punk", "Pop Punk", "Screamo", "Emo",  # hardcore/punk
            "Dubstep", "Drum and Bass", "Synthwave", "Trap",  # electronic
            "Drill", "Grime",  # hip-hop
            "Hard Rock", "Post-Rock", "Psychedelic Rock",  # rock
            "Synthpop", "Dream Pop",  # pop
            "Gospel", "Opera", "Bossa Nova", "Flamenco",  # other
        ):
            with self.subTest(genre=genre):
                self.assertTrue(_genre_is_music(genre), f"{genre!r} should be recognized as music")

    def test_audiobook_genres_still_classified_as_not_music(self):
        for genre in ("Audiobook", "Spoken Word", "Podcast", "Audio Drama"):
            with self.subTest(genre=genre):
                self.assertFalse(_genre_is_music(genre), f"{genre!r} should still be non-music")

    def test_unrecognized_genre_is_no_signal_not_a_false_positive(self):
        # Deliberately conservative: an odd/unlisted tag (e.g. a book's own
        # literary genre, like "Thriller") must stay None (no signal), not
        # get assumed to be music -- that's the safe default this whitelist
        # approach depends on.
        self.assertIsNone(_genre_is_music("Thriller"))
        self.assertIsNone(_genre_is_music("Some Made Up Genre"))

    def test_no_genre_tag_is_no_signal(self):
        self.assertIsNone(_genre_is_music(None))
        self.assertIsNone(_genre_is_music(""))


if __name__ == "__main__":
    unittest.main()
