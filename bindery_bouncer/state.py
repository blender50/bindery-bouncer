"""
Small persistent state file for --new-only mode (see cli.py): remembers
which folders have already been fully handled, so a scheduled/cron
invocation of this one-shot tool doesn't burn time re-running audio
analysis on folders nothing has changed in since the last tick.

Important: a folder is deliberately NOT recorded here just because it was
*seen* -- only once it's either clean, or actually resolved by --execute.
A folder flagged confirmed/suspect during a dry run stays unresolved and
gets reconsidered on every tick, so running --new-only in dry-run mode
first (to see what it would do, same as the README's calibration advice
for a normal full scan) can never cause a real problem to be silently
swallowed once you turn --execute on.
"""
from __future__ import annotations

import json
import os


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(path: str, state: dict) -> None:
    """Atomic write (via a temp file + rename) so a crash mid-write can't
    corrupt the state file a scheduled run depends on."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def folder_signature(latest_mtime: float, file_count: int) -> str:
    """A cheap fingerprint of a folder's contents -- changes if files are
    added/removed, or the newest file's mtime moves (e.g. a re-grab after
    a previous copy was deleted/quarantined)."""
    return f"{int(latest_mtime)}:{file_count}"
