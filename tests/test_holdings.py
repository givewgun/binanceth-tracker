"""Parsing the manual cost file."""
import pytest

from app.holdings import HoldingCost, HoldingsError, load_holdings, parse_holdings

VALID = """
[BTC]
qty = 0.14676540
cost_thb = 2100000

[SOL]
cost_usdt = 4500
"""


def test_parses_entries_and_upper_cases_assets():
    holdings = parse_holdings(VALID)
    assert set(holdings) == {"BTC", "SOL"}
    assert holdings["BTC"] == HoldingCost(asset="BTC", qty=D("0.14676540"),
                                          cost_thb=D("2100000"), cost_usdt=None)
    assert holdings["SOL"].qty is None       # omitted: covers whatever is unexplained
    assert holdings["SOL"].cost_usdt == D("4500")


def test_lower_case_asset_names_are_accepted():
    assert set(parse_holdings("[btc]\ncost_thb = 1")) == {"BTC"}


def test_missing_file_is_not_an_error(tmp_path):
    assert load_holdings(tmp_path / "nope.toml") == {}


def test_entry_without_any_cost_is_rejected():
    with pytest.raises(HoldingsError, match="BTC.*cost_thb.*cost_usdt"):
        parse_holdings("[BTC]\nqty = 1")


def test_negative_values_are_rejected():
    with pytest.raises(HoldingsError, match="BTC"):
        parse_holdings("[BTC]\ncost_thb = -5")
    with pytest.raises(HoldingsError, match="BTC"):
        parse_holdings("[BTC]\nqty = -1\ncost_thb = 5")


def test_unknown_key_is_rejected_rather_than_ignored():
    with pytest.raises(HoldingsError, match="avg_price"):
        parse_holdings("[BTC]\navg_price = 100")


def test_malformed_toml_is_rejected():
    with pytest.raises(HoldingsError):
        parse_holdings("[BTC\ncost_thb = 1")


def test_non_table_entry_is_rejected():
    with pytest.raises(HoldingsError, match="BTC"):
        parse_holdings("BTC = 100")


from app.models import D  # noqa: E402  (imported late to keep the file readable)
