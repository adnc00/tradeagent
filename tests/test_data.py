import pandas as pd
import pytest

from tradeagent.brokers import data
from tradeagent.brokers.data import MarketData, asset_class, market_type, normalize_ohlcv


@pytest.mark.parametrize(
    "symbol,expected",
    [("BTC/USDT", "crypto"), ("BTC/USDT:USDT", "crypto"), ("THYAO.IS", "bist"), ("thyao.is", "bist"), ("AAPL", "us_equity")],
)
def test_asset_class(symbol, expected):
    assert asset_class(symbol) == expected


@pytest.mark.parametrize(
    "symbol,exchange,expected",
    [
        ("BTC/USDT", "binance", "spot"),
        ("BTC/TRY", "btcturk", "spot"),
        ("BTC/USDT", "binanceusdm", "futures"),
        ("BTC/USDT:USDT", "binance", "futures"),
        ("THYAO.IS", "binanceusdm", "spot"),
        ("AAPL", "binance", "equity"),
    ],
)
def test_market_type(symbol, exchange, expected):
    assert market_type(symbol, exchange) == expected


class FakeCcxt:
    id = "binance"

    def __init__(self):
        self.calls = []

    def fetch_ohlcv(self, symbol, timeframe, limit=None):
        self.calls.append((symbol, timeframe, limit))
        base = 1_763_470_800_000  # 2025-11-18 13:00 UTC in ms
        return [[base + i * 900_000, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 10.0] for i in range(3)]

    def fetch_ticker(self, symbol):
        return {"last": 123.4}


def _yf_frame(tz: str) -> pd.DataFrame:
    index = pd.date_range("2026-11-17 10:00", periods=4, freq="15min", tz=tz)
    return pd.DataFrame(
        {
            "Open": [10.0, 10.1, float("nan"), 10.3],
            "High": [10.2, 10.3, float("nan"), 10.5],
            "Low": [9.9, 10.0, float("nan"), 10.2],
            "Close": [10.1, 10.2, float("nan"), 10.4],
            "Adj Close": [10.1, 10.2, float("nan"), 10.4],
            "Volume": [1, 2, 3, 4],
        },
        index=index,
    )


def test_crypto_goes_to_ccxt_with_utc_index():
    client = FakeCcxt()
    df = MarketData("binance", ccxt_client=client).get_ohlcv("BTC/USDT", "15m", 3)
    assert client.calls == [("BTC/USDT", "15m", 3)]
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert str(df.index.tz) == "UTC"
    assert df.index[0] == pd.Timestamp("2025-11-18 13:00", tz="UTC")
    assert MarketData("binance", ccxt_client=client).get_price("BTC/USDT") == 123.4


@pytest.mark.parametrize("symbol,tz", [("THYAO.IS", "Europe/Istanbul"), ("AAPL", "America/New_York")])
def test_stocks_go_to_yfinance_and_keep_exchange_timezone(monkeypatch, symbol, tz):
    requests = []

    def fake_history(ticker, interval, period):
        requests.append((ticker, interval, period))
        return _yf_frame(tz)

    monkeypatch.setattr(data, "_yfinance_history", fake_history)
    df = MarketData("binance", ccxt_client=FakeCcxt()).get_ohlcv(symbol, "15m", 2)
    assert requests == [(symbol, "15m", "60d")]
    assert len(df) == 2 and df["close"].iloc[-1] == 10.4
    assert str(df.index.tz) == tz


def test_yfinance_rejects_unsupported_timeframes():
    with pytest.raises(ValueError):
        MarketData("binance", ccxt_client=FakeCcxt()).get_ohlcv("AAPL", "4h", 10)


def test_normalize_rejects_empty_and_non_positive_data():
    with pytest.raises(ValueError):
        normalize_ohlcv(pd.DataFrame(columns=["open", "high", "low", "close", "volume"]), "X")
    bad = _yf_frame("UTC").dropna()
    bad.loc[bad.index[0], "Low"] = 0.0
    with pytest.raises(ValueError):
        normalize_ohlcv(bad, "X")


@pytest.mark.network
def test_public_ccxt_ohlcv_smoke():
    df = MarketData("binance").get_ohlcv("BTC/USDT", "1h", 20)
    assert len(df) == 20
    assert str(df.index.tz) == "UTC"
    assert (df[["open", "high", "low", "close"]] > 0).all().all()
