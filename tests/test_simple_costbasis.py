"""Average-cost accounting, and what happens when history runs out.

The rule these tests exist to pin down: a holding the exchange gave us no
purchase record for, and no manual cost covers, contributes market value and
nothing else. Never a profit, never a loss, never a zero that looks measured.
"""
import asyncio

from app.costbasis_simple import build_simple_state
from app.holdings import HoldingCost
from app.models import Balance, D, Trade
from tests.conftest import FakeOracle, approx

T0 = 1_700_000_000_000
DAY = 86_400_000


def trade(tid, symbol, base, quote, side, price, qty, ts, fee="0", fee_asset=""):
    price, qty = D(price), D(qty)
    return Trade(trade_id=tid, symbol=symbol, base_asset=base, quote_asset=quote,
                 side=side, price=price, qty=qty, quote_qty=price * qty,
                 fee=D(fee), fee_asset=fee_asset or quote, time=ts)


def build(trades, balances, holdings=None, spot=None):
    oracle = FakeOracle(spot=spot or {"BTCTHB": "2500000", "USDTTHB": "35",
                                      "SOLTHB": "5000"})
    bal = [Balance(asset=a, free=D(q), locked=D(0)) for a, q in balances.items()]
    return asyncio.run(build_simple_state(
        trades=trades, balances=bal, oracle=oracle, holdings=holdings or {},
    )), oracle


# -- the ordinary case -----------------------------------------------------


def test_average_cost_across_two_buys():
    state, _ = build(
        [trade("1", "BTCTHB", "BTC", "THB", "BUY", "2000000", "0.1", T0),
         trade("2", "BTCTHB", "BTC", "THB", "BUY", "3000000", "0.1", T0 + DAY)],
        {"BTC": "0.2"},
    )
    btc = state.positions["BTC"]
    assert approx(btc.cost.thb, "500000")
    assert approx(btc.avg_cost.thb, "2500000")
    assert approx(btc.unrealised.thb, "0")          # marked at 2,500,000
    assert btc.unknown_qty == 0


def test_sell_realises_against_the_average_not_a_lot():
    state, _ = build(
        [trade("1", "BTCTHB", "BTC", "THB", "BUY", "2000000", "0.1", T0),
         trade("2", "BTCTHB", "BTC", "THB", "BUY", "3000000", "0.1", T0 + DAY),
         trade("3", "BTCTHB", "BTC", "THB", "SELL", "4000000", "0.1", T0 + 2 * DAY)],
        {"BTC": "0.1"},
    )
    # sold at 4,000,000 against an average of 2,500,000
    assert approx(state.realised.thb, "150000")
    assert approx(state.positions["BTC"].cost.thb, "250000")


def test_fee_in_the_bought_asset_reduces_quantity_not_basis():
    state, _ = build(
        [trade("1", "BTCTHB", "BTC", "THB", "BUY", "2000000", "0.1", T0,
               fee="0.001", fee_asset="BTC")],
        {"BTC": "0.099"},
    )
    btc = state.positions["BTC"]
    assert approx(btc.qty, "0.099")
    assert approx(btc.cost.thb, "200000")           # you still paid the full 200k
    assert btc.unknown_qty == 0


# -- history that runs out --------------------------------------------------


def test_holding_with_no_purchase_record_is_excluded_from_pnl():
    """The BTC case: you hold more than the fills explain."""
    state, _ = build(
        [trade("1", "BTCTHB", "BTC", "THB", "BUY", "2000000", "0.1", T0)],
        {"BTC": "0.3"},
    )
    btc = state.positions["BTC"]
    assert approx(btc.qty, "0.3")
    assert approx(btc.unknown_qty, "0.2")
    assert btc.basis_unknown
    # cost and PnL cover only the 0.1 we have a fill for
    assert approx(btc.cost.thb, "200000")
    assert approx(btc.costed_value.thb, "250000")
    assert approx(btc.unrealised.thb, "50000")
    # the other 0.2 is reported as value, excluded from profit
    assert approx(btc.excluded_value.thb, "500000")
    assert approx(state.excluded_value.thb, "500000")
    assert state.unknown_assets == ["BTC"]


def test_manual_cost_covers_the_unexplained_quantity():
    holdings = {"BTC": HoldingCost("BTC", qty=None, cost_thb=D("300000"),
                                   cost_usdt=None)}
    state, _ = build(
        [trade("1", "BTCTHB", "BTC", "THB", "BUY", "2000000", "0.1", T0)],
        {"BTC": "0.3"}, holdings=holdings,
    )
    btc = state.positions["BTC"]
    assert btc.unknown_qty == 0
    assert not btc.basis_unknown
    assert approx(btc.cost.thb, "500000")           # 200,000 paid + 300,000 declared
    assert approx(btc.unrealised.thb, "250000")     # 0.3 at 2.5m = 750,000
    assert state.excluded_value.is_zero


def test_manual_quantity_larger_than_the_gap_is_clamped_and_warned():
    holdings = {"BTC": HoldingCost("BTC", qty=D("5"), cost_thb=D("1000000"),
                                   cost_usdt=None)}
    state, _ = build(
        [trade("1", "BTCTHB", "BTC", "THB", "BUY", "2000000", "0.1", T0)],
        {"BTC": "0.3"}, holdings=holdings,
    )
    btc = state.positions["BTC"]
    assert approx(btc.qty, "0.3")                   # no invented coins
    assert btc.unknown_qty == 0
    # cost scaled to the 0.2 actually unexplained: 1,000,000 * 0.2/5
    assert approx(btc.cost.thb, "240000")
    assert any(w.code == "manual_qty_clamped" for w in state.warnings)


def test_selling_a_pre_history_bag_realises_nothing_without_a_cost():
    """The SOL case: sold 45 that were never bought on record."""
    state, _ = build(
        [trade("1", "SOLTHB", "SOL", "THB", "SELL", "6000", "45", T0)],
        {"SOL": "0"},
    )
    assert state.realised.is_zero, "unknown basis cannot produce a realised gain"
    assert any(w.code == "basis_unknown" and w.asset == "SOL"
               for w in state.warnings)


def test_manual_cost_lets_a_pre_history_sale_realise():
    holdings = {"SOL": HoldingCost("SOL", qty=None, cost_thb=D("180000"),
                                   cost_usdt=None)}
    state, _ = build(
        [trade("1", "SOLTHB", "SOL", "THB", "SELL", "6000", "45", T0)],
        {"SOL": "0"}, holdings=holdings,
    )
    assert approx(state.realised.thb, "90000")      # 270,000 proceeds - 180,000


def test_manually_costed_pre_history_coins_join_the_average():
    holdings = {"BTC": HoldingCost("BTC", qty=None, cost_thb=D("100000"),
                                   cost_usdt=None)}
    state, _ = build(
        [trade("1", "BTCTHB", "BTC", "THB", "BUY", "2000000", "0.1", T0),
         trade("2", "BTCTHB", "BTC", "THB", "SELL", "3000000", "0.1", T0 + DAY)],
        {"BTC": "0.1"}, holdings=holdings,
    )
    # 0.1 at 100,000 declared plus 0.1 at 200,000 paid averages to 150,000 each
    assert approx(state.realised.thb, "150000")
    assert approx(state.positions["BTC"].cost.thb, "150000")


def test_unknown_coins_are_sold_before_costed_ones():
    """Only the *uncosted* bag jumps the queue — it has no average to join."""
    state, _ = build(
        [trade("1", "BTCTHB", "BTC", "THB", "BUY", "2000000", "0.1", T0),
         trade("2", "BTCTHB", "BTC", "THB", "SELL", "3000000", "0.1", T0 + DAY)],
        {"BTC": "0.1"},
    )
    # opening 0.1 has no basis, so it is sold first and realises nothing
    assert state.realised.is_zero
    assert approx(state.positions["BTC"].cost.thb, "200000")
    assert state.positions["BTC"].unknown_qty == 0


def test_holdings_lower_than_trades_imply_shrink_basis_without_profit():
    """The USDT case: coins left by a route the API never reported."""
    state, _ = build(
        [trade("1", "USDTTHB", "USDT", "THB", "BUY", "35", "1000", T0)],
        {"USDT": "100"},
    )
    usdt = state.positions["USDT"]
    assert approx(usdt.qty, "100")
    assert approx(usdt.cost.thb, "3500")            # 35,000 scaled to what remains
    assert state.realised.is_zero
    assert any(w.code == "untracked_outflow" and w.asset == "USDT"
               for w in state.warnings)


def test_quote_asset_spend_is_at_cost_and_makes_no_profit():
    """Spending USDT to buy BTC is not a USDT disposal for gain."""
    state, _ = build(
        [trade("1", "USDTTHB", "USDT", "THB", "BUY", "35", "1000", T0),
         trade("2", "BTCUSDT", "BTC", "USDT", "BUY", "70000", "0.01", T0 + DAY)],
        {"USDT": "300", "BTC": "0.01"},
        spot={"BTCTHB": "2500000", "USDTTHB": "35", "BTCUSDT": "71428"},
    )
    assert state.realised.is_zero
    assert approx(state.positions["USDT"].qty, "300")


def test_cash_carries_no_profit_and_no_unknown():
    state, _ = build([], {"THB": "1000000"})
    thb = state.positions["THB"]
    assert thb.unknown_qty == 0
    assert thb.unrealised.is_zero
