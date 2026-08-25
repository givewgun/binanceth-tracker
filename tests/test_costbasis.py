"""The accounting rules this tracker lives or dies by."""
import asyncio
from decimal import Decimal

from app.models import D, Trade, Transfer
from app.portfolio import CostBasisEngine, LedgerEvent
from tests.conftest import FakeOracle, approx

DAY = 86_400_000
T0 = 1_700_000_000_000


def trade(tid, symbol, base, quote, side, price, qty, ts, fee="0", fee_asset=""):
    price, qty = D(price), D(qty)
    return Trade(trade_id=tid, symbol=symbol, base_asset=base, quote_asset=quote,
                 side=side, price=price, qty=qty, quote_qty=price * qty,
                 fee=D(fee), fee_asset=fee_asset or quote, time=ts)


def run(engine, events):
    asyncio.run(engine.replay(events))


def ev(t):
    return LedgerEvent(t.time, "trade", trade=t)


def transfer_ev(tr):
    return LedgerEvent(tr.time, "deposit" if tr.kind == "DEPOSIT" else "withdrawal",
                       transfer=tr)


# ---------------------------------------------------------------------------


def test_thb_buy_then_sell_realises_baht_profit():
    oracle = FakeOracle(spot={"BTCTHB": "2500000", "USDTTHB": "35"})
    eng = CostBasisEngine(oracle, fiat="THB")
    run(eng, [
        ev(trade("1", "BTCTHB", "BTC", "THB", "BUY", "2000000", "0.01", T0)),
        ev(trade("2", "BTCTHB", "BTC", "THB", "SELL", "2400000", "0.01", T0 + DAY)),
    ])
    realised = sum((d.pnl.thb for d in eng.disposals if d.counts_as_realised), Decimal(0))
    # Bought for 20,000 THB, sold for 24,000 THB.
    assert approx(realised, "4000")
    assert eng.qty("BTC") == 0
    assert approx(eng.cash, "4000")   # -20,000 then +24,000


def test_usdt_quoted_buy_traces_the_baht_actually_paid():
    """The headline feature: THB basis follows the tether you funded with."""
    oracle = FakeOracle(
        spot={"SOLUSDT": "150", "USDTTHB": "36"},
        fx={T0: "34.00", T0 + 5 * DAY: "36.00"},
    )
    eng = CostBasisEngine(oracle, fx_mode="lots", fiat="THB")
    run(eng, [
        # Buy 1,000 USDT at 34.00 THB  ->  34,000 THB out of pocket.
        ev(trade("1", "USDTTHB", "USDT", "THB", "BUY", "34", "1000", T0)),
        # Five days later, rate is 36. Spend 750 USDT on SOL.
        ev(trade("2", "SOLUSDT", "SOL", "USDT", "BUY", "150", "5", T0 + 5 * DAY)),
    ])
    sol_cost = eng.basis("SOL")
    assert approx(sol_cost.usdt, "750")
    # Traced: 750 USDT cost 750 * 34.00 = 25,500 THB, NOT 750 * 36 = 27,000.
    assert approx(sol_cost.thb, "25500")
    # The funding leg must not invent an FX profit.
    funding = [d for d in eng.disposals if d.asset == "USDT"]
    assert approx(sum(d.pnl.thb for d in funding), "0")


def test_market_fx_mode_uses_the_rate_on_the_day():
    oracle = FakeOracle(
        spot={"SOLUSDT": "150", "USDTTHB": "36"},
        fx={T0: "34.00", T0 + 5 * DAY: "36.00"},
    )
    eng = CostBasisEngine(oracle, fx_mode="market", fiat="THB")
    run(eng, [
        ev(trade("1", "USDTTHB", "USDT", "THB", "BUY", "34", "1000", T0)),
        ev(trade("2", "SOLUSDT", "SOL", "USDT", "BUY", "150", "5", T0 + 5 * DAY)),
    ])
    assert approx(eng.basis("SOL").thb, "27000")     # 750 * 36
    funding = [d for d in eng.disposals if d.asset == "USDT"]
    # The 2 THB/USDT appreciation on 750 tethers is realised here.
    assert approx(sum(d.pnl.thb for d in funding), "1500")


def test_idle_tether_still_carries_an_fx_position():
    oracle = FakeOracle(spot={"USDTTHB": "36"}, fx={T0: "34.00"})
    eng = CostBasisEngine(oracle, fiat="THB")
    run(eng, [ev(trade("1", "USDTTHB", "USDT", "THB", "BUY", "34", "1000", T0))])
    assert approx(eng.basis("USDT").thb, "34000")
    value, _ = oracle.value("USDT", eng.qty("USDT"))
    assert approx(value.thb - eng.basis("USDT").thb, "2000")   # unrealised FX gain


def test_fifo_and_average_disagree_as_expected():
    oracle = FakeOracle(spot={"ETHTHB": "100000", "USDTTHB": "34"})
    fills = [
        ev(trade("1", "ETHTHB", "ETH", "THB", "BUY", "60000", "1", T0)),
        ev(trade("2", "ETHTHB", "ETH", "THB", "BUY", "80000", "1", T0 + DAY)),
        ev(trade("3", "ETHTHB", "ETH", "THB", "SELL", "90000", "1", T0 + 2 * DAY)),
    ]
    fifo = CostBasisEngine(oracle, method="fifo", fiat="THB")
    run(fifo, fills)
    assert approx(sum(d.pnl.thb for d in fifo.disposals), "30000")   # 90k - 60k

    avg = CostBasisEngine(oracle, method="avg", fiat="THB")
    run(avg, [ev(t.trade) for t in fills])
    assert approx(sum(d.pnl.thb for d in avg.disposals), "20000")    # 90k - 70k


def test_commission_in_the_bought_coin_reduces_quantity_not_cost():
    oracle = FakeOracle(spot={"BTCTHB": "2000000", "USDTTHB": "34"})
    eng = CostBasisEngine(oracle, fiat="THB")
    run(eng, [ev(trade("1", "BTCTHB", "BTC", "THB", "BUY", "2000000", "0.01", T0,
                       fee="0.00001", fee_asset="BTC"))])
    assert eng.qty("BTC") == D("0.00999")
    assert approx(eng.basis("BTC").thb, "20000")


def test_commission_in_a_third_asset_is_added_to_cost():
    oracle = FakeOracle(spot={"BTCTHB": "2000000", "BNBTHB": "20000", "USDTTHB": "34"})
    eng = CostBasisEngine(oracle, fiat="THB")
    run(eng, [
        ev(trade("0", "BNBTHB", "BNB", "THB", "BUY", "20000", "1", T0)),
        ev(trade("1", "BTCTHB", "BTC", "THB", "BUY", "2000000", "0.01", T0 + DAY,
                 fee="0.01", fee_asset="BNB")),
    ])
    # 20,000 THB of BTC plus 0.01 BNB (200 THB) of commission.
    assert approx(eng.basis("BTC").thb, "20200")
    assert approx(eng.fees_paid.thb, "200")


def test_deposit_is_costed_at_arrival_and_flagged():
    oracle = FakeOracle(spot={"BTCTHB": "2500000", "USDTTHB": "34"})
    eng = CostBasisEngine(oracle, fiat="THB")
    run(eng, [transfer_ev(Transfer("d1", "DEPOSIT", "BTC", D("0.05"), D("0"), T0))])
    assert approx(eng.basis("BTC").thb, "125000")
    assert any(w.code == "deposit_basis" for w in eng.warnings)


def test_withdrawal_is_a_transfer_not_a_sale_by_default():
    oracle = FakeOracle(spot={"BTCTHB": "3000000", "USDTTHB": "34"})
    eng = CostBasisEngine(oracle, fiat="THB")
    run(eng, [
        ev(trade("1", "BTCTHB", "BTC", "THB", "BUY", "2000000", "0.01", T0)),
        transfer_ev(Transfer("w1", "WITHDRAWAL", "BTC", D("0.01"), D("0"), T0 + DAY)),
    ])
    realised = sum((d.pnl.thb for d in eng.disposals if d.counts_as_realised), Decimal(0))
    assert approx(realised, "0")
    assert eng.qty("BTC") == 0

    taxed = CostBasisEngine(oracle, treat_withdrawal_as_sale=True, fiat="THB")
    run(taxed, [
        ev(trade("1", "BTCTHB", "BTC", "THB", "BUY", "2000000", "0.01", T0)),
        transfer_ev(Transfer("w1", "WITHDRAWAL", "BTC", D("0.01"), D("0"), T0 + DAY)),
    ])
    assert approx(sum(d.pnl.thb for d in taxed.disposals if d.counts_as_realised),
                  "10000")   # marked out at 3,000,000


def test_selling_more_than_we_can_explain_is_flagged_not_fabricated():
    oracle = FakeOracle(spot={"BTCTHB": "2000000", "USDTTHB": "34"})
    eng = CostBasisEngine(oracle, fiat="THB")
    run(eng, [ev(trade("1", "BTCTHB", "BTC", "THB", "SELL", "2000000", "0.5", T0))])
    assert approx(sum(d.pnl.thb for d in eng.disposals), "0")
    assert any(w.code == "missing_history" for w in eng.warnings)


def test_mixed_thb_and_usdt_purchases_of_the_same_coin_combine():
    """The exact situation the tracker exists for."""
    oracle = FakeOracle(
        spot={"BTCTHB": "2400000", "BTCUSDT": "70000", "USDTTHB": "35"},
        fx={T0: "34.00"},
    )
    eng = CostBasisEngine(oracle, fx_mode="lots", fiat="THB")
    run(eng, [
        # Leg 1: straight baht purchase — 0.01 BTC for 20,000 THB.
        ev(trade("1", "BTCTHB", "BTC", "THB", "BUY", "2000000", "0.01", T0)),
        # Leg 2: fund with tether at 34.00, then buy 0.01 BTC at 60,000 USDT.
        ev(trade("2", "USDTTHB", "USDT", "THB", "BUY", "34", "600", T0 + DAY)),
        ev(trade("3", "BTCUSDT", "BTC", "USDT", "BUY", "60000", "0.01", T0 + 2 * DAY)),
    ])
    cost = eng.basis("BTC")
    assert eng.qty("BTC") == D("0.02")
    assert approx(cost.thb, "40400")     # 20,000 + (600 * 34.00 = 20,400)
    assert approx(cost.usdt, str(D("20000") / D("34") + D("600")), tol="1")


def test_funding_conversions_are_not_counted_as_trades():
    """Spending tether you bought earlier is funding, not a realised gain."""
    oracle = FakeOracle(spot={"SOLUSDT": "150", "USDTTHB": "36"},
                        fx={T0: "34.00", T0 + 5 * DAY: "36.00"})
    eng = CostBasisEngine(oracle, fx_mode="lots", fiat="THB")
    run(eng, [
        ev(trade("1", "USDTTHB", "USDT", "THB", "BUY", "34", "1000", T0)),
        ev(trade("2", "SOLUSDT", "SOL", "USDT", "BUY", "150", "5", T0 + 5 * DAY)),
    ])
    funding = [d for d in eng.disposals if d.asset == "USDT"]
    assert funding and all(d.reason == "funding" for d in funding)
    assert all(not d.counts_as_realised for d in funding)
    assert all(d.proceeds.thb == d.cost.thb and d.proceeds.usdt == d.cost.usdt
               for d in funding)


def test_withdrawal_rows_balance_exactly():
    """A transfer out must show identical cost and proceeds, not near-misses."""
    oracle = FakeOracle(spot={"BTCTHB": "3000000", "USDTTHB": "34"})
    eng = CostBasisEngine(oracle, fiat="THB")
    run(eng, [
        ev(trade("1", "BTCTHB", "BTC", "THB", "BUY", "2000000", "0.01", T0)),
        ev(trade("2", "BTCTHB", "BTC", "THB", "BUY", "2500000", "0.01", T0 + DAY)),
        transfer_ev(Transfer("w1", "WITHDRAWAL", "BTC", D("0.015"), D("0.0002"),
                             T0 + 2 * DAY)),
    ])
    moves = [d for d in eng.disposals if d.reason == "transfer-out"]
    assert moves
    for d in moves:
        assert d.proceeds.thb == d.cost.thb
        assert d.proceeds.usdt == d.cost.usdt
    assert sum(d.pnl.thb for d in eng.disposals if d.counts_as_realised) == 0
