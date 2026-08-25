"""Guards on which host/dialect pair we talk to.

The tracker once auto-detected ``www.binance.th`` + ``apiv3`` because that host
answers ``/api/v3/time`` — but it proxies *global* binance.com, so every signed
request came back ``-2015 Invalid API-key, IP, or permissions for action``.
"""
from app.dialects import BASE_URL_CANDIDATES, DIALECTS


def test_thv1_is_probed_first():
    assert list(DIALECTS)[0] == "thv1"


def test_www_host_is_not_a_candidate():
    assert "https://www.binance.th" not in BASE_URL_CANDIDATES
    assert BASE_URL_CANDIDATES[0] == "https://api.binance.th"


def test_thv1_paths_match_the_published_spec():
    d = DIALECTS["thv1"]
    assert d.time()[0] == "/api/v1/time"
    assert d.symbols()[0] == "/api/v1/exchangeInfo"
    assert d.prices()[0] == "/api/v1/ticker/price"
    assert d.klines("BTCTHB", "1h", None, None, 5)[0] == "/api/v1/klines"
    assert d.account()[0] == "/api/v1/accountV2"
    assert d.my_trades("BTCTHB", None, None, None, 500)[0] == "/api/v1/userTrades"
    assert d.deposits(None, None, None, 500)[0] == "/api/v1/capital/deposit/history"
    assert d.withdrawals(None, None, None, 500)[0] == "/api/v1/capital/withdraw/history"


def test_thv1_returns_bare_payloads():
    """No ``{"code":0,"data":...}`` envelope on this surface."""
    payload = {"balances": [{"asset": "THB", "free": "1", "locked": "0"}]}
    assert DIALECTS["thv1"].unwrap(payload) is payload


def test_as_ms_understands_the_withdrawal_datetime_string():
    """``applyTime`` is a string while everything else is epoch millis."""
    from app.dialects import as_ms

    assert as_ms("2025-01-29 04:13:50") == 1738124030000
    assert as_ms("2025-01-29T04:13:50Z") == 1738124030000
    assert as_ms(1738124030000) == 1738124030000
    assert as_ms("not a date") == 0
    assert as_ms(None) == 0


def test_withdrawal_rows_keep_their_timestamp():
    payload = [{"id": "353098", "amount": "886.7775", "transactionFee": "0",
                "status": 6, "coin": "USDT", "applyTime": "2025-01-29 04:13:50",
                "network": "BSC", "txId": "237771762374"}]
    [row] = DIALECTS["thv1"].parse_withdrawals(payload)
    assert row.time == 1738124030000
