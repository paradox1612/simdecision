#!/usr/bin/env python3
"""Generic fixture HTTP server for scenarios: canned routes, fault endpoints, and a JSONL request log.

  --routes '{"/api/items": {"json": [{"id": 1}]}, "/health": {"status": 204}, "/slow-api": {"json": {}, "delay_ms": 3000}}'
Built-in endpoints (loading/timeout scenarios):  /slow?ms=3000   /status/<code>   /drop (closes the socket)
Every request is appended to --log as {"method", "path", "t"} so scenarios can assert the app made (or did not make) a call.
"""
import argparse, json, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOCK = threading.Lock()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--routes", default="{}")
    a = ap.parse_args()
    routes = json.loads(a.routes)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _record(self):
            with LOCK, open(a.log, "a") as f:
                f.write(json.dumps({"method": self.command, "path": self.path, "t": round(time.time(), 3)}) + "\n")

        def _send(self, status, body=b"", ctype="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._record()
            u = urlparse(self.path)
            if u.path == "/slow":
                time.sleep(int(parse_qs(u.query).get("ms", ["3000"])[0]) / 1000)
                return self._send(200, b"{}")
            if u.path.startswith("/status/"):
                return self._send(int(u.path.rsplit("/", 1)[1] or 500), b"{}")
            if u.path == "/drop":
                self.close_connection = True
                return self.connection.close()
            r = routes.get(u.path)
            if r is None:
                return self._send(404, b'{"error": "no such route"}')
            time.sleep(r.get("delay_ms", 0) / 1000)
            if "json" in r:
                return self._send(r.get("status", 200), json.dumps(r["json"]).encode())
            return self._send(r.get("status", 200), str(r.get("body", "")).encode(), r.get("type", "text/plain"))

        do_POST = do_GET

    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
