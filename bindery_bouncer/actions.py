"""File-system actions: quarantine or delete a flagged folder, with safety rails."""
from __future__ import annotations

import os
import shutil


class UnsafePathError(Exception):
    pass


def _require_inside(path: str, root: str) -> str:
    """Refuse to act on anything that isn't strictly inside the library root."""
    path_norm = os.path.realpath(path)
    root_norm = os.path.realpath(root)
    if os.path.commonpath([path_norm, root_norm]) != root_norm or path_norm == root_norm:
        raise UnsafePathError(f"refusing to act on {path!r}: not safely inside library root {root!r}")
    return path_norm


def quarantine_folder(folder: str, library_root: str, quarantine_root: str) -> str:
    """Move `folder` into quarantine_root, preserving its path relative to library_root. Returns destination."""
    folder = _require_inside(folder, library_root)
    rel = os.path.relpath(folder, library_root)
    dest = os.path.join(quarantine_root, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        # Avoid clobbering a previous quarantine of the same relative path.
        base = dest
        i = 2
        while os.path.exists(dest):
            dest = f"{base}__{i}"
            i += 1
    shutil.move(folder, dest)
    prune_empty_parents(os.path.dirname(folder), library_root)
    return dest


def delete_folder(folder: str, library_root: str) -> None:
    """Permanently delete `folder` and everything in it. No undo."""
    folder = _require_inside(folder, library_root)
    shutil.rmtree(folder)
    prune_empty_parents(os.path.dirname(folder), library_root)


def restore_folder(quarantined_folder: str, original_path: str, library_root: str) -> None:
    """
    Move a previously-quarantined folder back to its original location -- the
    other half of a manual review verdict (see --apply-decisions): the
    reviewer listened and decided this one really is a correctly-filed
    audiobook that got caught by a single, misleading signal. No undo once
    done, same as delete/quarantine.
    """
    quarantined_folder = _require_inside(quarantined_folder, library_root)
    original_path = _require_inside(original_path, library_root)
    if os.path.exists(original_path):
        raise UnsafePathError(
            f"refusing to restore over an existing path {original_path!r} -- "
            f"something else already occupies this folder's original spot"
        )
    os.makedirs(os.path.dirname(original_path), exist_ok=True)
    shutil.move(quarantined_folder, original_path)
    prune_empty_parents(os.path.dirname(quarantined_folder), library_root)


def prune_empty_parents(start_dir: str, library_root: str) -> None:
    """Remove now-empty parent directories left behind by a delete/quarantine, up to (not including) library_root."""
    root_norm = os.path.realpath(library_root)
    current = os.path.realpath(start_dir)
    while current != root_norm and current.startswith(root_norm):
        try:
            if os.path.isdir(current) and not os.listdir(current):
                os.rmdir(current)
                current = os.path.dirname(current)
            else:
                break
        except OSError:
            break
