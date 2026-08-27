"""A counting, latency-injecting HTTP proxy in front of a local S3 (moto).

Every forwarded request sleeps LATENCY_S first — approximating a real
endpoint's RTT — and is counted. GET /__proxy_stats__ returns the counters as
JSON; POST /__proxy_stats__/reset zeroes them. Runs threaded so concurrent
range reads behave like a real endpoint (parallel requests each pay the RTT
once, not serially).
"""
import http.client
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9601
LISTEN_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 9602
LATENCY_S = float(sys.argv[3]) if len(sys.argv) > 3 else 0.04

_lock = threading.Lock()
_stats = {"total": 0, "by_method": {}, "log": []}

HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade", "expect", "host"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        pass

    def _stats_endpoint(self):
        if self.path.startswith("/__proxy_stats__/reset"):
            with _lock:
                _stats["total"] = 0
                _stats["by_method"] = {}
                _stats["log"] = []
            body = b"{}"
        else:
            with _lock:
                body = json.dumps(_stats).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self):
        if self.path.startswith("/__proxy_stats__"):
            return self._stats_endpoint()
        time.sleep(LATENCY_S)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        rng = self.headers.get("Range", "")
        with _lock:
            _stats["total"] += 1
            _stats["by_method"][self.command] = _stats["by_method"].get(self.command, 0) + 1
            if len(_stats["log"]) < 4000:
                _stats["log"].append(f"{self.command} {self.path.split('?')[0]}"
                                     + (f" [{rng}]" if rng else ""))
        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=60)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
        headers["Host"] = f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            self.send_response(resp.status)
            # A HEAD's Content-Length describes the entity, not the (empty)
            # body — rewriting it to 0 makes S3 clients think the object is
            # empty and fall back to whole-file downloads.
            keep_length = self.command == "HEAD"
            for k, v in resp.getheaders():
                if k.lower() in HOP_HEADERS or (k.lower() == "content-length" and not keep_length):
                    continue
                self.send_header(k, v)
            if not keep_length:
                self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception:
            try:
                self.send_error(502)
            except Exception:
                pass
        finally:
            conn.close()

    do_GET = do_PUT = do_POST = do_HEAD = do_DELETE = _forward


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), Handler)
    print(f"proxy: :{LISTEN_PORT} -> :{UPSTREAM_PORT} (+{LATENCY_S * 1000:.0f}ms/request)",
          flush=True)
    server.serve_forever()
