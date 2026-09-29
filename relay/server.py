"""ANCHOR relay for Render (Frankfurt). Forwards pre-signed Bitget calls."""
import json, os, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    def _reply(self, code, raw):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._reply(200, b'{"ok":true,"relay":"anchor"}')

    def do_HEAD(self):
        self.send_response(200); self.end_headers()

    def do_POST(self):
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        except Exception:
            return self._reply(400, b'{"code":"relay400","msg":"bad json"}')
        path = req.get("path", "")
        if not path.startswith("/api/v2/mix/"):
            return self._reply(403, b'{"code":"relay403","msg":"path not allowed"}')
        body = req.get("body") or ""
        r = urllib.request.Request("https://api.bitget.com" + path, data=body.encode() if body else None,
                                   headers=req.get("headers") or {}, method=req.get("method", "GET"))
        try:
            with urllib.request.urlopen(r, timeout=12) as resp:
                code, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            code, raw = e.code, e.read()
        except Exception as e:
            code, raw = 502, json.dumps({"code": "relay502", "msg": str(e)}).encode()
        self._reply(code, raw)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))), H).serve_forever()
