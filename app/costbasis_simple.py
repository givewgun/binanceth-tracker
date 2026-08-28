"""Average-cost accounting for exchanges that will not give you a full ledger.

The FIFO engine in :mod:`app.portfolio` replays deposits, withdrawals and fills
into matched lots.  That is the right model when the exchange returns your
complete history.  Binance TH does not: trade history begins at a fixed point,
transfers stop months earlier, and no endpoint reports what you paid.  Fed half
a ledger, a lot-matching engine does not degrade gracefully — it costs whatever
it cannot explain at today's price and reports a confident zero profit.

This engine takes the opposite position.  It keeps one running average cost per
asset, and where the record genuinely runs out it says so:

* Quantity you hold that no fill accounts for is **unknown basis**.  You can
  supply the cost in ``holdings.toml``; otherwise it counts toward market value
  and allocation, and toward nothing else.  No cost, no profit, no loss.
* Selling into that pre-history bag realises nothing, for the same reason.
  Proceeds without a cost are not a gain, they are an unknown.
* Holding *less* than the fills imply means coins left by a route the API never
  reported.  The basis shrinks with the quantity and no gain is booked.

Coins with a known cost — bought, or declared in ``holdings.toml`` — join the
running average like any other.  Only the uncosted bag jumps the queue on a
sale: it has no average to join, and selling it before costed coins keeps a
knowable gain from being replaced by an unknown one.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional

from .holdings import HoldingCost
from .models import ZERO, Balance, D, Disposal, Money, Position, Trade
from .pricing import PriceOracle

log = logging.getLogger("binanceth.costbasis")

#: Reasons whose disposals never count toward realised PnL.
UNKNOWN_BASIS = "unknown-basis"


@dataclass
class _Book:
    """One asset's running position."""

    asset: str
    costed_qty: Decimal = ZERO
    cost: Money = field(default_factory=Money)
    #: Held (or once held) but never bought on record, and not manually costed.
    unknown_qty: Decimal = ZERO

    @property
    def qty(self) -> Decimal:
        return self.costed_qty + self.unknown_qty

    @property
    def unit_cost(self) -> Money:
        if self.costed_qty == 0:
            return Money()
        return Money(self.cost.thb / self.costed_qty,
                     self.cost.usdt / self.costed_qty)

    def add(self, qty: Decimal, cost: Money) -> None:
        self.costed_qty += qty
        self.cost = self.cost + cost

    def remove_costed(self, qty: Decimal) -> Money:
        """Drop ``qty`` from the costed pile; returns the cost that left with it."""
        if qty <= 0 or self.costed_qty <= 0:
            return Money()
        qty = min(qty, self.costed_qty)
        gone = self.cost.scaled(qty, self.costed_qty)
        self.costed_qty -= qty
        self.cost = self.cost - gone
        return gone


class SimpleCostBasis:
    """Running average cost per asset, with an explicit unknown-basis bucket."""

    def __init__(self, oracle: PriceOracle, fiat: str = "THB"):
        self.oracle = oracle
        self.fiat = fiat.upper()
        self.books: dict[str, _Book] = {}
        self.disposals: list[Disposal] = []
        self.fees_paid = Money()
        self.warnings: list = []

    # -- plumbing ---------------------------------------------------------

    def book(self, asset: str) -> _Book:
        if asset not in self.books:
            self.books[asset] = _Book(asset=asset)
        return self.books[asset]

    def warn(self, code: str, message: str, asset: str = "", time: int = 0) -> None:
        from .portfolio import Warning_
        self.warnings.append(Warning_(code=code, message=message, asset=asset,
                                      time=time))

    async def _value(self, asset: str, qty: Decimal, ts: int) -> Money:
        """What ``qty`` of ``asset`` was worth, in both currencies, at ``ts``."""
        if qty == 0:
            return Money()
        return await self.oracle.historical_value(asset, qty, ts)

    # -- opening positions ------------------------------------------------

    def open_position(self, asset: str, qty: Decimal,
                      manual: Optional[HoldingCost]) -> None:
        """Seed the quantity that predates the exchange's trade history.

        With a manual cost it enters the costed pile like any purchase; without
        one it goes to the unknown bucket, where it can be sold or held but
        never counted as gain.
        """
        if qty <= 0:
            return
        book = self.book(asset)
        if manual is None:
            book.unknown_qty += qty
            return

        covered = qty
        if manual.qty is not None and manual.qty != qty:
            if manual.qty > qty:
                self.warn(
                    "manual_qty_clamped",
                    f"holdings.toml declares {manual.qty:f} {asset} but only "
                    f"{qty:f} is unaccounted for; the cost was scaled to match.",
                    asset=asset,
                )
            else:
                self.warn(
                    "manual_qty_partial",
                    f"holdings.toml covers {manual.qty:f} of {qty:f} unaccounted "
                    f"{asset}; the rest carries no basis.",
                    asset=asset,
                )
            covered = min(manual.qty, qty)

        # Costs in the file are totals for `manual.qty`, so scale to what they
        # actually cover.
        scale = covered / manual.qty if manual.qty else D(1)
        thb = (manual.cost_thb or ZERO) * scale
        usdt = (manual.cost_usdt or ZERO) * scale
        if manual.cost_thb is None or manual.cost_usdt is None:
            fx = self.oracle.usdt_thb() or ZERO
            if manual.cost_thb is None and fx:
                thb = usdt * fx
            if manual.cost_usdt is None and fx:
                usdt = thb / fx

        book.add(covered, Money(thb, usdt))
        if covered < qty:
            book.unknown_qty += qty - covered

    # -- the replay -------------------------------------------------------

    async def apply_trade(self, t: Trade) -> None:
        consideration = await self._value(t.quote_asset, t.quote_qty, t.time)
        if t.side == "BUY":
            self.book(t.base_asset).add(t.qty, consideration)
            self._spend(t.quote_asset, t.quote_qty, t.time, t.trade_id)
        else:
            self._dispose(t.base_asset, t.qty, consideration, t.time, t.trade_id)
            self.book(t.quote_asset).add(t.quote_qty, consideration)
        if t.fee > 0 and t.fee_asset:
            await self._charge_fee(t.fee_asset, t.fee, t.time,
                                   acquired=t.base_asset if t.side == "BUY"
                                   else t.quote_asset)

    def _spend(self, asset: str, qty: Decimal, ts: int, ref: str) -> None:
        """Hand over a funding asset at its own cost — never a gain or a loss."""
        book = self.book(asset)
        from_unknown = min(qty, book.unknown_qty)
        book.unknown_qty -= from_unknown
        book.remove_costed(qty - from_unknown)

    def _dispose(self, asset: str, qty: Decimal, proceeds: Money, ts: int,
                 ref: str) -> None:
        book = self.book(asset)
        # Pre-history coins are the older ones, so they go first.
        from_unknown = min(qty, book.unknown_qty)
        if from_unknown > 0:
            book.unknown_qty -= from_unknown
            self.disposals.append(Disposal(
                asset=asset, qty=from_unknown,
                proceeds=proceeds.scaled(from_unknown, qty),
                cost=Money(), time=ts, acquired=0, reason=UNKNOWN_BASIS,
                ref=ref, cost_assumed=True,
            ))
            self.warn(
                "basis_unknown",
                f"Sold {from_unknown:f} {asset} that no purchase on record "
                f"accounts for. The proceeds are shown but no profit is "
                f"claimed — add {asset} to holdings.toml to value it.",
                asset=asset, time=ts,
            )

        remainder = qty - from_unknown
        if remainder <= 0:
            return
        cost = book.remove_costed(remainder)
        self.disposals.append(Disposal(
            asset=asset, qty=remainder, proceeds=proceeds.scaled(remainder, qty),
            cost=cost, time=ts, acquired=0, reason="sell", ref=ref,
        ))

    async def _charge_fee(self, asset: str, qty: Decimal, ts: int,
                          acquired: str = "") -> None:
        """Charge a commission.

        A fee taken in the asset you just received is part of what that asset
        cost: you paid the full consideration and got less back, so quantity
        falls and the cost stays put, raising the average.  A fee in any other
        asset is simply that asset leaving, at its own cost.
        """
        self.fees_paid = self.fees_paid + await self._value(asset, qty, ts)
        book = self.book(asset)
        if asset == acquired:
            taken = min(qty, book.costed_qty)
            book.costed_qty -= taken
            book.unknown_qty = max(ZERO, book.unknown_qty - (qty - taken))
            return
        self._spend(asset, qty, ts, "fee")

    async def replay(self, trades: Iterable[Trade]) -> None:
        for t in sorted(trades, key=lambda x: (x.time, str(x.trade_id))):
            await self.apply_trade(t)


# ---------------------------------------------------------------------------


def opening_quantities(trades: Iterable[Trade], balances: Iterable[Balance],
                       transfers: Iterable = ()) -> dict[str, Decimal]:
    """Quantity held before the trade history begins, per asset.

    The difference between what you hold and what the fills — and any
    *reported* transfer — add up to. Positive means coins arrived by some
    route the API doesn't return at all (or were sold out of a bag that did);
    negative means coins left the same way. Reported deposits and withdrawals
    are netted out here too, even though they don't predate the record: they
    still aren't in ``trades``, and leaving them in this gap would double-count
    them once :func:`app.portfolio._history_simple` (or
    :func:`build_simple_state` below) also applies them on their own date.
    """
    net: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for t in trades:
        signed = t.qty if t.side == "BUY" else -t.qty
        net[t.base_asset] += signed
        net[t.quote_asset] += -t.quote_qty if t.side == "BUY" else t.quote_qty
        if t.fee > 0 and t.fee_asset:
            net[t.fee_asset] -= t.fee
    for tr in transfers:
        net[tr.asset] += tr.amount if tr.kind == "DEPOSIT" else -(tr.amount + tr.fee)

    held = {b.asset: b.free + b.locked for b in balances}
    assets = set(net) | set(held)
    assets.discard("")
    return {a: held.get(a, ZERO) - net.get(a, ZERO) for a in assets}


def apply_transfer(engine: "SimpleCostBasis", tr, value: Money, fiat: str) -> None:
    """Move a reported deposit or withdrawal into or out of the book, dated to
    when it actually happened.

    Without this, a transfer's quantity only shows up as a gap between the
    trade replay and the current balance — and that gap gets attributed to
    the first or last day of the whole record (see ``opening_quantities`` and
    ``_settle_outflows`` in :mod:`app.portfolio`), regardless of when the
    transfer's own timestamp says it really happened.
    """
    if tr.asset.upper() == fiat.upper():
        return                            # cash is cash; carries no basis
    book = engine.book(tr.asset)
    if tr.kind == "DEPOSIT":
        book.add(tr.amount, value)
        engine.warn(
            "deposit_basis",
            f"{tr.asset} arrived by deposit, so no purchase price exists on "
            f"this exchange. It is costed at the market price on arrival.",
            tr.asset, tr.time,
        )
        return
    gross = tr.amount + tr.fee
    from_unknown = min(gross, book.unknown_qty)
    book.unknown_qty -= from_unknown
    book.remove_costed(gross - from_unknown)
    engine.warn(
        "withdrawal_basis",
        f"{gross:f} {tr.asset} left by withdrawal. The cost basis was "
        f"reduced to match and no profit was booked.",
        tr.asset, tr.time,
    )


async def build_simple_state(*, trades: Iterable[Trade],
                             balances: Iterable[Balance],
                             oracle: PriceOracle,
                             holdings: Optional[dict[str, HoldingCost]] = None,
                             transfers: Optional[Iterable] = None,
                             fiat: str = "THB"):
    """Average-cost portfolio state from fills, balances and manual costs."""
    from .portfolio import PortfolioState

    trades = list(trades)
    balances = list(balances)
    transfers = list(transfers or ())
    holdings = holdings or {}
    engine = SimpleCostBasis(oracle, fiat=fiat)

    opening = opening_quantities(trades, balances, transfers)
    for asset in sorted(opening):
        if asset.upper() == fiat:
            continue                     # cash is cash; it carries no basis
        engine.open_position(asset, opening[asset], holdings.get(asset))

    await engine.replay(trades)

    state = PortfolioState(
        disposals=engine.disposals,
        fees_paid=engine.fees_paid,
        fx_rate=oracle.usdt_thb(),
    )

    # Reported transfers, plus the baht the venue never reports at all: the
    # account cannot have spent more fiat than it held without funding from
    # somewhere, and leaving that out makes "net deposited" — and every total
    # return computed against it — meaningless. Applied to the book here too,
    # before reconciliation, so a deposit or withdrawal gets its own date's
    # cost instead of being folded into `opening`'s pre-history guess.
    for transfer in transfers:
        # Valued when it happened: a 2024 deposit is worth its 2024 baht, not
        # what the same coins would fetch today.
        value = await oracle.historical_value(transfer.asset, transfer.amount,
                                              transfer.time)
        apply_transfer(engine, transfer, value, fiat)
        if transfer.kind == "DEPOSIT":
            state.deposits_value = state.deposits_value + value
        else:
            state.withdrawals_value = state.withdrawals_value + value

    held = {b.asset: b for b in balances}
    for asset in sorted(set(engine.books) | set(held)):
        if not asset:
            continue
        book = engine.book(asset)
        balance = held.get(asset)
        qty = (balance.free + balance.locked) if balance else book.qty
        _reconcile(engine, book, qty, asset, fiat)

        price, source = oracle.price_pair(asset)
        position = Position(
            asset=asset,
            qty=book.qty,
            cost=book.cost,
            market_value=Money(price.thb * book.qty, price.usdt * book.qty),
            price=price,
            price_source=source,
            free=balance.free if balance else book.qty,
            locked=balance.locked if balance else ZERO,
            unknown_qty=book.unknown_qty,
            cost_assumed=book.unknown_qty != 0,
        )
        if position.qty == 0 and position.cost.is_zero:
            continue
        state.positions[asset] = position

    # Assigned last: reconciliation above raises warnings of its own.
    state.warnings = list(engine.warnings)

    realised_by_asset: dict[str, Money] = defaultdict(Money)
    for d in engine.disposals:
        if d.counts_as_realised:
            realised_by_asset[d.asset] = realised_by_asset[d.asset] + d.pnl
    for asset, pnl in realised_by_asset.items():
        if asset in state.positions:
            state.positions[asset].realised = pnl

    state.cash = state.positions[fiat].qty if fiat in state.positions else ZERO

    # The baht the venue never reports at all still has to have come from
    # somewhere, and leaving it out makes "net deposited" — and every total
    # return computed against it — meaningless.
    fx = oracle.usdt_thb() or ZERO
    unreported = opening.get(fiat, ZERO)
    if unreported > 0:
        state.deposits_value = state.deposits_value + Money(
            unreported, unreported / fx if fx else ZERO)

    return state


def _reconcile(engine: SimpleCostBasis, book: _Book, balance: Decimal,
               asset: str, fiat: str) -> None:
    """Square the replay against the exchange's own balance."""
    if asset == fiat:
        # Baht is not an investment with a basis; it is worth what it says.
        fx = engine.oracle.usdt_thb() or ZERO
        book.costed_qty = balance
        book.unknown_qty = ZERO
        book.cost = Money(balance, balance / fx if fx else ZERO)
        return

    drift = balance - book.qty
    if drift == 0:
        return

    if drift > 0:
        # More on hand than the fills explain. Opening positions already
        # covered the usual case; anything left is an airdrop, a reward, or
        # history we never saw. It gets no basis.
        book.unknown_qty += drift
        engine.warn(
            "unexplained_balance",
            f"You hold {drift:f} more {asset} than the trades on record "
            f"account for (airdrop, reward, or history the API does not "
            f"return). It is shown at market value with no cost basis.",
            asset=asset,
        )
        return

    # Less on hand: coins left by a route the API never reported. Shrink the
    # position to match, taking cost with it, and book no gain.
    missing = -drift
    from_unknown = min(missing, book.unknown_qty)
    book.unknown_qty -= from_unknown
    book.remove_costed(missing - from_unknown)
    engine.warn(
        "untracked_outflow",
        f"Your {asset} balance is {missing:f} lower than the trades on record "
        f"imply — a transfer or conversion the API did not return. The cost "
        f"basis was reduced to match and no profit was booked.",
        asset=asset,
    )
