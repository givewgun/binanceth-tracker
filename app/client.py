"""Authenticated HTTP client for the Binance TH REST API.

Handles the parts that are easy to get subtly wrong: HMAC signing over the
*exact* query string that gets sent, clock drift against the exchange, weight
limits, and the retry/backoff dance around 429/418/5xx.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from .config import settings
from .dialects import BASE_URL_CANDIDATES, DIALECTS, Dialect

log = logging.getLogger("binanceth.client")


class BinanceTHError(RuntimeError):
    """An error the exchange reported, or a transport failure we gave up on."""

    def __init__(self, message: str, *, status: int = 0, code: int = 0,
                 path: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.path = path

    @property
    def is_auth_error(self) -> bool:
        return self.status in (401, 403) or self.code in (-2014, -2015, -1022, -1099)

    @property
    def is_permission_error(self) -> bool:
        return self.code in (-2015, -1002)

    @property
    def is_unsupported(self) -> bool:
        """Endpoint simply is not there on this deployment."""
        return self.status in (404, 405) or self.code in (-1121, -1100)


class RateLimiter:
    """Simple async token bucket, refilled continuously."""

    def __init__(self, rate_per_second: float = 8.0, burst: int = 16):
        self.rate = rate_per_second
        self.capacity = burst
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, weight: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= weight:
                    self._tokens -= weight
                    return
                await asyncio.sleep((weight - self._tokens) / self.rate)


class BinanceTHClient:
    MAX_ATTEMPTS = 5

    def __init__(self, api_key: str = "", api_secret: str = "",
                 base_url: str = "", dialect: str = "",
                 recv_window: int = 10_000, timeout: float = 20.0):
        self.api_key = api_key or settings.api_key
        self.api_secret = api_secret or settings.api_secret
        self.base_url = (base_url or settings.base_url).rstrip("/")
        self.recv_window = recv_window or settings.recv_window
        self._dialect: Optional[Dialect] = DIALECTS.get(dialect or settings.dialect)
        self._time_offset = 0
        self._limiter = RateLimiter()
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": "binanceth-tracker/1.0", "Accept": "application/json"},
            follow_redirects=True,
        )
        self._ready = False
        self._detect_lock = asyncio.Lock()

    # -- lifecycle --------------------------------------------------------

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "BinanceTHClient":
        await self.ensure_ready()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    @property
    def dialect(self) -> Dialect:
        if self._dialect is None:
            raise BinanceTHError("API dialect not detected yet; call ensure_ready()")
        return self._dialect

    @property
    def dialect_name(self) -> str:
        return self._dialect.name if self._dialect else "(undetected)"

    async def ensure_ready(self) -> None:
        if self._ready:
            return
        async with self._detect_lock:
            if self._ready:
                return
            await self._detect()
            await self.sync_time()
            self._ready = True

    async def _detect(self) -> None:
        """Find a live (base_url, dialect) pair by probing public endpoints."""
        bases = [self.base_url] if self.base_url else list(BASE_URL_CANDIDATES)
        dialects = [self._dialect] if self._dialect else list(DIALECTS.values())
        errors: list[str] = []
        for base in bases:
            for dialect in dialects:
                path, params = dialect.time()
                try:
                    payload = await self._raw_get(base + path, params, signed=False,
                                                  attempts=2)
                except Exception as exc:                      # noqa: BLE001
                    errors.append(f"{base}{path}: {exc}")
                    continue
                if dialect.parse_time(payload) > 0:
                    self.base_url, self._dialect = base, dialect
                    log.info("Binance TH reachable at %s using %s dialect",
                             base, dialect.name)
                    return
                errors.append(f"{base}{path}: no serverTime in response")
        raise BinanceTHError(
            "Could not reach the Binance TH API. Tried:\n  " + "\n  ".join(errors)
            + "\nSet BINANCE_TH_BASE_URL / BINANCE_TH_DIALECT in .env to override."
        )

    async def sync_time(self) -> int:
        """Align our clock with the exchange's; drift causes -1021 rejects."""
        path, params = self.dialect.time()
        local_before = int(time.time() * 1000)
        payload = await self._raw_get(self.base_url + path, params, signed=False)
        local_after = int(time.time() * 1000)
        server = self.dialect.parse_time(payload)
        if server:
            self._time_offset = server - (local_before + local_after) // 2
        return self._time_offset

    def _timestamp(self) -> int:
        return int(time.time() * 1000) + self._time_offset

    # -- signing ----------------------------------------------------------

    def _sign(self, params: dict) -> str:
        """HMAC-SHA256 over the urlencoded query string, exactly as sent."""
        query = urlencode(params, doseq=True)
        signature = hmac.new(
            self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"{query}&signature={signature}"

    # -- transport --------------------------------------------------------

    async def _raw_get(self, url: str, params: dict, *, signed: bool,
                       attempts: int = 0) -> Any:
        attempts = attempts or self.MAX_ATTEMPTS
        last_exc: Optional[Exception] = None

        for attempt in range(attempts):
            await self._limiter.acquire()
            headers = {}
            request_url = url
            if signed:
                if not self.api_key or not self.api_secret:
                    raise BinanceTHError(
                        "API key and secret are required for this endpoint. "
                        "Put them in .env (see .env.example)."
                    )
                payload = {k: v for k, v in params.items() if v is not None and v != ""}
                payload["timestamp"] = self._timestamp()
                payload["recvWindow"] = self.recv_window
                request_url = f"{url}?{self._sign(payload)}"
                headers["X-MBX-APIKEY"] = self.api_key
            elif params:
                clean = {k: v for k, v in params.items() if v is not None and v != ""}
                if clean:
                    request_url = f"{url}?{urlencode(clean, doseq=True)}"

            try:
                response = await self._http.get(request_url, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                await self._backoff(attempt)
                continue

            if response.status_code in (429, 418):
                retry_after = float(response.headers.get("Retry-After", 0) or 0)
                await asyncio.sleep(retry_after or min(2 ** attempt, 30))
                last_exc = BinanceTHError("rate limited", status=response.status_code)
                continue

            if response.status_code >= 500:
                last_exc = BinanceTHError(
                    f"upstream {response.status_code}", status=response.status_code
                )
                await self._backoff(attempt)
                continue

            try:
                payload = response.json()
            except ValueError:
                snippet = response.text[:200].replace("\n", " ")
                raise BinanceTHError(
                    f"Non-JSON response ({response.status_code}) from {url}: {snippet}",
                    status=response.status_code, path=url,
                ) from None

            code, message = self._error_of(payload, response.status_code)
            if code is not None:
                err = BinanceTHError(message, status=response.status_code,
                                     code=code, path=url)
                # A stale clock is worth exactly one automatic retry.
                if code == -1021 and attempt == 0:
                    await self.sync_time()
                    last_exc = err
                    continue
                raise err

            return payload

        raise BinanceTHError(
            f"Request to {url} failed after {attempts} attempts: {last_exc}"
        ) from last_exc

    @staticmethod
    def _error_of(payload: Any, status: int) -> tuple[Optional[int], str]:
        """Return ``(code, message)`` when the payload is an error, else (None, '')."""
        if isinstance(payload, dict):
            code = payload.get("code")
            msg = str(payload.get("msg") or payload.get("message") or "")
            if code is not None:
                try:
                    code_int = int(code)
                except (TypeError, ValueError):
                    code_int = -1
                # code 0 / 200 mean success in the Open API envelope.
                if code_int not in (0, 200):
                    return code_int, f"Binance TH error {code_int}: {msg or 'unknown'}"
                return None, ""
        if status >= 400:
            return status, f"HTTP {status}"
        return None, ""

    @staticmethod
    async def _backoff(attempt: int) -> None:
        await asyncio.sleep(min(2 ** attempt, 20) * (0.5 + 0.5))

    # -- public API -------------------------------------------------------

    async def get(self, path: str, params: Optional[dict] = None,
                  signed: bool = False) -> Any:
        await self.ensure_ready()
        return await self._raw_get(self.base_url + path, params or {}, signed=signed)

    async def server_time(self) -> int:
        await self.ensure_ready()
        path, params = self.dialect.time()
        return self.dialect.parse_time(await self.get(path, params))

    async def exchange_symbols(self):
        await self.ensure_ready()
        path, params = self.dialect.symbols()
        return self.dialect.parse_symbols(await self.get(path, params))

    async def ticker_prices(self) -> dict:
        await self.ensure_ready()
        spec = self.dialect.prices()
        if spec is None:
            return {}
        path, params = spec
        try:
            return self.dialect.parse_prices(await self.get(path, params))
        except BinanceTHError as exc:
            if exc.is_unsupported:
                log.debug("bulk ticker endpoint unavailable: %s", exc)
                return {}
            raise

    async def klines(self, symbol: str, interval: str = "1h",
                     start: Optional[int] = None, end: Optional[int] = None,
                     limit: int = 500) -> list[list]:
        await self.ensure_ready()
        path, params = self.dialect.klines(symbol, interval, start, end, limit)
        return self.dialect.parse_klines(await self.get(path, params))

    async def balances(self):
        await self.ensure_ready()
        path, params = self.dialect.account()
        return self.dialect.parse_account(await self.get(path, params, signed=True))

    async def my_trades(self, symbol: str, start: Optional[int] = None,
                        end: Optional[int] = None, from_id: Optional[str] = None,
                        limit: int = 500, known=None, quotes=()):
        await self.ensure_ready()
        path, params = self.dialect.my_trades(symbol, start, end, from_id, limit)
        payload = await self.get(path, params, signed=True)
        return self.dialect.parse_trades(payload, symbol, known or {}, quotes)

    async def deposits(self, start: Optional[int] = None, end: Optional[int] = None,
                       asset: Optional[str] = None, limit: int = 500):
        await self.ensure_ready()
        path, params = self.dialect.deposits(start, end, asset, limit)
        return self.dialect.parse_deposits(await self.get(path, params, signed=True))

    async def withdrawals(self, start: Optional[int] = None, end: Optional[int] = None,
                          asset: Optional[str] = None, limit: int = 500):
        await self.ensure_ready()
        path, params = self.dialect.withdrawals(start, end, asset, limit)
        return self.dialect.parse_withdrawals(await self.get(path, params, signed=True))

    async def fiat_transfers(self, kind: str, start: Optional[int] = None,
                             end: Optional[int] = None):
        await self.ensure_ready()
        spec = self.dialect.fiat_transfers(kind, start, end)
        if spec is None:
            return []
        path, params = spec
        try:
            payload = await self.get(path, params, signed=True)
        except BinanceTHError as exc:
            if exc.is_unsupported or exc.is_permission_error:
                return []
            raise
        parse = (self.dialect.parse_deposits if kind == "DEPOSIT"
                 else self.dialect.parse_withdrawals)
        out = parse(payload)
        for t in out:
            t.is_fiat = True
        return out
