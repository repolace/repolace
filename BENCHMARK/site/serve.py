"""Serve BENCHMARK/site on 127.0.0.1 for local review: `python3 BENCHMARK/site/serve.py [port]`.

Stdlib only. Adds what a static file server needs for this page: the right types for woff2/mp4/webm and HTTP Range
support, which browsers require before they will seek or loop a <video>. Bound to loopback; share it with
`tailscale serve` if you want it elsewhere.

`scripts/visual_report/server.py` can mount the same handler under /site/ with
`from serve import SiteHandler` (it is not tracked in this branch, so that wiring is left to its owner).
"""

import mimetypes
import os
import re
import sys
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

SITE_ROOT = os.path.dirname(os.path.abspath(__file__))
RANGE = re.compile(r"bytes=(\d*)-(\d*)$")

mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("text/javascript", ".js")


class SiteHandler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map}

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_head(self):
        header = self.headers.get("Range")
        path = self.translate_path(self.path)
        if not header or os.path.isdir(path) or not os.path.isfile(path):
            return super().send_head()
        match = RANGE.match(header.strip())
        size = os.path.getsize(path)
        if not match or not (match.group(1) or match.group(2)):
            return super().send_head()
        if match.group(1):
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else size - 1
        else:  # suffix range: the last N bytes
            start = max(size - int(match.group(2)), 0)
            end = size - 1
        end = min(end, size - 1)
        if start > end or start >= size:
            self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            return None
        handle = open(path, "rb")
        handle.seek(start)
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self._remaining = end - start + 1
        return handle

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        while remaining > 0:
            chunk = source.read(min(64 * 1024, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)
        self._remaining = None
        return None

    def log_message(self, fmt, *args):
        sys.stderr.write("site: " + fmt % args + "\n")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8771
    handler = partial(SiteHandler, directory=SITE_ROOT)
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        print(f"serving {SITE_ROOT} on http://127.0.0.1:{port}/")
        server.serve_forever()


if __name__ == "__main__":
    main()
