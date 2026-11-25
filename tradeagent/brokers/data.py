"""Market data: OHLCV from ccxt (crypto, public endpoints) or yfinance (BIST ``.IS`` and US stocks).

ccxt candles are indexed in UTC; yfinance candles keep the exchange's own
timezone, so "previous day" means the exchange's trading day. No API keys
are used here.
"""

from typing import Any, Literal

import pandas as pd

from tradeagent.ict import validate_ohlc

AssetClass = Literal["crypto", "bist", "us_equity"]
DERIVATIVE_EXCHANGES = frozenset({"binanceusdm", "binancecoinm"})
COLUMNS = ["open", "high", "low", "close", "volume"]

# timeframe -> (yfinance interval, history period that covers enough candles)
_YFINANCE_INTERVALS = {
    "1m": ("1m", "7d"),
    "2m": ("2m", "60d"),
    "5m": ("5m", "60d"),
    "15m": ("15m", "60d"),
    "30m": ("30m", "60d"),
    "1h": ("60m", "730d"),
    "1d": ("1d", "5y"),
    "1wk": ("1wk", "10y"),
}


def asset_class(symbol: str) -> AssetClass:
    """``.IS`` -> BIST, ``BASE/QUOTE`` -> crypto, anything else -> US equity."""
    upper = symbol.upper()
    if upper.endswith(".IS"):
        return "bist"
    if "/" in upper:
        return "crypto"
    return "us_equity"


def market_type(symbol: str, crypto_exchange: str) -> str:
    """Venue kind a symbol trades on with the configured crypto exchange.

    Crypto is ``futures`` on derivative-only exchanges or for ccxt contract
    symbols (``BTC/USDT:USDT``), otherwise ``spot``. BIST is cash equity
    (``spot``); US stocks go to a margin broker (``equity``).
    """
    kind = asset_class(symbol)
    if kind == "crypto":
        return "futures" if ":" in symbol or crypto_exchange in DERIVATIVE_EXCHANGES else "spot"
    if kind == "bist":
        return "spot"
    return "equity"


def normalize_ohlcv(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Lower-case OHLCV columns, drop incomplete rows and validate the result."""
    df = raw.rename(columns=str.lower)
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{symbol}: OHLCV sütunları eksik: {missing}")
    df = df.loc[:, COLUMNS].astype(float).dropna(subset=["open", "high", "low", "close"])
    if df.empty:
        raise ValueError(f"{symbol}: veri yok (sembolü ve zaman dilimini kontrol edin).")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError(f"{symbol}: zaman damgası indeksi yok.")
    if df.index.tz is None:
        df = df.tz_localize("UTC")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    validate_ohlc(df)
    if (df.loc[:, ["open", "high", "low", "close"]].to_numpy() <= 0).any():
        raise ValueError(f"{symbol}: sıfır veya negatif fiyat içeren veri reddedildi.")
    return df


def _yfinance_history(ticker: str, interval: str, period: str) -> pd.DataFrame:
    import yfinance as yf

    return yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False, actions=False)


def fetch_yfinance(symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    if timeframe not in _YFINANCE_INTERVALS:
        supported = ", ".join(_YFINANCE_INTERVALS)
        raise ValueError(f"yfinance '{timeframe}' zaman dilimini desteklemiyor; desteklenenler: {supported}")
    interval, period = _YFINANCE_INTERVALS[timeframe]
    return normalize_ohlcv(_yfinance_history(symbol, interval, period), symbol).tail(limit)


def fetch_ccxt(client: Any, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    rows = client.fetch_ohlcv(symbol, timeframe, limit=limit)
    frame = pd.DataFrame(rows, columns=["ts", *COLUMNS])
    frame.index = pd.to_datetime(frame["ts"], unit="ms", utc=True)
    return normalize_ohlcv(frame.drop(columns=["ts"]), symbol)


class MarketData:
    """Read-only OHLCV/price access that routes each symbol to its data source."""

    def __init__(self, crypto_exchange: str = "binance", *, ccxt_client: Any | None = None) -> None:
        self.crypto_exchange = crypto_exchange
        self._ccxt_client = ccxt_client

    def _client(self) -> Any:
        if self._ccxt_client is None:
            import ccxt

            exchange_class = getattr(ccxt, self.crypto_exchange, None)
            if exchange_class is None:
                raise ValueError(f"ccxt'de '{self.crypto_exchange}' borsası yok.")
            self._ccxt_client = exchange_class({"enableRateLimit": True})
        return self._ccxt_client

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame:
        if asset_class(symbol) == "crypto":
            return fetch_ccxt(self._client(), symbol, timeframe, limit)
        return fetch_yfinance(symbol, timeframe, limit)

    def get_price(self, symbol: str) -> float:
        if asset_class(symbol) == "crypto":
            last = self._client().fetch_ticker(symbol).get("last")
            if last is not None:
                return float(last)
            return float(fetch_ccxt(self._client(), symbol, "1m", 2)["close"].iloc[-1])
        return float(fetch_yfinance(symbol, "1d", 5)["close"].iloc[-1])
