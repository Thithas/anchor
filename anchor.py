#!/usr/bin/env python3
"""ANCHOR - Bitget USDT-M futures agent. Survive first, then grow.

Rules live in params.json (hot-reloaded). MODE file = demo | live. HALT file = stop.
"""
import datetime as dt
import json, math, os, subprocess, time, traceback, urllib.request, urllib.parse

from bitget import Bitget, BitgetError

ROOT = os.path.dirname(os.path.abspath(__file__))
P = lambda f: os.path.join(ROOT, f)
RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "50"))
GRAN = {"15m": 900, "1H": 3600}


# ---------------- small helpers ----------------
def read(f, default=""):
    try:
        with open(P(f)) as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return default


def load_json(f, default):
    try:
        with open(P(f)) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(f, obj):
    if f == "state.json" and os.environ.get("TG_CHAT") and not os.environ.get("TG_CHAT_FROM_SECRET"):
        obj.setdefault("tg_chat", os.environ["TG_CHAT"])
    with open(P(f), "w") as fh:
        json.dump(obj, fh, indent=2)


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def log(msg):
    print(f"{utcnow():%H:%M:%S} {msg}", flush=True)


def tg_chat():
    """Use TG_CHAT if set; otherwise learn it from the last message sent to the bot."""
    chat = os.environ.get("TG_CHAT") or load_json("state.json", {}).get("tg_chat")
    token = os.environ.get("TG_TOKEN")
    if chat or not token:
        return chat
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10) as r:
            ups = json.load(r).get("result", [])
        for u in reversed(ups):
            m = u.get("message") or u.get("my_chat_member") or {}
            if m.get("chat", {}).get("id"):
                chat = str(m["chat"]["id"])
                os.environ["TG_CHAT"] = chat
                st = load_json("state.json", {})
                st["tg_chat"] = chat
                save_json("state.json", st)
                return chat
    except Exception as e:
        log(f"telegram chat lookup failed: {e}")
    return None


def tg(msg):
    log("TG " + msg.replace("\n", " | "))
    token, chat = os.environ.get("TG_TOKEN"), tg_chat()
    if not token or not chat:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10)
    except Exception as e:
        log(f"telegram failed: {e}")


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def commit(msg):
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    files = [f for f in ("state.json", "trades.jsonl", "journal.md", "status.md", "HALT") if os.path.exists(P(f))]
    git("add", "-A", *files)
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-q", "-m", msg)
    git("pull", "--rebase", "--autostash", "-q")
    r = git("push", "-q")
    if r.returncode:
        log("push failed: " + r.stderr[-200:])


def pull():
    if os.environ.get("GITHUB_ACTIONS"):
        git("pull", "--rebase", "--autostash", "-q")


def status(line):
    with open(P("status.md"), "w") as fh:
        fh.write(f"{utcnow():%Y-%m-%d %H:%M} UTC - {line}\n")


def journal(line):
    with open(P("journal.md"), "a") as fh:
        fh.write(f"- {utcnow():%Y-%m-%d %H:%M} UTC - {line}\n")


def record_trade(t):
    with open(P("trades.jsonl"), "a") as fh:
        fh.write(json.dumps(t) + "\n")


# ---------------- indicators ----------------
def ema(vals, n):
    k, e, out = 2 / (n + 1), vals[0], []
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def atr(c, n):
    trs = [c[0]["h"] - c[0]["l"]]
    for i in range(1, len(c)):
        pc = c[i - 1]["c"]
        trs.append(max(c[i]["h"] - c[i]["l"], abs(c[i]["h"] - pc), abs(c[i]["l"] - pc)))
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def signal(c15, c1h, p):
    """Return (side, entry, stop, tp, atr) or None. Uses closed candles only."""
    if len(c15) < 40 or len(c1h) < 60:
        return None
    h = [x["c"] for x in c1h]
    e50 = ema(h, 50)
    if h[-1] > e50[-1] and e50[-1] > e50[-4]:
        trend = "long"
    elif h[-1] < e50[-1] and e50[-1] < e50[-4]:
        trend = "short"
    else:
        return None
    closes = [x["c"] for x in c15]
    e20 = ema(closes, 20)[-1]
    a = atr(c15, 14)
    last = c15[-1]
    if trend == "long" and last["l"] <= e20 < last["c"]:
        entry = last["c"] - p["entry_offset_atr"] * a
        stop = entry - p["stop_atr"] * a
        tp = entry + p["tp_r"] * (entry - stop)
    elif trend == "short" and last["h"] >= e20 > last["c"]:
        entry = last["c"] + p["entry_offset_atr"] * a
        stop = entry + p["stop_atr"] * a
        tp = entry - p["tp_r"] * (stop - entry)
    else:
        return None
    return trend, entry, stop, tp, a


# ---------------- exchange wrapper ----------------
class Ex:
    def __init__(self, mode, p):
        relay, bypass = os.environ.get("RELAY_URL"), os.environ.get("VERCEL_BYPASS")
        pre = "LIVE" if mode == "live" else "DEMO"
        self.demo = mode != "live"
        self.api = Bitget(os.environ.get(f"{pre}_KEY", ""), os.environ.get(f"{pre}_SECRET", ""),
                          os.environ.get(f"{pre}_PASSPHRASE", ""), demo=self.demo, relay=relay, bypass=bypass)
        self.mkt = Bitget(relay=relay, bypass=bypass)  # real prices for signals
        self.coins = p["coins"]
        self.contracts, self.pt, self.mc = self._discover()

    def _discover(self):
        tries = [("SUSDT-FUTURES", True), ("USDT-FUTURES", True)] if self.demo else [("USDT-FUTURES", False)]
        for pt, _ in tries:
            try:
                data = self.api.get("/api/v2/mix/market/contracts", {"productType": pt}) or []
            except BitgetError as e:
                log(f"contracts {pt}: {e}")
                continue
            found = {}
            for coin in self.coins:
                names = {f"{coin}USDT", f"S{coin}SUSDT"}
                for c in data:
                    if c.get("symbol") in names:
                        found[coin] = c
            if len(found) == len(self.coins):
                c0 = next(iter(found.values()))
                mc = (c0.get("supportMarginCoins") or ["SUSDT" if pt.startswith("S") else "USDT"])[0]
                log(f"contracts ok: {pt} {mc} {[c['symbol'] for c in found.values()]}")
                return found, pt, mc
        raise BitgetError("could not find contracts for " + ",".join(self.coins))

    def sym(self, coin):
        return self.contracts[coin]["symbol"]

    def setup(self, lev):
        for label, fn in [("pos mode", lambda: self.api.post("/api/v2/mix/account/set-position-mode",
                                                             {"productType": self.pt, "posMode": "one_way_mode"}))]:
            try:
                fn()
            except BitgetError as e:
                log(f"{label}: {e}")
        for coin in self.coins:
            base = {"symbol": self.sym(coin), "productType": self.pt, "marginCoin": self.mc}
            try:
                self.api.post("/api/v2/mix/account/set-margin-mode", {**base, "marginMode": "isolated"})
            except BitgetError as e:
                log(f"{coin} margin mode: {e}")
            for side in ("long", "short"):
                try:
                    self.api.post("/api/v2/mix/account/set-leverage", {**base, "leverage": str(lev), "holdSide": side})
                except BitgetError as e:
                    log(f"{coin} leverage {side}: {e}")

    def candles(self, coin, gran, limit):
        rows = self.mkt.get("/api/v2/mix/market/candles", {"symbol": f"{coin}USDT", "productType": "USDT-FUTURES",
                                                            "granularity": gran, "limit": str(limit)}) or []
        c = sorted(({"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])}
                    for r in rows), key=lambda x: x["t"])
        cutoff = time.time() * 1000 - GRAN[gran] * 1000
        return [x for x in c if x["t"] <= cutoff]  # drop the still-forming candle

    def prices(self):
        rows = self.mkt.get("/api/v2/mix/market/tickers", {"productType": "USDT-FUTURES"}) or []
        want = {f"{c}USDT": c for c in self.coins}
        return {want[r["symbol"]]: float(r["lastPr"]) for r in rows if r.get("symbol") in want}

    def equity(self):
        rows = self.api.get("/api/v2/mix/account/accounts", {"productType": self.pt}, auth=True) or []
        for r in rows:
            if r.get("marginCoin", "").upper() == self.mc.upper():
                return float(r.get("accountEquity") or r.get("usdtEquity") or 0), float(r.get("available") or 0)
        raise BitgetError("no account row for " + self.mc)

    def positions(self):
        rows = self.api.get("/api/v2/mix/position/all-position",
                            {"productType": self.pt, "marginCoin": self.mc}, auth=True) or []
        bysym = {self.sym(c): c for c in self.coins}
        return {bysym[r["symbol"]]: r for r in rows if r.get("symbol") in bysym and float(r.get("total") or 0) > 0}

    def fmt_size(self, coin, q):
        c = self.contracts[coin]
        mult = float(c.get("sizeMultiplier") or 10 ** -int(c.get("volumePlace", 3)))
        q = math.floor(q / mult + 1e-9) * mult
        return q, f"{q:.{int(c.get('volumePlace', 3))}f}"

    def fmt_price(self, coin, px):
        c = self.contracts[coin]
        dp = int(c.get("pricePlace", 2))
        step = int(c.get("priceEndStep", 1) or 1) * 10 ** -dp
        return f"{round(px / step) * step:.{dp}f}"

    def place(self, coin, side, size_s, price, sl, tp):
        body = {"symbol": self.sym(coin), "productType": self.pt, "marginMode": "isolated",
                "marginCoin": self.mc, "size": size_s, "price": self.fmt_price(coin, price),
                "side": "buy" if side == "long" else "sell", "orderType": "limit", "force": "post_only",
                "clientOid": f"anchor{int(time.time() * 1000)}",
                "presetStopLossPrice": self.fmt_price(coin, sl),
                "presetStopSurplusPrice": self.fmt_price(coin, tp)}
        return self.api.post("/api/v2/mix/order/place-order", body)["orderId"]

    def order(self, coin, oid):
        return self.api.get("/api/v2/mix/order/detail",
                            {"symbol": self.sym(coin), "productType": self.pt, "orderId": oid}, auth=True)

    def cancel(self, coin, oid):
        try:
            self.api.post("/api/v2/mix/order/cancel-order",
                          {"symbol": self.sym(coin), "productType": self.pt, "orderId": oid})
        except BitgetError as e:
            log(f"cancel {coin}: {e}")

    def close(self, coin):
        self.api.post("/api/v2/mix/order/close-positions", {"symbol": self.sym(coin), "productType": self.pt})

    def last_closed(self, coin):
        d = self.api.get("/api/v2/mix/position/history-position",
                         {"productType": self.pt, "symbol": self.sym(coin), "limit": "5"}, auth=True) or {}
        rows = d.get("list", d) if isinstance(d, dict) else d
        rows = sorted(rows or [], key=lambda r: int(r.get("utime") or r.get("ctime") or 0))
        return rows[-1] if rows else None


# ---------------- the agent ----------------
class Anchor:
    def __init__(self):
        self.p = load_json("params.json", {})
        self.mode = read("MODE", "demo").lower()
        self.s = load_json("state.json", {})
        if self.s.get("mode") != self.mode:
            self.s = {"mode": self.mode, "pending": {}, "open": {}, "cooldown": {}, "last_candle": {}}
            journal(f"state reset for {self.mode.upper()} mode")
        self.ex = Ex(self.mode, self.p)
        self.ex.setup(self.p["lev_cap"])
        self.c15, self.c1h, self.fetched = {}, {}, {}

    # virtual $40 bankroll in demo, real balance in live
    def eq(self):
        real, avail = self.ex.equity()
        if self.mode == "live":
            return real, avail
        if self.s.get("demo_base") is None:
            self.s["demo_base"] = real
        v = self.p["bankroll"] + (real - self.s["demo_base"])
        return v, v

    def tag(self):
        return "LIVE" if self.mode == "live" else "DEMO"

    def halt(self, why):
        for coin, o in list(self.s["pending"].items()):
            self.ex.cancel(coin, o["oid"])
        self.s["pending"] = {}
        with open(P("HALT"), "w") as fh:
            fh.write(f"{utcnow():%Y-%m-%d %H:%M} UTC: {why}\n")
        journal("HALT: " + why)
        tg(f"ANCHOR {self.tag()} HALTED\n{why}\nDelete the HALT file to restart.")
        save_json("state.json", self.s)
        commit("anchor: halt")

    def day_roll(self, equity):
        today = f"{utcnow():%Y-%m-%d}"
        if self.s.get("day") != today:
            if self.s.get("day"):
                start = self.s.get("day_start_equity") or equity
                tg(f"ANCHOR {self.tag()} day {self.s['day']} done\n"
                   f"equity ${equity:.2f} ({equity - start:+.2f})\nentries {self.s.get('entries_today', 0)}")
            self.s.update(day=today, day_start_equity=equity, entries_today=0, paused=False)

    def sync_closed(self, pos, prices):
        for coin in list(self.s["open"]):
            if coin in pos:
                continue
            o = self.s["open"].pop(coin)
            h = None
            try:
                h = self.ex.last_closed(coin)
            except BitgetError as e:
                log(f"history {coin}: {e}")
            pnl = float((h or {}).get("netProfit") or (h or {}).get("pnl") or 0)
            fees = abs(float((h or {}).get("openFee") or 0)) + abs(float((h or {}).get("closeFee") or 0))
            funding = float((h or {}).get("totalFunding") or 0)
            exit_px = float((h or {}).get("closeAvgPrice") or prices.get(coin, 0))
            eq_after, _ = self.eq()
            t = {"mode": self.mode, "coin": coin, "side": o["side"], "entry": o["entry"], "exit": exit_px,
                 "stop": o["stop"], "tp": o["tp"], "size": o["size"], "opened": o["opened"],
                 "closed": utcnow().isoformat(), "net_pnl": pnl, "fees": fees, "funding": funding,
                 "equity_after": round(eq_after, 4), "exit_reason": o.get("exit_reason", "stop/tp on exchange")}
            record_trade(t)
            journal(f"CLOSE {coin} {o['side']} {o['entry']:.4f}->{exit_px:.4f} net {pnl:+.4f} "
                    f"(fees {fees:.4f}) equity ${eq_after:.2f} - {t['exit_reason']}")
            tg(f"{'WIN' if pnl > 0 else 'LOSS'} {coin} {o['side']} {pnl:+.2f} USDT\n"
               f"fees {fees:.3f} | equity ${eq_after:.2f} ({self.tag()})")
            if pnl <= 0:
                self.s["cooldown"][coin] = time.time() + self.p["cooldown_min"] * 60
            commit(f"anchor: close {coin}")

    def manage_pending(self, pos):
        tf = GRAN["15m"]
        for coin, o in list(self.s["pending"].items()):
            try:
                d = self.ex.order(coin, o["oid"])
            except BitgetError as e:
                log(f"order {coin}: {e}")
                continue
            state = d.get("state") or d.get("status")
            filled = float(d.get("baseVolume") or 0)
            expired = time.time() - o["placed"] > self.p["order_ttl_candles"] * tf
            if state == "filled" or (coin in pos and (expired or state != "live")):
                if state != "filled":
                    self.ex.cancel(coin, o["oid"])
                px = float(d.get("priceAvg") or o["entry"])
                size = float(pos[coin]["total"]) if coin in pos else filled
                self.s["open"][coin] = {**o, "entry": px, "size": size, "opened": utcnow().isoformat(),
                                        "best": px, "trail": None}
                del self.s["pending"][coin]
                journal(f"OPEN {coin} {o['side']} size {size} @ {px:.4f} stop {o['stop']:.4f} tp {o['tp']:.4f}")
                tg(f"ANCHOR {self.tag()} OPEN {coin} {o['side'].upper()}\n@ {px:.4f} | stop {o['stop']:.4f} | "
                   f"tp {o['tp']:.4f}")
                commit(f"anchor: open {coin}")
            elif state in ("canceled", "cancelled") or expired:
                if state not in ("canceled", "cancelled"):
                    self.ex.cancel(coin, o["oid"])
                del self.s["pending"][coin]
                journal(f"unfilled {coin} {o['side']} limit cancelled")

    def manage_open(self, prices):
        for coin, o in self.s["open"].items():
            px = prices.get(coin)
            if not px:
                continue
            long = o["side"] == "long"
            o["best"] = max(o["best"], px) if long else min(o["best"], px)
            r = abs(o["entry"] - o["stop"])
            gain = (o["best"] - o["entry"]) if long else (o["entry"] - o["best"])
            if gain >= self.p["trail_after_r"] * r:
                t = o["best"] - self.p["trail_atr"] * o["atr"] if long else o["best"] + self.p["trail_atr"] * o["atr"]
                o["trail"] = max(t, o["trail"] or t) if long else min(t, o["trail"] or t)
            if o["trail"] and ((long and px <= o["trail"]) or (not long and px >= o["trail"])):
                try:
                    self.ex.close(coin)
                    o["exit_reason"] = f"trailing stop {o['trail']:.4f}"
                    log(f"trail close {coin} @ {px}")
                except BitgetError as e:
                    log(f"trail close {coin}: {e}")

    def refresh_candles(self, coin):
        now = time.time()
        for gran, store, n in (("15m", self.c15, 120), ("1H", self.c1h, 120)):
            key = (coin, gran)
            slot = int(now // GRAN[gran])
            if self.fetched.get(key) != slot and now % GRAN[gran] > 5:
                store[coin] = self.ex.candles(coin, gran, n)
                self.fetched[key] = slot

    def try_entries(self, equity, avail):
        p = self.p
        if self.s.get("paused") or self.s.get("entries_today", 0) >= p["max_entries_day"]:
            return
        for coin in p["coins"]:
            if len(self.s["open"]) + len(self.s["pending"]) >= p["max_open"]:
                return
            if coin in self.s["open"] or coin in self.s["pending"]:
                continue
            if time.time() < self.s["cooldown"].get(coin, 0):
                continue
            try:
                self.refresh_candles(coin)
            except BitgetError as e:
                log(f"candles {coin}: {e}")
                continue
            c15, c1h = self.c15.get(coin, []), self.c1h.get(coin, [])
            if not c15 or self.s["last_candle"].get(coin) == c15[-1]["t"]:
                continue
            self.s["last_candle"][coin] = c15[-1]["t"]
            sig = signal(c15, c1h, p)
            if not sig:
                continue
            side, entry, stop, tp, a = sig
            dist = abs(entry - stop)
            q = equity * p["risk_pct"] / dist
            q = min(q, equity * p["lev_cap"] / p["max_open"] / entry)
            c = self.ex.contracts[coin]
            minq = float(c.get("minTradeNum") or 0)
            if q < minq:
                if minq * dist / equity > p["max_risk_pct"]:
                    journal(f"skip {coin} {side}: min size would risk {minq * dist / equity:.1%}")
                    continue
                q = minq
            q, qs = self.ex.fmt_size(coin, q)
            notional = q * entry
            if q <= 0 or notional < float(c.get("minTradeUSDT") or 5):
                journal(f"skip {coin} {side}: size too small (${notional:.2f})")
                continue
            if notional / p["lev_cap"] > avail * 0.95:
                journal(f"skip {coin} {side}: not enough margin")
                continue
            try:
                oid = self.ex.place(coin, side, qs, entry, stop, tp)
            except BitgetError as e:
                journal(f"order rejected {coin} {side}: {e}")
                continue
            self.s["pending"][coin] = {"oid": oid, "side": side, "entry": entry, "stop": stop, "tp": tp,
                                       "atr": a, "size": q, "placed": time.time()}
            self.s["entries_today"] = self.s.get("entries_today", 0) + 1
            risk = q * dist
            journal(f"LIMIT {coin} {side} {qs} @ {entry:.4f} stop {stop:.4f} tp {tp:.4f} risk ${risk:.2f}")
            tg(f"ANCHOR {self.tag()} setup {coin} {side.upper()}\nlimit {entry:.4f} | risk ${risk:.2f} "
               f"| notional ${notional:.2f}")
            commit(f"anchor: limit {coin}")

    def tick(self):
        equity, avail = self.eq()
        self.day_roll(equity)
        self.s["equity"] = round(equity, 4)
        if equity <= self.p["survival_floor"]:
            for coin in list(self.s["open"]):
                try:
                    self.ex.close(coin)
                    self.s["open"][coin]["exit_reason"] = "survival line"
                except BitgetError as e:
                    log(f"survival close {coin}: {e}")
            self.halt(f"equity ${equity:.2f} hit survival line ${self.p['survival_floor']}")
            return False
        start = self.s.get("day_start_equity") or equity
        if not self.s.get("paused") and equity <= start * (1 - self.p["daily_loss_pct"]):
            self.s["paused"] = True
            journal(f"daily loss limit hit (${equity:.2f} vs ${start:.2f}); paused until next UTC day")
            tg(f"ANCHOR {self.tag()} paused for today: equity ${equity:.2f} ({equity - start:+.2f})")
        prices = self.ex.prices()
        pos = self.ex.positions()
        self.sync_closed(pos, prices)
        self.manage_pending(pos)
        self.manage_open(prices)
        self.try_entries(equity, avail)
        return True

    def run(self):
        t0, last_pull, last_beat = time.time(), time.time(), self.s.get("last_beat", 0)
        e0 = self.eq()[0]
        status(f"{self.tag()} connected to Bitget, equity ${e0:.2f}")
        save_json("state.json", self.s)
        commit("anchor: awake")
        tg(f"ANCHOR {self.tag()} awake | equity ${e0:.2f} | open {list(self.s['open']) or 'none'}")
        errors = 0
        while time.time() - t0 < RUN_MINUTES * 60:
            if time.time() - last_pull > 120:
                pull()
                last_pull = time.time()
                self.p = load_json("params.json", self.p)
                if read("MODE", self.mode).lower() != self.mode:
                    for coin, o in list(self.s["pending"].items()):
                        self.ex.cancel(coin, o["oid"])
                    tg("MODE changed - restarting in new mode")
                    break
            if os.path.exists(P("HALT")):
                log("HALT file present - stopping")
                return False
            try:
                if not self.tick():
                    return False
                errors = 0
            except Exception as e:
                errors += 1
                log(f"tick error: {e}\n{traceback.format_exc()}")
                if errors in (3, 20):
                    tg(f"ANCHOR {self.tag()} error x{errors}: {str(e)[:200]}")
            if time.time() - last_beat > 4 * 3600:
                last_beat = self.s["last_beat"] = time.time()
                tg(f"ANCHOR {self.tag()} alive | equity ${self.s.get('equity', 0):.2f} | "
                   f"open {list(self.s['open']) or 'none'} | today {self.s.get('entries_today', 0)} entries")
            save_json("state.json", self.s)
            time.sleep(self.p.get("scan_seconds", 30))
        save_json("state.json", self.s)
        commit("anchor: checkpoint")
        return True


def redispatch():
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        return
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/anchor.yml/dispatches",
        data=json.dumps({"ref": os.environ.get("GITHUB_REF_NAME", "main")}).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    try:
        urllib.request.urlopen(req, timeout=15)
        log("next run dispatched")
    except Exception as e:
        log(f"redispatch failed (cron will cover): {e}")


if __name__ == "__main__":
    if os.path.exists(P("HALT")):
        print("HALT present - not trading. Delete HALT to restart.")
        raise SystemExit(0)
    try:
        keep_going = Anchor().run()
    except Exception as e:
        status(f"FAILED to start: {str(e)[:300]}")
        commit("anchor: start failed")
        tg(f"ANCHOR failed to start: {str(e)[:300]}")
        raise
    if keep_going:
        redispatch()
