"""A stand-in Binance TH server, for developing without hitting the real API.

Speaks the ``/open/v1`` dialect (pass ``--dialect thv1``/``apiv3`` for the
others) and
serves a synthetic account that exercises the interesting cases: baht-quoted
buys, tether-quoted buys, a stablecoin funding leg, fiat deposits, a crypto
withdrawal and a BNB-paid commission.

    python3 tools/mock_binance_th.py --port 9998
    BINANCE_TH_BASE_URL=http://127.0.0.1:9998 \
    BINANCE_TH_API_KEY=demo BINANCE_TH_API_SECRET=demo \
    python3 -m app.main sync
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import random
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qsl, urlparse

DAY = 86_400_000
NOW = int(time.time() * 1000)
SECRET = "demo"

SYMBOLS = [
    ("BTCTHB", "BTC", "THB"), ("ETHTHB", "ETH", "THB"), ("USDTTHB", "USDT", "THB"),
    ("BNBTHB", "BNB", "THB"), ("SOLTHB", "SOL", "THB"),
    ("BTCUSDT", "BTC", "USDT"), ("ETHUSDT", "ETH", "USDT"),
    ("SOLUSDT", "SOL", "USDT"), ("BNBUSDT", "BNB", "USDT"),
]

SPOT = {
    "BTCTHB": 3_150_000, "ETHTHB": 118_000, "USDTTHB": 34.85, "BNBTHB": 22_400,
    "SOLTHB": 6_100, "BTCUSDT": 90_400, "ETHUSDT": 3_386, "SOLUSDT": 175,
    "BNBUSDT": 643,
}

# (id, symbol, side, price, qty, days_ago, fee, fee_asset)
TRADES = [
    ("101", "USDTTHB", "BUY",      34.10,   30_000, 300, 0.30,   "USDT"),
    ("102", "BTCTHB",  "BUY",  2_310_000,  0.05,    295, 0.00005, "BTC"),
    ("103", "BNBTHB",  "BUY",     18_900,  2.0,     290, 0.002,   "BNB"),
    ("104", "BTCUSDT", "BUY",     68_500,  0.08,    240, 0.006,   "BNB"),
    ("105", "ETHTHB",  "BUY",     92_000,  1.2,     210, 0.0012,  "ETH"),
    ("106", "SOLUSDT", "BUY",        118,  40,      180, 0.03,    "BNB"),
    ("107", "USDTTHB", "BUY",      35.40,  20_000,  150, 0.20,    "USDT"),
    ("108", "ETHUSDT", "BUY",      2_950,  1.5,     120, 0.0015,  "ETH"),
    ("109", "BTCTHB",  "SELL", 2_980_000,  0.03,     90, 894.00,  "THB"),
    ("110", "SOLUSDT", "SELL",       196,  15,       60, 2.94,    "USDT"),
    ("111", "ETHTHB",  "BUY",    104_500,  0.8,      45, 0.0008,  "ETH"),
    ("112", "BTCUSDT", "BUY",     84_200,  0.02,     20, 0.00002, "BTC"),
    ("113", "USDTTHB", "SELL",     34.90,  5_000,     7, 4.36,    "THB"),
]

DEPOSITS = [
    ("d1", "THB", 500_000, 320, 0),
    ("d2", "THB", 700_000, 250, 0),
    ("d3", "USDT", 2_000,  200, 0),
    ("d4", "THB", 300_000,  70, 0),
]
WITHDRAWALS = [
    ("w1", "THB", 150_000, 100, 0),
    ("w2", "BTC", 0.01,     50, 0.0002),
]


def trade_rows(symbol):
    out = []
    for tid, sym, side, price, qty, days, fee, fee_asset in TRADES:
        if sym != symbol:
            continue
        out.append({
            "id": tid, "symbol": sym, "orderId": "o" + tid,
            "price": str(price), "qty": str(qty),
            "quoteQty": str(round(price * qty, 8)),
            "commission": str(fee), "commissionAsset": fee_asset,
            "time": NOW - days * DAY, "isBuyer": side == "BUY", "isMaker": False,
        })
    return out


def balances():
    held = {}
    for _, sym, side, price, qty, _, fee, fee_asset in TRADES:
        base, quote = next((b, q) for s, b, q in SYMBOLS if s == sym)
        if side == "BUY":
            held[base] = held.get(base, 0) + qty
            held[quote] = held.get(quote, 0) - price * qty
        else:
            held[base] = held.get(base, 0) - qty
            held[quote] = held.get(quote, 0) + price * qty
        held[fee_asset] = held.get(fee_asset, 0) - fee
    for _, asset, amount, _, _ in DEPOSITS:
        held[asset] = held.get(asset, 0) + amount
    for _, asset, amount, _, fee in WITHDRAWALS:
        held[asset] = held.get(asset, 0) - amount - fee
    return [{"asset": a, "free": f"{max(v, 0):.8f}", "locked": "0"}
            for a, v in sorted(held.items()) if abs(v) > 1e-9]


def klines(symbol, start, end, interval):
    """Deterministic pseudo-history: a smooth drift plus a seeded wobble."""
    step = DAY if interval == "1d" else 3_600_000
    spot = SPOT.get(symbol)
    if spot is None:
        return []
    out = []
    t = (start // step) * step
    while t <= end and len(out) < 1000:
        age_days = (NOW - t) / DAY
        drift = 1 - min(age_days, 400) * 0.0009
        rng = random.Random(f"{symbol}{t // step}")
        wobble = 1 + (rng.random() - 0.5) * 0.05
        close = spot * drift * wobble
        out.append([t, f"{close:.8f}", f"{close * 1.01:.8f}",
                    f"{close * 0.99:.8f}", f"{close:.8f}", "1.0"])
        t += step
    return out


class Handler(BaseHTTPRequestHandler):
    dialect = "openv1"

    def log_message(self, *args):
        pass

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _wrap(self, data):
        if self.dialect == "openv1":
            return {"code": 0, "msg": "", "data": data, "timestamp": NOW}
        return data

    def _check_signature(self, query: str) -> bool:
        params = dict(parse_qsl(query))
        signature = params.pop("signature", None)
        if not signature:
            return False
        base = query.split("&signature=")[0]
        expected = hmac.new(SECRET.encode(), base.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def do_GET(self):
        url = urlparse(self.path)
        path, query = url.path, url.query
        params = dict(parse_qsl(query))
        signed = any(k in path for k in
                     ("account", "myTrades", "userTrades", "orders/trades", "deposit",
                      "withdraw"))

        if signed:
            if self.headers.get("X-MBX-APIKEY") is None:
                return self._send({"code": -2015, "msg": "missing API key"}, 401)
            if not self._check_signature(query):
                return self._send({"code": -1022, "msg": "bad signature"}, 401)

        # --- public ---------------------------------------------------
        if path in ("/open/v1/common/time", "/api/v3/time", "/api/v1/time"):
            return self._send(self._wrap({"serverTime": int(time.time() * 1000)}))

        if path in ("/open/v1/common/symbols", "/api/v3/exchangeInfo",
                    "/api/v1/exchangeInfo"):
            rows = [{"symbol": s, "baseAsset": b, "quoteAsset": q, "status": "TRADING"}
                    for s, b, q in SYMBOLS]
            if self.dialect == "openv1":
                return self._send(self._wrap({"list": rows}))
            return self._send({"symbols": rows})

        if path in ("/open/v1/market/tickers", "/api/v3/ticker/price",
                    "/api/v1/ticker/price"):
            rows = [{"symbol": s, "price": f"{p:.8f}"} for s, p in SPOT.items()]
            if self.dialect == "openv1":
                return self._send(self._wrap({"list": rows}))
            return self._send(rows)

        if path in ("/open/v1/market/klines", "/api/v3/klines", "/api/v1/klines"):
            symbol = params.get("symbol", "")
            interval = params.get("interval", "1d")
            start = int(params.get("startTime") or NOW - 400 * DAY)
            end = int(params.get("endTime") or NOW)
            rows = klines(symbol, start, end, interval)
            if not rows:
                return self._send({"code": -1121, "msg": "Invalid symbol."}, 400)
            if self.dialect == "openv1":
                return self._send(self._wrap({"list": rows}))
            return self._send(rows)

        # --- signed ---------------------------------------------------
        if path in ("/open/v1/account/spot", "/api/v3/account", "/api/v1/accountV2"):
            if self.dialect == "openv1":
                return self._send(self._wrap({"accountAssets": balances()}))
            return self._send({"balances": balances()})

        if path in ("/open/v1/orders/trades", "/api/v3/myTrades",
                    "/api/v1/userTrades"):
            symbol = params.get("symbol", "")
            rows = trade_rows(symbol)
            if "startTime" in params:
                lo, hi = int(params["startTime"]), int(params.get("endTime", NOW))
                rows = [r for r in rows if lo <= r["time"] <= hi]
            if "fromId" in params:
                rows = [r for r in rows if int(r["id"]) >= int(params["fromId"])]
            if self.dialect == "openv1":
                return self._send(self._wrap({"list": rows}))
            return self._send(rows)

        if path in ("/open/v1/deposits", "/sapi/v1/capital/deposit/hisrec",
                    "/api/v1/capital/deposit/history"):
            rows = [{"id": i, "asset": a, "coin": a, "amount": str(amt),
                     "insertTime": NOW - d * DAY, "status": 1, "network": "BSC",
                     "txId": f"0x{i}"} for i, a, amt, d, _ in DEPOSITS]
            rows = self._window(rows, params, "insertTime")
            if self.dialect == "openv1":
                return self._send(self._wrap({"list": rows}))
            return self._send(rows)

        if path in ("/open/v1/withdraws", "/sapi/v1/capital/withdraw/history",
                    "/api/v1/capital/withdraw/history"):
            rows = [{"id": i, "asset": a, "coin": a, "amount": str(amt),
                     "applyTime": NOW - d * DAY, "status": 6,
                     "transactionFee": str(fee), "network": "BSC",
                     "txId": f"0x{i}"} for i, a, amt, d, fee in WITHDRAWALS]
            rows = self._window(rows, params, "applyTime")
            if self.dialect == "openv1":
                return self._send(self._wrap({"list": rows}))
            return self._send(rows)

        return self._send({"code": -1121, "msg": f"Unknown endpoint {path}"}, 404)

    @staticmethod
    def _window(rows, params, field):
        lo = int(params.get("startTime") or params.get("beginTime") or 0)
        hi = int(params.get("endTime") or NOW)
        return [r for r in rows if lo <= r[field] <= hi]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9998)
    parser.add_argument("--dialect", default="openv1", choices=["openv1", "apiv3", "thv1"])
    args = parser.parse_args()
    Handler.dialect = args.dialect
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock Binance TH ({args.dialect}) on http://127.0.0.1:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
