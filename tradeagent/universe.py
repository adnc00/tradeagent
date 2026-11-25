"""Symbol universe: every tradable pair of the configured exchange, ranked by 24h quote volume.

Public, read-only ccxt calls only (``load_markets`` and ``fetch_tickers``).
"""

from collections.abc import Iterable
from typing import Any

# Pegged or fiat-like bases: their price barely moves, so ICT structure on them is noise.
STABLE_BASES = frozenset(
    {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "USDE", "PYUSD", "USD1", "EUR", "EURI", "TRY", "BRL", "AEUR", "XUSD", "BFUSD"}
)


def _excluded(symbol: str, base: str, exclude: Iterable[str]) -> bool:
    wanted = {e.strip().upper() for e in exclude if e.strip()}
    return symbol.upper() in wanted or base.upper() in wanted


def top_symbols(
    client: Any,
    *,
    derivatives: bool,
    quote: str = "USDT",
    top: int = 100,
    min_quote_volume: float = 0.0,
    exclude: Iterable[str] = (),
) -> list[str]:
    """Active pairs quoted (and, for perpetuals, settled) in ``quote``, highest 24h volume first.

    ``derivatives`` selects linear perpetual swaps (``BTC/USDT:USDT``); otherwise
    spot pairs. ``top`` 0 means no cap.
    """
    quote = quote.upper()
    exclude = tuple(exclude)
    candidates = []
    for market in client.load_markets().values():
        if market.get("active") is False or str(market.get("quote", "")).upper() != quote:
            continue
        base = str(market.get("base", ""))
        if base.upper() in STABLE_BASES or _excluded(market["symbol"], base, exclude):
            continue
        if derivatives:
            if not (market.get("swap") and market.get("linear") and str(market.get("settle", "")).upper() == quote):
                continue
        elif not market.get("spot"):
            continue
        candidates.append(market["symbol"])
    if not candidates:
        return []
    tickers = client.fetch_tickers(candidates)
    volumes = {s: float((tickers.get(s) or {}).get("quoteVolume") or 0.0) for s in candidates}
    ranked = sorted((s for s in candidates if volumes[s] >= min_quote_volume), key=lambda s: (-volumes[s], s))
    return ranked[:top] if top > 0 else ranked
