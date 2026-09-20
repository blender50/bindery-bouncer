"""
Audio content analysis ("listening").

Rather than trust file/tag metadata, this module samples short windows out
of an audio file (using ffmpeg for fast, format-agnostic seeking) and runs
DSP measures over the raw waveform to estimate how "music-like" it sounds:
a steady tempo, regular beat spacing, and real percussive energy are all
far more characteristic of a music track than of a narrated audiobook
chapter.

This is not a trained music/speech classifier -- it's a set of cheap,
explainable heuristics, deliberately kept inspectable (every raw feature
ends up in the report) so thresholds can be tuned against your own library
rather than trusted blindly. Combined with the tag and Bindery-catalogue
signals in scorer.py, it's meant to catch the obvious cases (a Dire Straits
album sitting where an audiobook chapter should be) without needing a
trained model or network calls per file.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional

import numpy as np
import soundfile as sf
import librosa

SAMPLE_RATE = 22050
WINDOW_SECONDS = 25.0
# Fractional offsets into the file to sample from, skipping likely
# silence/intro/outro at the very start and end.
SAMPLE_POINTS = (0.15, 0.45, 0.75)
MIN_WINDOW_SECONDS = 5.0

FFMPEG_BIN = shutil.which("ffmpeg")


@dataclass
class AudioSignal:
    path: str
    windows_analyzed: int = 0
    tempo_bpm: float = 0.0
    pulse_clarity: float = 0.0     # 0..1, higher = one sharp periodic pulse peak (rhythmic)
    percussive_ratio: float = 0.0  # 0..1, share of energy that is percussive (secondary/noisy signal)
    music_score: float = 0.0       # 0..1 combined estimate; higher = more music-like
    error: Optional[str] = None


def _extract_window(path: str, offset_s: float, duration_s: float) -> Optional[np.ndarray]:
    """Pull one short mono PCM window out of an audio file at a given offset via ffmpeg."""
    if FFMPEG_BIN is None:
        raise RuntimeError(
            "ffmpeg not found on PATH -- required to sample audio for content analysis. "
            "Install it (e.g. `apt-get install ffmpeg`, or it's already on most Unraid docker images)."
        )
    fd, tmp_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        cmd = [
            FFMPEG_BIN, "-nostdin", "-y",
            "-ss", str(max(offset_s, 0.0)),
            "-t", str(duration_s),
            "-i", path,
            "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-f", "wav", tmp_path,
        ]
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
        if result.returncode != 0 or not os.path.exists(tmp_path):
            return None
        data, sr = sf.read(tmp_path, dtype="float32")
        if data.size == 0:
            return None
        if data.ndim > 1:
            data = data.mean(axis=1)
        if sr != SAMPLE_RATE:
            data = librosa.resample(data, orig_sr=sr, target_sr=SAMPLE_RATE)
        return data
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _periodicity_from_autocorrelation(onset_env: np.ndarray, sr: int) -> tuple[float, float]:
    """
    Measure genuine rhythmic periodicity in an onset-strength envelope.

    IMPORTANT: this deliberately does NOT use librosa.beat.beat_track's output
    beat times to judge regularity. beat_track fits a tempo via dynamic
    programming and will emit a near-uniformly-spaced beat grid *even when the
    input has no real periodic pulse* (confirmed empirically: random/noise-like
    input still comes out with low inter-beat-interval variance, because the
    tracker is designed to always commit to a best-guess tempo). Measuring
    "regularity" from its output is circular and doesn't discriminate.

    Instead we autocorrelate the onset envelope itself and look at how sharp
    the tallest peak is, within a plausible music-tempo lag range, relative to
    the surrounding autocorrelation floor. A real rhythmic pulse produces one
    tall, narrow peak; noise or unstructured speech produces a comparatively
    flat autocorrelation with no standout peak.

    Returns (tempo_bpm, clarity 0..1).
    """
    hop_length = 512
    frame_rate = sr / hop_length
    min_bpm, max_bpm = 40.0, 220.0
    min_lag = max(int(frame_rate * 60.0 / max_bpm), 1)
    max_lag = min(int(frame_rate * 60.0 / min_bpm), len(onset_env) - 1)
    if max_lag <= min_lag + 2:
        return 0.0, 0.0

    onset_env = onset_env - onset_env.mean()
    ac = librosa.autocorrelate(onset_env, max_size=max_lag + 1)
    if ac.size <= max_lag or not np.any(ac[min_lag:max_lag]):
        return 0.0, 0.0

    window = ac[min_lag:max_lag]
    peak_idx = int(np.argmax(window)) + min_lag
    peak_val = float(ac[peak_idx])
    baseline = float(np.median(np.abs(window)) + 1e-9)
    spread = float(np.std(window) + 1e-9)

    # Prominence of the peak above the local floor, scaled into 0..1.
    prominence = (peak_val - baseline) / (peak_val + baseline + 1e-9)
    sharpness = np.clip(spread / (abs(peak_val) + 1e-9), 0.0, 1.0)
    clarity = float(np.clip(prominence * (0.5 + 0.5 * (1.0 - sharpness)), 0.0, 1.0))
    # Guard against a negative or noise-floor peak being reported as clear.
    if peak_val <= 0:
        clarity = 0.0

    tempo_bpm = float(frame_rate * 60.0 / peak_idx) if peak_idx > 0 else 0.0
    return tempo_bpm, clarity


def _score_window(y: np.ndarray) -> Optional[tuple[float, float, float]]:
    """Return (tempo_bpm, pulse_clarity, percussive_ratio) for one window, or None."""
    try:
        onset_env = librosa.onset.onset_strength(y=y, sr=SAMPLE_RATE)
        if not np.any(onset_env):
            return None

        tempo_val, clarity = _periodicity_from_autocorrelation(onset_env, SAMPLE_RATE)

        y_harm, y_perc = librosa.effects.hpss(y)
        harm_energy = float(np.sum(y_harm ** 2))
        perc_energy = float(np.sum(y_perc ** 2))
        total_energy = harm_energy + perc_energy + 1e-9
        perc_ratio = float(np.clip(perc_energy / total_energy, 0.0, 1.0))

        return tempo_val, clarity, perc_ratio
    except Exception:
        return None


def analyze_file(path: str, duration_s: float) -> AudioSignal:
    """Sample a few windows of `path` and score how music-like the audio sounds."""
    sig = AudioSignal(path=path)

    if duration_s <= 0:
        sig.error = "unknown duration, skipped"
        return sig

    windows: list[np.ndarray] = []
    for frac in SAMPLE_POINTS:
        offset = max(duration_s * frac, 0.0)
        win_len = min(WINDOW_SECONDS, max(duration_s - offset - 1.0, 0.0))
        if win_len < MIN_WINDOW_SECONDS:
            continue
        try:
            y = _extract_window(path, offset, win_len)
        except RuntimeError as e:
            sig.error = str(e)
            return sig
        if y is not None and len(y) > SAMPLE_RATE * 3:
            windows.append(y)

    if not windows:
        sig.error = "no usable audio windows extracted"
        return sig

    results = [r for r in (_score_window(y) for y in windows) if r is not None]
    if not results:
        sig.error = "DSP analysis failed on all sampled windows"
        return sig

    tempos, clarities, perc_ratios = (np.array(x) for x in zip(*results))

    sig.windows_analyzed = len(results)
    sig.tempo_bpm = float(np.median(tempos))
    sig.pulse_clarity = float(np.median(clarities))
    sig.percussive_ratio = float(np.median(perc_ratios))

    # pulse_clarity (a genuinely periodic onset pattern, at a plausible music
    # tempo) is the ONLY signal that showed real separation when validated
    # against synthetic music vs. speech-like audio during development
    # (music ~0.75, speech-like proxies ~0.49-0.51). percussive_ratio via
    # harmonic/percussive source separation was tested as a second signal
    # but turned out to be *counter*-indicative here -- broadband/noisy
    # non-music content scored higher on it than the clean music sample did
    # (HPSS tends to dump anything non-tonal into the "percussive" bucket,
    # not just drum hits). It's kept in the report for your own inspection
    # and possible re-weighting, but deliberately excluded from the score.
    tempo_plausible = 1.0 if 55.0 <= sig.tempo_bpm <= 200.0 else 0.4
    sig.music_score = float(np.clip(sig.pulse_clarity * tempo_plausible, 0.0, 1.0))
    return sig
