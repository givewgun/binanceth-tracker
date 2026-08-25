# Simple cost basis for Binance TH

**Status:** approved, not yet implemented
**Date:** 2026-08-25

## Problem

The tracker computes profit and loss by replaying a full ledger: every deposit,
every withdrawal, every fill, matched into FIFO lots across two currencies. That
design assumes the exchange will hand over a complete history. Binance TH does
not, and never will.

Measured against the live API on 2026-08-25:

| Source | What it returns |
| --- | --- |
| `GET /api/v1/userTrades` | Fills from 2026-08-20 onward. `fromId` paging reaches nothing older; `fromId=0` is rejected outright. |
| `GET /api/v1/capital/deposit/history` | Nothing after 2026-04-16. |
| `GET /api/v1/capital/withdraw/history` | Nothing after 2025-01-29. |
| Cost basis / average price | No endpoint. `asset/getUserAsset`, `asset/assetDetail`, `accountSnapshot` all 404. The average cost shown in the Binance app comes from their web-internal API, not the key-authenticated one. |
| Fiat (THB) movements | No endpoint. `fiat/orders` 404s on this host. |

The resulting gap between holdings and explainable history:

```
asset            held    from trades    unexplained
BTC        0.31514259     0.16837719     0.14676540
ETH        5.14991215     5.14994275    -0.00003060
SOL        0.00043600   -45.16925000    45.16968600
HBAR       0.90000000     0.00000000     0.90000000
THB   1114630.05377664  -1274.46835675  1115904.52213339
```

ETH is fully covered. Everything else is partly or wholly unexplainable. A
ledger engine fed a half-ledger does not produce approximate answers — it
produces confident wrong ones. Today it silently costs unexplained quantity at
*today's* price, manufacturing a 0.00 unrealised figure that looks like a
measurement and is not.

## Design

### 1. Average cost replaces ledger replay as the default

A new `simple` cost-basis method. Per asset:

- **Basis** — quantity-weighted average of BUY fills, in THB and USDT, from
  whatever `userTrades` returned. Fees in the fee asset reduce that asset's
  quantity.
- **Realised** — SELL fills, valued against the running average at the time of
  sale. No lot matching.
- **Unrealised** — `(market price − average cost) × quantity held`.

`COST_BASIS_METHOD` gains `simple` (new default). `fifo` and `avg` keep the
existing engine unchanged, so nothing already working is lost.

### 2. Unexplained quantity comes from a file, or is excluded

For each asset, `unexplained = balance − net quantity from synced fills`.

A positive value has two causes, and they are the same problem: coins acquired
before the history begins. Either the coins are still held (BTC: 0.147 sitting
in the account, never bought on record) or they were sold inside the visible
window (SOL: 45.17 sold, never bought on record). Both need a basis, one to
value a holding and one to value a disposal.

A negative value — holdings lower than the fills imply, as with USDT's
−15,623 — is an outflow the API did not report. Reduce the asset's basis
proportionally and record no realised PnL: the coins left, we do not know for
what, and guessing would invent profit.

When `unexplained` is positive, the tracker looks in `holdings.toml`:

```toml
# Cost of coins you held before the exchange's history begins.
# qty is optional: omit it to cover the whole unexplained amount.
[BTC]
qty = 0.14676540
cost_thb = 2100000        # total paid, not price per coin

[HBAR]
cost_thb = 45
```

Resolution order per asset:

1. Quantity explained by synced fills → average-cost basis, included in PnL.
2. Unexplained quantity with a `holdings.toml` entry → that basis, included.
   If the quantity was sold inside the visible window, the entry feeds realised
   PnL; if it is still held, it feeds unrealised.
3. Unexplained quantity with no entry → **excluded from cost, basis, realised,
   and unrealised.** Still held, it counts toward market value and allocation,
   flagged `basis unknown`. Already sold, the disposal is listed with its
   proceeds and no PnL figure.

Rule (3) is the point of the design. An asset with unknown basis reports no
profit and no loss, rather than a fabricated zero.

### 3. Drop the history-window machinery

`HISTORY_START`, `Store.purge_before`, and the sync cutoff are removed. They
solved the wrong problem: the history is not too long, it is too short.

Trade sync stops walking time windows. It calls `userTrades` with no time
parameters (the endpoint returns everything it holds), then pages forward by
`fromId` on later syncs. This also removes the 7-day-window problem entirely —
no windows, no `-4088`.

If a bare call returns exactly `limit` rows, log a warning that history may be
truncated for that symbol.

### 4. Reporting

The dashboard and CLI summary gain an explicit line for excluded value, e.g.
`basis unknown: 809,465 THB across BTC, SOL, HBAR`. Total PnL percentages are
computed against *included* cost only, so the denominator matches the numerator.

## Components

| Unit | Responsibility |
| --- | --- |
| `app/holdings.py` (new) | Parse and validate `holdings.toml`. Pure; no I/O beyond one read. |
| `app/costbasis_simple.py` (new) | Average-cost engine. Input: trades, balances, manual entries, price oracle. Output: `PortfolioState`. No knowledge of sync or HTTP. |
| `app/portfolio.py` | Dispatch on `COST_BASIS_METHOD` to the simple or FIFO engine. Existing FIFO code untouched. |
| `app/sync.py` | Lose the cutoff and the window walk for trades. Everything else unchanged. |
| `app/config.py` | Lose `HISTORY_START`. Gain `HOLDINGS_FILE` (default `holdings.toml`). |
| `app/store.py` | Lose `purge_before`. |

The simple engine emits the same `PortfolioState` the FIFO engine does, so the
API layer, dashboard, and history chart need no changes beyond the new
excluded-value field.

## Error handling

- **Malformed `holdings.toml`** — fail loudly at startup with the offending key
  and line. A silently ignored cost file is worse than no cost file.
- **Manual `qty` exceeding unexplained quantity** — clamp to the unexplained
  amount and warn. Do not invent holdings.
- **Missing file** — not an error. Every unexplained holding is simply excluded.
- **Rejected trade query** — already fixed: warn per symbol, and raise if every
  symbol fails. Never demote to debug.

## Testing

- Average-cost arithmetic: buys, partial sells, fee-in-base, fee-in-quote,
  sell-everything-then-rebuy.
- Unexplained-quantity resolution across all three branches, including the
  clamp when manual `qty` is too large.
- Positive unexplained quantity in both forms: still-held (BTC-shaped) and
  sold-inside-the-window (SOL-shaped).
- Negative unexplained quantity (USDT-shaped): basis shrinks proportionally,
  realised PnL stays untouched.
- Excluded value never leaks into cost, basis, realised, or unrealised.
- `holdings.toml` parsing: valid, absent, malformed, unknown asset.
- Existing FIFO tests continue to pass unchanged under `COST_BASIS_METHOD=fifo`.
- Integration test asserts a `simple`-mode sync of the mock exchange produces
  the expected basis and excluded set.

## Out of scope

- Recovering pre-2026-08-20 history by any means. It is not available.
- Importing Binance's web-internal API or CSV statement exports. If the user
  later wants this, it becomes a source for `holdings.toml`, not a new sync path.
- Changing the FIFO engine's behaviour.
