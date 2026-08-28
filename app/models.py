"""Normalised domain objects.

Everything the exchange returns is squashed into these shapes so the rest of
the app never has to care which API dialect produced them.  All monetary
quantities are :class:`~decimal.Decimal` — never float — because this is
accounting, not physics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Optional

ZERO = Decimal("0")
Side = Literal["BUY", "SELL"]
TransferKind = Literal["DEPOSIT", "WITHDRAWAL"]


def D(value) -> Decimal:
    """Coerce anything sane into a Decimal, defaulting to zero."""
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(slots=True)
class SymbolInfo:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str = "TRADING"
    base_precision: int = 8
    quote_precision: int = 8


@dataclass(slots=True)
class Trade:
    """A single fill."""

    trade_id: str
    symbol: str
    base_asset: str
    quote_asset: str
    side: Side
    price: Decimal
    qty: Decimal          # amount of base_asset
    quote_qty: Decimal    # amount of quote_asset moved
    fee: Decimal
    fee_asset: str
    time: int             # epoch milliseconds
    order_id: str = ""
    is_maker: bool = False

    @property
    def effective_quote_qty(self) -> Decimal:
        """Quote moved, falling back to price*qty when the venue omits it."""
        return self.quote_qty if self.quote_qty > 0 else self.price * self.qty


@dataclass(slots=True)
class Transfer:
    """A deposit or withdrawal — crypto or fiat."""

    transfer_id: str
    kind: TransferKind
    asset: str
    amount: Decimal
    fee: Decimal
    time: int
    status: str = "COMPLETED"
    tx_id: str = ""
    network: str = ""
    address: str = ""
    is_fiat: bool = False
    note: str = ""

    @property
    def net_amount(self) -> Decimal:
        """Amount that actually landed in / left the account."""
        if self.kind == "DEPOSIT":
            return self.amount
        return self.amount + self.fee


@dataclass(slots=True)
class Balance:
    asset: str
    free: Decimal
    locked: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass(slots=True)
class Money:
    """A value expressed simultaneously in both reporting currencies.

    The whole point of this tracker: a Thai user's cost basis lives in baht,
    but half their trades are priced in tether.  Carrying both numbers side by
    side from the moment of the fill means neither view is ever a guess made
    after the fact.
    """

    thb: Decimal = ZERO
    usdt: Decimal = ZERO

    def __add__(self, other: "Money") -> "Money":
        return Money(self.thb + other.thb, self.usdt + other.usdt)

    def __sub__(self, other: "Money") -> "Money":
        return Money(self.thb - other.thb, self.usdt - other.usdt)

    def __mul__(self, k) -> "Money":
        k = D(k)
        return Money(self.thb * k, self.usdt * k)

    def scaled(self, numerator: Decimal, denominator: Decimal) -> "Money":
        """Proportional slice, e.g. 'the part of this lot being sold'."""
        if denominator == 0:
            return Money()
        return Money(
            self.thb * numerator / denominator,
            self.usdt * numerator / denominator,
        )

    def get(self, currency: str) -> Decimal:
        return self.thb if currency.upper() == "THB" else self.usdt

    def as_dict(self) -> dict:
        return {"thb": str(self.thb), "usdt": str(self.usdt)}

    @property
    def is_zero(self) -> bool:
        return self.thb == ZERO and self.usdt == ZERO


@dataclass(slots=True)
class Lot:
    """An open parcel of an asset with the cost actually paid for it."""

    asset: str
    qty: Decimal
    cost: Money            # total cost of the *remaining* qty
    acquired: int          # epoch ms
    source: str = "trade"  # trade | deposit | fee-rebate
    ref: str = ""
    #: True when the cost had to be assumed (e.g. coin arrived by deposit and
    #: was marked at the price on arrival rather than a price you actually paid).
    cost_assumed: bool = False

    @property
    def unit_cost(self) -> Money:
        if self.qty == 0:
            return Money()
        return Money(self.cost.thb / self.qty, self.cost.usdt / self.qty)


@dataclass(slots=True)
class Disposal:
    """A closed parcel — the raw material of realised PnL."""

    asset: str
    qty: Decimal
    proceeds: Money
    cost: Money
    time: int
    acquired: int
    reason: str = "sell"   # sell | withdrawal | fee
    ref: str = ""
    cost_assumed: bool = False

    @property
    def pnl(self) -> Money:
        return self.proceeds - self.cost

    @property
    def holding_days(self) -> float:
        return max(0.0, (self.time - self.acquired) / 86_400_000)

    @property
    def counts_as_realised(self) -> bool:
        # 'transfer-out' is a move between wallets and 'funding' is a stablecoin
        # conversion whose basis was carried onward — neither is a gain or loss.
        # 'unknown-basis' is a sale out of a bag the exchange has no purchase
        # record for: proceeds without a cost are not profit, they are an
        # unknown, and booking them would invent a gain of the entire sale.
        return self.reason not in ("transfer-out", "funding", "unknown-basis")


@dataclass(slots=True)
class Position:
    """Current holding of one asset, fully costed."""

    asset: str
    qty: Decimal = ZERO
    cost: Money = field(default_factory=Money)
    market_value: Money = field(default_factory=Money)
    price: Money = field(default_factory=Money)
    realised: Money = field(default_factory=Money)
    lots: list[Lot] = field(default_factory=list)
    price_source: str = ""
    cost_assumed: bool = False
    free: Decimal = ZERO
    locked: Decimal = ZERO
    #: Quantity the exchange gave us no purchase record for and no manual cost
    #: covers. It is real — you hold it — but its profit is unknowable, so it
    #: is kept out of every cost and PnL figure instead of being guessed at.
    unknown_qty: Decimal = ZERO

    @property
    def costed_qty(self) -> Decimal:
        """The part of the holding whose cost we actually know."""
        return self.qty - self.unknown_qty

    @property
    def costed_value(self) -> Money:
        """Market value of the costed part only."""
        if self.qty == 0 or self.unknown_qty == 0:
            return self.market_value
        return self.market_value.scaled(self.costed_qty, self.qty)

    @property
    def excluded_value(self) -> Money:
        """Market value carrying no basis — reported, never counted as gain."""
        if self.unknown_qty == 0:
            return Money()
        return self.market_value.scaled(self.unknown_qty, self.qty)

    @property
    def basis_unknown(self) -> bool:
        return self.unknown_qty != 0

    @property
    def unrealised(self) -> Money:
        return self.costed_value - self.cost

    @property
    def avg_cost(self) -> Money:
        """Average cost of the costed quantity, not of the whole holding."""
        qty = self.costed_qty
        if qty == 0:
            return Money()
        return Money(self.cost.thb / qty, self.cost.usdt / qty)

    def roi(self, currency: str) -> Optional[Decimal]:
        basis = self.cost.get(currency)
        if basis == 0:
            return None
        return self.unrealised.get(currency) / basis * Decimal(100)
