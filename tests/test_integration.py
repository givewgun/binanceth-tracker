"""End-to-end: mock exchange → client → store → cost basis → HTTP API.

Runs the bundled mock server in a background thread, so it exercises signing,
pagination, both API dialects and the whole accounting path without touching
the real exchange.
"""
import os
import socket
import sys
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import mock_binance_th as mock  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    port = free_port()
    httpd = HTTPServer(("127.0.0.1", port), mock.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


@pytest.fixture
def app_env(server, tmp_path, monkeypatch):
    """A fresh app instance pointed at the mock, with its own database."""
    monkeypatch.setenv("BINANCE_TH_API_KEY", "demo")
    monkeypatch.setenv("BINANCE_TH_API_SECRET", "demo")

    from app.client import BinanceTHClient
    from app.config import settings
    from app.pricing import PriceOracle
    from app.store import Store
    from app.sync import Synchroniser

    store = Store(tmp_path / "t.db")
    client = BinanceTHClient(api_key="demo", api_secret="demo", base_url=server)
    oracle = PriceOracle(client, store)
    syncer = Synchroniser(client, store, oracle)
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    # The developer's own .env may select a different engine.
    monkeypatch.setattr(settings, "cost_basis_method", "fifo")
    yield store, client, oracle, syncer


@pytest.mark.parametrize("dialect", ["thv1", "openv1", "apiv3"])
@pytest.mark.asyncio
async def test_full_sync_and_valuation(app_env, dialect, monkeypatch):
    from app.client import BinanceTHClient
    from app.dialects import DIALECTS
    from app.portfolio import build_portfolio

    store, client, oracle, syncer = app_env
    client._dialect = DIALECTS[dialect]
    mock.Handler.dialect = dialect

    result = await syncer.run(full=True, with_candles=True)
    assert not result["progress"]["error"], result["progress"]["error"]

    counts = result["counts"]
    assert counts["trades"] == len(mock.TRADES)
    assert counts["deposits"] == len(mock.DEPOSITS)
    assert counts["withdrawals"] == len(mock.WITHDRAWALS)
    assert counts["symbols"] == len(mock.SYMBOLS)

    state = await build_portfolio(store, oracle)
    assert state.positions, "expected open positions"
    assert state.market_value.thb > 0
    assert state.market_value.usdt > 0

    # Both books must agree once converted through the live rate.
    fx = oracle.usdt_thb()
    assert fx and fx > 0
    implied = state.market_value.usdt * fx
    drift = abs(implied - state.market_value.thb) / state.market_value.thb
    assert drift < 0.02, f"THB and USDT views disagree by {drift:.2%}"

    # Every open lot must carry a cost in both currencies.
    for position in state.positions.values():
        if position.price_source == "cash" or position.qty == 0:
            continue
        assert position.cost.thb > 0, f"{position.asset} has no THB basis"
        assert position.cost.usdt > 0, f"{position.asset} has no USDT basis"

    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_incremental_sync_adds_nothing_the_second_time(app_env):
    store, client, oracle, syncer = app_env
    mock.Handler.dialect = "openv1"
    from app.dialects import DIALECTS
    client._dialect = DIALECTS["openv1"]

    await syncer.run(full=True, with_candles=False)
    first = store.counts()
    await syncer.run(full=False, with_candles=False)
    assert store.counts()["trades"] == first["trades"]
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_bad_signature_is_reported(server):
    from app.client import BinanceTHClient, BinanceTHError

    client = BinanceTHClient(api_key="demo", api_secret="not-the-secret",
                             base_url=server)
    with pytest.raises(BinanceTHError) as excinfo:
        await client.balances()
    assert excinfo.value.is_auth_error
    await client.close()


@pytest.mark.asyncio
async def test_http_api_serves_the_dashboard(app_env, monkeypatch, server):
    """The FastAPI surface answers with the shapes the frontend expects."""
    from fastapi.testclient import TestClient

    store, client, oracle, syncer = app_env
    mock.Handler.dialect = "openv1"
    await syncer.run(full=True, with_candles=True)

    from app import api as api_module
    from app.service import service as real_service

    # Point the shared service at this test's already-synced objects.
    monkeypatch.setattr(real_service, "store", store)
    monkeypatch.setattr(real_service, "client", client)
    monkeypatch.setattr(real_service, "oracle", oracle)
    monkeypatch.setattr(real_service, "sync", syncer)
    real_service.invalidate()

    with TestClient(api_module.app) as http:
        portfolio = http.get("/api/portfolio").json()
        assert portfolio["totals"]["equity"]["thb"] > 0
        assert portfolio["positions"]
        for key in ("value", "cost", "unrealised", "price", "avg_cost"):
            assert {"thb", "usdt"} <= set(portfolio["positions"][0][key])

        trades = http.get("/api/trades?limit=5").json()
        assert trades["total"] == len(mock.TRADES)
        assert {"thb", "usdt"} <= set(trades["rows"][0]["value"])

        transfers = http.get("/api/transfers").json()
        assert transfers["total"] == len(mock.DEPOSITS) + len(mock.WITHDRAWALS)

        assert http.get("/api/realised").json()["rows"]
        assert http.get("/api/history").json()["rows"]
        assert http.get("/api/status").status_code == 200
        assert http.get("/").status_code == 200

    await client.close()
    store.close()
