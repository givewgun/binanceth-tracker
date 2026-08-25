"""Price discovery and dual-currency valuation.

Binance TH quotes some pairs in baht and some in tether.  To show one coherent
portfolio we need every asset priced in *both* currencies at once, which means
routing through whatever pairs actually exist:

    BTC → BTCTHB                          (direct)
    SOL → SOLUSDT → USDTTHB               (one hop)
    XYZ → XYZBTC  → BTCTHB                (one hop via a crypto quote)

The same routing runs against historical candles so a fill from eight months
ago can be valued in the currency it *wasn't* priced in.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

from .client import BinanceTHClient, BinanceTHError
from .config import settings
from .models import D, Money, SymbolInfo
from .store import Store

log = logging.getLogger("binanceth.pricing")

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
STABLES = {"USDT", "USDC", "BUSD", "FDUSD", "DAI", "TUSD"}


class PriceRoute:
    """How a price was arrived at — surfaced in the UI so nothing is magic."""

    def __init__(self, value: Optional[Decimal], path: str = "", stale: bool = False):
        self.value = value
        self.path = path
        self.stale = stale

    def __bool__(self) -> bool:
        return self.value is not None and self.value > 0


class PriceOracle:
    def __init__(self, client: BinanceTHClient, store: Store):
        self.client = client
        self.store = store
        self.prices: dict[str, Decimal] = {}
        self.symbols: dict[str, SymbolInfo] = {}
        self._by_pair: dict[tuple[str, str], str] = {}
        self.last_refresh: int = 0
        self._missing_history: set[tuple[str, int]] = set()

    # -- symbol graph -----------------------------------------------------

    def load_symbols(self) -> None:
        self.symbols = self.store.symbols()
        self._by_pair = {
            (s.base_asset, s.quote_asset): s.symbol
            for s in self.symbols.values()
            if s.base_asset and s.quote_asset
        }

    async def refresh_symbols(self) -> int:
        try:
            symbols = await self.client.exchange_symbols()
        except BinanceTHError as exc:
            log.warning("could not refresh symbol list: %s", exc)
            self.load_symbols()
            return 0
        count = self.store.upsert_symbols(symbols)
        self.load_symbols()
        return count

    @property
    def quote_assets(self) -> set[str]:
        quotes = {s.quote_asset for s in self.symbols.values() if s.quote_asset}
        return quotes or set(settings.quote_preference)

    def pair(self, base: str, quote: str) -> Optional[str]:
        """Symbol name for base/quote, if the exchange lists it."""
        direct = self._by_pair.get((base, quote))
        if direct:
            return direct
        # Fall back to the conventional concatenation for pairs we saw in the
        # trade history but never in exchangeInfo.
        guess = f"{base}{quote}"
        return guess if guess in self.symbols else None

    # -- live prices ------------------------------------------------------

    async def refresh_prices(self, assets: Optional[set[str]] = None) -> int:
        """Pull the latest marks. Bulk endpoint first, per-symbol klines after."""
        import time as _time

        fetched = await self.client.ticker_prices()
        if fetched:
            self.prices.update({k: v for k, v in fetched.items() if v and v > 0})

        # Any asset we still cannot route gets a targeted 1m-kline lookup.
        if assets:
            for asset in sorted(assets):
                if asset == "THB" or self.route(asset, "THB") or self.route(asset, "USDT"):
                    continue
                for quote in settings.quote_preference:
                    symbol = self.pair(asset, quote)
                    if not symbol or symbol in self.prices:
                        continue
                    try:
                        candles = await self.client.klines(symbol, "1m", limit=1)
                    except BinanceTHError:
                        continue
                    if candles:
                        self.prices[symbol] = candles[-1][4]
                        break

        self.last_refresh = int(_time.time() * 1000)
        return len(self.prices)

    def spot(self, symbol: str) -> Optional[Decimal]:
        px = self.prices.get(symbol)
        return px if px and px > 0 else None

    # -- routing ----------------------------------------------------------

    def route(self, asset: str, target: str, _depth: int = 0) -> PriceRoute:
        """Price of one unit of ``asset`` expressed in ``target``."""
        asset, target = asset.upper(), target.upper()
        if asset == target:
            return PriceRoute(Decimal(1), asset)

        direct = self.pair(asset, target)
        if direct and (px := self.spot(direct)):
            return PriceRoute(px, direct)

        inverse = self.pair(target, asset)
        if inverse and (px := self.spot(inverse)):
            return PriceRoute(Decimal(1) / px, f"1/{inverse}")

        if _depth >= 1:
            return PriceRoute(None)

        for mid in settings.quote_preference:
            if mid in (asset, target):
                continue
            leg1 = self.route(asset, mid, _depth + 1)
            if not leg1:
                continue
            leg2 = self.route(mid, target, _depth + 1)
            if not leg2:
                continue
            return PriceRoute(leg1.value * leg2.value, f"{leg1.path} × {leg2.path}")

        return PriceRoute(None)

    def price_pair(self, asset: str) -> tuple[Money, str]:
        """Unit price of ``asset`` in both THB and USDT, plus the route taken."""
        thb = self.route(asset, "THB")
        usdt = self.route(asset, "USDT")
        # If only one leg resolved, derive the other through USDTTHB.
        fx = self.usdt_thb()
        if thb and not usdt and fx:
            usdt = PriceRoute(thb.value / fx, f"{thb.path} ÷ USDTTHB")
        if usdt and not thb and fx:
            thb = PriceRoute(usdt.value * fx, f"{usdt.path} × USDTTHB")
        source = thb.path or usdt.path or "unpriced"
        return Money(thb.value or D(0), usdt.value or D(0)), source

    def value(self, asset: str, qty: Decimal) -> tuple[Money, str]:
        price, source = self.price_pair(asset)
        return Money(price.thb * qty, price.usdt * qty), source

    def usdt_thb(self) -> Optional[Decimal]:
        """The rate that stitches the two halves of the portfolio together."""
        for symbol in ("USDTTHB", "USDCTHB"):
            if px := self.spot(symbol):
                return px
        btc_thb, btc_usdt = self.spot("BTCTHB"), self.spot("BTCUSDT")
        if btc_thb and btc_usdt:
            return btc_thb / btc_usdt
        return None

    # -- history ----------------------------------------------------------

    async def ensure_candles(self, symbol: str, interval: str,
                             start: int, end: int) -> int:
        """Backfill and cache closes for ``symbol`` over a time range.

        Deliberately sends ``startTime`` without ``endTime``.  Binance TH caps
        a *bounded* kline query at a 7-day span and answers -4088 above it,
        which for daily candles would mean one request per week of history; an
        open-ended query is not capped and returns a full page from the cursor
        forward.  Anything past ``end`` is simply dropped on our side.
        """
        step = HOUR_MS if interval == "1h" else DAY_MS if interval == "1d" else HOUR_MS
        cursor, stored, guard = start, 0, 0
        while cursor < end and guard < 200:
            guard += 1
            try:
                candles = await self.client.klines(
                    symbol, interval, start=cursor, end=None, limit=500
                )
            except BinanceTHError as exc:
                # Loud, not debug: an empty candle cache silently flatlines the
                # whole equity chart, which is how this went unnoticed.
                log.warning("kline backfill failed for %s (%s): %s",
                            symbol, interval, exc)
                break
            if not candles:
                break
            wanted = [(c[0], c[4]) for c in candles if c[0] <= end]
            if wanted:
                self.store.upsert_candles(symbol, interval, wanted)
                stored += len(wanted)
            newest = candles[-1][0]
            if newest >= end or newest <= cursor:
                break
            cursor = newest + step
        return stored

    async def historical_rate(self, symbol: str, ts: int,
                              interval: str = "1h") -> Optional[Decimal]:
        """Close of ``symbol`` at or nearest to ``ts``, fetching if we must."""
        hit = self.store.candle_at_or_before(symbol, interval, ts)
        if hit and ts - hit[0] <= 3 * DAY_MS:
            return hit[1]

        key = (symbol, ts // DAY_MS)
        if key not in self._missing_history:
            self._missing_history.add(key)
            try:
                candles = await self.client.klines(
                    symbol, interval, start=ts - 2 * DAY_MS, end=ts + DAY_MS, limit=100
                )
                if candles:
                    self.store.upsert_candles(
                        symbol, interval, [(c[0], c[4]) for c in candles]
                    )
            except BinanceTHError as exc:
                log.debug("no historical candles for %s at %s: %s", symbol, ts, exc)

        hit = self.store.candle_at_or_before(symbol, interval, ts)
        if hit:
            return hit[1]
        after = self.store.candle_at_or_after(symbol, interval, ts)
        return after[1] if after else None

    async def historical_usdt_thb(self, ts: int) -> Optional[Decimal]:
        rate = await self.historical_rate("USDTTHB", ts)
        if rate:
            return rate
        btc_thb = await self.historical_rate("BTCTHB", ts)
        btc_usdt = await self.historical_rate("BTCUSDT", ts)
        if btc_thb and btc_usdt and btc_usdt > 0:
            return btc_thb / btc_usdt
        return self.usdt_thb()

    async def historical_price_pair(self, asset: str, ts: int) -> Money:
        """Both-currency unit price of ``asset`` as of ``ts``."""
        asset = asset.upper()
        if asset == "THB":
            fx = await self.historical_usdt_thb(ts)
            return Money(D(1), D(1) / fx if fx else D(0))

        thb = usdt = None
        if sym := self.pair(asset, "THB"):
            thb = await self.historical_rate(sym, ts)
        if sym := self.pair(asset, "USDT"):
            usdt = await self.historical_rate(sym, ts)
        if thb is None and usdt is None and asset in STABLES:
            usdt = D(1)

        fx = await self.historical_usdt_thb(ts)
        if thb is None and usdt is not None and fx:
            thb = usdt * fx
        if usdt is None and thb is not None and fx and fx > 0:
            usdt = thb / fx
        return Money(thb or D(0), usdt or D(0))

    async def historical_value(self, asset: str, qty: Decimal, ts: int) -> Money:
        price = await self.historical_price_pair(asset, ts)
        try:
            return Money(price.thb * qty, price.usdt * qty)
        except InvalidOperation:
            return Money()
