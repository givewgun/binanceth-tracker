"""SQLite persistence.

The exchange only keeps trade history reachable through paginated, per-symbol
queries, so the tracker keeps its own copy.  That also means the dashboard
stays useful (and fast) when the API is slow or unreachable.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional

from .models import Balance, D, SymbolInfo, Trade, Transfer

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS trades (
    trade_id     TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    base_asset   TEXT NOT NULL,
    quote_asset  TEXT NOT NULL,
    side         TEXT NOT NULL,
    price        TEXT NOT NULL,
    qty          TEXT NOT NULL,
    quote_qty    TEXT NOT NULL,
    fee          TEXT NOT NULL,
    fee_asset    TEXT NOT NULL,
    time         INTEGER NOT NULL,
    order_id     TEXT DEFAULT '',
    is_maker     INTEGER DEFAULT 0,
    PRIMARY KEY (symbol, trade_id)
);
CREATE INDEX IF NOT EXISTS idx_trades_time  ON trades(time);
CREATE INDEX IF NOT EXISTS idx_trades_base  ON trades(base_asset);

CREATE TABLE IF NOT EXISTS transfers (
    transfer_id  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    asset        TEXT NOT NULL,
    amount       TEXT NOT NULL,
    fee          TEXT NOT NULL,
    time         INTEGER NOT NULL,
    status       TEXT NOT NULL,
    tx_id        TEXT DEFAULT '',
    network      TEXT DEFAULT '',
    address      TEXT DEFAULT '',
    is_fiat      INTEGER DEFAULT 0,
    PRIMARY KEY (kind, transfer_id)
);
CREATE INDEX IF NOT EXISTS idx_transfers_time ON transfers(time);

CREATE TABLE IF NOT EXISTS symbols (
    symbol          TEXT PRIMARY KEY,
    base_asset      TEXT NOT NULL,
    quote_asset     TEXT NOT NULL,
    status          TEXT NOT NULL,
    base_precision  INTEGER DEFAULT 8,
    quote_precision INTEGER DEFAULT 8
);

CREATE TABLE IF NOT EXISTS balances (
    asset   TEXT PRIMARY KEY,
    free    TEXT NOT NULL,
    locked  TEXT NOT NULL,
    updated INTEGER NOT NULL
);

-- Historical closes, used to value trades and deposits at the time they happened.
CREATE TABLE IF NOT EXISTS candles (
    symbol   TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    close    TEXT NOT NULL,
    PRIMARY KEY (symbol, interval, open_time)
);

-- One row per UTC day: the equity curve behind the historical PnL chart.
CREATE TABLE IF NOT EXISTS equity_history (
    day             TEXT PRIMARY KEY,
    ts              INTEGER NOT NULL,
    equity_thb      TEXT NOT NULL,
    equity_usdt     TEXT NOT NULL,
    cost_thb        TEXT NOT NULL,
    cost_usdt       TEXT NOT NULL,
    realised_thb    TEXT NOT NULL,
    realised_usdt   TEXT NOT NULL,
    net_deposit_thb TEXT NOT NULL DEFAULT '0',
    detail          TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    """Thread-safe-enough SQLite wrapper (one connection, guarded by a lock)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- meta -------------------------------------------------------------

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            self._db.commit()

    def get_meta_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get_meta(key) or default)
        except ValueError:
            return default

    # -- symbols ----------------------------------------------------------

    def upsert_symbols(self, symbols: Iterable[SymbolInfo]) -> int:
        payload = [
            (s.symbol, s.base_asset, s.quote_asset, s.status,
             s.base_precision, s.quote_precision)
            for s in symbols
        ]
        if not payload:
            return 0
        with self._lock:
            self._db.executemany(
                "INSERT INTO symbols(symbol,base_asset,quote_asset,status,"
                "base_precision,quote_precision) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET base_asset=excluded.base_asset,"
                "quote_asset=excluded.quote_asset,status=excluded.status",
                payload,
            )
            self._db.commit()
        return len(payload)

    def symbols(self) -> dict[str, SymbolInfo]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM symbols").fetchall()
        return {
            r["symbol"]: SymbolInfo(
                symbol=r["symbol"], base_asset=r["base_asset"],
                quote_asset=r["quote_asset"], status=r["status"],
                base_precision=r["base_precision"], quote_precision=r["quote_precision"],
            )
            for r in rows
        }

    # -- trades -----------------------------------------------------------

    def upsert_trades(self, trades: Iterable[Trade]) -> int:
        payload = [
            (t.trade_id, t.symbol, t.base_asset, t.quote_asset, t.side,
             str(t.price), str(t.qty), str(t.quote_qty), str(t.fee), t.fee_asset,
             t.time, t.order_id, int(t.is_maker))
            for t in trades
        ]
        if not payload:
            return 0
        with self._lock:
            before = self._db.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]
            self._db.executemany(
                "INSERT INTO trades(trade_id,symbol,base_asset,quote_asset,side,price,"
                "qty,quote_qty,fee,fee_asset,time,order_id,is_maker) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol,trade_id) DO UPDATE SET "
                "price=excluded.price, qty=excluded.qty, quote_qty=excluded.quote_qty,"
                "fee=excluded.fee, fee_asset=excluded.fee_asset, time=excluded.time",
                payload,
            )
            after = self._db.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]
            self._db.commit()
        return after - before

    def trades(self, since: Optional[int] = None, until: Optional[int] = None,
               asset: Optional[str] = None, symbol: Optional[str] = None,
               limit: Optional[int] = None) -> list[Trade]:
        sql = "SELECT * FROM trades WHERE 1=1"
        args: list = []
        if since is not None:
            sql += " AND time >= ?"; args.append(since)
        if until is not None:
            sql += " AND time <= ?"; args.append(until)
        if asset:
            sql += " AND (base_asset = ? OR quote_asset = ?)"; args += [asset, asset]
        if symbol:
            sql += " AND symbol = ?"; args.append(symbol)
        sql += " ORDER BY time ASC, rowid ASC"
        if limit:
            sql += " LIMIT ?"; args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [self._row_to_trade(r) for r in rows]

    @staticmethod
    def _row_to_trade(r: sqlite3.Row) -> Trade:
        return Trade(
            trade_id=r["trade_id"], symbol=r["symbol"], base_asset=r["base_asset"],
            quote_asset=r["quote_asset"], side=r["side"], price=D(r["price"]),
            qty=D(r["qty"]), quote_qty=D(r["quote_qty"]), fee=D(r["fee"]),
            fee_asset=r["fee_asset"], time=r["time"], order_id=r["order_id"],
            is_maker=bool(r["is_maker"]),
        )

    def traded_symbols(self) -> list[str]:
        with self._lock:
            rows = self._db.execute("SELECT DISTINCT symbol FROM trades").fetchall()
        return [r["symbol"] for r in rows]

    def last_trade_time(self, symbol: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(time) t FROM trades WHERE symbol=?", (symbol,)
            ).fetchone()
        return int(row["t"] or 0)

    def last_trade_id(self, symbol: str) -> Optional[str]:
        """Highest numeric trade id we hold for a symbol, for fromId paging."""
        with self._lock:
            rows = self._db.execute(
                "SELECT trade_id FROM trades WHERE symbol=?", (symbol,)
            ).fetchall()
        numeric = [int(r["trade_id"]) for r in rows if str(r["trade_id"]).isdigit()]
        return str(max(numeric)) if numeric else None

    # -- transfers --------------------------------------------------------

    def upsert_transfers(self, transfers: Iterable[Transfer]) -> int:
        payload = [
            (t.transfer_id, t.kind, t.asset, str(t.amount), str(t.fee), t.time,
             t.status, t.tx_id, t.network, t.address, int(t.is_fiat))
            for t in transfers
        ]
        if not payload:
            return 0
        with self._lock:
            before = self._db.execute("SELECT COUNT(*) c FROM transfers").fetchone()["c"]
            self._db.executemany(
                "INSERT INTO transfers(transfer_id,kind,asset,amount,fee,time,status,"
                "tx_id,network,address,is_fiat) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(kind,transfer_id) DO UPDATE SET status=excluded.status,"
                "amount=excluded.amount, fee=excluded.fee, time=excluded.time",
                payload,
            )
            after = self._db.execute("SELECT COUNT(*) c FROM transfers").fetchone()["c"]
            self._db.commit()
        return after - before

    def transfers(self, kind: Optional[str] = None, since: Optional[int] = None,
                  until: Optional[int] = None, asset: Optional[str] = None,
                  completed_only: bool = True) -> list[Transfer]:
        sql = "SELECT * FROM transfers WHERE 1=1"
        args: list = []
        if kind:
            sql += " AND kind = ?"; args.append(kind)
        if since is not None:
            sql += " AND time >= ?"; args.append(since)
        if until is not None:
            sql += " AND time <= ?"; args.append(until)
        if asset:
            sql += " AND asset = ?"; args.append(asset)
        if completed_only:
            sql += " AND status = 'COMPLETED'"
        sql += " ORDER BY time ASC, rowid ASC"
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [
            Transfer(
                transfer_id=r["transfer_id"], kind=r["kind"], asset=r["asset"],
                amount=D(r["amount"]), fee=D(r["fee"]), time=r["time"],
                status=r["status"], tx_id=r["tx_id"], network=r["network"],
                address=r["address"], is_fiat=bool(r["is_fiat"]),
            )
            for r in rows
        ]

    # -- balances ---------------------------------------------------------

    def upsert_balances(self, balances: Iterable[Balance]) -> None:
        now = int(time.time() * 1000)
        payload = [(b.asset, str(b.free), str(b.locked), now) for b in balances]
        with self._lock:
            self._db.execute("DELETE FROM balances")
            if payload:
                self._db.executemany(
                    "INSERT INTO balances(asset,free,locked,updated) VALUES(?,?,?,?)",
                    payload,
                )
            self._db.commit()

    def balances(self) -> list[Balance]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM balances ORDER BY asset").fetchall()
        return [Balance(r["asset"], D(r["free"]), D(r["locked"])) for r in rows]

    # -- candles ----------------------------------------------------------

    def upsert_candles(self, symbol: str, interval: str,
                       candles: Iterable[tuple[int, Decimal]]) -> None:
        payload = [(symbol, interval, int(t), str(c)) for t, c in candles]
        if not payload:
            return
        with self._lock:
            self._db.executemany(
                "INSERT INTO candles(symbol,interval,open_time,close) VALUES(?,?,?,?) "
                "ON CONFLICT(symbol,interval,open_time) DO UPDATE SET close=excluded.close",
                payload,
            )
            self._db.commit()

    def candle_at_or_before(self, symbol: str, interval: str,
                            ts: int) -> Optional[tuple[int, Decimal]]:
        with self._lock:
            row = self._db.execute(
                "SELECT open_time, close FROM candles WHERE symbol=? AND interval=? "
                "AND open_time <= ? ORDER BY open_time DESC LIMIT 1",
                (symbol, interval, ts),
            ).fetchone()
        return (row["open_time"], D(row["close"])) if row else None

    def candle_at_or_after(self, symbol: str, interval: str,
                           ts: int) -> Optional[tuple[int, Decimal]]:
        with self._lock:
            row = self._db.execute(
                "SELECT open_time, close FROM candles WHERE symbol=? AND interval=? "
                "AND open_time >= ? ORDER BY open_time ASC LIMIT 1",
                (symbol, interval, ts),
            ).fetchone()
        return (row["open_time"], D(row["close"])) if row else None

    def candle_series(self, symbol: str, interval: str, since: int = 0
                      ) -> list[tuple[int, Decimal]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT open_time, close FROM candles WHERE symbol=? AND interval=? "
                "AND open_time >= ? ORDER BY open_time ASC",
                (symbol, interval, since),
            ).fetchall()
        return [(r["open_time"], D(r["close"])) for r in rows]

    def candle_count(self, symbol: str, interval: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) c FROM candles WHERE symbol=? AND interval=?",
                (symbol, interval),
            ).fetchone()
        return int(row["c"])

    # -- equity history ---------------------------------------------------

    def upsert_equity(self, day: str, ts: int, equity, cost, realised,
                      net_deposit_thb: Decimal, detail: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO equity_history(day,ts,equity_thb,equity_usdt,cost_thb,"
                "cost_usdt,realised_thb,realised_usdt,net_deposit_thb,detail) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(day) DO UPDATE SET ts=excluded.ts,"
                "equity_thb=excluded.equity_thb, equity_usdt=excluded.equity_usdt,"
                "cost_thb=excluded.cost_thb, cost_usdt=excluded.cost_usdt,"
                "realised_thb=excluded.realised_thb, realised_usdt=excluded.realised_usdt,"
                "net_deposit_thb=excluded.net_deposit_thb, detail=excluded.detail",
                (day, ts, str(equity.thb), str(equity.usdt), str(cost.thb),
                 str(cost.usdt), str(realised.thb), str(realised.usdt),
                 str(net_deposit_thb), json.dumps(detail, default=str)),
            )
            self._db.commit()

    def equity_history(self, since_day: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM equity_history"
        args: list = []
        if since_day:
            sql += " WHERE day >= ?"; args.append(since_day)
        sql += " ORDER BY day ASC"
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # -- stats ------------------------------------------------------------

    def clear_equity_history(self) -> None:
        """Drop the cached daily curve so it is rebuilt from current data."""
        with self._lock:
            self._db.execute("DELETE FROM equity_history")
            self._db.commit()

    def counts(self) -> dict:
        with self._lock:
            def one(sql: str) -> int:
                return int(self._db.execute(sql).fetchone()[0] or 0)
            return {
                "trades": one("SELECT COUNT(*) FROM trades"),
                "deposits": one("SELECT COUNT(*) FROM transfers WHERE kind='DEPOSIT'"),
                "withdrawals": one("SELECT COUNT(*) FROM transfers WHERE kind='WITHDRAWAL'"),
                "symbols": one("SELECT COUNT(*) FROM symbols"),
                "candles": one("SELECT COUNT(*) FROM candles"),
                "first_trade": one("SELECT COALESCE(MIN(time),0) FROM trades"),
                "last_trade": one("SELECT COALESCE(MAX(time),0) FROM trades"),
            }
