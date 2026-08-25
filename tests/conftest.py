import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import D, Money, SymbolInfo  # noqa: E402


class FakeOracle:
    """Deterministic price source: fixed spot marks and a fixed FX curve."""

    def __init__(self, spot=None, fx=None):
        self.prices = {k: D(v) for k, v in (spot or {}).items()}
        # fx: mapping of timestamp -> USDTTHB. Missing keys use the last one set.
        self.fx_curve = {int(k): D(v) for k, v in (fx or {}).items()}
        self.default_fx = D("34")
        self.symbols = {}
        for sym in self.prices:
            for quote in ("THB", "USDT", "BTC"):
                if sym.endswith(quote) and len(sym) > len(quote):
                    self.symbols[sym] = SymbolInfo(sym, sym[: -len(quote)], quote)
                    break

    # -- interface used by the engine ---------------------------------------
    def pair(self, base, quote):
        sym = f"{base}{quote}"
        return sym if sym in self.prices else None

    def usdt_thb(self):
        return self.prices.get("USDTTHB", self.default_fx)

    async def historical_usdt_thb(self, ts):
        best = self.default_fx
        for k in sorted(self.fx_curve):
            if k <= ts:
                best = self.fx_curve[k]
        return best

    async def historical_price_pair(self, asset, ts):
        fx = await self.historical_usdt_thb(ts)
        if asset == "THB":
            return Money(D(1), D(1) / fx)
        if asset == "USDT":
            return Money(fx, D(1))
        thb = self.prices.get(f"{asset}THB")
        usdt = self.prices.get(f"{asset}USDT")
        if thb is None and usdt is not None:
            thb = usdt * fx
        if usdt is None and thb is not None:
            usdt = thb / fx
        return Money(thb or D(0), usdt or D(0))

    async def historical_value(self, asset, qty, ts):
        p = await self.historical_price_pair(asset, ts)
        return Money(p.thb * qty, p.usdt * qty)

    def price_pair(self, asset):
        fx = self.usdt_thb()
        if asset == "THB":
            return Money(D(1), D(1) / fx), "cash"
        if asset == "USDT":
            return Money(fx, D(1)), "USDTTHB"
        thb = self.prices.get(f"{asset}THB")
        usdt = self.prices.get(f"{asset}USDT")
        if thb is None and usdt is not None:
            thb = usdt * fx
        if usdt is None and thb is not None:
            usdt = thb / fx
        return Money(thb or D(0), usdt or D(0)), f"{asset}THB"

    def value(self, asset, qty):
        p, src = self.price_pair(asset)
        return Money(p.thb * qty, p.usdt * qty), src


def approx(value, expected, tol="0.01"):
    return abs(Decimal(str(value)) - Decimal(str(expected))) <= Decimal(tol)
