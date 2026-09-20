"""
Combine the three independent signals -- tag genre, Bindery catalogue
metadata, and audio content analysis -- into a per-folder verdict.

Deliberately rule-based rather than a single weighted-sum threshold: a
single noisy signal (especially the audio DSP score, which genuinely
overlaps between real music and non-music content -- see listen.py's
docstring and the README's calibration section) should never by itself be
enough to delete a file. Two independent signals agreeing is required for
the "confirmed" tier; one signal alone only ever earns "suspect"
(quarantine). An explicit audiobook genre tag is treated as a protective
signal that suppresses a false-positive flag unless the catalogue
metadata actively contradicts it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mode, StatisticsError
from typing import Optional

from rapidfuzz import fuzz

from .bindery_client import BookRecord
from .listen import AudioSignal
from .tags import TrackTags


@dataclass
class ScoringConfig:
    audio_strong_threshold: float = 0.68
    meta_strong_threshold: float = 0.6


@dataclass
class FolderAssessment:
    folder: str
    tier: str  # "confirmed" | "suspect" | "clean"
    reasons: list[str] = field(default_factory=list)
    audio_music_score: Optional[float] = None
    tag_genre_music_score: Optional[float] = None
    metadata_mismatch_score: Optional[float] = None
    expected_title: Optional[str] = None
    expected_author: Optional[str] = None
    observed_genre: Optional[str] = None
    observed_artist: Optional[str] = None
    observed_album: Optional[str] = None
    has_bindery_match: bool = False
    sample_files: list[str] = field(default_factory=list)


def compute_tag_genre_score(tags_list: list[TrackTags]) -> tuple[Optional[float], Optional[str]]:
    """1.0 = genre tag says music, 0.0 = genre tag says audiobook/spoken-word, None = no signal."""
    votes = [t.genre_is_music for t in tags_list if t.genre_is_music is not None]
    genres_seen = [t.genre for t in tags_list if t.genre]
    if not votes:
        return None, (genres_seen[0] if genres_seen else None)
    try:
        winner = mode(votes)
    except StatisticsError:
        winner = votes[0]
    return (1.0 if winner else 0.0), (genres_seen[0] if genres_seen else None)


def compute_metadata_mismatch(
    tags_list: list[TrackTags], expected: Optional[BookRecord]
) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """
    Compare embedded album/artist tags against what Bindery's catalogue expects
    for this book. Returns (mismatch_score 0..1 or None, observed_artist, observed_album).
    1.0 = tags look nothing like the expected title/author. None = no catalogue match to compare against.

    Author/artist similarity is what actually decides this, not title. Taking
    the best similarity across title-or-author (the obvious first approach)
    is wrong for exactly the failure mode this tool exists to catch: a music
    album can coincidentally share an audiobook's title (Dire Straits'
    "Brothers in Arms" vs. the book of the same name) and that coincidental
    title match would paper over a completely wrong artist. So a confident
    author mismatch always wins; title is only used as a fallback when there's
    no artist tag to compare at all.
    """
    albums = [t.album for t in tags_list if t.album]
    artists = [t.artist or t.albumartist for t in tags_list if (t.artist or t.albumartist)]
    observed_album = albums[0] if albums else None
    observed_artist = artists[0] if artists else None

    if expected is None or not (expected.title or expected.author):
        return None, observed_artist, observed_album

    author_sim = None
    if expected.author and observed_artist:
        author_sim = fuzz.token_sort_ratio(expected.author, observed_artist) / 100.0

    title_candidates = []
    if expected.title and observed_album:
        title_candidates.append(fuzz.token_sort_ratio(expected.title, observed_album) / 100.0)
    if expected.title and tags_list and tags_list[0].title:
        title_candidates.append(fuzz.token_sort_ratio(expected.title, tags_list[0].title) / 100.0)
    title_sim = max(title_candidates) if title_candidates else None

    if author_sim is None and title_sim is None:
        return None, observed_artist, observed_album

    if author_sim is not None:
        mismatch = 1.0 - author_sim
        # A wrong title on top of a wrong author only reinforces the call;
        # it never gets to override a bad author match with a coincidental
        # title match.
        if title_sim is not None:
            mismatch = max(mismatch, (1.0 - title_sim) * 0.5)
    else:
        mismatch = 1.0 - title_sim

    return mismatch, observed_artist, observed_album


def assess_folder(
    folder: str,
    tags_list: list[TrackTags],
    audio_signals: list[AudioSignal],
    expected: Optional[BookRecord],
    config: ScoringConfig = ScoringConfig(),
) -> FolderAssessment:
    valid_audio = [a.music_score for a in audio_signals if a.error is None]
    audio_score = (sum(valid_audio) / len(valid_audio)) if valid_audio else None

    tag_genre_score, observed_genre = compute_tag_genre_score(tags_list)
    meta_mismatch, observed_artist, observed_album = compute_metadata_mismatch(tags_list, expected)

    strong_audio = audio_score is not None and audio_score >= config.audio_strong_threshold
    strong_tag_music = tag_genre_score == 1.0
    strong_tag_audiobook = tag_genre_score == 0.0
    strong_meta_mismatch = meta_mismatch is not None and meta_mismatch >= config.meta_strong_threshold

    reasons: list[str] = []
    if strong_audio:
        reasons.append(f"audio content analysis looks music-like (score {audio_score:.2f})")
    if strong_tag_music:
        reasons.append(f"embedded genre tag is a music genre ({observed_genre!r})")
    if strong_tag_audiobook:
        reasons.append(f"embedded genre tag says audiobook/spoken-word ({observed_genre!r})")
    if strong_meta_mismatch:
        reasons.append(
            f"tags ({observed_artist!r} / {observed_album!r}) don't match Bindery's catalogue "
            f"record ({expected.author!r} / {expected.title!r})"
        )
    if expected is None:
        reasons.append("no matching Bindery catalogue record found for this folder/path")

    corroborating = sum([strong_tag_music, strong_meta_mismatch])

    if strong_tag_audiobook and not strong_meta_mismatch:
        # Explicit protective signal: don't flag on audio-score alone when the
        # file's own genre tag says audiobook and the catalogue doesn't disagree.
        tier = "clean"
    elif strong_audio and corroborating >= 1:
        tier = "confirmed"
    elif strong_audio or strong_tag_music or strong_meta_mismatch:
        tier = "suspect"
    else:
        tier = "clean"

    return FolderAssessment(
        folder=folder,
        tier=tier,
        reasons=reasons,
        audio_music_score=audio_score,
        tag_genre_music_score=tag_genre_score,
        metadata_mismatch_score=meta_mismatch,
        expected_title=expected.title if expected else None,
        expected_author=expected.author if expected else None,
        observed_genre=observed_genre,
        observed_artist=observed_artist,
        observed_album=observed_album,
        has_bindery_match=expected is not None,
        sample_files=[t.path for t in tags_list[:3]],
    )
