"""Endpoint maps and tolerant response parsers for the Binance TH REST API.

Binance TH publishes a *"REST Open API v1.0.0"* — the same white-label spec
Binance ships for its licensed local exchanges.  It authenticates exactly like
binance.com (``X-MBX-APIKEY`` header, HMAC-SHA256 over the query string) but
several endpoints sit under ``/open/v1/`` and wrap their payload in a
``{"code":0,"msg":"","data":{...}}`` envelope instead of returning it bare.

Some deployments additionally expose the classic ``/api/v3/`` surface.  Rather
than betting on one, we describe both dialects and probe at startup.  Parsers
are deliberately forgiving about field names — local exchanges rename things
(``insertTime`` vs ``createTime``, ``list`` vs ``rows``) and a tracker that
falls over on a renamed key is useless.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from .models import Balance, D, SymbolInfo, Trade, Transfer

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def pick(obj: dict, *names: str, default: Any = None) -> Any:
    """First present, non-None value among ``names``."""
    for n in names:
        if isinstance(obj, dict) and obj.get(n) is not None:
            return obj[n]
    return default


def rows(payload: Any, *containers: str) -> list[dict]:
    """Dig a list of records out of whatever wrapper the venue used."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in (*containers, "list", "rows", "data", "items", "result", "records"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
            if isinstance(inner, dict):
                nested = rows(inner, *containers)
                if nested:
                    return nested
    return []


def as_ms(value: Any) -> int:
    """Normalise a timestamp to epoch milliseconds."""
    if value is None:
        return 0
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return 0
    if n > 10**15:      # microseconds
        return n // 1000
    if n < 10**11:      # seconds
        return n * 1000
    return n


_TERMINAL_OK = {
    "1", "6", "success", "successful", "completed", "complete", "done",
    "finished", "confirmed", "credited", "ok",
}


def status_ok(value: Any) -> bool:
    """Whether a deposit/withdrawal status means 'the money actually moved'."""
    if value is None:
        return True
    return str(value).strip().lower() in _TERMINAL_OK


def status_label(value: Any) -> str:
    if value is None:
        return "COMPLETED"
    text = str(value).strip()
    numeric = {
        "0": "PENDING", "1": "COMPLETED", "2": "REJECTED", "3": "PROCESSING",
        "4": "FAILED", "5": "FAILED", "6": "COMPLETED", "7": "CANCELLED",
    }
    return numeric.get(text, text.upper() or "COMPLETED")


def split_symbol(symbol: str, known: dict[str, SymbolInfo],
                 quotes: Iterable[str]) -> tuple[str, str]:
    """Break ``BTCTHB`` into ``('BTC', 'THB')``.

    Uses exchange metadata when available and falls back to a longest-suffix
    match against known quote assets, which is what actually happens the first
    time a brand-new listing shows up in your trade history.
    """
    info = known.get(symbol)
    if info and info.base_asset and info.quote_asset:
        return info.base_asset, info.quote_asset
    if "_" in symbol:                      # some venues use BTC_THB
        base, _, quote = symbol.partition("_")
        return base, quote
    candidates = sorted(set(quotes), key=len, reverse=True)
    for quote in candidates:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return symbol, ""


# --------------------------------------------------------------------------
# dialects
# --------------------------------------------------------------------------


class Dialect:
    """Describes where endpoints live and how to read their answers."""

    name: str = "base"
    #: Endpoints that need no signature, used to sniff which dialect is live.
    probe_path: str = ""

    # -- request builders -------------------------------------------------
    def time(self) -> tuple[str, dict]: raise NotImplementedError
    def symbols(self) -> tuple[str, dict]: raise NotImplementedError
    def prices(self) -> Optional[tuple[str, dict]]: return None
    def klines(self, symbol: str, interval: str, start: Optional[int],
               end: Optional[int], limit: int) -> tuple[str, dict]:
        raise NotImplementedError
    def account(self) -> tuple[str, dict]: raise NotImplementedError
    def my_trades(self, symbol: str, start: Optional[int], end: Optional[int],
                  from_id: Optional[str], limit: int) -> tuple[str, dict]:
        raise NotImplementedError
    def deposits(self, start: Optional[int], end: Optional[int],
                 asset: Optional[str], limit: int) -> tuple[str, dict]:
        raise NotImplementedError
    def withdrawals(self, start: Optional[int], end: Optional[int],
                    asset: Optional[str], limit: int) -> tuple[str, dict]:
        raise NotImplementedError
    def fiat_transfers(self, kind: str, start: Optional[int],
                       end: Optional[int]) -> Optional[tuple[str, dict]]:
        return None

    # -- envelope ---------------------------------------------------------
    def unwrap(self, payload: Any) -> Any:
        return payload

    # -- parsers ----------------------------------------------------------
    def parse_time(self, payload: Any) -> int:
        data = self.unwrap(payload)
        if isinstance(data, dict):
            return as_ms(pick(data, "serverTime", "server_time", "timestamp", "time"))
        return as_ms(data)

    def parse_symbols(self, payload: Any) -> list[SymbolInfo]:
        out: list[SymbolInfo] = []
        for r in rows(self.unwrap(payload), "symbols"):
            sym = pick(r, "symbol", "name", default="")
            if not sym:
                continue
            out.append(SymbolInfo(
                symbol=str(sym).upper(),
                base_asset=str(pick(r, "baseAsset", "base_asset", "base", default="")).upper(),
                quote_asset=str(pick(r, "quoteAsset", "quote_asset", "quote", default="")).upper(),
                status=str(pick(r, "status", "state", default="TRADING")).upper(),
                base_precision=int(pick(r, "baseAssetPrecision", "basePrecision", default=8) or 8),
                quote_precision=int(pick(r, "quotePrecision", "quoteAssetPrecision", default=8) or 8),
            ))
        return out

    def parse_prices(self, payload: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        data = self.unwrap(payload)
        records = rows(data, "ticker", "tickers", "prices")
        if not records and isinstance(data, dict):
            # A bare {"BTCTHB": "2400000", ...} map.
            for k, v in data.items():
                if isinstance(v, (str, int, float)):
                    out[str(k).upper()] = D(v)
            return out
        for r in records:
            sym = pick(r, "symbol", "s", "pair", default="")
            px = pick(r, "price", "lastPrice", "close", "c", "last", "p")
            if sym and px is not None:
                out[str(sym).upper()] = D(px)
        return out

    def parse_klines(self, payload: Any) -> list[list]:
        data = self.unwrap(payload)
        if isinstance(data, dict):
            data = pick(data, "list", "klines", "data", "rows", default=[])
        out: list[list] = []
        for k in data or []:
            if isinstance(k, (list, tuple)) and len(k) >= 5:
                out.append([as_ms(k[0]), D(k[1]), D(k[2]), D(k[3]), D(k[4])])
            elif isinstance(k, dict):
                out.append([
                    as_ms(pick(k, "openTime", "open_time", "t", "time")),
                    D(pick(k, "open", "o")), D(pick(k, "high", "h")),
                    D(pick(k, "low", "l")), D(pick(k, "close", "c")),
                ])
        out.sort(key=lambda r: r[0])
        return out

    def parse_account(self, payload: Any) -> list[Balance]:
        data = self.unwrap(payload)
        records = rows(data, "balances", "accountAssets", "assets", "coins")
        out: list[Balance] = []
        for r in records:
            asset = str(pick(r, "asset", "coin", "currency", "assetName", default="")).upper()
            if not asset:
                continue
            free = D(pick(r, "free", "available", "availableBalance", "freeAmount"))
            locked = D(pick(r, "locked", "frozen", "freeze", "lockedBalance", "hold"))
            if free == 0 and locked == 0:
                total = D(pick(r, "total", "balance", "totalBalance"))
                free = total
            if free == 0 and locked == 0:
                continue
            out.append(Balance(asset=asset, free=free, locked=locked))
        return out

    def parse_trades(self, payload: Any, symbol: str, known: dict[str, SymbolInfo],
                     quotes: Iterable[str]) -> list[Trade]:
        out: list[Trade] = []
        for r in rows(self.unwrap(payload), "trades", "fills"):
            sym = str(pick(r, "symbol", "pair", default=symbol) or symbol).upper()
            base, quote = split_symbol(sym, known, quotes)
            qty = D(pick(r, "qty", "quantity", "executedQty", "amount", "volume"))
            price = D(pick(r, "price", "avgPrice", "dealPrice"))
            quote_qty = D(pick(r, "quoteQty", "quoteQuantity", "cummulativeQuoteQty",
                               "quoteAmount", "total", "dealVolume"))
            if quote_qty == 0:
                quote_qty = price * qty
            if price == 0 and qty != 0:
                price = quote_qty / qty

            is_buyer = pick(r, "isBuyer", "is_buyer", "buyer")
            if is_buyer is None:
                side = str(pick(r, "side", "type", "direction", default="")).upper()
                if side in {"1", "BUY", "BID"}:
                    is_buyer = True
                elif side in {"2", "SELL", "ASK"}:
                    is_buyer = False
                else:
                    is_buyer = True
            is_buyer = bool(is_buyer) and str(is_buyer).lower() not in {"false", "0"}

            trade_id = str(pick(r, "id", "tradeId", "trade_id", "matchId", default="") or "")
            order_id = str(pick(r, "orderId", "order_id", default="") or "")
            if not trade_id:
                trade_id = f"{sym}-{order_id}-{as_ms(pick(r, 'time', 'transactTime', 'createTime'))}"

            out.append(Trade(
                trade_id=trade_id,
                symbol=sym,
                base_asset=base,
                quote_asset=quote,
                side="BUY" if is_buyer else "SELL",
                price=price,
                qty=qty,
                quote_qty=quote_qty,
                fee=D(pick(r, "commission", "fee", "feeAmount", "tradeFee")),
                fee_asset=str(pick(r, "commissionAsset", "feeCoin", "feeAsset",
                                   "feeCurrency", default=quote) or quote).upper(),
                time=as_ms(pick(r, "time", "createTime", "transactTime", "timestamp", "tradeTime")),
                order_id=order_id,
                is_maker=bool(pick(r, "isMaker", "is_maker", "maker", default=False)),
            ))
        return out

    def _parse_transfers(self, payload: Any, kind: str,
                         containers: tuple[str, ...]) -> list[Transfer]:
        out: list[Transfer] = []
        for r in rows(self.unwrap(payload), *containers):
            asset = str(pick(r, "asset", "coin", "currency", "fiatCurrency",
                             default="")).upper()
            amount = D(pick(r, "amount", "quantity", "indicatedAmount", "value"))
            if not asset or amount == 0:
                continue
            raw_status = pick(r, "status", "state", "orderStatus")
            tid = str(pick(r, "id", "txId", "tranId", "orderNo", "withdrawOrderId",
                           "depositId", default="") or "")
            ts = as_ms(pick(r, "insertTime", "createTime", "applyTime", "time",
                            "updateTime", "successTime", "createdAt"))
            if not tid:
                tid = f"{kind}-{asset}-{ts}-{amount}"
            out.append(Transfer(
                transfer_id=tid,
                kind="DEPOSIT" if kind == "DEPOSIT" else "WITHDRAWAL",
                asset=asset,
                amount=amount,
                fee=D(pick(r, "transactionFee", "fee", "totalFee", "withdrawFee")),
                time=ts,
                status="COMPLETED" if status_ok(raw_status) else status_label(raw_status),
                tx_id=str(pick(r, "txId", "txid", "transactionId", default="") or ""),
                network=str(pick(r, "network", "chain", "coinNetwork", default="") or ""),
                address=str(pick(r, "address", "toAddress", default="") or ""),
                is_fiat=asset in {"THB", "USD", "EUR"},
            ))
        return out

    def parse_deposits(self, payload: Any) -> list[Transfer]:
        return self._parse_transfers(payload, "DEPOSIT", ("deposits", "depositList"))

    def parse_withdrawals(self, payload: Any) -> list[Transfer]:
        return self._parse_transfers(payload, "WITHDRAWAL", ("withdraws", "withdrawals",
                                                             "withdrawList"))


class OpenV1Dialect(Dialect):
    """The ``/open/v1`` surface documented at binance.th/api-docs/en/."""

    name = "openv1"
    probe_path = "/open/v1/common/time"

    def unwrap(self, payload: Any) -> Any:
        if isinstance(payload, dict) and "data" in payload and "code" in payload:
            return payload["data"]
        return payload

    def time(self): return "/open/v1/common/time", {}
    def symbols(self): return "/open/v1/common/symbols", {}
    def prices(self): return "/open/v1/market/tickers", {}

    def klines(self, symbol, interval, start, end, limit):
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start: params["startTime"] = start
        if end: params["endTime"] = end
        return "/open/v1/market/klines", params

    def account(self): return "/open/v1/account/spot", {}

    def my_trades(self, symbol, start, end, from_id, limit):
        params = {"symbol": symbol, "limit": limit}
        if start: params["startTime"] = start
        if end: params["endTime"] = end
        if from_id: params["fromId"] = from_id
        return "/open/v1/orders/trades", params

    def deposits(self, start, end, asset, limit):
        params = {"limit": limit}
        if start: params["startTime"] = start
        if end: params["endTime"] = end
        if asset: params["asset"] = asset
        return "/open/v1/deposits", params

    def withdrawals(self, start, end, asset, limit):
        params = {"limit": limit}
        if start: params["startTime"] = start
        if end: params["endTime"] = end
        if asset: params["asset"] = asset
        return "/open/v1/withdraws", params


class ApiV3Dialect(Dialect):
    """The classic binance.com-compatible ``/api/v3`` + ``/sapi/v1`` surface."""

    name = "apiv3"
    probe_path = "/api/v3/time"

    def time(self): return "/api/v3/time", {}
    def symbols(self): return "/api/v3/exchangeInfo", {}
    def prices(self): return "/api/v3/ticker/price", {}

    def klines(self, symbol, interval, start, end, limit):
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start: params["startTime"] = start
        if end: params["endTime"] = end
        return "/api/v3/klines", params

    def account(self): return "/api/v3/account", {}

    def my_trades(self, symbol, start, end, from_id, limit):
        params = {"symbol": symbol, "limit": limit}
        if from_id:
            params["fromId"] = from_id
        else:
            if start: params["startTime"] = start
            if end: params["endTime"] = end
        return "/api/v3/myTrades", params

    def deposits(self, start, end, asset, limit):
        params = {"limit": limit}
        if start: params["startTime"] = start
        if end: params["endTime"] = end
        if asset: params["coin"] = asset
        return "/sapi/v1/capital/deposit/hisrec", params

    def withdrawals(self, start, end, asset, limit):
        params = {"limit": limit}
        if start: params["startTime"] = start
        if end: params["endTime"] = end
        if asset: params["coin"] = asset
        return "/sapi/v1/capital/withdraw/history", params

    def fiat_transfers(self, kind, start, end):
        params = {"transactionType": "0" if kind == "DEPOSIT" else "1", "rows": 500}
        if start: params["beginTime"] = start
        if end: params["endTime"] = end
        return "/sapi/v1/fiat/orders", params


DIALECTS: dict[str, Dialect] = {d.name: d for d in (OpenV1Dialect(), ApiV3Dialect())}

#: Hosts tried in order when no BINANCE_TH_BASE_URL is configured.
BASE_URL_CANDIDATES = (
    "https://api.binance.th",
    "https://www.binance.th",
)
