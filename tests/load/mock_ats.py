"""Local Greenhouse-shaped ATS for resource tests (run as its own process).

/v1/boards/<board>/jobs: `big` -> ~78 MB (94 postings), `gone*` -> 404,
`slow*` -> 5 s delay, otherwise 25 small fresh postings. Deterministic, so a
second poll is an unchanged snapshot.
"""
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

POSTED = (datetime.now(UTC) - timedelta(hours=3)).isoformat()


def posting(board, i, size):
    return {"id": abs(hash((board, i))) % 10**12, "title": f"Backend Engineer {i}" if i % 3 else f"Senior Manager {i}",
            "company_name": board, "location": {"name": "New York, NY"},
            "absolute_url": f"https://example.com/{board}/{i}", "updated_at": POSTED,
            "first_published": POSTED, "requisition_id": f"R-{i}",
            "content": ("&lt;p&gt;" + ("Build reliable backend services in Python. " * (size // 44)) + "&lt;/p&gt;")}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        board = self.path.split("/v1/boards/")[-1].split("/")[0]
        if board.startswith("gone"):
            self.send_response(404); self.end_headers(); return
        if board.startswith("slow"):
            time.sleep(5)
        count, size = (94, 830_000) if board == "big" else (25, 3_000)
        body = json.dumps({"jobs": [posting(board, i, size) for i in range(count)]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
