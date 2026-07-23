import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DATA_ROOT = "/data"


def state_path(feed):
    return os.path.join(DATA_ROOT, feed, "state.json")


def load_state(feed):
    path = state_path(feed)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(feed, state):
    path = state_path(feed)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def valid_feed(feed):
    return bool(feed) and "/" not in feed and ".." not in feed


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/state":
            feed = parse_qs(parsed.query).get("feed", [""])[0]
            if not valid_feed(feed):
                self._send_json(400, {"error": "invalid feed"})
                return
            self._send_json(200, load_state(feed))
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/state":
            feed = parse_qs(parsed.query).get("feed", [""])[0]
            if not valid_feed(feed):
                self._send_json(400, {"error": "invalid feed"})
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                self._send_json(400, {"error": "invalid json"})
                return
            filename = body.get("filename")
            if not filename:
                self._send_json(400, {"error": "missing filename"})
                return
            state = load_state(feed)
            entry = state.get(filename, {})
            for k, v in body.items():
                if k != "filename":
                    entry[k] = v
            state[filename] = entry
            save_state(feed, state)
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": "not found"})

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8098), Handler)
    server.serve_forever()
