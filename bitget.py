"""Minimal Bitget v2 client. Signs locally; can route through the Vercel relay."""
import base64, hashlib, hmac, json, time, urllib.request, urllib.parse, urllib.error

BASE = "https://api.bitget.com"


class BitgetError(Exception):
    pass


class Bitget:
    def __init__(self, key="", secret="", passphrase="", demo=False, relay=None, bypass=None):
        self.key, self.secret, self.passphrase = key, secret, passphrase
        self.demo, self.relay, self.bypass = demo, (relay or "").strip() or None, bypass

    def _send(self, method, path, params=None, body=None, auth=False):
        query = urllib.parse.urlencode(params) if params else ""
        req_path = path + ("?" + query if query else "")
        body_str = json.dumps(body, separators=(",", ":")) if body else ""
        headers = {"Content-Type": "application/json", "locale": "en-US"}
        if self.demo:
            headers["paptrading"] = "1"
        if auth:
            ts = str(int(time.time() * 1000))
            msg = ts + method.upper() + req_path + body_str
            sign = base64.b64encode(
                hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).digest()
            ).decode()
            headers.update({
                "ACCESS-KEY": self.key, "ACCESS-SIGN": sign,
                "ACCESS-TIMESTAMP": ts, "ACCESS-PASSPHRASE": self.passphrase,
            })
        if self.relay:
            payload = json.dumps({"method": method, "path": req_path,
                                  "headers": headers, "body": body_str}).encode()
            h = {"Content-Type": "application/json"}
            if self.bypass:
                h["x-vercel-protection-bypass"] = self.bypass
            req = urllib.request.Request(self.relay.rstrip("/") + "/forward",
                                         data=payload, headers=h, method="POST")
        else:
            req = urllib.request.Request(BASE + req_path,
                                         data=body_str.encode() if body_str else None,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
        except Exception as e:
            raise BitgetError(f"{path}: network {e}")
        try:
            data = json.loads(raw)
        except Exception:
            raise BitgetError(f"{path}: non-JSON reply {raw[:120]!r}")
        if str(data.get("code")) != "00000":
            raise BitgetError(f"{path}: {data.get('code')} {data.get('msg')}")
        return data.get("data")

    def get(self, path, params=None, auth=False):
        return self._send("GET", path, params=params, auth=auth)

    def post(self, path, body):
        return self._send("POST", path, body=body, auth=True)
