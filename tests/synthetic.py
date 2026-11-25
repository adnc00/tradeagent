"""Synthetic candle builders shared by the tests and the offline paper demo."""

from collections.abc import Sequence

import pandas as pd

from tradeagent.models import Candle

Row = tuple[float, float, float, float]

# 15-minute candles of one session that contain a single ICT long setup:
#   * swing low 100.00 (index 9) is swept to 99.50 by candle 13, which closes back above it
#   * candle 15 closes above the last swing high 100.70 with a 0.95 body (prior ATR ~0.45)
#   * that displacement leaves a bullish FVG 100.35-100.95 (CE 100.65), unfilled at the end
#   * the leg tops at 104.00 (confirmed swing high), then retraces to 101.05 (entry untouched)
# With the daily rows below (D-1 closes above D-2's high -> bullish bias, PDH 105.0,
# PDL 99.0) the long setup is: entry 100.65, stop 99.50 - 0.05%, T1 104.00, T2 105.0
# (R:R 2.24 after 0.1% fees).
LONG_SETUP_ROWS: list[Row] = [
    (102.00, 102.30, 101.85, 101.95),
    (101.95, 102.10, 101.70, 101.80),
    (101.80, 101.90, 101.45, 101.55),
    (101.55, 101.70, 101.30, 101.40),
    (101.40, 101.60, 101.25, 101.50),
    (101.50, 101.65, 101.35, 101.45),
    (101.45, 101.50, 101.05, 101.10),
    (101.10, 101.20, 100.70, 100.80),
    (100.80, 100.90, 100.40, 100.50),
    (100.50, 100.60, 100.00, 100.20),
    (100.20, 100.55, 100.15, 100.45),
    (100.45, 100.70, 100.35, 100.55),
    (100.55, 100.60, 100.20, 100.25),
    (100.25, 100.35, 99.50, 100.10),
    (100.10, 100.35, 100.05, 100.30),
    (100.30, 101.30, 100.25, 101.25),
    (101.25, 101.60, 100.95, 101.50),
    (101.50, 102.00, 101.40, 101.90),
    (101.90, 102.50, 101.80, 102.40),
    (102.40, 103.10, 102.30, 103.00),
    (103.00, 104.00, 102.90, 103.80),
    (103.80, 103.90, 103.30, 103.40),
    (103.40, 103.50, 102.80, 102.90),
    (102.90, 103.00, 102.30, 102.40),
    (102.40, 102.50, 101.40, 101.50),
    (101.50, 101.60, 101.00, 101.05),
]

# Completed days before the setup day: D-3, D-2, D-1.
LONG_SETUP_DAILY_ROWS: list[Row] = [
    (96.0, 99.0, 95.0, 98.0),
    (98.0, 101.0, 97.0, 100.5),
    (100.5, 105.0, 99.0, 102.0),
]

# Price path after the setup: retraces into the FVG (fills 100.65), then runs to 104.20 (T1 hit).
FOLLOW_THROUGH_ROWS: list[Row] = [
    (101.05, 101.20, 100.80, 100.90),
    (100.90, 101.00, 100.60, 100.80),
    (100.80, 101.60, 100.70, 101.50),
    (101.50, 102.60, 101.40, 102.50),
    (102.50, 103.50, 102.40, 103.40),
    (103.40, 104.20, 103.30, 104.10),
]

DEFAULT_END = "2026-11-18 14:45+00:00"  # 09:45 New York; the MSS candle is at 07:15 (ny_am)


def frame(rows: Sequence[Row], start: str | pd.Timestamp, freq: str = "15min") -> pd.DataFrame:
    """Build an OHLCV DataFrame from ``(open, high, low, close)`` rows (volume 1.0).

    ``start`` must be tz-aware (or a string with an offset).
    """
    start_ts = pd.Timestamp(start)
    if start_ts.tzinfo is None:
        raise ValueError("start must be tz-aware")
    index = pd.date_range(start_ts, periods=len(rows), freq=freq)
    df = pd.DataFrame(list(rows), columns=["open", "high", "low", "close"], index=index, dtype=float)
    df["volume"] = 1.0
    return df


def mirror(df: pd.DataFrame, pivot: float) -> pd.DataFrame:
    """Reflect prices (p -> pivot - p), swapping high and low.

    A bullish pattern becomes the equivalent bearish pattern while prices stay
    positive, which lets tests check long/short symmetry.
    """
    return pd.DataFrame(
        {
            "open": pivot - df["open"],
            "high": pivot - df["low"],
            "low": pivot - df["high"],
            "close": pivot - df["close"],
            "volume": df["volume"] if "volume" in df.columns else 1.0,
        },
        index=df.index,
    )


def _scaled(rows: Sequence[Row], scale: float) -> list[Row]:
    return [(o * scale, h * scale, low * scale, c * scale) for o, h, low, c in rows]


def long_setup_frames(
    end: str | pd.Timestamp = DEFAULT_END, scale: float = 1.0
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(15m frame ending at ``end``, daily frame) that contain exactly one long setup.

    The daily frame holds three completed days plus the current day built from
    the intraday candles. ``scale`` multiplies every price (the pattern is
    scale-invariant).
    """
    end_ts = pd.Timestamp(end)
    rows = _scaled(LONG_SETUP_ROWS, scale)
    ltf = frame(rows, end_ts - pd.Timedelta(minutes=15 * (len(rows) - 1)))
    today = end_ts.tz_convert("UTC").normalize()
    current_day: Row = (rows[0][0], float(ltf["high"].max()), float(ltf["low"].min()), rows[-1][3])
    daily_rows = _scaled(LONG_SETUP_DAILY_ROWS, scale) + [current_day]
    daily = frame(daily_rows, today - pd.Timedelta(days=len(LONG_SETUP_DAILY_ROWS)), freq="1D")
    return ltf, daily


def follow_through_candles(after: str | pd.Timestamp = DEFAULT_END, scale: float = 1.0) -> list[Candle]:
    """Candles that continue the setup frame: the entry fills, then T1 is hit."""
    first = pd.Timestamp(after) + pd.Timedelta(minutes=15)
    return [
        Candle(ts=(first + pd.Timedelta(minutes=15 * i)).to_pydatetime(), open=o, high=h, low=low, close=c)
        for i, (o, h, low, c) in enumerate(_scaled(FOLLOW_THROUGH_ROWS, scale))
    ]
