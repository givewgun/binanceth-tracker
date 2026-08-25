"""Cost-basis accounting and PnL.

The model this file implements
-----------------------------
Every trade is read as *one asset leaving and another arriving*.  The value of
that exchange is recorded in **both** THB and USDT, so a portfolio funded half
in baht and half in tether has one coherent set of books instead of two
half-broken ones.

The interesting decision is what THB cost to attach to a coin bought with USDT.
Two answers, chosen with ``FX_MODE``:

``lots`` (default)
    Trace the baht you actually paid for those tethers.  Buy USDT at 34.20,
    buy SOL with it a week later, and the SOL's THB basis reflects 34.20 — not
    whatever the rate happened to be on the day of the SOL trade.  Spending a
    stablecoin is treated as funding, not as a taxable FX event, so the basis
    rides through into the coin.

``market``
    Convert at the USDTTHB rate at the moment of the trade, and realise the
    FX gain or loss on the tether leg right there.

Trades quoted in a *crypto* (BTC, BNB, …) always realise normally — spending
appreciated BTC to buy an altcoin is a real disposal, and hiding it would
understate what happened.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from .config import settings
from .models import (D, Disposal, Lot, Money, Position, Trade, Transfer, ZERO)
from .pricing import STABLES, PriceOracle

log = logging.getLogger("binanceth.portfolio")

DAY_MS = 86_400_000


def day_key(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class LedgerEvent:
    """A trade or transfer, flattened onto one timeline."""

    time: int
    kind: str          # trade | deposit | withdrawal
    trade: Optional[Trade] = None
    transfer: Optional[Transfer] = None

    @property
    def sort_key(self) -> tuple:
        ref = self.trade.trade_id if self.trade else (
            self.transfer.transfer_id if self.transfer else "")
        # Deposits settle before the trades they fund on the same millisecond.
        rank = {"deposit": 0, "trade": 1, "withdrawal": 2}[self.kind]
        return (self.time, rank, str(ref))


@dataclass
class Warning_:
    code: str
    message: str
    asset: str = ""
    time: int = 0

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "asset": self.asset, "time": self.time}


@dataclass
class PortfolioState:
    positions: dict[str, Position] = field(default_factory=dict)
    disposals: list[Disposal] = field(default_factory=list)
    cash: Decimal = ZERO                                   # idle THB
    deposits_value: Money = field(default_factory=Money)
    withdrawals_value: Money = field(default_factory=Money)
    fees_paid: Money = field(default_factory=Money)
    warnings: list[Warning_] = field(default_factory=list)
    fx_rate: Optional[Decimal] = None

    @property
    def realised(self) -> Money:
        total = Money()
        for d in self.disposals:
            if d.counts_as_realised:
                total = total + d.pnl
        return total

    @property
    def cost(self) -> Money:
        total = Money()
        for p in self.positions.values():
            total = total + p.cost
        return total

    @property
    def market_value(self) -> Money:
        total = Money()
        for p in self.positions.values():
            total = total + p.market_value
        return total

    @property
    def unrealised(self) -> Money:
        # Summed per position rather than ``market_value - cost``: holdings
        # with no known basis contribute value but must not contribute profit.
        total = Money()
        for p in self.positions.values():
            total = total + p.unrealised
        return total

    @property
    def excluded_value(self) -> Money:
        """Market value the tracker refuses to claim a profit or loss on."""
        total = Money()
        for p in self.positions.values():
            total = total + p.excluded_value
        return total

    @property
    def unknown_assets(self) -> list[str]:
        return sorted(p.asset for p in self.positions.values() if p.basis_unknown)

    @property
    def net_invested(self) -> Money:
        return self.deposits_value - self.withdrawals_value


class CostBasisEngine:
    """Replays a ledger and maintains open lots + realised disposals."""

    def __init__(self, oracle: PriceOracle, method: str = "fifo",
                 fx_mode: str = "lots", treat_withdrawal_as_sale: bool = False,
                 fiat: str = "THB"):
        self.oracle = oracle
        self.method = method if method in ("fifo", "avg") else "fifo"
        self.fx_mode = fx_mode if fx_mode in ("lots", "market") else "lots"
        self.treat_withdrawal_as_sale = treat_withdrawal_as_sale
        self.fiat = fiat

        self.lots: dict[str, deque[Lot]] = defaultdict(deque)
        self.disposals: list[Disposal] = []
        self.cash: Decimal = ZERO
        self.deposits_value = Money()
        self.withdrawals_value = Money()
        self.fees_paid = Money()
        self.warnings: list[Warning_] = []
        self._warned: set[tuple] = set()

    # -- helpers ----------------------------------------------------------

    def warn(self, code: str, message: str, asset: str = "", time: int = 0) -> None:
        key = (code, asset)
        if key in self._warned:
            return
        self._warned.add(key)
        self.warnings.append(Warning_(code, message, asset, time))

    def _carries_basis(self, asset: str) -> bool:
        """True when spending this asset should pass its THB basis along."""
        return self.fx_mode == "lots" and asset.upper() in STABLES

    def qty(self, asset: str) -> Decimal:
        return sum((lot.qty for lot in self.lots[asset]), ZERO)

    def basis(self, asset: str) -> Money:
        total = Money()
        for lot in self.lots[asset]:
            total = total + lot.cost
        return total

    # -- lot mechanics ----------------------------------------------------

    def add_lot(self, asset: str, qty: Decimal, cost: Money, ts: int,
                source: str = "trade", ref: str = "", assumed: bool = False) -> None:
        if qty <= 0:
            return
        lot = Lot(asset=asset, qty=qty, cost=cost, acquired=ts,
                  source=source, ref=ref, cost_assumed=assumed)
        if self.method == "avg" and self.lots[asset]:
            # Weighted average: collapse everything into a single parcel.
            merged = self.lots[asset][0]
            merged_qty = merged.qty + qty
            merged.cost = merged.cost + cost
            merged.qty = merged_qty
            merged.cost_assumed = merged.cost_assumed or assumed
            merged.acquired = min(merged.acquired, ts)
            return
        self.lots[asset].append(lot)

    def take_lots(self, asset: str, qty: Decimal, proceeds: Money, ts: int,
                  reason: str = "sell", ref: str = "") -> tuple[Money, bool]:
        """Consume ``qty`` of ``asset``; returns (cost released, cost_was_assumed).

        Proceeds are split across the consumed lots pro-rata so each disposal
        row carries its own honest PnL.
        """
        if qty <= 0:
            return Money(), False

        remaining = qty
        released = Money()
        assumed = False
        pool = self.lots[asset]

        while remaining > 0 and pool:
            lot = pool[0]
            take = min(lot.qty, remaining)
            lot_cost = lot.cost.scaled(take, lot.qty)
            lot_proceeds = proceeds.scaled(take, qty)

            self.disposals.append(Disposal(
                asset=asset, qty=take, proceeds=lot_proceeds, cost=lot_cost,
                time=ts, acquired=lot.acquired, reason=reason, ref=ref,
                cost_assumed=lot.cost_assumed,
            ))
            released = released + lot_cost
            assumed = assumed or lot.cost_assumed

            lot.qty -= take
            lot.cost = lot.cost - lot_cost
            remaining -= take
            if lot.qty <= 0:
                pool.popleft()

        if remaining > 0:
            # Selling more than our history explains: the missing units almost
            # always predate the exchange's trade-history window.
            short_proceeds = proceeds.scaled(remaining, qty)
            self.disposals.append(Disposal(
                asset=asset, qty=remaining, proceeds=short_proceeds,
                cost=short_proceeds, time=ts, acquired=ts, reason=reason,
                ref=ref, cost_assumed=True,
            ))
            released = released + short_proceeds
            assumed = True
            self.warn(
                "missing_history",
                f"Disposed {remaining} {asset} with no matching purchase on record. "
                "Its cost was assumed equal to proceeds (zero PnL) — sync a longer "
                "history window if this looks wrong.",
                asset, ts,
            )
        return released, assumed

    # -- event handlers ---------------------------------------------------

    async def _market_value(self, asset: str, qty: Decimal, ts: int) -> Money:
        if asset == self.fiat:
            fx = await self.oracle.historical_usdt_thb(ts)
            return Money(qty, qty / fx if fx else ZERO)
        return await self.oracle.historical_value(asset, qty, ts)

    async def _consideration(self, got_asset: str, got_qty: Decimal,
                             paid_asset: str, paid_qty: Decimal, ts: int) -> Money:
        """What this exchange was worth, in both currencies.

        A fill already tells us its own price, so whenever either leg is baht or
        tether that side is exact and no candle lookup is needed — the number
        used is the one that actually moved through the account.  Only
        crypto-for-crypto pairs fall back to a historical mark.
        """
        thb: Optional[Decimal] = None
        usdt: Optional[Decimal] = None
        for asset, qty in ((got_asset, got_qty), (paid_asset, paid_qty)):
            if asset == self.fiat:
                thb = qty
            elif asset == "USDT":
                usdt = qty

        fx = await self.oracle.historical_usdt_thb(ts)
        if thb is None and usdt is not None and fx:
            thb = usdt * fx
        if usdt is None and thb is not None and fx and fx > 0:
            usdt = thb / fx
        if thb is None and usdt is None:
            mark = await self._market_value(paid_asset, paid_qty, ts)
            thb, usdt = mark.thb, mark.usdt
        return Money(thb or ZERO, usdt or ZERO)

    async def apply_trade(self, t: Trade) -> None:
        ts = t.time
        if t.side == "BUY":
            got_asset, got_qty = t.base_asset, t.qty
            paid_asset, paid_qty = t.quote_asset, t.effective_quote_qty
        else:
            got_asset, got_qty = t.quote_asset, t.effective_quote_qty
            paid_asset, paid_qty = t.base_asset, t.qty

        if not got_asset or not paid_asset or got_qty <= 0 or paid_qty <= 0:
            self.warn("bad_trade", f"Skipped malformed fill on {t.symbol}.",
                      t.base_asset, ts)
            return

        fee_asset = (t.fee_asset or paid_asset).upper()
        fee = t.fee if t.fee > 0 else ZERO
        third_party_fee = ZERO

        if fee > 0:
            if fee_asset == paid_asset:
                # Commission taken out of what we spent: we simply spent more.
                paid_qty += fee
            elif fee_asset == got_asset:
                # Commission taken out of what arrived: fewer units, same outlay.
                got_qty -= fee
            else:
                third_party_fee = fee

        consideration = await self._consideration(got_asset, got_qty,
                                                  paid_asset, paid_qty, ts)

        # --- release the asset we spent -----------------------------------
        if paid_asset == self.fiat:
            released = consideration
            self.cash -= paid_qty
            assumed_leg = False
        else:
            released, assumed_leg = self.take_lots(
                paid_asset, paid_qty, consideration, ts,
                reason="sell", ref=t.trade_id,
            )

        # --- decide the cost we attach to what we received ----------------
        if paid_asset == self.fiat:
            cost = consideration
        elif self._carries_basis(paid_asset):
            # Funding leg: the baht basis passes straight through into the coin
            # and no FX gain is realised on the stablecoin.
            cost = Money(released.thb, consideration.usdt)
            self._zero_out_funding_leg(paid_asset, ts, t.trade_id)
        else:
            cost = consideration

        # --- commission paid in some third asset (classically BNB) ---------
        if third_party_fee > 0:
            fee_value = await self._market_value(fee_asset, third_party_fee, ts)
            self.take_lots(fee_asset, third_party_fee, fee_value, ts,
                           reason="fee", ref=t.trade_id)
            cost = cost + fee_value
            self.fees_paid = self.fees_paid + fee_value
        elif fee > 0:
            self.fees_paid = self.fees_paid + await self._market_value(
                fee_asset, fee, ts)

        if got_qty <= 0:
            return

        # --- book what we received ----------------------------------------
        if got_asset == self.fiat:
            self.cash += got_qty
        else:
            assumed = assumed_leg and self._carries_basis(paid_asset)
            self.add_lot(got_asset, got_qty, cost, ts, source="trade",
                         ref=t.trade_id, assumed=assumed)

    def _zero_out_funding_leg(self, asset: str, ts: int, ref: str) -> None:
        """Make a carried-basis funding leg realise exactly zero PnL.

        ``take_lots`` already recorded disposals using market proceeds; when the
        basis is carried instead, the proceeds must equal the cost so no phantom
        FX gain appears on a leg that was really just moving money into position.
        These rows are tagged ``funding`` so they stay out of realised PnL and
        out of the win-rate.
        """
        for d in reversed(self.disposals):
            if d.ref != ref or d.asset != asset or d.time != ts:
                break
            d.proceeds = Money(d.cost.thb, d.cost.usdt)
            d.reason = "funding"

    def _match_proceeds_to_cost(self, asset: str, ts: int, ref: str) -> None:
        """Book a non-disposal at its own cost, so the row nets to zero."""
        for d in reversed(self.disposals):
            if d.ref != ref or d.asset != asset or d.time != ts:
                break
            d.proceeds = d.cost

    async def apply_transfer(self, tr: Transfer) -> None:
        ts = tr.time
        value = await self._market_value(tr.asset, tr.amount, ts)

        if tr.kind == "DEPOSIT":
            self.deposits_value = self.deposits_value + value
            if tr.asset == self.fiat:
                self.cash += tr.amount
            else:
                self.add_lot(tr.asset, tr.amount, value, ts, source="deposit",
                             ref=tr.transfer_id, assumed=True)
                self.warn(
                    "deposit_basis",
                    f"{tr.asset} arrived by deposit, so no purchase price exists on "
                    "this exchange. It is costed at the market price on arrival.",
                    tr.asset, ts,
                )
            return

        # Withdrawal: the fee leaves the account too.
        gross = tr.amount + tr.fee
        gross_value = await self._market_value(tr.asset, gross, ts)
        self.withdrawals_value = self.withdrawals_value + gross_value

        if tr.asset == self.fiat:
            self.cash -= gross
            return

        reason = "sell" if self.treat_withdrawal_as_sale else "transfer-out"
        self.take_lots(tr.asset, gross, gross_value, ts, reason=reason,
                       ref=tr.transfer_id)
        if not self.treat_withdrawal_as_sale:
            # Moving coins off-exchange is not a sale: release each lot at its
            # own cost so nothing fictional lands in realised PnL.
            self._match_proceeds_to_cost(tr.asset, ts, tr.transfer_id)

    # -- driver -----------------------------------------------------------

    async def replay(self, events: Iterable[LedgerEvent]) -> None:
        for event in sorted(events, key=lambda e: e.sort_key):
            if event.kind == "trade" and event.trade:
                await self.apply_trade(event.trade)
            elif event.transfer:
                await self.apply_transfer(event.transfer)


# --------------------------------------------------------------------------
# assembling a live snapshot
# --------------------------------------------------------------------------

DUST = Decimal("0.00000001")


def collect_events(store, since: Optional[int] = None) -> list[LedgerEvent]:
    events = [LedgerEvent(t.time, "trade", trade=t) for t in store.trades(since=since)]
    for tr in store.transfers(since=since):
        events.append(LedgerEvent(tr.time,
                                  "deposit" if tr.kind == "DEPOSIT" else "withdrawal",
                                  transfer=tr))
    return sorted(events, key=lambda e: e.sort_key)


async def build_portfolio(store, oracle: PriceOracle, *,
                          method: Optional[str] = None,
                          fx_mode: Optional[str] = None,
                          treat_withdrawal_as_sale: Optional[bool] = None,
                          reconcile: bool = True) -> PortfolioState:
    """Value the account with whichever cost-basis engine is configured."""
    method = method or settings.cost_basis_method
    if method == "simple":
        return await _build_simple(store, oracle)
    return await _build_fifo(
        store, oracle, method=method, fx_mode=fx_mode,
        treat_withdrawal_as_sale=treat_withdrawal_as_sale, reconcile=reconcile,
    )


async def _build_simple(store, oracle: PriceOracle) -> PortfolioState:
    """Average cost, with holdings the exchange cannot explain left uncosted."""
    from .costbasis_simple import build_simple_state
    from .holdings import HoldingsError, load_holdings

    try:
        holdings = load_holdings(settings.holdings_path)
    except HoldingsError as exc:
        # A silently ignored cost file is worse than no cost file: you would
        # think your basis was counted when it was not.
        raise HoldingsError(f"{settings.holdings_path}: {exc}") from exc

    return await build_simple_state(
        trades=store.trades(),
        balances=store.balances(),
        oracle=oracle,
        holdings=holdings,
        transfers=store.transfers(),
        fiat=settings.fiat,
    )


async def _build_fifo(store, oracle: PriceOracle, *,
                      method: Optional[str] = None,
                      fx_mode: Optional[str] = None,
                      treat_withdrawal_as_sale: Optional[bool] = None,
                      reconcile: bool = True) -> PortfolioState:
    """Replay everything, then square the books against live balances."""
    engine = CostBasisEngine(
        oracle,
        method=method or settings.cost_basis_method,
        fx_mode=fx_mode or settings.fx_mode,
        treat_withdrawal_as_sale=(settings.treat_withdrawal_as_sale
                                  if treat_withdrawal_as_sale is None
                                  else treat_withdrawal_as_sale),
        fiat=settings.fiat,
    )
    await engine.replay(collect_events(store))

    balances = {b.asset: b for b in store.balances()}
    state = PortfolioState(
        disposals=engine.disposals,
        cash=engine.cash,
        deposits_value=engine.deposits_value,
        withdrawals_value=engine.withdrawals_value,
        fees_paid=engine.fees_paid,
        warnings=list(engine.warnings),
        fx_rate=oracle.usdt_thb(),
    )

    realised_by_asset: dict[str, Money] = defaultdict(Money)
    for d in engine.disposals:
        if d.counts_as_realised:
            realised_by_asset[d.asset] = realised_by_asset[d.asset] + d.pnl

    assets = set(engine.lots) | set(balances) | set(realised_by_asset)
    assets.discard("")

    for asset in sorted(assets):
        ledger_qty = engine.qty(asset)
        cost = engine.basis(asset)
        lots = [l for l in engine.lots[asset] if l.qty > 0]
        exchange = balances.get(asset)
        qty = ledger_qty

        if asset == settings.fiat:
            # Idle baht is cash, not a position: value it, never PnL it.
            qty = exchange.total if exchange else max(engine.cash, ZERO)
            if qty <= DUST and not realised_by_asset.get(asset):
                continue
            value, source = oracle.value(asset, qty)
            state.positions[asset] = Position(
                asset=asset, qty=qty, cost=value, market_value=value,
                price=oracle.price_pair(asset)[0], realised=Money(),
                lots=[], price_source="cash",
                free=exchange.free if exchange else qty,
                locked=exchange.locked if exchange else ZERO,
            )
            continue

        if reconcile and exchange is not None:
            qty, cost, lots = _reconcile(engine, state, asset, ledger_qty,
                                         exchange.total, cost, lots, oracle)

        if qty <= DUST and realised_by_asset.get(asset, Money()).is_zero:
            continue

        value, source = oracle.value(asset, qty)
        price, _ = oracle.price_pair(asset)
        if qty > DUST and price.thb == ZERO and price.usdt == ZERO:
            state.warnings.append(Warning_(
                "unpriced",
                f"No trading pair found to price {asset}; it is shown at zero.",
                asset,
            ))
        state.positions[asset] = Position(
            asset=asset, qty=qty, cost=cost, market_value=value, price=price,
            realised=realised_by_asset.get(asset, Money()), lots=lots,
            price_source=source,
            cost_assumed=any(l.cost_assumed for l in lots),
            free=exchange.free if exchange else qty,
            locked=exchange.locked if exchange else ZERO,
        )

    return state


def _reconcile(engine: CostBasisEngine, state: PortfolioState, asset: str,
               ledger_qty: Decimal, exchange_qty: Decimal, cost: Money,
               lots: list[Lot], oracle: PriceOracle
               ) -> tuple[Decimal, Money, list[Lot]]:
    """Force the position to match the balance the exchange actually reports.

    Trade history windows expire, airdrops and staking rewards never appear as
    trades, and dust conversions vanish.  Rather than quietly showing a number
    that disagrees with the app, adjust to the real balance and say so.
    """
    import time as _time

    drift = exchange_qty - ledger_qty
    scale = max(abs(exchange_qty), abs(ledger_qty), Decimal("1e-12"))
    if abs(drift) <= max(DUST, scale * Decimal("0.0005")):
        return exchange_qty if exchange_qty > 0 else ledger_qty, cost, lots

    now = int(_time.time() * 1000)
    if drift > 0:
        value, _ = oracle.value(asset, drift)
        extra = Lot(asset=asset, qty=drift, cost=value, acquired=now,
                    source="reconciliation", cost_assumed=True)
        lots = lots + [extra]
        cost = cost + value
        state.warnings.append(Warning_(
            "balance_higher",
            f"Your {asset} balance is {drift} higher than the trades on record "
            "(airdrop, staking reward, or history older than the sync window). "
            "The surplus is costed at today's price.",
            asset, now,
        ))
    else:
        short = -drift
        remaining = short
        trimmed: list[Lot] = []
        for lot in lots:
            if remaining <= 0:
                trimmed.append(lot)
                continue
            take = min(lot.qty, remaining)
            lot.cost = lot.cost - lot.cost.scaled(take, lot.qty)
            lot.qty -= take
            remaining -= take
            if lot.qty > 0:
                trimmed.append(lot)
        lots = trimmed
        cost = Money()
        for lot in lots:
            cost = cost + lot.cost
        state.warnings.append(Warning_(
            "balance_lower",
            f"Your {asset} balance is {short} lower than the trades on record "
            "(likely a transfer or conversion the API did not return). "
            "Lots were trimmed to match the exchange.",
            asset, now,
        ))
    return exchange_qty, cost, lots


# --------------------------------------------------------------------------
# historical equity curve
# --------------------------------------------------------------------------


def _cached_close(store, symbol: str, ts: int) -> Optional[Decimal]:
    hit = store.candle_at_or_before(symbol, "1d", ts)
    return hit[1] if hit else None


def _cached_fx(store, ts: int) -> Optional[Decimal]:
    rate = _cached_close(store, "USDTTHB", ts)
    if rate:
        return rate
    btc_thb, btc_usdt = _cached_close(store, "BTCTHB", ts), _cached_close(store, "BTCUSDT", ts)
    if btc_thb and btc_usdt and btc_usdt > 0:
        return btc_thb / btc_usdt
    return None


def _cached_price_pair(store, oracle: PriceOracle, asset: str, ts: int) -> Money:
    """Both-currency close for ``asset`` on a given day, DB only (no network)."""
    fx = _cached_fx(store, ts)
    if asset == settings.fiat:
        return Money(D(1), D(1) / fx if fx else ZERO)

    thb = usdt = None
    if sym := oracle.pair(asset, "THB"):
        thb = _cached_close(store, sym, ts)
    if sym := oracle.pair(asset, "USDT"):
        usdt = _cached_close(store, sym, ts)
    if thb is None and usdt is None and asset in STABLES:
        usdt = D(1)
    if thb is None and usdt is not None and fx:
        thb = usdt * fx
    if usdt is None and thb is not None and fx and fx > 0:
        usdt = thb / fx
    return Money(thb or ZERO, usdt or ZERO)


async def build_history(store, oracle: PriceOracle, *,
                        method: Optional[str] = None,
                        fx_mode: Optional[str] = None,
                        persist: bool = True) -> list[dict]:
    """Daily equity / cost / realised-PnL curve.

    Uses whichever engine values the live snapshot; a chart drawn by different
    accounting than the headline numbers is worse than no chart.
    """
    if (method or settings.cost_basis_method) == "simple":
        return await _history_simple(store, oracle, persist=persist)
    return await _history_fifo(store, oracle, method=method, fx_mode=fx_mode,
                               persist=persist)


async def _history_simple(store, oracle: PriceOracle, *,
                          persist: bool = True) -> list[dict]:
    """Daily curve under average-cost accounting."""
    import time as _time

    from .costbasis_simple import SimpleCostBasis, opening_quantities
    from .holdings import HoldingsError, load_holdings

    trades = sorted(store.trades(), key=lambda t: (t.time, str(t.trade_id)))
    balances = store.balances()
    if not trades and not balances:
        return []

    try:
        manual = load_holdings(settings.holdings_path)
    except HoldingsError:
        manual = {}                      # the snapshot reports this properly

    fiat = settings.fiat
    engine = SimpleCostBasis(oracle, fiat=fiat)
    opening = opening_quantities(trades, balances)
    pending_outflow: dict[str, Decimal] = {}
    for asset, qty in sorted(opening.items()):
        if asset == fiat:
            # Coins predating the record were genuinely held from the start, so
            # they are seeded. Baht is not: it is the funding rail, and seeding
            # two years of unreported deposits on day one would draw a flat
            # line at today's total and hide every baht that arrived since.
            continue
        if qty > 0:
            engine.open_position(asset, qty, manual.get(asset))
        elif qty < 0:
            # Holdings short of what the fills imply: coins left by a route the
            # API never reported. We cannot know when, so we take them at the
            # first moment the book can cover them — which keeps every later
            # point, and the final one especially, matching the real balance.
            pending_outflow[asset] = -qty

    transfers = sorted(store.transfers(), key=lambda t: t.time)
    first = min([t.time for t in trades] + [t.time for t in transfers if t.time]
                or [int(_time.time() * 1000)])
    start_day = first // DAY_MS
    end_day = int(_time.time() * 1000) // DAY_MS

    idx = tidx = seen = 0
    realised = Money()
    deposits = Money()
    withdrawals = Money()
    out: list[dict] = []

    for day in range(start_day, end_day + 1):
        day_end = (day + 1) * DAY_MS - 1
        while idx < len(trades) and trades[idx].time <= day_end:
            trade = trades[idx]
            deposits = deposits + _fund_fiat(engine, trade, fiat, store, day_end)
            await engine.apply_trade(trade)
            idx += 1
        if idx >= len(trades):
            # Only once every fill is in. Draining earlier would empty the book
            # under a later sale, whose cost would then be clamped and its
            # proceeds booked almost entirely as profit.
            _settle_outflows(engine, pending_outflow)
        while tidx < len(transfers) and transfers[tidx].time <= day_end:
            tr = transfers[tidx]
            price = _cached_price_pair(store, oracle, tr.asset, tr.time)
            value = Money(price.thb * tr.amount, price.usdt * tr.amount)
            if tr.kind == "DEPOSIT":
                deposits = deposits + value
            else:
                withdrawals = withdrawals + value
            tidx += 1

        for d in engine.disposals[seen:]:
            if d.counts_as_realised:
                realised = realised + d.pnl
        seen = len(engine.disposals)

        equity = Money()
        cost = Money()
        holdings: dict[str, str] = {}
        for asset, book in engine.books.items():
            qty = book.qty
            if qty <= DUST:
                continue
            if asset == fiat:
                fx = _cached_fx(store, day_end)
                price = Money(D(1), D(1) / fx if fx else ZERO)
            else:
                price = _cached_price_pair(store, oracle, asset, day_end)
            equity = equity + Money(price.thb * qty, price.usdt * qty)
            cost = cost + book.cost
            holdings[asset] = str(qty)

        key = day_key(day_end)
        net_deposit = deposits - withdrawals
        out.append({
            "day": key,
            "ts": day_end,
            "equity_thb": str(equity.thb), "equity_usdt": str(equity.usdt),
            "cost_thb": str(cost.thb), "cost_usdt": str(cost.usdt),
            "realised_thb": str(realised.thb), "realised_usdt": str(realised.usdt),
            "net_deposit_thb": str(net_deposit.thb),
            "net_deposit_usdt": str(net_deposit.usdt),
            "unrealised_thb": str(equity.thb - cost.thb),
            "unrealised_usdt": str(equity.usdt - cost.usdt),
        })
        if persist:
            store.upsert_equity(key, day_end, equity, cost, realised,
                                net_deposit.thb, {"holdings": holdings})
    return out


def _settle_outflows(engine, pending: dict) -> None:
    """Drain quantity that left by a route the API never reported.

    Taken at cost, so no profit is booked on coins we cannot see the fate of.
    Call this only after the whole record is replayed: the API does not say
    when the coins went, and removing them early would leave a later sale
    short of basis, inflating realised PnL by most of the sale.
    """
    for asset in list(pending):
        book = engine.book(asset)
        take = min(pending[asset], book.qty)
        if take <= 0:
            continue
        from_unknown = min(take, book.unknown_qty)
        book.unknown_qty -= from_unknown
        book.remove_costed(take - from_unknown)
        pending[asset] -= take
        if pending[asset] <= 0:
            del pending[asset]


def _fund_fiat(engine, trade: Trade, fiat: str, store, ts: int) -> Money:
    """Top the baht balance up to cover a purchase, and call it a deposit.

    Binance TH reports no fiat movements at all, so the baht spent across the
    record has to have arrived somehow. Crediting it at the moment it is spent
    keeps the equity curve honest — capital appears as it is deployed — and
    gives the net-deposited line something real to plot.
    """
    needed = ZERO
    if trade.side == "BUY" and trade.quote_asset == fiat:
        needed += trade.quote_qty
    if trade.fee > 0 and trade.fee_asset == fiat:
        needed += trade.fee
    if needed <= 0:
        return Money()

    book = engine.book(fiat)
    short = needed - book.qty
    if short <= 0:
        return Money()

    book.add(short, Money(short, ZERO))
    fx = _cached_fx(store, ts)
    return Money(short, short / fx if fx else ZERO)


async def _history_fifo(store, oracle: PriceOracle, *,
                        method: Optional[str] = None,
                        fx_mode: Optional[str] = None,
                        persist: bool = True) -> list[dict]:
    """Daily equity / cost / realised-PnL curve, replayed from the ledger."""
    events = collect_events(store)
    if not events:
        return []

    engine = CostBasisEngine(
        oracle,
        method=method or settings.cost_basis_method,
        fx_mode=fx_mode or settings.fx_mode,
        treat_withdrawal_as_sale=settings.treat_withdrawal_as_sale,
        fiat=settings.fiat,
    )

    import time as _time
    start_day = events[0].time // DAY_MS
    end_day = int(_time.time() * 1000) // DAY_MS
    idx = 0
    realised = Money()
    deposits = Money()
    withdrawals = Money()
    seen_disposals = 0
    out: list[dict] = []

    for day in range(start_day, end_day + 1):
        day_end = (day + 1) * DAY_MS - 1
        while idx < len(events) and events[idx].time <= day_end:
            event = events[idx]
            if event.kind == "trade" and event.trade:
                await engine.apply_trade(event.trade)
            elif event.transfer:
                await engine.apply_transfer(event.transfer)
            idx += 1

        for d in engine.disposals[seen_disposals:]:
            if d.counts_as_realised:
                realised = realised + d.pnl
        seen_disposals = len(engine.disposals)
        deposits, withdrawals = engine.deposits_value, engine.withdrawals_value

        equity = Money()
        cost = Money()
        holdings: dict[str, str] = {}
        for asset, pool in engine.lots.items():
            qty = sum((l.qty for l in pool), ZERO)
            if qty <= DUST:
                continue
            price = _cached_price_pair(store, oracle, asset, day_end)
            equity = equity + Money(price.thb * qty, price.usdt * qty)
            for lot in pool:
                cost = cost + lot.cost
            holdings[asset] = str(qty)
        if engine.cash > 0:
            fx = _cached_fx(store, day_end)
            equity = equity + Money(engine.cash, engine.cash / fx if fx else ZERO)
            cost = cost + Money(engine.cash, engine.cash / fx if fx else ZERO)
            holdings[settings.fiat] = str(engine.cash)

        key = day_key(day_end)
        net_deposit = deposits - withdrawals
        row = {
            "day": key,
            "ts": day_end,
            "equity_thb": str(equity.thb), "equity_usdt": str(equity.usdt),
            "cost_thb": str(cost.thb), "cost_usdt": str(cost.usdt),
            "realised_thb": str(realised.thb), "realised_usdt": str(realised.usdt),
            "net_deposit_thb": str(net_deposit.thb),
            "net_deposit_usdt": str(net_deposit.usdt),
            "unrealised_thb": str(equity.thb - cost.thb),
            "unrealised_usdt": str(equity.usdt - cost.usdt),
        }
        out.append(row)
        if persist:
            store.upsert_equity(key, day_end, equity, cost, realised,
                                net_deposit.thb, {"holdings": holdings})
    return out
