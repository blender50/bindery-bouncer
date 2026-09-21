"""
Embedded audio tag extraction.

Reads whatever ID3/MP4/Vorbis tags a file has and normalizes them into a
plain dict, regardless of container format (mp3, m4a/m4b, flac, ogg, opus).
This is the "cheap" signal -- fast, no audio decoding required -- used
alongside the audio-content analysis in listen.py.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from mutagen import File as MutagenFile

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus", ".wav"}

# Genres that strongly indicate spoken-word / audiobook content.
AUDIOBOOK_GENRES = {
    "audiobook", "audiobooks", "spoken word", "speech", "books & spoken",
    "books and spoken", "podcast", "non-music", "nonmusic", "audio drama",
}

# Common music genre tags. Not exhaustive -- if a genre is present and NOT
# in AUDIOBOOK_GENRES and NOT unknown, we treat it as a music signal.
#
# This is deliberately broad, not just the "top level" genres: caught from a
# real false negative during calibration against a live ~1700-book library,
# two folders genuinely containing metal albums (genre tags "Death Metal"
# and "Hardcore") scored no signal at all here, because neither subgenre was
# on the original, much shorter list -- an unrecognized genre falls through
# to "no signal" (None) by design (see genre_is_music below), which is the
# safe default for an odd/unexpected tag, but is exactly the wrong default
# for a real, common music subgenre nobody thought to list. A whitelist like
# this can never be fully exhaustive, so if you hit another one, add it here.
KNOWN_MUSIC_GENRES = {
    "rock", "pop", "metal", "heavy metal", "hip-hop", "hip hop", "rap",
    "country", "jazz", "classical", "electronic", "dance", "r&b", "rnb",
    "soul", "funk", "blues", "reggae", "punk", "folk", "indie", "alternative",
    "alternative rock", "singer/songwriter", "singer-songwriter",
    "soundtrack", "world", "latin", "k-pop", "edm", "house", "techno",
    "disco", "grunge", "new wave", "ambient", "experimental",
    # Metal subgenres
    "death metal", "black metal", "doom metal", "doom", "sludge",
    "thrash metal", "thrash", "power metal", "symphonic metal",
    "folk metal", "nu metal", "metalcore", "deathcore", "grindcore",
    "gothic metal", "progressive metal", "prog metal", "industrial metal",
    # Hardcore / punk subgenres
    "hardcore", "hardcore punk", "punk rock", "pop punk", "post-hardcore",
    "screamo", "emo", "crust punk", "ska punk",
    # Electronic subgenres
    "trance", "psytrance", "dubstep", "drum and bass", "dnb",
    "drum & bass", "breakbeat", "garage", "uk garage", "jungle", "trap",
    "future bass", "synthwave", "vaporwave", "chillwave", "downtempo",
    "idm", "glitch", "industrial", "ebm", "deep house", "tech house",
    # Hip-hop / rap subgenres
    "drill", "grime", "boom bap",
    # Rock subgenres
    "indie rock", "classic rock", "hard rock", "soft rock", "prog rock",
    "progressive rock", "post-rock", "math rock", "psychedelic rock",
    "garage rock", "southern rock", "glam rock", "folk rock",
    # Pop subgenres
    "synthpop", "electropop", "dream pop", "art pop", "j-pop", "dance pop",
    # Other
    "bluegrass", "gospel", "christian rock", "worship", "opera",
    "orchestral", "film score", "video game music", "chiptune", "vgm",
    "dub", "dancehall", "afrobeat", "bossa nova", "samba", "salsa",
    "flamenco", "lounge", "easy listening", "swing", "big band",
    "americana", "noise", "drone", "musical theatre", "a cappella",
    "barbershop",
}


@dataclass
class TrackTags:
    path: str
    duration_s: float = 0.0
    title: Optional[str] = None
    artist: Optional[str] = None
    albumartist: Optional[str] = None
    album: Optional[str] = None
    genre: Optional[str] = None
    track_number: Optional[int] = None
    total_tracks: Optional[int] = None
    raw: dict = field(default_factory=dict)

    @property
    def genre_is_music(self) -> Optional[bool]:
        """True/False if the genre tag clearly indicates music/audiobook, None if unknown/absent."""
        if not self.genre:
            return None
        g = self.genre.strip().lower()
        if g in AUDIOBOOK_GENRES:
            return False
        if g in KNOWN_MUSIC_GENRES:
            return True
        return None


def _first(tag_obj, *keys):
    """Return the first non-empty value found for any of the given mutagen tag keys."""
    for k in keys:
        try:
            v = tag_obj.get(k)
        except Exception:
            v = None
        if v:
            if isinstance(v, list):
                v = v[0]
            v = str(v).strip()
            if v:
                return v
    return None


def read_tags(path: str) -> Optional[TrackTags]:
    """Read tags from a single audio file. Returns None if the file can't be parsed as audio."""
    try:
        mf = MutagenFile(path, easy=True)
    except Exception:
        mf = None
    if mf is None:
        return None

    duration = 0.0
    try:
        if mf.info is not None and getattr(mf.info, "length", None):
            duration = float(mf.info.length)
    except Exception:
        pass

    tags = mf.tags or {}
    title = _first(tags, "title")
    artist = _first(tags, "artist")
    albumartist = _first(tags, "albumartist", "album_artist", "performer")
    album = _first(tags, "album")
    genre = _first(tags, "genre")

    track_number = None
    total_tracks = None
    tn = _first(tags, "tracknumber")
    if tn:
        # tracknumber is often "3/12"
        parts = str(tn).split("/")
        try:
            track_number = int(parts[0])
            if len(parts) > 1:
                total_tracks = int(parts[1])
        except ValueError:
            pass

    return TrackTags(
        path=path,
        duration_s=duration,
        title=title,
        artist=artist,
        albumartist=albumartist,
        album=album,
        genre=genre,
        track_number=track_number,
        total_tracks=total_tracks,
        raw=dict(tags) if tags else {},
    )


def iter_audio_files(root: str):
    """Yield every audio file path under root, recursively."""
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext in AUDIO_EXTENSIONS:
                yield os.path.join(dirpath, fn)


def group_by_folder(root: str) -> dict[str, list[str]]:
    """Group audio file paths by their immediate parent folder (a Bindery 'book unit')."""
    groups: dict[str, list[str]] = {}
    for path in iter_audio_files(root):
        folder = os.path.dirname(path)
        groups.setdefault(folder, []).append(path)
    return groups
