"""
Minimal client for Bindery's REST API (https://github.com/vavallee/bindery),
used to cross-check what Bindery *thinks* a book's title/author/identifiers
are against what a file's embedded tags and audio content actually look
like.

Honesty check up front: Bindery's exact JSON response schema for a book
record isn't published in its docs beyond "book detail (with editions,
history, formats)". Rather than hardcode field names that might be wrong,
this client searches recursively through whatever JSON comes back for
known-ish field names and for path-shaped strings, and it ships a
`dump_sample()` you should run once against your own instance (see
README) to eyeball the real shape and confirm the FIELD_CANDIDATES below
actually match. If they don't, this is the one file you'll need to adjust.

Auth: header `X-Api-Key: <key>` (per Bindery's README example), against a
base URL like `http://blender:8787/api/v1`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

FIELD_CANDIDATES = {
    "id": ["id", "bookId", "book_id"],
    "title": ["title", "name", "bookTitle", "book_title"],
    "author": ["author", "authorName", "author_name", "authors"],
    "asin": ["asin", "ASIN"],
    "isbn": ["isbn", "isbn13", "isbn_13", "isbn10", "isbn_10", "ISBN"],
    "media_type": ["mediaType", "media_type", "type"],
}

HISTORY_ID_CANDIDATES = ["id", "historyId", "history_id"]

AUDIO_EXT_HINTS = (".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus", ".wav")


def _search_keys(obj: Any, candidates: list[str], _depth: int = 0) -> Optional[Any]:
    """Recursively search a nested dict/list for the first value under any of `candidates` (case-insensitive)."""
    if _depth > 6 or obj is None:
        return None
    if isinstance(obj, dict):
        lower_map = {k.lower(): k for k in obj.keys()}
        for cand in candidates:
            real_key = lower_map.get(cand.lower())
            if real_key is not None and obj[real_key] not in (None, "", []):
                return obj[real_key]
        for v in obj.values():
            found = _search_keys(v, candidates, _depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _search_keys(item, candidates, _depth + 1)
            if found is not None:
                return found
    return None


def _find_paths(obj: Any, _depth: int = 0, _out: Optional[list] = None) -> list[str]:
    """Recursively collect any string values that look like filesystem paths to audio/ebook files."""
    if _out is None:
        _out = []
    if _depth > 8 or obj is None:
        return _out
    if isinstance(obj, str):
        low = obj.lower()
        if low.endswith(AUDIO_EXT_HINTS) or (os.sep in obj and "/" in obj and len(obj) > 3):
            _out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _find_paths(v, _depth + 1, _out)
    elif isinstance(obj, list):
        for item in obj:
            _find_paths(item, _depth + 1, _out)
    return _out


def _author_to_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # Confirmed via --dump-sample: a book's "author" field is a nested
        # object shaped like {"authorName": "...", "sortName": "...", ...},
        # not a flat string -- authorName has to come first here.
        return (
            value.get("authorName")
            or value.get("name")
            or value.get("author")
            or value.get("displayName")
        )
    if isinstance(value, list):
        names = [_author_to_str(v) for v in value]
        names = [n for n in names if n]
        return ", ".join(names) if names else None
    return str(value)


@dataclass
class BookRecord:
    id: Any
    title: Optional[str]
    author: Optional[str]
    asin: Optional[str]
    isbn: Optional[str]
    media_type: Optional[str]
    paths: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_json(cls, obj: dict) -> "BookRecord":
        return cls(
            id=_search_keys(obj, FIELD_CANDIDATES["id"]),
            title=_search_keys(obj, FIELD_CANDIDATES["title"]),
            author=_author_to_str(_search_keys(obj, FIELD_CANDIDATES["author"])),
            asin=_search_keys(obj, FIELD_CANDIDATES["asin"]),
            isbn=_search_keys(obj, FIELD_CANDIDATES["isbn"]),
            media_type=_search_keys(obj, FIELD_CANDIDATES["media_type"]),
            paths=_find_paths(obj),
            raw=obj,
        )


class BinderyClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": api_key, "Accept": "application/json"})
        self.timeout = timeout

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    def _post(self, path: str, json_body: Optional[dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        resp = self.session.post(url, json=json_body, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    def _put(self, path: str, json_body: dict) -> Any:
        url = f"{self.base_url}{path}"
        resp = self.session.put(url, json=json_body, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    def list_books_raw(self, status: Optional[str] = None, page: Optional[int] = None) -> Any:
        params = {}
        if status:
            params["status"] = status
        if page is not None:
            params["page"] = page
        return self._get("/book", params=params)

    def iter_all_books(self, status: str = "imported") -> list[BookRecord]:
        """
        Fetch every book at the given status, defensively handling either a
        bare JSON array response or a {"items": [...], "total": N, ...} /
        {"data": [...]} paginated envelope, and simple page-number pagination
        if one of those keys is present.
        """
        records: list[BookRecord] = []
        page = 1
        seen_ids = set()
        while True:
            payload = self.list_books_raw(status=status, page=page)
            if isinstance(payload, list):
                items = payload
                has_more = False
            elif isinstance(payload, dict):
                items = payload.get("items") or payload.get("data") or payload.get("books") or []
                total = payload.get("total") or payload.get("totalCount")
                has_more = bool(total) and (page * max(len(items), 1)) < total and len(items) > 0
            else:
                items = []
                has_more = False

            if not items:
                break

            for item in items:
                rec = BookRecord.from_json(item)
                key = rec.id if rec.id is not None else id(item)
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                records.append(rec)

            if not has_more:
                break
            page += 1
            if page > 500:  # sanity guard against a pagination-detection bug looping forever
                break

        return records

    def dump_sample(self, status: str = "imported") -> Any:
        """Return the raw JSON for the first page/book, for schema inspection. See README."""
        return self.list_books_raw(status=status)

    # -- Closing the loop after a confirmed delete: blocklist the bad release,
    # re-want the book, optionally trigger an immediate re-search. -----------
    #
    # These four endpoints exist per Bindery's docs/API.md, but (like the book
    # schema above) the exact request/response field names for history entries
    # and the PUT /book/{id} body aren't published. Same approach as
    # BookRecord.from_json: search defensively rather than assume, and dump a
    # sample once against your own instance if this misfires -- see README's
    # "closing the loop" section.

    def list_history_raw(self, book_id: Optional[Any] = None, page: Optional[int] = None) -> Any:
        params = {}
        if book_id is not None:
            params["bookId"] = book_id
        if page is not None:
            params["page"] = page
        return self._get("/history", params=params)

    def find_history_entry_for_path(self, book_id: Any, path: str) -> Optional[dict]:
        """
        Best-effort: find the history entry (grab/import record) that put `path`
        on disk, so we know what to blocklist. Falls back to the most recent
        history entry for the book if no entry's paths match exactly -- better
        to blocklist *a* recent bad grab for this book than blocklist nothing.
        """
        try:
            payload = self.list_history_raw(book_id=book_id)
        except requests.HTTPError:
            return None
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get("items") or payload.get("data") or payload.get("history") or []
        else:
            items = []

        target = os.path.normpath(path)
        target_base = os.path.basename(target)
        for item in items:
            candidate_paths = _find_paths(item)
            if any(os.path.normpath(p) == target or os.path.basename(p) == target_base for p in candidate_paths):
                return item
        return items[0] if items else None

    @staticmethod
    def extract_history_id(entry: dict) -> Optional[Any]:
        return _search_keys(entry, HISTORY_ID_CANDIDATES)

    def blocklist_history(self, history_id: Any, reason: Optional[str] = None) -> None:
        body = {"reason": reason} if reason else None
        self._post(f"/history/{history_id}/blocklist", json_body=body)

    def mark_wanted(self, book_id: Any) -> None:
        """
        Explicitly flip a book back to monitored/wanted after removing its
        (bad) file. The field name Bindery expects here is unconfirmed --
        `monitored: true` is the most common convention in this class of
        *arr-style tool and matches the README's own "each format has its own
        lifecycle" / monitored language, but verify with --dump-sample-style
        inspection if this doesn't stick.
        """
        self._put(f"/book/{book_id}", {"monitored": True})

    def trigger_search(self, book_id: Any) -> None:
        self._post(f"/book/{book_id}/search")


def build_path_index(records: list[BookRecord]) -> dict[str, BookRecord]:
    """Map every normalized file/folder path we found in the catalogue to its BookRecord."""
    index: dict[str, BookRecord] = {}
    for rec in records:
        for p in rec.paths:
            norm = os.path.normpath(p)
            index[norm] = rec
            index[os.path.dirname(norm)] = rec
    return index


def remap_prefix(path: str, from_prefix: Optional[str], to_prefix: Optional[str]) -> str:
    """Translate a Bindery-reported path to your local mount, if the two differ (common in docker)."""
    if not from_prefix or not to_prefix:
        return path
    norm = os.path.normpath(path)
    from_norm = os.path.normpath(from_prefix)
    if norm.startswith(from_norm):
        return os.path.normpath(to_prefix + norm[len(from_norm):])
    return path
