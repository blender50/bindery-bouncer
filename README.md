# bindery-bouncer

Finds audio files sitting in a [Bindery](https://github.com/vavallee/bindery)
audiobook library that are actually music -- e.g. Dire Straits' *Brothers in
Arms* filed where the audiobook of the same name should be -- and lets you
clear them out with a confidence tier instead of one all-or-nothing guess.

## TL;DR: flag reference

Quick reference -- full context for each flag is in the sections below.
Everything here works tacked onto `docker compose run --rm bindery-bouncer
...`; `--library-path` is already baked in as `/audiobooks` by the compose
file, so you only need the rest.

| Flag | Default | What it does |
|---|---|---|
| `--library-path` | *(required)* | Root folder of your audiobook library to scan. Baked in as `/audiobooks` by `docker-compose.yml`. |

**Bindery API (catalogue cross-check)**

| Flag | Default | What it does |
|---|---|---|
| `--bindery-url` | `$BINDERY_URL` | Bindery's API base URL, e.g. `http://blender:8787/api/v1`. |
| `--bindery-api-key` | `$BINDERY_API_KEY` | Bindery API key (Settings -> API in the UI). |
| `--bindery-status` | `imported` | Book status pulled for the catalogue cross-check. Bindery's real enum is `imported`/`wanted`, not `downloaded`. |
| `--no-bindery` | off | Skip the catalogue cross-check entirely -- tags + audio only. Loses the strongest signal (author comparison); expect more `suspect`, fewer `confirmed`. |
| `--dump-sample` | off | Print one raw page of Bindery's `/book` response and exit, for checking `bindery_client.py`'s field guesses against your instance. |
| `--bindery-path-prefix` | none | Path prefix as Bindery's own container sees your library, if different from `--library-path`. **Don't assume it matches -- verify with `--dump-sample`** (see the path-prefix section below). |
| `--local-path-prefix` | `--library-path` | Path prefix as this script sees your library. |

**Audio content analysis**

| Flag | Default | What it does |
|---|---|---|
| `--skip-audio` | off | Skip DSP audio analysis -- fast, tag+catalogue only (audio sampling is the slowest step). |
| `--samples-per-folder` | `2` | How many files per folder to run audio analysis on. |
| `--audio-strong-threshold` | `0.68` | `music_score` at/above this counts as strong audio evidence. Tune against your own library -- see Calibrate below. |
| `--meta-strong-threshold` | `0.6` | Tag-vs-catalogue mismatch score at/above this counts as strong metadata evidence. |

**What to do with flagged folders**

| Flag | Default | What it does |
|---|---|---|
| `--execute` | off (dry run) | Actually move/delete files. Without it, everything is dry-run -- report written, nothing on disk changes. |
| `--confirmed-action` | `delete` | Action for the `confirmed` tier: `delete` / `quarantine` / `report`. |
| `--suspect-action` | `quarantine` | Action for the `suspect` tier: `delete` / `quarantine` / `report`. |
| `--quarantine-dir` | `<library-path>/_bindery_bouncer_quarantine` | Where quarantined folders go. |

**Closing the loop with Bindery** (only after an actual delete, never a quarantine)

| Flag | Default | What it does |
|---|---|---|
| `--no-close-loop` | off | Don't blocklist the release or re-want the book after a delete -- just delete the file and stop. |
| `--no-search-after-blocklist` | off | After blocklisting + re-wanting, don't trigger an immediate re-search -- let Bindery's normal sweep (~12h) pick it up. |

**Scheduled / incremental scanning**

| Flag | Default | What it does |
|---|---|---|
| `--new-only` | off | Only assess folders changed since the last `--new-only` run and quiet for `--min-quiet-seconds`. Meant for a cron/scheduled invocation -- see "Running it automatically" below. |
| `--min-quiet-seconds` | `3600` | In `--new-only` mode, how many seconds a folder's newest file must sit untouched before it's eligible (guards against catching a mid-download/import folder). |
| `--state-file` | `<library-path>/.bindery_bouncer_state.json` | In `--new-only` mode, where already-handled folders are remembered. |

**Output**

| Flag | Default | What it does |
|---|---|---|
| `--report-csv` | `bindery_bouncer_report_<timestamp>.csv` in cwd | CSV report path. |
| `--verbose` | off | Print every folder's verdict as it's scanned, not just flagged ones. |

## Why this exists

Bindery's audiobook release matching has a known gap
([vavallee/bindery#2470](https://github.com/vavallee/bindery/issues/2470)):
when it grabs a release without a specific book ID attached (which is exactly
what happens from the global Search page), its matcher doesn't reliably read
the release title back, so a music album that happens to share a book's title
can get pulled in and imported as if it were the audiobook. There's no
Bindery setting that prevents this today -- it's a gap in the matcher itself.

Rather than try to get the *pre-download* match right (the indexer search
step usually has nothing better than a title to go on), this tool checks
*after the fact*: it looks at what's actually on disk in your library,
compares it against what Bindery's own catalogue says that book should be,
and listens to the audio itself to see if it sounds like a mistake.

## How it decides

Three independent signals, each looked at separately:

1. **Embedded tags** -- genre, artist, album. A `genre: Rock` tag on a file
   inside an audiobook folder is a strong tell on its own.
2. **Bindery's catalogue** -- pulled live from your Bindery instance's API.
   The file's artist/album tags are compared against the author/title Bindery
   expects for that book. Author is what actually decides this, not title --
   see the note below on why.
3. **Audio content analysis** -- the file is sampled (not fully decoded; a
   few 25-second windows spread through the file) and scored on how
   rhythmically periodic it sounds. A steady, clear musical pulse is
   characteristic of music; narrated speech doesn't produce one.

These are combined with a rule, not a single blended score:

- **confirmed** -- the audio sounds music-like *and* at least one other
  signal agrees (wrong genre tag, or catalogue mismatch). Default action:
  **delete**.
- **suspect** -- exactly one signal fired on its own. Default action:
  **quarantine** (moved to a `_bindery_bouncer_quarantine` folder inside your
  library, nothing deleted).
- **clean** -- nothing corroborated, or the file's own genre tag explicitly
  says audiobook/spoken-word and the catalogue doesn't contradict it (this is
  a deliberate protective rule against false positives).

## Closing the loop with Bindery (deletes only)

Deleting the bad file isn't the end of it -- left alone, Bindery could grab
the exact same wrong release again next sweep, and the book's status doesn't
automatically reflect that it's now missing. So on an **actual delete**
(never on quarantine -- the file's still there, so the book isn't really
missing yet), this tool also, by default:

1. Finds the history entry (grab/import record) that put the bad file there
   and blocklists it via `POST /api/v1/history/{id}/blocklist`, so Bindery
   won't grab that release again.
2. Re-wants the book via `PUT /api/v1/book/{id}`, so it's back on Bindery's
   radar instead of sitting there marked done with nothing on disk.
3. Triggers an immediate re-search via `POST /api/v1/book/{id}/search`,
   rather than waiting for the default 12-hour sweep.

`--no-close-loop` turns off all three and just deletes the file.
`--no-search-after-blocklist` keeps the blocklist + re-want but leaves the
timing to the normal sweep instead of firing an immediate search per
deletion (worth using if you're clearing out a large backlog in one run and
don't want to hammer your indexers).

Same honesty note as the book-catalogue lookup: Bindery's exact field names
for a history entry and the `PUT /book/{id}` body aren't published either.
This is implemented the same defensive way (search the JSON for plausible
field names rather than assume), but it's the least-tested part of the
tool -- check the CSV report's `bindery_loop` column after your first real
`--execute` run to confirm it actually did what it says.

### Why author, not title, drives the catalogue check

The whole failure mode this tool exists for is a **title collision** between
a book and an album. So when a file's album tag matches the expected book
title, that match is *not* trusted on its own -- it's exactly what you'd
expect to see in the broken case too. The artist tag vs. the book's author is
what actually catches it. This was a real bug caught while building this
tool: an earlier version took the *best* similarity across title-or-author
and got fooled by the coincidental title match in the "Brothers in Arms"
scenario. See `tests/test_integration.py` for the regression test.

## Running it automatically as new audiobooks arrive

This is still a one-shot tool, not a service -- there's no daemon to keep
running. Instead, `--new-only` makes each invocation cheap enough to put on
a schedule: it only assesses folders that have changed since the last
`--new-only` run *and* have gone quiet (no file changes) for at least
`--min-quiet-seconds` (default 1 hour, tune with that flag), so a freshly
grabbed audiobook gets checked automatically without rescanning your whole
library every tick, and without catching a folder mid-download/import.

```bash
docker compose run --rm bindery-bouncer --new-only --execute --confirmed-action quarantine
```

It remembers what it's already handled in a small JSON state file (default
`<library-path>/.bindery_bouncer_state.json`, override with `--state-file`)
-- keyed on each folder's newest-file mtime and file count, so a re-grab of
the same book (new mtime) gets reconsidered. Importantly, a folder only
gets marked "done" once it's genuinely resolved: a `clean` verdict, or an
actual `--execute`'d delete/quarantine. A folder flagged `confirmed` or
`suspect` during a dry run is *not* marked done, so it keeps showing up on
every tick until you either act on it (turn on `--execute`) or the folder's
contents change -- nothing gets silently swallowed just because a scheduled
run already looked at it once.

**Why polling instead of watching the folder live:** Unraid's `/mnt/user`
share is a FUSE overlay (shfs), and filesystem-watch APIs like inotify are
well known to be unreliable on it -- it's the same reason Sonarr/Radarr/
Readarr all recommend polling over real-time monitoring on Unraid. A cron
tick that just checks mtimes avoids that whole class of missed-event bugs.

**Wiring up the schedule on Unraid** (via the free "User Scripts" plugin
from Community Apps -- Settings -> User Scripts -> Add New Script):

```bash
#!/bin/bash
cd /mnt/user/appdata/compose.manager/projects/bindery-bouncer
docker compose run --rm bindery-bouncer --new-only --execute --confirmed-action quarantine
```

Set its schedule to "Custom" with a cron expression like `*/15 * * * *`
(every 15 minutes) or `0 * * * *` (hourly) -- since `--min-quiet-seconds`
already gates *when* a folder becomes eligible, ticking more often than
that just means less latency once something goes quiet, not repeated work.

Only turn this on after you trust the tool against your own library (see
calibration below), and start with `--confirmed-action quarantine` rather
than `delete` here too, same as a manual run.

## Calibrate before you trust it

Be honest with yourself about what the audio analysis is: a cheap DSP
heuristic, not a trained classifier. During development it was validated
against synthetic music vs. speech-like audio (see `bindery_bouncer/listen.py`
docstrings) and showed a real but modest separation -- clean music scored
around 0.75, speech-like audio around 0.49-0.51, but a busier/noisier music
mix scored as low as 0.54. **It has not been validated against your actual
library.** Do this before trusting `--execute`:

1. Run a dry run first (the default -- see Usage below) and look at the CSV
   report's `audio_music_score` column across a chunk of your real,
   known-good audiobooks and a few known-bad ones if you have them.
2. Adjust `--audio-strong-threshold` to sit cleanly between those two
   distributions for your library.
3. Only then run with `--execute`, and start with `--confirmed-action
   quarantine` instead of `delete` for your first real pass.

## Install and run (Docker -- recommended, especially on Unraid)

This is a one-shot CLI, not a service -- there's nothing to leave running.
You build the image once, then invoke it by hand whenever you actually want
to scan. That also sidesteps Unraid's OS being non-persistent (it boots from
USB into RAM, so anything `pip install`ed straight onto the array host
doesn't survive a reboot) and librosa's heavier compiled dependency chain
(numpy/scipy/numba) -- both are solved once, in the image, rather than on
every host you might run this from.

```bash
git clone <this repo>
cd bindery-bouncer
cp .env.example .env
# edit .env: AUDIOBOOKS_PATH (host path to your library), BINDERY_URL, BINDERY_API_KEY
docker compose build
```

**Sanity-check the API integration first** (Bindery's exact JSON field names
for a book record aren't published -- this prints one raw response so you can
confirm the guesses in `bindery_client.py`'s `FIELD_CANDIDATES` actually match
your instance):

```bash
docker compose run --rm bindery-bouncer --dump-sample
```

**Then a dry run** (default -- nothing on disk is touched, just a report --
note the report lands at `/audiobooks/bindery_bouncer_report_*.csv` *inside*
the container's view of your library unless you pass `--report-csv`, so it's
actually sitting in your library folder on the host afterwards):

```bash
docker compose run --rm bindery-bouncer --verbose
```

Check the printed summary and the CSV report. Once you trust the calls it's
making:

```bash
docker compose run --rm bindery-bouncer --execute --confirmed-action quarantine
```

Move to `--confirmed-action delete` only once you've reviewed a few
quarantine runs and are comfortable with the false-positive rate on your own
library. Every flag from `python3 -m bindery_bouncer --help` (see below)
works the same way tacked onto `docker compose run --rm bindery-bouncer ...`
-- `--library-path` is already baked in as `/audiobooks` by the compose file,
so you don't need to pass it yourself.

**Don't assume the mount points line up -- check.** It's tempting to think
that if Bindery is also a container on blender, and its bind-mount
destination happens to also be named `/audiobooks`, the paths just line up.
On Steve's real setup they didn't: `--dump-sample` on a book with a
populated `audiobookFilePath` showed Bindery reporting paths under
`/data/media/audiobooks/...`, a completely different root than this
container's own `/audiobooks` mount -- and with `--bindery-path-prefix`
unset (the default), every local folder path failed to match anything in
the catalogue index, silently, with no error and no warning. The catalogue
cross-check (the strongest signal this tool has) was effectively disabled
for the whole library, and it looked indistinguishable from "nothing wrong
here" in the output.

`docker-compose.yml` now bakes in the value confirmed correct for Steve's
own Bindery instance (`--bindery-path-prefix /data/media/audiobooks`). If
you're setting this up against a different Bindery instance, don't trust
that value or the "matching convention" assumption -- verify for yourself:
run `--dump-sample`, or better, fetch a book you know has an audiobook file
and look at its raw `audiobookFilePath`, and compare that prefix against
what this container's `/audiobooks` mount actually corresponds to on your
host. Only skip `--bindery-path-prefix` once you've confirmed the two
really do match.

## Running without Docker

```bash
git clone <this repo>
cd bindery-bouncer
pip install -r requirements.txt
```

Also needs `ffmpeg` on PATH (used to sample short windows out of files
without decoding entire multi-hour audiobooks) -- `apt-get install ffmpeg` if
it's not already there. Same commands as above, just `python3 -m
bindery_bouncer` in place of `docker compose run --rm bindery-bouncer`:

```bash
python3 -m bindery_bouncer --library-path /mnt/user/media/audiobooks \
  --bindery-url http://blender:8787/api/v1 \
  --bindery-api-key "$BINDERY_API_KEY" \
  --dump-sample
```

### If Bindery sees a different path than you do

Use `--bindery-path-prefix` / `--local-path-prefix` to translate, e.g. if
Bindery's container sees `/audiobooks/...` but you're running this script
against `/mnt/user/media/audiobooks/...`:

```bash
--bindery-path-prefix /audiobooks --local-path-prefix /mnt/user/media/audiobooks
```

### Running without the Bindery API

`--no-bindery` skips the catalogue cross-check entirely and relies on tags +
audio only. It works, but you lose the strongest signal (the author
comparison) -- expect more "suspect" and fewer "confirmed" results.

### All options

```bash
python3 -m bindery_bouncer --help
```

## Safety notes

- Every destructive action requires `--execute`; without it, everything is a
  dry run and only the CSV report is written.
- Deletes and quarantine moves are restricted to strictly inside
  `--library-path` -- the tool refuses to touch anything outside it, even if
  a path-matching bug somewhere tried to point it there.
- Quarantine preserves each folder's path relative to the library root, so
  nothing gets mixed together or overwritten.
- There's no undo for `--confirmed-action delete`. Quarantine first.
- Closing the loop (blocklist/re-want/search) only ever runs *after* a
  successful delete, and a failure there is reported in the CSV's
  `bindery_loop` column rather than treated as the delete having failed --
  the file is gone either way, so the run doesn't stop or roll back.

## Running the tests

```bash
python3 -m unittest tests.test_integration -v
```

This builds a small synthetic library (one folder that's genuinely a music
album mistagged as a book, one correctly tagged audiobook), spins up a mock
Bindery API, and checks the verdicts and file actions come out right.

## License

MIT -- see [LICENSE](LICENSE).
