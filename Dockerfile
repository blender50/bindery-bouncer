# bindery-bouncer: run as a one-shot container, not a long-running service.
# Invoke it with `docker run --rm ...` (or `docker compose run --rm ...`)
# whenever you want to scan -- see README.md's Docker section.

FROM python:3.11-slim

# ffmpeg: used to sample short windows out of audio files for content analysis
# without decoding entire multi-hour audiobooks (see bindery_bouncer/listen.py).
# libsndfile1: belt-and-suspenders for the `soundfile` package -- most pip
# wheels bundle it already, but this guarantees it's there regardless of
# platform/arch.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bindery_bouncer/ ./bindery_bouncer/

# No CMD -- always pass the real arguments (--library-path, --bindery-url,
# --execute, etc.) after the image name / compose service.
ENTRYPOINT ["python3", "-m", "bindery_bouncer"]
