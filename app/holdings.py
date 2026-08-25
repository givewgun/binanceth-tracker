"""The manual cost file: what you paid for coins the exchange cannot tell us about.

Binance TH returns trade history from a fixed point onward and nothing before
it, so a portfolio held across that boundary has quantity with no purchase on
record.  Rather than invent a cost for it — the old behaviour, which marked
such coins at *today's* price and so reported a confident zero profit — the
tracker asks for the number, and excludes the holding from PnL when it is not
given.

The file is deliberately tiny.  It covers only the unexplained remainder, so a
typical one is three or four entries::

    [BTC]
    qty = 0.14676540        # optional; omit to cover the whole remainder
    cost_thb = 2100000      # total paid, not price per coin

    [SOL]
    cost_usdt = 4500
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from .models import D

#: Keys a holding entry may carry. Anything else is a typo worth shouting about.
_KEYS = {"qty", "cost_thb", "cost_usdt"}


class HoldingsError(ValueError):
    """The cost file exists but cannot be trusted."""


@dataclass(frozen=True, slots=True)
class HoldingCost:
    """What one asset's pre-history quantity cost."""

    asset: str
    qty: Optional[Decimal]          # None = whatever quantity is unexplained
    cost_thb: Optional[Decimal]
    cost_usdt: Optional[Decimal]

    def unit_cost_thb(self, qty: Decimal) -> Optional[Decimal]:
        return self.cost_thb / qty if self.cost_thb is not None and qty else None

    def unit_cost_usdt(self, qty: Decimal) -> Optional[Decimal]:
        return self.cost_usdt / qty if self.cost_usdt is not None and qty else None


def _decimal(asset: str, key: str, raw) -> Decimal:
    try:
        value = D(str(raw))
    except (InvalidOperation, TypeError) as exc:
        raise HoldingsError(f"[{asset}] {key} = {raw!r} is not a number") from exc
    if value < 0:
        raise HoldingsError(f"[{asset}] {key} must not be negative (got {raw!r})")
    return value


def parse_holdings(text: str) -> dict[str, HoldingCost]:
    """Read the file's contents. Raises ``HoldingsError`` on anything doubtful."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise HoldingsError(f"holdings file is not valid TOML: {exc}") from exc

    out: dict[str, HoldingCost] = {}
    for name, entry in raw.items():
        asset = str(name).strip().upper()
        if not isinstance(entry, dict):
            raise HoldingsError(
                f"[{asset}] must be a table, e.g. '[{asset}]' on its own line "
                f"followed by cost_thb = ..."
            )
        unknown = set(entry) - _KEYS
        if unknown:
            raise HoldingsError(
                f"[{asset}] has unknown key(s): {', '.join(sorted(unknown))}. "
                f"Allowed: {', '.join(sorted(_KEYS))}"
            )
        values = {k: _decimal(asset, k, v) for k, v in entry.items()}
        if "cost_thb" not in values and "cost_usdt" not in values:
            raise HoldingsError(
                f"[{asset}] needs cost_thb or cost_usdt — otherwise there is "
                f"nothing to cost it with"
            )
        out[asset] = HoldingCost(
            asset=asset,
            qty=values.get("qty"),
            cost_thb=values.get("cost_thb"),
            cost_usdt=values.get("cost_usdt"),
        )
    return out


def load_holdings(path: Path) -> dict[str, HoldingCost]:
    """Read the file at ``path``; an absent file simply means no manual costs."""
    if not path.exists():
        return {}
    return parse_holdings(path.read_text(encoding="utf-8"))
