#!/usr/bin/env python3
"""Serve the AuntieJoy demo UI from this folder, with browser caching switched OFF.

Use this instead of `python3 -m http.server`. The built-in server answers without a
Cache-Control header, which lets the browser keep its own copy of every file, so a
change you make to live.js or styles.css stays invisible until you remember to
hard-reload. During a demo that is the worst kind of bug: the code on disk is right
and the page on screen is not, and nothing tells you which one you are looking at.

This server sends `Cache-Control: no-store` on every response, so the browser has to
come back for the file each time. What you edit is what you see, with nothing to
remember and nothing to bump.

    python3 serve.py            # http://127.0.0.1:8098
    python3 serve.py 8099       # a different port

Always from this folder is not required: the folder served is the one this file sits
in, so `python3 frontend/serve.py` from the project root works too.

This is a demo convenience only. It is a plain single-threaded-per-request static
file server bound to 127.0.0.1, with no authentication; it is not the way the app
would be served in production.
"""
from __future__ import annotations

import functools
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 8098
# no-store is the one that matters: do not keep a copy at all. The other three are
# belt-and-braces for older browsers and for proxies that ignore no-store.
NO_CACHE_HEADERS = (
    ("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"),
    ("Pragma", "no-cache"),
    ("Expires", "0"),
)


class NoStoreHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler with the cache headers added to every response.

    end_headers() is the last thing called before the blank line that ends a response,
    for successful files and for 404s alike, so overriding it here catches everything
    the handler can send. There is no need to touch send_head() or guess_type().
    """

    def end_headers(self) -> None:
        for name, value in NO_CACHE_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        # One short line per request, so the terminal stays readable while you work.
        sys.stderr.write("  %s %s\n" % (self.address_string(), fmt % args))


def main(argv: list[str]) -> int:
    port = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else DEFAULT_PORT
    directory = str(Path(__file__).resolve().parent)
    handler = functools.partial(NoStoreHandler, directory=directory)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"Joy UI on http://127.0.0.1:{port}  (caching off, serving {directory})")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
