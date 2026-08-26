"""Long-lived application state: one client, one store, one price loop."""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Optional

from .client import BinanceTHClient, BinanceTHError
from .config import settings
from .models import Money, Position
from .portfolio import PortfolioState, build_history, build_portfolio
from .pricing import PriceOracle
from .store import Store
from .sync import Synchroniser

log = logging.getLogger("binanceth.service")


def num(value) -> float:
    """Decimal → float, for JSON. Exactness lives in the engine, not the wire."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def money(m: Money) -> dict:
    return {"thb": num(m.thb), "usdt": num(m.usdt)}


class PortfolioService:
    def __init__(self):
        self.store = Store(settings.db_file)
        self.client = BinanceTHClient()
        self.oracle = PriceOracle(self.client, self.store)
        self.sync = Synchroniser(self.client, self.store, self.oracle)

        self.oracle.load_symbols()
        self._snapshot: Optional[PortfolioState] = None
        self._snapshot_at: int = 0
        self._snapshot_key: tuple = ()
        self._lock = asyncio.Lock()
        self._price_task: Optional[asyncio.Task] = None
        self.connection_error: str = ""
        self.subscribers: set[asyncio.Queue] = set()

    # -- lifecycle --------------------------------------------------------

    async def startup(self) -> None:
        try:
            await self.client.ensure_ready()
            self.connection_error = ""
        except BinanceTHError as exc:
            self.connection_error = str(exc)
            log.warning("starting up offline: %s", exc)
        if not self.oracle.symbols:
            try:
                await self.oracle.refresh_symbols()
            except Exception as exc:                       # noqa: BLE001
                log.warning("symbol list unavailable: %s", exc)
        self._price_task = asyncio.create_task(self._price_loop())

    async def shutdown(self) -> None:
        if self._price_task:
            self._price_task.cancel()
            try:
                await self._price_task
            except (asyncio.CancelledError, Exception):    # noqa: BLE001
                pass
        await self.client.close()
        self.store.close()

    # -- live prices ------------------------------------------------------

    async def _price_loop(self) -> None:
        """Keep marks fresh and push a compact tick to every open dashboard."""
        while True:
            try:
                if settings.has_credentials or self.oracle.symbols:
                    assets = {b.asset for b in self.store.balances()}
                    await self.oracle.refresh_prices(assets)
                    self.connection_error = ""
                    await self._broadcast_tick()
            except asyncio.CancelledError:
                raise
            except BinanceTHError as exc:
                self.connection_error = str(exc)
                log.debug("price refresh failed: %s", exc)
            except Exception as exc:                       # noqa: BLE001
                log.debug("price loop error: %s", exc)
            await asyncio.sleep(max(2, settings.price_refresh_seconds))

    async def _broadcast_tick(self) -> None:
        if not self.subscribers:
            return
        try:
            snapshot = await self.portfolio()
        except Exception:                                  # noqa: BLE001
            return
        payload = {
            "type": "tick",
            "at": int(time.time() * 1000),
            "fx": num(self.oracle.usdt_thb()),
            "totals": self._totals(snapshot),
            "prices": {
                p.asset: {"thb": num(p.price.thb), "usdt": num(p.price.usdt),
                          "value_thb": num(p.market_value.thb),
                          "value_usdt": num(p.market_value.usdt),
                          "unrealised_thb": num(p.unrealised.thb),
                          "unrealised_usdt": num(p.unrealised.usdt)}
                for p in snapshot.positions.values()
            },
        }
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    # -- snapshot ---------------------------------------------------------

    async def portfolio(self, *, method: Optional[str] = None,
                        fx_mode: Optional[str] = None,
                        force: bool = False) -> PortfolioState:
        """Cached portfolio state; the replay is cheap but not free."""
        key = (method or settings.cost_basis_method,
               fx_mode or settings.fx_mode,
               self.store.counts()["trades"],
               self.store.counts()["deposits"] + self.store.counts()["withdrawals"])
        async with self._lock:
            fresh = (self._snapshot is not None
                     and self._snapshot_key == key
                     and not force)
            if fresh:
                # Re-mark the cached lots at the latest prices without replaying.
                self._remark(self._snapshot)
                return self._snapshot
            state = await build_portfolio(self.store, self.oracle,
                                          method=method, fx_mode=fx_mode)
            self._snapshot, self._snapshot_key = state, key
            self._snapshot_at = int(time.time() * 1000)
            return state

    def _remark(self, state: PortfolioState) -> None:
        for position in state.positions.values():
            value, source = self.oracle.value(position.asset, position.qty)
            price, _ = self.oracle.price_pair(position.asset)
            position.market_value = value
            position.price = price
            if source != "unpriced":
                position.price_source = source
            if position.price_source == "cash":
                position.cost = value
        state.fx_rate = self.oracle.usdt_thb()

    def invalidate(self) -> None:
        self._snapshot = None
        self._snapshot_key = ()

    # -- serialisation ----------------------------------------------------

    def _totals(self, state: PortfolioState) -> dict:
        equity = state.market_value
        cost = state.cost
        unrealised = state.unrealised
        realised = state.realised
        net_invested = state.net_invested
        total_pnl = Money(equity.thb + state.withdrawals_value.thb
                          - state.deposits_value.thb,
                          equity.usdt + state.withdrawals_value.usdt
                          - state.deposits_value.usdt)
        btc_price = self.oracle.route("BTC", "USDT").value
        equity_btc = equity.usdt / btc_price if btc_price else None
        return {
            "equity": money(equity),
            "equity_btc": num(equity_btc),
            "btc_price": num(btc_price),
            "cost": money(cost),
            "excluded": money(state.excluded_value),
            "unknown_assets": state.unknown_assets,
            "unrealised": money(unrealised),
            "realised": money(realised),
            "fees": money(state.fees_paid),
            "deposits": money(state.deposits_value),
            "withdrawals": money(state.withdrawals_value),
            "net_invested": money(net_invested),
            "total_pnl": money(total_pnl),
            "unrealised_pct": {
                "thb": num(unrealised.thb / cost.thb * 100) if cost.thb else 0.0,
                "usdt": num(unrealised.usdt / cost.usdt * 100) if cost.usdt else 0.0,
            },
            "total_pnl_pct": {
                "thb": num(total_pnl.thb / net_invested.thb * 100)
                       if net_invested.thb else 0.0,
                "usdt": num(total_pnl.usdt / net_invested.usdt * 100)
                        if net_invested.usdt else 0.0,
            },
            "fx_rate": num(state.fx_rate),
        }

    def position_dict(self, p: Position, total_equity: Decimal) -> dict:
        weight = float(p.market_value.thb / total_equity * 100) if total_equity else 0.0
        return {
            "asset": p.asset,
            "qty": num(p.qty),
            "qty_exact": str(p.qty),
            "free": num(p.free),
            "locked": num(p.locked),
            "price": money(p.price),
            "avg_cost": money(p.avg_cost),
            "cost": money(p.cost),
            "value": money(p.market_value),
            "unrealised": money(p.unrealised),
            "realised": money(p.realised),
            "roi": {"thb": num(p.roi("THB")), "usdt": num(p.roi("USDT"))},
            "weight": weight,
            "price_source": p.price_source,
            "cost_assumed": p.cost_assumed,
            "basis_unknown": p.basis_unknown,
            "unknown_qty": num(p.unknown_qty),
            "excluded_value": money(p.excluded_value),
            "is_cash": p.price_source == "cash",
            "lots": [
                {"qty": num(l.qty), "cost": money(l.cost),
                 "unit_cost": money(l.unit_cost), "acquired": l.acquired,
                 "source": l.source, "assumed": l.cost_assumed}
                for l in sorted(p.lots, key=lambda x: x.acquired)
            ],
        }

    async def portfolio_dict(self, **kwargs) -> dict:
        state = await self.portfolio(**kwargs)
        equity = state.market_value.thb
        positions = sorted(
            (self.position_dict(p, equity) for p in state.positions.values()),
            key=lambda d: d["value"]["thb"], reverse=True,
        )
        return {
            "totals": self._totals(state),
            "positions": positions,
            "warnings": [w.as_dict() for w in state.warnings],
            "meta": {
                "base_currency": settings.base_currency,
                "cost_basis_method": kwargs.get("method") or settings.cost_basis_method,
                "fx_mode": kwargs.get("fx_mode") or settings.fx_mode,
                "last_sync": self.store.get_meta_int("last_sync", 0),
                "last_price_refresh": self.oracle.last_refresh,
                "dialect": self.client.dialect_name,
                "base_url": self.client.base_url,
                "connection_error": self.connection_error,
                "counts": self.store.counts(),
            },
        }

    async def realised_rows(self, limit: int = 500, asset: str = "") -> list[dict]:
        state = await self.portfolio()
        rows = []
        for d in state.disposals:
            if asset and d.asset != asset.upper():
                continue
            rows.append({
                "asset": d.asset,
                "qty": num(d.qty),
                "proceeds": money(d.proceeds),
                "cost": money(d.cost),
                "pnl": money(d.pnl),
                "roi": {
                    "thb": num(d.pnl.thb / d.cost.thb * 100) if d.cost.thb else 0.0,
                    "usdt": num(d.pnl.usdt / d.cost.usdt * 100) if d.cost.usdt else 0.0,
                },
                "time": d.time,
                "acquired": d.acquired,
                "holding_days": round(d.holding_days, 2),
                "reason": d.reason,
                "counts": d.counts_as_realised,
                "assumed": d.cost_assumed,
                "ref": d.ref,
            })
        rows.sort(key=lambda r: r["time"], reverse=True)
        return rows[:limit]

    #: Bumped whenever the curve's arithmetic changes, so a cache built by
    #: older code is rebuilt rather than served forever.
    HISTORY_VERSION = 2

    def _history_signature(self) -> str:
        """What the cached curve was built from."""
        counts = self.store.counts()
        return "|".join(str(x) for x in (
            self.HISTORY_VERSION,
            settings.cost_basis_method,
            settings.fx_mode,
            counts["trades"], counts["deposits"], counts["withdrawals"],
            counts["last_trade"],
            self.store.get_meta_int("last_sync", 0),
        ))

    async def history_rows(self, refresh: bool = False) -> list[dict]:
        """The daily curve, rebuilt whenever its inputs have moved.

        Freshness was previously judged by the date of the last cached row, so
        a curve computed once — however wrongly — was served for the rest of
        the day and every day after. A run that stored 641 zeroes therefore
        survived every fix to the code that produced them.
        """
        signature = self._history_signature()
        cached = self.store.equity_history()
        if (cached and not refresh
                and self.store.get_meta("equity_history_sig") == signature):
            return [self._history_row(r) for r in cached]

        rows = await build_history(self.store, self.oracle)
        self.store.set_meta("equity_history_sig", signature)
        return [self._history_row(r) for r in rows]

    @staticmethod
    def _history_row(r: dict) -> dict:
        def g(key: str) -> float:
            return num(r.get(key) or 0)
        equity_thb, cost_thb = g("equity_thb"), g("cost_thb")
        equity_usdt, cost_usdt = g("equity_usdt"), g("cost_usdt")
        return {
            "day": r["day"],
            "ts": r["ts"],
            "equity": {"thb": equity_thb, "usdt": equity_usdt},
            "cost": {"thb": cost_thb, "usdt": cost_usdt},
            "unrealised": {"thb": equity_thb - cost_thb,
                           "usdt": equity_usdt - cost_usdt},
            "realised": {"thb": g("realised_thb"), "usdt": g("realised_usdt")},
            "net_deposit": {"thb": g("net_deposit_thb"),
                            "usdt": g("net_deposit_usdt")},
        }


service = PortfolioService()
