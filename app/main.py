"""Entry point: ``python -m app.main`` or ``./run.sh``."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import settings


def _use_utf8_output() -> None:
    """Stop a legacy Windows console from killing the process.

    Python picks the console's own codepage for stdout, which on a Thai Windows
    install is cp874 and cannot encode an arrow or an em dash.  Printing the
    banner then raises UnicodeEncodeError and the server never starts.  Ask for
    UTF-8 and fall back to replacement characters rather than an exception.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):        # pragma: no cover - odd hosts
                pass


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _cli_sync(full: bool, deep: bool) -> None:
    """Headless sync — handy for cron, and for proving credentials work."""
    from .service import service

    await service.startup()
    try:
        if service.connection_error:
            print(f"!! {service.connection_error}")
            return
        print(f"connected to {service.client.base_url} "
              f"({service.client.dialect_name} dialect)")
        result = await service.sync.run(full=full, deep=deep)
        counts = result.get("counts", {})
        progress = result.get("progress", {})
        if progress.get("error"):
            print(f"!! sync error: {progress['error']}")
        print(f"trades={counts.get('trades', 0)}  "
              f"deposits={counts.get('deposits', 0)}  "
              f"withdrawals={counts.get('withdrawals', 0)}")

        snapshot = await service.portfolio_dict(force=True)
        totals = snapshot["totals"]
        cur = settings.base_currency.lower()
        print(f"\nequity      {totals['equity'][cur]:>18,.2f} {settings.base_currency}")
        print(f"cost basis  {totals['cost'][cur]:>18,.2f}")
        print(f"unrealised  {totals['unrealised'][cur]:>18,.2f} "
              f"({totals['unrealised_pct'][cur]:+.2f}%)")
        print(f"realised    {totals['realised'][cur]:>18,.2f}")
        excluded = totals.get("excluded", {}).get(cur, 0)
        if excluded:
            names = ", ".join(totals.get("unknown_assets", []))
            print(f"no basis    {excluded:>18,.2f}  ({names} — excluded from PnL; "
                  f"add them to {settings.holdings_file})")
        print(f"\n{'asset':<8}{'qty':>18}{'value':>18}{'unrealised':>18}")
        for p in snapshot["positions"][:25]:
            print(f"{p['asset']:<8}{p['qty']:>18,.8f}"
                  f"{p['value'][cur]:>18,.2f}{p['unrealised'][cur]:>18,.2f}")
        for w in snapshot["warnings"]:
            print(f"  ! {w['message']}")
    finally:
        await service.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="Binance TH portfolio tracker")
    parser.add_argument("command", nargs="?", default="serve",
                        choices=["serve", "sync"],
                        help="serve the dashboard (default) or run a headless sync")
    parser.add_argument("--full", action="store_true",
                        help="re-fetch all history instead of resuming")
    parser.add_argument("--deep", action="store_true",
                        help="scan every listed pair for fills, not just likely ones")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    _use_utf8_output()
    _configure_logging(args.verbose)

    if not settings.has_credentials:
        print("No API credentials found. Copy .env.example to .env and fill in "
              "BINANCE_TH_API_KEY / BINANCE_TH_API_SECRET.\n")

    if args.command == "sync":
        asyncio.run(_cli_sync(args.full, args.deep))
        return

    import uvicorn
    print(f"\n  Binance TH portfolio tracker  →  http://{args.host}:{args.port}\n")
    uvicorn.run("app.api:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
