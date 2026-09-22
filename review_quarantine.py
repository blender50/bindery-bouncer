#!/usr/bin/env python3
"""
review_quarantine.py -- listen to bindery-bouncer's quarantined "suspect"
folders one at a time and record an audiobook/music verdict for each.

This is meant to run on your own machine (macOS, using `afplay`), against a
LOCAL COPY of the quarantine folder -- not on the server, and not on the
quarantine folder in place. Pull it over first, e.g.:

    rsync -avz root@blender:/mnt/user/data/media/audiobooks/_bindery_bouncer_quarantine/ \\
        ~/bindery_review/quarantine/
    rsync -avz root@blender:/mnt/user/appdata/compose.manager/projects/bindery-bouncer/bindery_bouncer_report_*.csv \\
        ~/bindery_review/

Then run this against the local copy:

    python3 review_quarantine.py --quarantine-root ~/bindery_review/quarantine \\
        --report-csv ~/bindery_review/bindery_bouncer_report_20260922_003738.csv

It shows you the tool's own reasoning for each folder first (often enough to
decide without listening at all), plays a short snippet of the first file on
request, and records your answer to a decisions.csv (default: inside
--quarantine-root) as you go -- safe to quit and resume any time, already-
decided folders are skipped on the next run.

That decisions.csv is what --apply-decisions on blender then reads to
restore real audiobooks to the library or permanently delete confirmed
music (closing the loop with Bindery for you). See README.md.

No dependencies beyond the standard library and macOS's built-in `afplay`.
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from datetime import datetime, timezone

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus", ".wav"}


def find_book_folders(root: str) -> dict[str, list[str]]:
    """Group audio files by their immediate parent folder, same granularity bindery-bouncer itself uses."""
    groups: dict[str, list[str]] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in sorted(filenames):
            if os.path.splitext(fn)[1].lower() in AUDIO_EXTENSIONS:
                groups.setdefault(dirpath, []).append(os.path.join(dirpath, fn))
    return groups


def load_context(report_csv: str, library_prefix: str) -> dict[str, dict]:
    """Index the bindery-bouncer CSV report by relative path (stripping library_prefix) for quick lookup."""
    context: dict[str, dict] = {}
    if not report_csv:
        return context
    with open(report_csv, newline="") as f:
        for row in csv.DictReader(f):
            folder = row.get("folder", "")
            rel = os.path.relpath(folder, library_prefix) if folder.startswith(library_prefix) else folder
            context[os.path.normpath(rel)] = row
    return context


def load_existing_decisions(decisions_path: str) -> set[str]:
    if not os.path.isfile(decisions_path):
        return set()
    with open(decisions_path, newline="") as f:
        return {os.path.normpath(row["relative_path"]) for row in csv.DictReader(f) if row.get("relative_path")}


def append_decision(decisions_path: str, relpath: str, decision: str) -> None:
    is_new = not os.path.isfile(decisions_path)
    with open(decisions_path, "a", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["relative_path", "decision", "decided_at"])
        w.writerow([relpath, decision, datetime.now(timezone.utc).isoformat()])


def play_snippet(path: str, seconds: int) -> None:
    try:
        subprocess.run(["afplay", "-t", str(seconds), path])
    except FileNotFoundError:
        print("  (afplay not found -- this script expects to run on macOS; "
              "open the file manually instead)")
    except KeyboardInterrupt:
        pass  # let Ctrl-C stop playback without quitting the whole review


def print_context(relpath: str, files: list[str], context_row: dict | None) -> None:
    print()
    print(f"=== {relpath} ===")
    print(f"    {len(files)} audio file(s)")
    if context_row:
        print(f"    expected:  {context_row.get('expected_author', '')!r} / {context_row.get('expected_title', '')!r}")
        print(f"    observed:  {context_row.get('observed_artist', '')!r} / {context_row.get('observed_album', '')!r}"
              f"  genre={context_row.get('observed_genre', '')!r}")
        print(f"    reasons:   {context_row.get('reasons', '')}")
    else:
        print("    (no matching row in --report-csv -- decide by ear)")


def review_folder(relpath: str, files: list[str], context_row: dict | None, snippet_seconds: int) -> str | None:
    """Returns 'audiobook', 'music', 'skip', or None (quit)."""
    print_context(relpath, files, context_row)
    idx = 0
    played_once = False
    while True:
        if not played_once:
            print(f"    playing ({snippet_seconds}s): {os.path.basename(files[idx])}")
            play_snippet(files[idx], snippet_seconds)
            played_once = True
        choice = input("    [a]udiobook  [m]usic  [r]eplay  [n]ext file  [s]kip for now  [q]uit > ").strip().lower()
        if choice == "a":
            return "audiobook"
        elif choice == "m":
            return "music"
        elif choice == "r":
            print(f"    playing ({snippet_seconds}s): {os.path.basename(files[idx])}")
            play_snippet(files[idx], snippet_seconds)
        elif choice == "n":
            idx = (idx + 1) % len(files)
            print(f"    playing ({snippet_seconds}s): {os.path.basename(files[idx])}")
            play_snippet(files[idx], snippet_seconds)
        elif choice == "s":
            return "skip"
        elif choice == "q":
            return None
        else:
            print("    (didn't catch that -- a/m/r/n/s/q)")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--quarantine-root", required=True,
                   help="Local copy of the quarantine folder (see rsync command in the module docstring).")
    p.add_argument("--report-csv", default=None,
                   help="The bindery-bouncer CSV report from the run that created this quarantine batch -- "
                        "used to show expected/observed metadata for each folder before you even listen.")
    p.add_argument("--library-prefix", default="/audiobooks",
                   help="Path prefix the report's 'folder' column is rooted at (default: /audiobooks, matching "
                        "the container's own view -- see docker-compose.yml).")
    p.add_argument("--decisions-file", default=None,
                   help="Where to record decisions (default: <quarantine-root>/decisions.csv).")
    p.add_argument("--snippet-seconds", type=int, default=20,
                   help="How many seconds of each file to play (default: 20).")
    args = p.parse_args(argv)

    quarantine_root = os.path.abspath(args.quarantine_root)
    if not os.path.isdir(quarantine_root):
        print(f"Not a directory: {quarantine_root}", file=sys.stderr)
        return 2

    decisions_path = args.decisions_file or os.path.join(quarantine_root, "decisions.csv")
    context = load_context(args.report_csv, args.library_prefix) if args.report_csv else {}
    already_decided = load_existing_decisions(decisions_path)

    folders = find_book_folders(quarantine_root)
    pending = sorted(
        relpath for relpath in (os.path.relpath(f, quarantine_root) for f in folders)
        if os.path.normpath(relpath) not in already_decided
    )

    print(f"{len(folders)} folder(s) in quarantine, {len(already_decided)} already decided, "
          f"{len(pending)} remaining.")
    if not pending:
        print("Nothing left to review.")
        return 0

    for i, relpath in enumerate(pending):
        abs_folder = os.path.join(quarantine_root, relpath)
        files = folders[abs_folder]
        context_row = context.get(os.path.normpath(relpath))
        print(f"\n[{i + 1}/{len(pending)}]", end="")
        decision = review_folder(relpath, files, context_row, args.snippet_seconds)
        if decision is None:
            print("\nStopping -- progress is saved, run again any time to continue.")
            return 0
        if decision != "skip":
            append_decision(decisions_path, relpath, decision)
            print(f"    -> recorded: {decision}")
        else:
            print("    -> left for later")

    print(f"\nAll caught up. Decisions written to {decisions_path}")
    print("Copy that file back to blender and run bindery-bouncer with --apply-decisions "
          "(dry run first, then --execute) to act on it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
