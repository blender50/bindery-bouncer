"""
Focused regression test for BinderyClient.iter_all_books' pagination.

This guards against a real bug caught during first calibration against a
live ~1700-book Bindery instance: an earlier version paginated with a
"page" query param, but Bindery's actual API is offset/limit based (see
--dump-sample's real response shape: {"items": [...], "total": N,
"limit": 100, "offset": 0}). Bindery silently ignores an unrecognized
"page" param, so every request came back as the exact same first page --
the loop kept "succeeding" (no error, no warning), but only the first
~100 books in the whole library were ever cross-checked against the file
library, and the rest silently lost their catalogue signal for the
entire scan.

Run with: python3 -m unittest tests.test_bindery_client -v
"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bindery_bouncer.bindery_client import BinderyClient  # noqa: E402


class _PaginatedBookHandler(BaseHTTPRequestHandler):
    # Configured per-test via class attributes before the server starts.
    total_books = 0
    include_total = True
    requests_seen: list = []

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        self.__class__.requests_seen.append(dict(qs))
        limit = int(qs.get("limit", ["100"])[0])
        offset = int(qs.get("offset", ["0"])[0])
        all_ids = list(range(1, self.total_books + 1))
        page = all_ids[offset : offset + limit]
        items = [{"id": i, "title": f"Book {i}", "author": {"authorName": f"Author {i}"}} for i in page]
        body_obj = {"items": items, "limit": limit, "offset": offset}
        if self.include_total:
            body_obj["total"] = self.total_books
        body = json.dumps(body_obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet the test output
        pass


class BinderyClientPaginationTest(unittest.TestCase):
    def _start_server(self):
        server = HTTPServer(("127.0.0.1", 0), _PaginatedBookHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def _stop():
            server.shutdown()  # stop serve_forever()'s loop
            thread.join(timeout=5)
            server.server_close()  # only safe once the loop has actually stopped

        self.addCleanup(_stop)
        return port

    def setUp(self):
        _PaginatedBookHandler.requests_seen = []
        _PaginatedBookHandler.include_total = True

    def test_iter_all_books_walks_every_page_via_offset(self):
        # More than one page at the default page_size=100 -- this is the
        # shape of Steve's real library (~1700 books).
        _PaginatedBookHandler.total_books = 250
        port = self._start_server()

        client = BinderyClient(base_url=f"http://127.0.0.1:{port}/api/v1", api_key="x")
        records = client.iter_all_books(status="imported")

        self.assertEqual(len(records), 250)
        self.assertEqual({r.id for r in records}, set(range(1, 251)))

        # The actual regression: assert every request advanced the offset,
        # and that nothing sends the old, Bindery-ignored "page" param.
        offsets = [int(q["offset"][0]) for q in _PaginatedBookHandler.requests_seen]
        self.assertEqual(offsets, [0, 100, 200])
        self.assertTrue(
            all("page" not in q for q in _PaginatedBookHandler.requests_seen),
            "must not send a 'page' param -- Bindery's real API doesn't understand it",
        )

    def test_iter_all_books_stops_on_short_final_page(self):
        # A page shorter than page_size, even with no "total" in the
        # response, must still end the loop rather than re-requesting the
        # same last page forever.
        _PaginatedBookHandler.total_books = 30
        _PaginatedBookHandler.include_total = False
        port = self._start_server()

        client = BinderyClient(base_url=f"http://127.0.0.1:{port}/api/v1", api_key="x")
        records = client.iter_all_books(status="imported", page_size=100)

        self.assertEqual(len(records), 30)
        self.assertEqual(len(_PaginatedBookHandler.requests_seen), 1)

    def test_iter_all_books_handles_empty_library(self):
        _PaginatedBookHandler.total_books = 0
        port = self._start_server()

        client = BinderyClient(base_url=f"http://127.0.0.1:{port}/api/v1", api_key="x")
        records = client.iter_all_books(status="imported")

        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
