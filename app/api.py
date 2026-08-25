"""HTTP surface: JSON for the dashboard, SSE for the live ticks."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .service import money, num, service

log = logging.getLogger("binanceth.api")
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await service.startup()
    yield
    await service.shutdown()


app = FastAPI(title="Binance TH Portfolio Tracker", version="1.0.0",
              lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
async def status():
    return {
        "connected": not service.connection_error,
        "connection_error": service.connection_error,
        "has_credentials": settings.has_credentials,
        "base_url": service.client.base_url,
        "dialect": service.client.dialect_name,
        "settings": settings.redacted(),
        "counts": service.store.counts(),
        "last_sync": service.store.get_meta_int("last_sync", 0),
        "sync": service.sync.progress.as_dict(),
        "fx_rate": num(service.oracle.usdt_thb()),
    }


@app.post("/api/sync")
async def start_sync(full: bool = Query(False), deep: bool = Query(False)):
    """Kick off a background sync. ``deep`` scans every listed pair."""
    if not settings.has_credentials:
        raise HTTPException(400, "No API credentials configured. See .env.example.")
    if service.sync.progress.running:
        return {"started": False, "progress": service.sync.progress.as_dict()}

    async def run():
        await service.sync.run(full=full, deep=deep)
        service.invalidate()

    asyncio.create_task(run())
    await asyncio.sleep(0.1)
    return {"started": True, "progress": service.sync.progress.as_dict()}


@app.get("/api/sync/status")
async def sync_status():
    return {"progress": service.sync.progress.as_dict(),
            "counts": service.store.counts()}


@app.get("/api/portfolio")
async def portfolio(method: str = Query(""), fx_mode: str = Query(""),
                    force: bool = Query(False)):
    return await service.portfolio_dict(
        method=method or None, fx_mode=fx_mode or None, force=force)


@app.get("/api/trades")
async def trades(limit: int = Query(500, le=5000), offset: int = Query(0),
                 asset: str = Query(""), symbol: str = Query(""),
                 side: str = Query(""), since: int = Query(0)):
    rows = service.store.trades(
        since=since or None, asset=asset.upper() or None,
        symbol=symbol.upper() or None)
    if side:
        rows = [t for t in rows if t.side == side.upper()]
    rows.sort(key=lambda t: t.time, reverse=True)
    total = len(rows)
    page = rows[offset: offset + limit]

    fx = service.oracle.usdt_thb()
    out = []
    for t in page:
        quote_qty = t.effective_quote_qty
        thb = quote_qty if t.quote_asset == "THB" else None
        usdt = quote_qty if t.quote_asset == "USDT" else None
        if thb is None and usdt is not None and fx:
            thb = usdt * fx
        if usdt is None and thb is not None and fx:
            usdt = thb / fx
        out.append({
            "id": t.trade_id, "symbol": t.symbol, "base": t.base_asset,
            "quote": t.quote_asset, "side": t.side, "price": num(t.price),
            "qty": num(t.qty), "qty_exact": str(t.qty),
            "quote_qty": num(quote_qty),
            "value": {"thb": num(thb), "usdt": num(usdt)},
            "fee": num(t.fee), "fee_asset": t.fee_asset, "time": t.time,
            "order_id": t.order_id, "maker": t.is_maker,
        })
    return {"total": total, "offset": offset, "rows": out}


@app.get("/api/transfers")
async def transfers(kind: str = Query(""), asset: str = Query(""),
                    limit: int = Query(1000, le=5000)):
    rows = service.store.transfers(
        kind=kind.upper() or None, asset=asset.upper() or None,
        completed_only=False)
    rows.sort(key=lambda t: t.time, reverse=True)
    fx = service.oracle.usdt_thb()

    out = []
    for t in rows[:limit]:
        if t.asset == settings.fiat:
            value_now = {"thb": num(t.amount),
                         "usdt": num(t.amount / fx) if fx else 0.0}
        else:
            value_now = money(service.oracle.value(t.asset, t.amount)[0])
        out.append({
            "id": t.transfer_id, "kind": t.kind, "asset": t.asset,
            "amount": num(t.amount), "amount_exact": str(t.amount),
            "fee": num(t.fee), "time": t.time, "status": t.status,
            "tx_id": t.tx_id, "network": t.network, "address": t.address,
            "is_fiat": t.is_fiat,
            "value_now": value_now,
        })
    return {"total": len(rows), "rows": out}


@app.get("/api/realised")
async def realised(limit: int = Query(500, le=5000), asset: str = Query("")):
    return {"rows": await service.realised_rows(limit=limit, asset=asset)}


@app.get("/api/history")
async def history(refresh: bool = Query(False)):
    return {"rows": await service.history_rows(refresh=refresh)}


@app.get("/api/prices")
async def prices():
    return {
        "fx_rate": num(service.oracle.usdt_thb()),
        "updated": service.oracle.last_refresh,
        "prices": {k: num(v) for k, v in sorted(service.oracle.prices.items())},
    }


@app.get("/api/events")
async def events(request: Request):
    """Server-sent events: one message per price refresh."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=8)
    service.subscribers.add(queue)

    async def stream():
        try:
            first = await service.portfolio_dict()
            yield f"data: {json.dumps({'type': 'snapshot', 'portfolio': first})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=20)
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            service.subscribers.discard(queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"error": str(exc)})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
