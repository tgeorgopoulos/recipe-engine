"""
Serve one of the fixture sites on http://127.0.0.1:8765.

    python tests/serve_fixture.py A     # variant A: nav link is called "Invoices"
    python tests/serve_fixture.py B     # variant B: the same link is called "Billing" (breaks recipes)

Every request is printed, so "no /invoices.html hit after a failed replay step"
is visible in this terminal. `make_server()` is what the tests use in-process.

Download endpoints (any variant), for the download tool's four outcomes:
    /report.csv      a real 3 KB attachment (Content-Disposition)     -> ok
    /empty.csv       an attachment with a 0-byte body                  -> empty_file
    /slow-download   headers + 1 KB, then the body stalls for 40 s     -> timeout
    /invoices.html     an ordinary page (opens inline, no download)      -> no_download_started
"""
from __future__ import annotations

import http.server
import os
import sys
import threading
import time

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def make_handler(root: str):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=root, **kw)

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def log_message(self, fmt, *args):  # quiet inside tests; the CLI prints below
            if os.environ.get("FIXTURE_VERBOSE"):
                super().log_message(fmt, *args)

        def _attachment(self, name: str, body: bytes, total: int | None = None):
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(total if total is not None else len(body)))
            self.end_headers()
            return body

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/report.csv":
                p = os.path.join(root, "report.csv")
                body = open(p, "rb").read() if os.path.exists(p) else b"id,item,amount\n" + b"".join(f"{i},row {i},{i * 3}.50\n".encode() for i in range(150))
                self.wfile.write(self._attachment("report.csv", body))
                return
            if path == "/empty.csv":
                self.wfile.write(self._attachment("empty.csv", b""))
                return
            if path == "/slow-download":
                body = self._attachment("slow.csv", b"", total=100_000)
                try:
                    self.wfile.write(b"x" * 1024)
                    self.wfile.flush()
                    time.sleep(40)                      # the transfer stalls: a replay must not wait for it
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                return
            return super().do_GET()

    return Handler


def make_server(variant: str = "A", port: int = 8765):
    root = os.path.join(FIXTURES, f"site_{variant.lower()}")
    if not os.path.isdir(root):
        raise SystemExit(f"unknown variant {variant}")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(root))
    srv.daemon_threads = True
    return srv


def serve_in_thread(variant: str = "A", port: int = 8765):
    srv = make_server(variant, port)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, name="fixture-site", daemon=True)
    t.start()
    return srv


if __name__ == "__main__":
    os.environ["FIXTURE_VERBOSE"] = "1"
    variant = (sys.argv[1] if len(sys.argv) > 1 else "A").upper()
    srv = make_server(variant)
    print(f"serving variant {variant} on http://127.0.0.1:8765  (Ctrl-C to stop)")
    srv.serve_forever()
