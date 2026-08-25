"""The equity curve: it must be priced, and it must match the live snapshot."""
import asyncio

import pytest

from app.models import D
from app.pricing import PriceOracle


class RecordingClient:
    """A kline source that refuses bounded queries, as Binance TH does."""

    def __init__(self):
        self.calls = []

    async def klines(self, symbol, interval="1h", start=None, end=None, limit=500):
        self.calls.append({"symbol": symbol, "interval": interval,
                           "start": start, "end": end, "limit": limit})
        if start is not None and end is not None:
            from app.client import BinanceTHError
            raise BinanceTHError("Binance TH error -4088: Maximum time interval "
                                 "is 7 days.", status=400, code=-4088)
        day = 86_400_000
        base = start or 0
        return [[base + i * day, D(1), D(1), D(1), D("100")] for i in range(limit)]


@pytest.fixture
def oracle(tmp_path):
    from app.store import Store
    return PriceOracle(RecordingClient(), Store(tmp_path / "c.db"))


def test_backfill_never_sends_a_bounded_kline_query(oracle):
    """Sending both bounds is what emptied the candle cache and flatlined the chart."""
    day = 86_400_000
    stored = asyncio.run(oracle.ensure_candles("BTCTHB", "1d", 0, 400 * day))

    assert stored > 0, "a bounded query would have raised -4088 and stored nothing"
    assert oracle.client.calls, "no request was made"
    assert all(c["end"] is None for c in oracle.client.calls), \
        "endTime must be omitted: with both bounds the venue caps the span at 7 days"


def test_backfill_drops_candles_past_the_requested_end(oracle):
    day = 86_400_000
    asyncio.run(oracle.ensure_candles("BTCTHB", "1d", 0, 10 * day))

    series = oracle.store.candle_series("BTCTHB", "1d", 0)
    assert series, "expected cached candles"
    assert max(t for t, _ in series) <= 10 * day


# ---------------------------------------------------------------------------


def _seed(tmp_path, monkeypatch):
    """A book that buys, sells, and holds less than the fills imply."""
    from app.config import settings
    from app.models import Balance, Trade
    from app.store import Store
    from tests.conftest import FakeOracle

    day = 86_400_000
    t0 = 1_700_000_000_000

    def trade(tid, side, price, qty, ts):
        price, qty = D(price), D(qty)
        return Trade(trade_id=tid, symbol="USDTTHB", base_asset="USDT",
                     quote_asset="THB", side=side, price=price, qty=qty,
                     quote_qty=price * qty, fee=D(0), fee_asset="THB", time=ts)

    store = Store(tmp_path / "h.db")
    store.upsert_trades([
        trade("1", "BUY", "34", "1000", t0),
        trade("2", "BUY", "34", "1000", t0 + day),
        trade("3", "SELL", "31", "1500", t0 + 2 * day),
    ])
    # 500 USDT should remain; only 100 is there. 400 left unreported.
    store.upsert_balances([Balance(asset="USDT", free=D("100"), locked=D(0))])
    # The curve prices holdings from cached daily closes; without them every
    # point reads zero, which is the bug that started this.
    store.upsert_candles("USDTTHB", "1d",
                         [(t0 + n * day, D("31")) for n in range(-1, 5)])
    monkeypatch.setattr(settings, "cost_basis_method", "simple")
    monkeypatch.setattr(settings, "holdings_file", str(tmp_path / "absent.toml"))
    return store, FakeOracle(spot={"USDTTHB": "31"})


def test_curve_realised_matches_the_snapshot(tmp_path, monkeypatch):
    """An untracked outflow drained mid-replay leaves a later sale short of
    basis, and books most of its proceeds as profit. The chart and the
    headline must not disagree about that."""
    from app.portfolio import build_history, build_portfolio

    store, oracle = _seed(tmp_path, monkeypatch)
    rows = asyncio.run(build_history(store, oracle, persist=False))
    state = asyncio.run(build_portfolio(store, oracle))

    assert rows, "expected a curve"
    curve = D(rows[-1]["realised_thb"])
    assert curve == state.realised.thb, (
        f"curve says {curve}, snapshot says {state.realised.thb}")
    # 1500 sold at 31 against an average of 34
    assert curve == D("-4500")


def test_curve_ends_on_the_real_balance(tmp_path, monkeypatch):
    from app.portfolio import build_history

    store, oracle = _seed(tmp_path, monkeypatch)
    rows = asyncio.run(build_history(store, oracle, persist=False))
    # 100 USDT at 31 baht (not the 500 the fills imply), plus 46,500 baht of
    # sale proceeds sitting in the account
    assert D(rows[-1]["equity_thb"]) == D("49600")
