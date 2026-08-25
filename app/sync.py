"""Pulling history off the exchange and into the local database.

Two things make this less trivial than it sounds:

* ``myTrades`` is per-symbol, and the API will not tell you which symbols you
  have ever traded.  We infer a candidate list from balances, transfers and
  previously-seen trades, then remember which symbols came back empty so the
  next sync does not re-scan the whole board.
* Deposit and withdrawal history is capped to roughly 90 days per request, so
  the full record has to be walked in windows.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .client import BinanceTHClient, BinanceTHError
from .config import settings
from .pricing import PriceOracle
from .store import Store

log = logging.getLogger("binanceth.sync")

DAY_MS = 86_400_000
WINDOW_MS = 89 * DAY_MS          # stay inside the venue's 90-day cap
TRADE_PAGE = 500                 # rows per myTrades/userTrades request
DEFAULT_LOOKBACK_DAYS = 1460     # four years is past any retail account's start


@dataclass
class SyncProgress:
    running: bool = False
    stage: str = "idle"
    detail: str = ""
    done: int = 0
    total: int = 0
    started: int = 0
    finished: int = 0
    error: str = ""
    added: dict = field(default_factory=lambda: {"trades": 0, "deposits": 0,
                                                 "withdrawals": 0})

    def as_dict(self) -> dict:
        pct = round(self.done / self.total * 100) if self.total else 0
        return {**self.__dict__, "percent": pct}


class Synchroniser:
    def __init__(self, client: BinanceTHClient, store: Store, oracle: PriceOracle):
        self.client = client
        self.store = store
        self.oracle = oracle
        self.progress = SyncProgress()
        self._lock = asyncio.Lock()

    # -- helpers ----------------------------------------------------------

    def _step(self, stage: str, detail: str = "", done: Optional[int] = None,
              total: Optional[int] = None) -> None:
        self.progress.stage = stage
        self.progress.detail = detail
        if done is not None:
            self.progress.done = done
        if total is not None:
            self.progress.total = total

    @staticmethod
    def _now() -> int:
        return int(time.time() * 1000)

    def _lookback_start(self) -> int:
        configured = self.store.get_meta_int("history_start", 0)
        if configured:
            return configured
        return self._now() - DEFAULT_LOOKBACK_DAYS * DAY_MS

    # -- discovery --------------------------------------------------------

    def candidate_symbols(self, deep: bool = False) -> list[str]:
        """Symbols worth asking about, best guesses first."""
        symbols = self.oracle.symbols
        if deep:
            return sorted(symbols)

        interesting: set[str] = set(self.store.traded_symbols())
        assets = {b.asset for b in self.store.balances()}
        assets |= {t.asset for t in self.store.transfers(completed_only=False)}
        assets |= {s.base_asset for s in symbols.values()
                   if s.base_asset in {"BTC", "ETH", "USDT", "BNB"}}

        for asset in assets:
            for quote in settings.quote_preference:
                if asset == quote:
                    continue
                if sym := self.oracle.pair(asset, quote):
                    interesting.add(sym)
        # Always keep the pair that ties the two currencies together.
        for essential in ("USDTTHB", "BTCTHB", "BTCUSDT"):
            if essential in symbols:
                interesting.add(essential)

        empty = set((self.store.get_meta("empty_symbols", "") or "").split(","))
        forced = set(self.store.traded_symbols()) | {
            s for s in interesting
            if any(s.startswith(a) for a in {b.asset for b in self.store.balances()})
        }
        return sorted(interesting - (empty - forced))

    # -- individual syncs -------------------------------------------------

    async def sync_symbols(self) -> int:
        self._step("symbols", "loading instrument list")
        return await self.oracle.refresh_symbols()

    async def sync_balances(self) -> int:
        self._step("balances", "reading account balances")
        balances = await self.client.balances()
        self.store.upsert_balances(balances)
        return len(balances)

    async def sync_trades_for(self, symbol: str, full: bool = False) -> int:
        """Page one symbol's fills into the database.

        Always by trade id, never by time window.  Binance TH caps a trade
        query at a 7-day span, so covering years of history in windows would
        mean hundreds of mostly-empty requests per symbol — and the venue
        returns its whole record from ``fromId=1`` anyway.  (Zero is rejected;
        one is the first valid id.)
        """
        known = self.oracle.symbols
        quotes = self.oracle.quote_assets
        added = 0

        last_id = None if full else self.store.last_trade_id(symbol)
        cursor = int(last_id) + 1 if last_id and last_id.isdigit() else 1

        for _ in range(200):
            try:
                trades = await self.client.my_trades(
                    symbol, from_id=str(cursor), limit=TRADE_PAGE,
                    known=known, quotes=quotes)
            except BinanceTHError as exc:
                if exc.is_unsupported:
                    break
                raise
            if not trades:
                break
            added += self.store.upsert_trades(trades)
            numeric = [int(t.trade_id) for t in trades if t.trade_id.isdigit()]
            if not numeric or len(trades) < TRADE_PAGE:
                break
            cursor = max(numeric) + 1
        else:
            log.warning("stopped paging %s after 200 pages; history may be "
                        "incomplete", symbol)
        return added

    async def sync_trades(self, deep: bool = False, full: bool = False) -> int:
        symbols = self.candidate_symbols(deep=deep)
        self._step("trades", f"scanning {len(symbols)} pairs", 0, len(symbols))
        added = 0
        empty: list[str] = []
        failed: list[str] = []
        last_error: Optional[BinanceTHError] = None
        for i, symbol in enumerate(symbols, 1):
            self._step("trades", symbol, i, len(symbols))
            try:
                found = await self.sync_trades_for(symbol, full=full)
            except BinanceTHError as exc:
                if exc.is_auth_error:
                    raise
                # Never demote this to debug: a rejected query looks exactly
                # like "you have no trades" once it is swallowed, and that is
                # how a 90-day window against a 7-day cap silently produced an
                # empty portfolio.
                log.warning("trade sync failed for %s: %s", symbol, exc)
                failed.append(symbol)
                last_error = exc
                continue
            added += found
            if found == 0 and not self.store.last_trade_time(symbol):
                empty.append(symbol)
        if failed and not added:
            raise BinanceTHError(
                f"Every trade query was rejected ({len(failed)} symbols); "
                f"last error: {last_error}"
            )
        if empty:
            previous = set((self.store.get_meta("empty_symbols", "") or "").split(","))
            previous.update(empty)
            previous.discard("")
            self.store.set_meta("empty_symbols", ",".join(sorted(previous)))
        self.progress.added["trades"] += added
        return added

    async def _sync_transfer_kind(self, kind: str, full: bool) -> int:
        meta_key = f"last_{kind.lower()}_sync"
        start = self._lookback_start() if full else max(
            self.store.get_meta_int(meta_key, 0) - DAY_MS, self._lookback_start())
        now = self._now()
        fetch = self.client.deposits if kind == "DEPOSIT" else self.client.withdrawals
        added = 0
        cursor = start
        while cursor < now:
            end = min(cursor + WINDOW_MS, now)
            self._step(kind.lower() + "s", _fmt_range(cursor, end))
            try:
                records = await fetch(start=cursor, end=end, limit=500)
            except BinanceTHError as exc:
                if exc.is_unsupported or exc.is_permission_error:
                    log.info("%s history unavailable on this key: %s", kind, exc)
                    break
                raise
            added += self.store.upsert_transfers(records)
            cursor = end + 1

        # Fiat rails are a separate endpoint on the api/v3 dialect.
        try:
            fiat = await self.client.fiat_transfers(kind, start=start, end=now)
            added += self.store.upsert_transfers(fiat)
        except BinanceTHError as exc:
            log.debug("fiat %s history unavailable: %s", kind, exc)

        self.store.set_meta(meta_key, now)
        self.progress.added[kind.lower() + "s"] += added
        return added

    async def sync_transfers(self, full: bool = False) -> tuple[int, int]:
        deposits = await self._sync_transfer_kind("DEPOSIT", full)
        withdrawals = await self._sync_transfer_kind("WITHDRAWAL", full)
        return deposits, withdrawals

    async def sync_candles(self) -> int:
        """Backfill the daily closes the history chart is drawn from."""
        counts = self.store.counts()
        first = counts["first_trade"] or (self._now() - 90 * DAY_MS)
        start = first - DAY_MS
        now = self._now()

        assets = {b.asset for b in self.store.balances()}
        assets |= {t.base_asset for t in self.store.trades()}
        assets |= {t.asset for t in self.store.transfers()}
        assets.discard(settings.fiat)
        assets.discard("")

        wanted: list[str] = ["USDTTHB", "BTCTHB", "BTCUSDT"]
        for asset in sorted(assets):
            for quote in ("THB", "USDT"):
                if sym := self.oracle.pair(asset, quote):
                    wanted.append(sym)
        wanted = [s for s in dict.fromkeys(wanted) if s in self.oracle.symbols
                  or s in ("USDTTHB", "BTCTHB", "BTCUSDT")]

        self._step("candles", f"{len(wanted)} price series", 0, len(wanted))
        stored = 0
        for i, symbol in enumerate(wanted, 1):
            self._step("candles", symbol, i, len(wanted))
            have = self.store.candle_count(symbol, "1d")
            expected = max(1, (now - start) // DAY_MS)
            if have >= expected * 0.95:
                latest = self.store.candle_at_or_before(symbol, "1d", now)
                resume = latest[0] + DAY_MS if latest else start
            else:
                resume = start
            if resume >= now:
                continue
            stored += await self.oracle.ensure_candles(symbol, "1d", resume, now)
        return stored

    # -- orchestration ----------------------------------------------------

    async def run(self, *, full: bool = False, deep: bool = False,
                  with_candles: bool = True) -> dict:
        if self._lock.locked():
            return {"skipped": "a sync is already running",
                    "progress": self.progress.as_dict()}

        async with self._lock:
            self.progress = SyncProgress(running=True, started=self._now())
            try:
                await self.client.ensure_ready()
                await self.sync_symbols()
                await self.sync_balances()
                await self.sync_transfers(full=full)
                await self.sync_trades(deep=deep, full=full)
                await self.oracle.refresh_prices(
                    {b.asset for b in self.store.balances()})
                if with_candles:
                    await self.sync_candles()
                self.store.set_meta("last_sync", self._now())
                self._step("done", "up to date")
            except Exception as exc:                       # noqa: BLE001
                self.progress.error = str(exc)
                self._step("error", str(exc))
                log.exception("sync failed")
            finally:
                self.progress.running = False
                self.progress.finished = self._now()
            return {"progress": self.progress.as_dict(),
                    "counts": self.store.counts()}


def _fmt_range(start: int, end: int) -> str:
    from datetime import datetime, timezone
    fmt = "%Y-%m-%d"
    a = datetime.fromtimestamp(start / 1000, tz=timezone.utc).strftime(fmt)
    b = datetime.fromtimestamp(end / 1000, tz=timezone.utc).strftime(fmt)
    return f"{a} → {b}"
