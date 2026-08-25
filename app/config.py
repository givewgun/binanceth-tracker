"""Runtime configuration, loaded from environment / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so the app has no extra dependency for it."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Real environment variables always win over the file.
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


@dataclass
class Settings:
    api_key: str = field(default_factory=lambda: _str("BINANCE_TH_API_KEY"))
    api_secret: str = field(default_factory=lambda: _str("BINANCE_TH_API_SECRET"))
    base_url: str = field(default_factory=lambda: _str("BINANCE_TH_BASE_URL"))
    dialect: str = field(default_factory=lambda: _str("BINANCE_TH_DIALECT"))
    recv_window: int = field(default_factory=lambda: _int("BINANCE_TH_RECV_WINDOW", 10000))

    base_currency: str = field(default_factory=lambda: _str("BASE_CURRENCY", "THB").upper())
    cost_basis_method: str = field(
        default_factory=lambda: _str("COST_BASIS_METHOD", "fifo").lower()
    )
    fx_mode: str = field(default_factory=lambda: _str("FX_MODE", "lots").lower())
    treat_withdrawal_as_sale: bool = field(
        default_factory=lambda: _bool("TREAT_WITHDRAWAL_AS_SALE", False)
    )

    db_path: str = field(default_factory=lambda: _str("DB_PATH", "data/portfolio.db"))
    price_refresh_seconds: int = field(
        default_factory=lambda: _int("PRICE_REFRESH_SECONDS", 5)
    )
    host: str = field(default_factory=lambda: _str("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("PORT", 8787))

    #: Fiat currency of the exchange. Binance TH settles fiat in Thai baht.
    fiat: str = "THB"
    #: Preferred quote assets when routing a price for an arbitrary coin.
    quote_preference: tuple[str, ...] = ("THB", "USDT", "BTC", "BNB", "ETH")

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        if not p.is_absolute():
            p = ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def redacted(self) -> dict:
        def mask(s: str) -> str:
            if not s:
                return ""
            return f"{s[:4]}…{s[-4:]}" if len(s) > 10 else "…"

        return {
            "api_key": mask(self.api_key),
            "base_url": self.base_url or "(auto-detect)",
            "dialect": self.dialect or "(auto-detect)",
            "base_currency": self.base_currency,
            "cost_basis_method": self.cost_basis_method,
            "fx_mode": self.fx_mode,
            "treat_withdrawal_as_sale": self.treat_withdrawal_as_sale,
        }


settings = Settings()
