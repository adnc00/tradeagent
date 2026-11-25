"""ICT / SMC detectors for OHLCV DataFrames.

Input contract: a DataFrame with float columns ``open, high, low, close``
(``volume`` optional) and a tz-aware, strictly ascending DatetimeIndex. Values
must be finite and ``high >= low``. Price positivity is enforced at the data
and scanner boundary, not here, because the scanner evaluates short setups on
a negated (mirrored) copy of the prices.

Look-ahead: every detector only uses candles up to the point where an event
becomes known. Swing points need ``length`` candles on the right, so the last
``length`` candles can never be swing points.

Definitions follow concepts.md of the trading-smc-ict skill. They are
rule-based heuristics; they describe structure and do not predict price.
"""

from dataclasses import dataclass
from datetime import datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NEW_YORK = ZoneInfo("America/New_York")
OHLC_COLUMNS = ("open", "high", "low", "close")

# (name, start, end) in New York local time; start inclusive, end exclusive, None = midnight.
KILLZONES: tuple[tuple[str, time, time | None], ...] = (
    ("asia", time(20, 0), None),
    ("london", time(2, 0), time(5, 0)),
    ("ny_am", time(7, 0), time(10, 0)),
    ("london_close", time(10, 0), time(12, 0)),
    ("ny_pm", time(13, 30), time(16, 0)),
)
KILLZONE_NAMES = tuple(name for name, _, _ in KILLZONES)

Zone = Literal["premium", "discount", "equilibrium"]
Bias = Literal["bullish", "bearish", "neutral"]


def validate_ohlc(df: pd.DataFrame) -> None:
    """Raise ``ValueError`` unless ``df`` satisfies the module's input contract."""
    missing = [c for c in OHLC_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"OHLC columns missing: {missing}")
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        raise ValueError("index must be a tz-aware DatetimeIndex")
    if not df.index.is_monotonic_increasing or not df.index.is_unique:
        raise ValueError("index must be strictly ascending")
    values = df.loc[:, list(OHLC_COLUMNS)].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("OHLC values must be finite (no NaN/inf)")
    if (df["high"].to_numpy(dtype=float) < df["low"].to_numpy(dtype=float)).any():
        raise ValueError("every candle needs high >= low")


# --- swing points ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SwingPoint:
    """A fractal pivot. It becomes known at the close of ``confirmed_index``."""

    kind: Literal["high", "low"]
    index: int
    ts: pd.Timestamp
    price: float
    confirmed_index: int


def swing_points(df: pd.DataFrame, length: int = 1) -> list[SwingPoint]:
    """Fractal swing highs/lows: the pivot is strictly beyond ``length`` candles on each side.

    ``length=1`` is the classic three-candle swing. Equal neighbours do not
    qualify. Results are ordered by index (high before low on the same candle).
    """
    validate_ohlc(df)
    if length < 1:
        raise ValueError("length must be >= 1")
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    swings: list[SwingPoint] = []
    for i in range(length, len(df) - length):
        neighbours_high = np.concatenate((high[i - length : i], high[i + 1 : i + length + 1]))
        neighbours_low = np.concatenate((low[i - length : i], low[i + 1 : i + length + 1]))
        if high[i] > neighbours_high.max():
            swings.append(SwingPoint("high", i, df.index[i], float(high[i]), i + length))
        if low[i] < neighbours_low.min():
            swings.append(SwingPoint("low", i, df.index[i], float(low[i]), i + length))
    return swings


# --- fair value gaps -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FairValueGap:
    """Three-candle imbalance. ``index`` is the middle (displacement) candle.

    Bullish (BISI): candle 3 low above candle 1 high. Bearish (SIBI): candle 3
    high below candle 1 low. ``ce`` is the 50% level (consequent encroachment).
    ``filled`` means a later candle traded through the whole gap (reached the
    bottom of a bullish gap / the top of a bearish gap).
    """

    kind: Literal["bullish", "bearish"]
    index: int
    ts: pd.Timestamp
    top: float
    bottom: float
    ce: float
    filled: bool
    filled_index: int | None


def find_fvgs(df: pd.DataFrame) -> list[FairValueGap]:
    """All bullish and bearish FVGs in ``df`` with their fill status at the last candle."""
    validate_ohlc(df)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    gaps: list[FairValueGap] = []
    for i in range(1, len(df) - 1):
        if low[i + 1] > high[i - 1]:
            bottom, top = float(high[i - 1]), float(low[i + 1])
            hits = np.flatnonzero(low[i + 2 :] <= bottom)
            kind: Literal["bullish", "bearish"] = "bullish"
        elif high[i + 1] < low[i - 1]:
            bottom, top = float(high[i + 1]), float(low[i - 1])
            hits = np.flatnonzero(high[i + 2 :] >= top)
            kind = "bearish"
        else:
            continue
        filled_index = int(i + 2 + hits[0]) if hits.size else None
        gaps.append(
            FairValueGap(
                kind=kind,
                index=i,
                ts=df.index[i],
                top=top,
                bottom=bottom,
                ce=(top + bottom) / 2.0,
                filled=filled_index is not None,
                filled_index=filled_index,
            )
        )
    return gaps


# --- equal highs / lows ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LiquidityPool:
    """Relative equal highs (EQH) or lows (EQL): two or more swings at nearly one price.

    ``price`` is the extreme of the cluster (highest high / lowest low), where
    the resting stops sit beyond.
    """

    kind: Literal["EQH", "EQL"]
    price: float
    indices: tuple[int, ...]


def equal_levels(df: pd.DataFrame, length: int = 1, tolerance_pct: float = 0.1) -> list[LiquidityPool]:
    """Cluster swing highs (and lows) whose prices lie within ``tolerance_pct`` % of each other.

    Swings are sorted by price and linked to their price neighbour (single
    linkage): a swing joins the current cluster when it is within tolerance of
    the cluster's last member, as long as the whole cluster stays within twice
    the tolerance of its first member. Clusters with at least two swings become
    pools, ordered by their first swing index.
    """
    if tolerance_pct < 0:
        raise ValueError("tolerance_pct must be >= 0")
    swings = swing_points(df, length)
    pools: list[LiquidityPool] = []
    kinds: tuple[tuple[Literal["high", "low"], Literal["EQH", "EQL"]], ...] = (("high", "EQH"), ("low", "EQL"))
    for swing_kind, pool_kind in kinds:
        ordered = sorted((s for s in swings if s.kind == swing_kind), key=lambda s: (s.price, s.index))
        clusters: list[list[SwingPoint]] = []
        for swing in ordered:
            if clusters:
                first, last = clusters[-1][0].price, clusters[-1][-1].price
                near_last = abs(swing.price - last) <= abs(last) * tolerance_pct / 100.0
                within_cap = abs(swing.price - first) <= abs(first) * 2 * tolerance_pct / 100.0
                if near_last and within_cap:
                    clusters[-1].append(swing)
                    continue
            clusters.append([swing])
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            prices = [s.price for s in cluster]
            price = max(prices) if pool_kind == "EQH" else min(prices)
            pools.append(LiquidityPool(pool_kind, price, tuple(sorted(s.index for s in cluster))))
    return sorted(pools, key=lambda p: p.indices[0])


# --- daily levels and bias ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreviousDay:
    """High/low of the last completed calendar day; ``end`` is that day's end (exclusive)."""

    high: float
    low: float
    start: pd.Timestamp
    end: pd.Timestamp


def daily_candles(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate candles into calendar days of the index's own timezone.

    Daily input passes through unchanged (one candle per day). Days without
    data are dropped.
    """
    validate_ohlc(df)
    daily = df.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"})
    return daily.dropna()


def _completed_days(df: pd.DataFrame, at: datetime | pd.Timestamp | None) -> pd.DataFrame:
    days = daily_candles(df)
    moment = df.index[-1] if at is None else pd.Timestamp(at)
    if moment.tzinfo is None:
        raise ValueError("at must be tz-aware")
    day_start = moment.tz_convert(df.index.tz).normalize()
    return days.loc[days.index < day_start]


def previous_day_levels(df: pd.DataFrame, at: datetime | pd.Timestamp | None = None) -> PreviousDay | None:
    """PDH/PDL: the last calendar day that ended before the day containing ``at``.

    ``at`` defaults to the last candle. Works on intraday or daily candles; the
    day boundary is midnight in the index's timezone (UTC for ccxt data, the
    exchange's local time for yfinance data).
    """
    days = _completed_days(df, at)
    if days.empty:
        return None
    start = days.index[-1]
    last = days.iloc[-1]
    return PreviousDay(
        high=float(last["high"]),
        low=float(last["low"]),
        start=start,
        end=start + pd.DateOffset(days=1),
    )


def daily_bias(df: pd.DataFrame, at: datetime | pd.Timestamp | None = None) -> Bias:
    """Daily bias from the last completed day versus the day before it.

    Bullish: the day closed above the prior day's high, or swept the prior
    low and did not close below it. Bearish is the mirror. Both or neither
    -> neutral.
    """
    days = _completed_days(df, at)
    if len(days) < 2:
        return "neutral"
    prior, last = days.iloc[-2], days.iloc[-1]
    bullish = last["close"] > prior["high"] or (last["low"] < prior["low"] and last["close"] >= prior["low"])
    bearish = last["close"] < prior["low"] or (last["high"] > prior["high"] and last["close"] <= prior["high"])
    if bullish and not bearish:
        return "bullish"
    if bearish and not bullish:
        return "bearish"
    return "neutral"


# --- premium / discount ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DealingRange:
    """A swing range; above equilibrium (50%) is premium, below is discount."""

    high: float
    low: float

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ValueError("dealing range high must be >= low")

    @property
    def equilibrium(self) -> float:
        return (self.high + self.low) / 2.0

    def zone(self, price: float) -> Zone:
        eq = self.equilibrium
        if price > eq:
            return "premium"
        if price < eq:
            return "discount"
        return "equilibrium"


def dealing_range(df: pd.DataFrame, length: int = 1) -> DealingRange | None:
    """The current dealing range: between the latest confirmed swing high and swing low."""
    swings = swing_points(df, length)
    last_high = next((s for s in reversed(swings) if s.kind == "high"), None)
    last_low = next((s for s in reversed(swings) if s.kind == "low"), None)
    if last_high is None or last_low is None:
        return None
    return DealingRange(high=max(last_high.price, last_low.price), low=min(last_high.price, last_low.price))


# --- displacement / market structure shift ------------------------------------------------


def average_true_range(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Simple moving average of the true range (the first candle uses high - low)."""
    validate_ohlc(df)
    if period < 1:
        raise ValueError("period must be >= 1")
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    return true_range.rolling(period, min_periods=1).mean()


@dataclass(frozen=True, slots=True)
class StructureShift:
    """Market structure shift: first close beyond the last opposite swing, with displacement.

    ``atr`` is the ATR of the candles before the shift candle; the shift
    candle's body is at least ``k * atr``.
    """

    kind: Literal["bullish", "bearish"]
    index: int
    ts: pd.Timestamp
    level: float
    swing_index: int
    body: float
    atr: float


def market_structure_shifts(
    df: pd.DataFrame, length: int = 1, k: float = 1.5, atr_period: int = 14
) -> list[StructureShift]:
    """Bullish/bearish MSS events.

    The most recent confirmed swing high (low) is the level to break. The
    first candle that closes beyond it consumes the level; it is an MSS only
    if that candle is in the break direction and its body is at least ``k``
    times the ATR of the preceding candles. Breaks without displacement are
    not reported.
    """
    if k <= 0:
        raise ValueError("k must be > 0")
    opens = df["open"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    atr_before = average_true_range(df, atr_period).shift(1).to_numpy(dtype=float)
    confirmed_at: dict[int, list[SwingPoint]] = {}
    for swing in swing_points(df, length):
        confirmed_at.setdefault(swing.confirmed_index, []).append(swing)

    shifts: list[StructureShift] = []
    level_high: SwingPoint | None = None
    level_low: SwingPoint | None = None
    for i in range(len(df)):
        body = float(abs(closes[i] - opens[i]))
        atr = atr_before[i]
        displaced = bool(np.isfinite(atr) and atr > 0 and body >= k * atr)
        if level_high is not None and closes[i] > level_high.price:
            if displaced and closes[i] > opens[i]:
                shifts.append(
                    StructureShift("bullish", i, df.index[i], level_high.price, level_high.index, body, float(atr))
                )
            level_high = None
        if level_low is not None and closes[i] < level_low.price:
            if displaced and closes[i] < opens[i]:
                shifts.append(
                    StructureShift("bearish", i, df.index[i], level_low.price, level_low.index, body, float(atr))
                )
            level_low = None
        for swing in confirmed_at.get(i, ()):
            if swing.kind == "high":
                level_high = swing
            else:
                level_low = swing
    return shifts


def displacement_break(
    df: pd.DataFrame,
    level: float,
    start: int,
    end: int,
    k: float = 1.5,
    atr_period: int = 14,
    swing_index: int = -1,
) -> StructureShift | None:
    """Bullish MSS of a given level: the first close above ``level`` in ``[start, end]``.

    It counts only if that candle is bullish with a body of at least ``k`` x
    the ATR of the candles before it; a weak first close consumes the level and
    returns ``None``. Bearish breaks are found by calling this on negated prices.
    """
    if k <= 0:
        raise ValueError("k must be > 0")
    opens = df["open"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    atr_before = average_true_range(df, atr_period).shift(1).to_numpy(dtype=float)
    for i in range(max(start, 0), min(end, len(df) - 1) + 1):
        if closes[i] <= level:
            continue
        body = float(abs(closes[i] - opens[i]))
        atr = atr_before[i]
        if closes[i] > opens[i] and np.isfinite(atr) and atr > 0 and body >= k * atr:
            return StructureShift("bullish", i, df.index[i], float(level), swing_index, body, float(atr))
        return None
    return None


# --- killzones -----------------------------------------------------------------------------


def killzone(ts: datetime | pd.Timestamp) -> str | None:
    """ICT killzone of a tz-aware timestamp in New York time, or ``None`` outside them.

    asia 20:00-00:00, london 02:00-05:00, ny_am 07:00-10:00,
    london_close 10:00-12:00, ny_pm 13:30-16:00 (start inclusive, end exclusive).
    """
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("timestamp must be tz-aware")
    local = ts.astimezone(NEW_YORK).time()
    for name, start, end in KILLZONES:
        if local >= start and (end is None or local < end):
            return name
    return None
