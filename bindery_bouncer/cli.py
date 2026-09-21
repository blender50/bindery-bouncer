"""
bindery-bouncer: find audio files sitting in your audiobook library that
are actually music (or otherwise don't match what Bindery's catalogue
expects), using three independent signals -- embedded tags, Bindery's own
catalogue metadata, and DSP analysis of the actual audio content -- and
act on them with a confidence tier: confirmed (both signals agree) gets
the strong action, suspect (one signal only) gets quarantined for you to
check by hand.

See README.md before your first real run, especially the "calibrate
before you trust it" section -- the audio-content signal is a heuristic,
not a trained classifier, and its thresholds were only validated against
synthetic test audio, not your actual library.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from typing import Optional

import requests

from . import actions
from .bindery_client import BinderyClient, BookRecord, build_path_index, remap_prefix
from .listen import analyze_file
from .scorer import ScoringConfig, assess_folder
from .state import folder_signature, load_state, save_state
from .tags import group_by_folder, read_tags

try:
    from tqdm import tqdm
except ImportError:  # tqdm is optional; fall back to no progress bar
    def tqdm(iterable, **kwargs):
        return iterable


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bindery-bouncer",
        description="Scan a Bindery audiobook library for misfiled music and act on it by confidence tier.",
    )
    p.add_argument("--library-path", required=True, help="Root folder of your audiobook library to scan.")

    bindery = p.add_argument_group("Bindery API (catalogue cross-check)")
    bindery.add_argument("--bindery-url", default=os.environ.get("BINDERY_URL"),
                          help="e.g. http://blender:8787/api/v1. Or set BINDERY_URL.")
    bindery.add_argument("--bindery-api-key", default=os.environ.get("BINDERY_API_KEY"),
                          help="Bindery API key (Settings -> API in the UI). Or set BINDERY_API_KEY.")
    bindery.add_argument("--bindery-status", default="imported",
                          help="Book status to pull from Bindery for the catalogue cross-check (default: imported -- "
                               "confirmed via --dump-sample against a real instance; Bindery's enum is "
                               "imported/wanted, not \"downloaded\").")
    bindery.add_argument("--no-bindery", action="store_true",
                          help="Skip the Bindery catalogue cross-check entirely (tags + audio only, less accurate).")
    bindery.add_argument("--dump-sample", action="store_true",
                          help="Print one raw page of Bindery's /book response and exit -- "
                               "use this once to confirm bindery_client.py's field guesses match your instance.")
    bindery.add_argument("--bindery-path-prefix", default=None,
                          help="Path prefix as Bindery/its container sees your library, if different from --library-path.")
    bindery.add_argument("--local-path-prefix", default=None,
                          help="Path prefix as this script sees your library (usually == --library-path).")

    audio = p.add_argument_group("Audio content analysis")
    audio.add_argument("--skip-audio", action="store_true",
                        help="Skip DSP audio analysis (fast, tag+catalogue only -- audio is the slowest step).")
    audio.add_argument("--samples-per-folder", type=int, default=2,
                        help="How many files per folder to run audio analysis on (default: 2).")
    audio.add_argument("--audio-strong-threshold", type=float, default=0.68,
                        help="music_score at/above this counts as strong audio evidence (default: 0.68). Tune this "
                             "against your own library -- see README calibration section.")
    audio.add_argument("--meta-strong-threshold", type=float, default=0.6,
                        help="Tag-vs-catalogue mismatch at/above this counts as strong metadata evidence (default: 0.6).")

    action = p.add_argument_group("What to do with flagged folders")
    action.add_argument("--execute", action="store_true",
                         help="Actually move/delete files. Without this flag, everything is dry-run: "
                              "the report is written but nothing on disk changes. Strongly recommended "
                              "to omit this on your first run against a new library.")
    action.add_argument("--confirmed-action", choices=["delete", "quarantine", "report"], default="delete",
                         help="Action for the 'confirmed' tier (audio + at least one other signal agree). Default: delete.")
    action.add_argument("--suspect-action", choices=["delete", "quarantine", "report"], default="quarantine",
                         help="Action for the 'suspect' tier (exactly one signal fired). Default: quarantine.")
    action.add_argument("--quarantine-dir", default=None,
                         help="Where quarantined folders go (default: <library-path>/_bindery_bouncer_quarantine).")

    loop = p.add_argument_group("Closing the loop with Bindery (only after an actual delete, never a quarantine)")
    loop.add_argument("--no-close-loop", action="store_true",
                       help="Don't blocklist the bad release or re-want the book after deleting -- just delete the "
                            "local file and stop there.")
    loop.add_argument("--no-search-after-blocklist", action="store_true",
                       help="After blocklisting and re-wanting, don't trigger an immediate re-search -- "
                            "let Bindery's normal sweep (default: every 12h) pick it up instead.")

    incr = p.add_argument_group("Scheduled / incremental scanning")
    incr.add_argument("--new-only", action="store_true",
                       help="Only assess folders that have changed since the last --new-only run and have "
                            "gone quiet (no file changes) for at least --min-quiet-seconds. Meant to be "
                            "invoked on a schedule (e.g. cron via Unraid's User Scripts plugin) so freshly "
                            "grabbed audiobooks get checked automatically without rescanning your whole "
                            "library every tick. A folder flagged confirmed/suspect in a dry run is never "
                            "marked done -- only a clean verdict or a real --execute resolves it, so nothing "
                            "gets silently skipped just because a scheduled tick already looked at it once. "
                            "Calibrate thresholds with a normal full run first -- see README.")
    incr.add_argument("--min-quiet-seconds", type=int, default=3600,
                       help="In --new-only mode, only consider a folder once its newest file's mtime is at "
                            "least this many seconds old (default: 3600 = 1 hour) -- guards against catching "
                            "a folder mid-download/import.")
    incr.add_argument("--state-file", default=None,
                       help="In --new-only mode, where to remember already-handled folders (default: "
                            "<library-path>/.bindery_bouncer_state.json).")

    p.add_argument("--report-csv", default=None,
                    help="CSV report path (default: bindery_bouncer_report_<timestamp>.csv in the current directory).")
    p.add_argument("--verbose", action="store_true", help="Print every folder's verdict as it's scanned, not just flagged ones.")
    return p


def _pick_sample_files(files: list[str], n: int) -> list[str]:
    if len(files) <= n:
        return files
    if n <= 1:
        return [files[len(files) // 2]]
    step = len(files) / n
    return [files[int(i * step)] for i in range(n)]


def _to_bindery_view(local_path: str, args) -> str:
    return remap_prefix(local_path, args.local_path_prefix or args.library_path, args.bindery_path_prefix)


def _close_bindery_loop(client, expected, tags_list, args) -> str:
    """
    After an actual delete of a confirmed-bad folder: blocklist the release
    that put it there and re-want the book, so the next sweep (or an
    immediate manual search) goes looking for a real copy instead of quietly
    leaving the book marked done-and-missing. Best-effort -- an API hiccup
    here should never be treated as the delete itself having failed.
    """
    if client is None:
        return "skipped (no Bindery connection, e.g. --no-bindery)"
    if expected is None or expected.id is None:
        return "skipped (no matched Bindery book id for this folder)"

    try:
        sample_path = tags_list[0].path if tags_list else None
        entry = client.find_history_entry_for_path(expected.id, sample_path) if sample_path else None
        history_id = client.extract_history_id(entry) if entry else None

        if history_id is not None:
            client.blocklist_history(history_id, reason="bindery-bouncer: confirmed music/wrong-file mismatch")
            blocklist_note = f"blocklisted history #{history_id}"
        else:
            blocklist_note = "no matching history entry found to blocklist"

        client.mark_wanted(expected.id)
        wanted_note = "re-wanted"

        search_note = "sweep will pick it up"
        if not args.no_search_after_blocklist:
            client.trigger_search(expected.id)
            search_note = "triggered immediate search"

        return f"{blocklist_note}; {wanted_note}; {search_note}"
    except requests.exceptions.RequestException as e:
        return f"FAILED ({e})"


def run(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    client: Optional[BinderyClient] = None
    if not args.no_bindery:
        if not args.bindery_url or not args.bindery_api_key:
            print("No --bindery-url/--bindery-api-key (or BINDERY_URL/BINDERY_API_KEY) given.\n"
                  "Pass --no-bindery to run tag+audio-only, or supply credentials for the catalogue cross-check.",
                  file=sys.stderr)
            return 2
        client = BinderyClient(args.bindery_url, args.bindery_api_key)

    if args.dump_sample:
        if client is None:
            print("--dump-sample needs Bindery credentials.", file=sys.stderr)
            return 2
        print(json.dumps(client.dump_sample(status=args.bindery_status), indent=2)[:8000])
        print("\n--- Compare the field names above against FIELD_CANDIDATES in bindery_client.py ---")
        return 0

    if not os.path.isdir(args.library_path):
        print(f"Library path not found: {args.library_path}", file=sys.stderr)
        return 2

    path_index: dict[str, BookRecord] = {}
    if client is not None:
        print(f"Fetching catalogue from Bindery ({args.bindery_url}, status={args.bindery_status})...")
        try:
            records = client.iter_all_books(status=args.bindery_status)
        except Exception as e:
            print(f"Could not reach Bindery API ({e}). Re-run with --no-bindery to proceed without it, "
                  f"or check --bindery-url/--bindery-api-key.", file=sys.stderr)
            return 2
        path_index = build_path_index(records)
        print(f"  {len(records)} book records, {len(path_index)} indexed paths.")

    quarantine_dir = args.quarantine_dir or os.path.join(args.library_path, "_bindery_bouncer_quarantine")

    folders = group_by_folder(args.library_path)
    # Never scan into our own quarantine folder.
    folders = {f: files for f, files in folders.items() if not f.startswith(quarantine_dir)}
    print(f"Found {len(folders)} folders with audio files under {args.library_path}.")

    state_path = None
    state: dict = {}
    signatures: dict[str, str] = {}
    if args.new_only:
        state_path = args.state_file or os.path.join(args.library_path, ".bindery_bouncer_state.json")
        state = load_state(state_path)
        now = time.time()
        still_active = 0
        unchanged = 0
        eligible = {}
        for folder, files in folders.items():
            try:
                latest_mtime = max(os.path.getmtime(f) for f in files)
            except OSError:
                continue
            if now - latest_mtime < args.min_quiet_seconds:
                still_active += 1
                continue
            sig = folder_signature(latest_mtime, len(files))
            if state.get(folder) == sig:
                unchanged += 1
                continue
            signatures[folder] = sig
            eligible[folder] = files
        folders = eligible
        print(f"  --new-only: {len(folders)} folder(s) changed and quiet for >= {args.min_quiet_seconds}s "
              f"({still_active} still active/recent, {unchanged} unchanged since last recorded run).")

    config = ScoringConfig(
        audio_strong_threshold=args.audio_strong_threshold,
        meta_strong_threshold=args.meta_strong_threshold,
    )

    rows = []
    tier_counts = {"confirmed": 0, "suspect": 0, "clean": 0}

    for folder, files in tqdm(sorted(folders.items()), desc="Scanning"):
        tags_list = [t for t in (read_tags(f) for f in files) if t is not None]
        if not tags_list:
            continue

        expected = None
        if path_index:
            bindery_folder = _to_bindery_view(folder, args)
            expected = path_index.get(bindery_folder)
            if expected is None:
                for f in files:
                    expected = path_index.get(_to_bindery_view(f, args))
                    if expected:
                        break

        audio_signals = []
        if not args.skip_audio:
            for f in _pick_sample_files([t.path for t in tags_list], args.samples_per_folder):
                dur = next((t.duration_s for t in tags_list if t.path == f), 0.0)
                audio_signals.append(analyze_file(f, dur))

        assessment = assess_folder(folder, tags_list, audio_signals, expected, config)
        tier_counts[assessment.tier] += 1

        action_taken = "none"
        loop_status = "n/a"
        if assessment.tier in ("confirmed", "suspect"):
            chosen = args.confirmed_action if assessment.tier == "confirmed" else args.suspect_action
            verb = {"delete": "DELETE", "quarantine": "QUARANTINE", "report": "report-only"}[chosen]
            action_taken = f"{'would ' if not args.execute else ''}{verb}"
            if args.execute:
                try:
                    if chosen == "delete":
                        actions.delete_folder(folder, args.library_path)
                        # Close the loop with Bindery -- ONLY on an actual delete, never on
                        # quarantine (the file's still there, so the book isn't really missing yet).
                        if not args.no_close_loop:
                            loop_status = _close_bindery_loop(client, expected, tags_list, args)
                        else:
                            loop_status = "skipped (--no-close-loop)"
                    elif chosen == "quarantine":
                        dest = actions.quarantine_folder(folder, args.library_path, quarantine_dir)
                        action_taken += f" -> {dest}"
                except actions.UnsafePathError as e:
                    action_taken = f"SKIPPED (safety check failed: {e})"

        if args.new_only:
            # Only mark a folder "done" once it's genuinely resolved -- clean,
            # or actually acted on by --execute. A dry-run flag (or a
            # "report"-only action) leaves it unresolved so the next tick
            # reconsiders it, instead of a scheduled run silently treating a
            # one-time look as having handled it.
            resolved = assessment.tier == "clean"
            if assessment.tier in ("confirmed", "suspect") and args.execute:
                chosen = args.confirmed_action if assessment.tier == "confirmed" else args.suspect_action
                if chosen in ("delete", "quarantine") and not action_taken.startswith("SKIPPED"):
                    resolved = True
            if resolved:
                state[folder] = signatures[folder]
                save_state(state_path, state)

        if assessment.tier != "clean" or args.verbose:
            print(f"[{assessment.tier.upper():9s}] {action_taken:35s} {folder}")
            for reason in assessment.reasons:
                print(f"             - {reason}")
            if loop_status != "n/a":
                print(f"             loop: {loop_status}")

        rows.append({
            "folder": folder,
            "tier": assessment.tier,
            "action": action_taken,
            "bindery_loop": loop_status,
            "audio_music_score": assessment.audio_music_score,
            "tag_genre_music_score": assessment.tag_genre_music_score,
            "metadata_mismatch_score": assessment.metadata_mismatch_score,
            "has_bindery_match": assessment.has_bindery_match,
            "expected_title": assessment.expected_title,
            "expected_author": assessment.expected_author,
            "observed_genre": assessment.observed_genre,
            "observed_artist": assessment.observed_artist,
            "observed_album": assessment.observed_album,
            "reasons": " | ".join(assessment.reasons),
            "sample_files": " | ".join(assessment.sample_files),
        })

    report_path = args.report_csv or f"bindery_bouncer_report_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    if rows:
        with open(report_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    print("\n--- Summary ---")
    print(f"  confirmed: {tier_counts['confirmed']}   suspect: {tier_counts['suspect']}   clean: {tier_counts['clean']}")
    if rows:
        print(f"  Full report: {report_path}")
    elif args.new_only:
        print("  Nothing new/changed-and-quiet to check this tick -- no report written.")
    if args.new_only:
        print(f"  State file: {state_path}")
    if not args.execute and (tier_counts["confirmed"] or tier_counts["suspect"]):
        print("  This was a DRY RUN -- nothing on disk was changed. Re-run with --execute once you've "
              "checked the report and are happy with the calls it's making.")
    return 0


def main():
    raise SystemExit(run())


if __name__ == "__main__":
    main()
