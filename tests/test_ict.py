from datetime import datetime, timezone

import pandas as pd
import pytest

from tradeagent.ict import (
    DealingRange,
    average_true_range,
    daily_bias,
    dealing_range,
    displacement_break,
    equal_levels,
    find_fvgs,
    killzone,
    market_structure_shifts,
    previous_day_levels,
    swing_points,
    validate_ohlc,
)
from tests.synthetic import frame, mirror

T0 = "2026-11-18 05:00+00:00"

# A small downtrend with a swing high at index 3 (105.0) and a swing low at
# index 6 (102.0); candle 8 closes above 105.0 with a large body (displacement).
MSS_ROWS = [
    (104.0, 104.5, 103.5, 104.0),
    (104.0, 104.6, 103.6, 104.2),
    (104.2, 104.8, 103.8, 104.5),
    (104.5, 105.0, 104.0, 104.3),
    (104.3, 104.6, 103.0, 103.2),
    (103.2, 103.5, 102.4, 102.6),
    (102.6, 102.9, 102.0, 102.3),
    (102.3, 102.8, 102.1, 102.7),
    (102.7, 106.0, 102.6, 105.8),
    (105.8, 106.5, 105.5, 106.2),
]


def kinds(swings):
    return [(s.kind, s.index, s.price) for s in swings]


# --- input validation ----------------------------------------------------------------


def test_validate_rejects_naive_index_nan_and_inverted_candles():
    df = frame(MSS_ROWS, T0)
    with pytest.raises(ValueError):
        validate_ohlc(df.tz_localize(None))
    bad = df.copy()
    bad.loc[bad.index[2], "close"] = float("nan")
    with pytest.raises(ValueError):
        validate_ohlc(bad)
    inverted = df.copy()
    inverted.loc[inverted.index[1], "high"] = 100.0
    with pytest.raises(ValueError):
        validate_ohlc(inverted)
    with pytest.raises(ValueError):
        validate_ohlc(df.drop(columns=["low"]))


# --- swing points ----------------------------------------------------------------------


def test_swing_points_three_candle_fractal_without_look_ahead():
    rows = [
        (9.0, 10.0, 8.0, 9.0),
        (9.0, 12.0, 9.0, 11.0),  # swing high
        (11.0, 11.0, 7.0, 8.0),  # swing low
        (8.0, 13.0, 8.0, 12.0),  # swing high
        (12.0, 12.0, 6.0, 7.0),  # lowest low but unconfirmed (last candle)
    ]
    swings = swing_points(frame(rows, T0), length=1)
    assert kinds(swings) == [("high", 1, 12.0), ("low", 2, 7.0), ("high", 3, 13.0)]
    assert [s.confirmed_index for s in swings] == [2, 3, 4]


def test_swing_points_configurable_length():
    rows = [(1.0, h, 0.5, 1.0) for h in (1.0, 2.0, 5.0, 2.0, 1.0)]
    assert kinds(swing_points(frame(rows, T0), length=2)) == [("high", 2, 5.0)]
    rows_fail = [(1.0, h, 0.5, 1.0) for h in (1.0, 2.0, 5.0, 6.0, 1.0)]
    assert [s for s in swing_points(frame(rows_fail, T0), length=2) if s.kind == "high"] == []
    with pytest.raises(ValueError):
        swing_points(frame(rows, T0), length=0)


def test_equal_highs_need_strictly_higher_pivot():
    rows = [(1.0, 1.0, 0.5, 1.0), (1.0, 2.0, 0.5, 1.0), (1.0, 2.0, 0.5, 1.0), (1.0, 1.0, 0.5, 1.0)]
    assert [s for s in swing_points(frame(rows, T0)) if s.kind == "high"] == []


# --- fair value gaps -----------------------------------------------------------------------


def test_bullish_fvg_with_ce_and_filled_flag():
    rows = [
        (9.5, 10.0, 9.0, 9.8),
        (10.0, 13.0, 9.5, 12.8),
        (12.8, 14.0, 11.0, 13.5),
        (13.5, 13.6, 10.6, 11.0),  # dips into the gap, below CE, not through it
        (11.0, 11.2, 9.9, 10.1),  # trades through the gap bottom -> filled
    ]
    gaps = find_fvgs(frame(rows, T0))
    assert len(gaps) == 1
    gap = gaps[0]
    assert (gap.kind, gap.index, gap.bottom, gap.top, gap.ce) == ("bullish", 1, 10.0, 11.0, 10.5)
    assert gap.filled and gap.filled_index == 4

    unfilled = find_fvgs(frame(rows[:4], T0))[0]
    assert not unfilled.filled and unfilled.filled_index is None


def test_bearish_fvg_is_the_mirror():
    rows = [
        (9.5, 10.0, 9.0, 9.8),
        (10.0, 13.0, 9.5, 12.8),
        (12.8, 14.0, 11.0, 13.5),
        (13.5, 13.6, 10.6, 11.0),
        (11.0, 11.2, 9.9, 10.1),
    ]
    gaps = find_fvgs(mirror(frame(rows, T0), 30.0))
    assert len(gaps) == 1
    gap = gaps[0]
    assert (gap.kind, gap.top, gap.bottom, gap.ce) == ("bearish", 20.0, 19.0, 19.5)
    assert gap.filled and gap.filled_index == 4


# --- equal highs / lows -----------------------------------------------------------------------


def _eq_frame():
    rows = [
        (99.0, 99.5, 98.0, 99.0),
        (99.0, 100.00, 98.5, 99.5),  # swing high 100.00
        (99.5, 99.6, 97.0, 98.0),  # swing low 97.00
        (98.0, 100.05, 97.5, 99.0),  # swing high 100.05 (0.05% from 100.00)
        (99.0, 99.2, 97.02, 98.0),  # swing low 97.02
        (98.0, 101.0, 97.6, 100.5),  # swing high 101.0 (alone)
        (100.5, 100.6, 99.0, 99.5),
    ]
    return frame(rows, T0)


def test_equal_highs_and_lows_within_tolerance():
    pools = equal_levels(_eq_frame(), length=1, tolerance_pct=0.1)
    assert [(p.kind, p.price, p.indices) for p in pools] == [
        ("EQH", 100.05, (1, 3)),
        ("EQL", 97.0, (2, 4)),
    ]


def test_equal_levels_respect_tighter_tolerance():
    assert equal_levels(_eq_frame(), length=1, tolerance_pct=0.01) == []


# --- previous day / daily bias -----------------------------------------------------------------


def _daily(rows, start="2026-11-14 00:00+00:00"):
    return frame(rows, start, freq="1D")


def test_previous_day_levels_from_intraday_candles():
    # three UTC days of 6h candles; day 2 range is 95..112
    rows = [
        (100, 101, 99, 100), (100, 103, 98, 102), (102, 104, 100, 101), (101, 102, 97, 98),
        (98, 99, 95, 96), (96, 110, 96, 109), (109, 112, 105, 106), (106, 107, 100, 101),
        (101, 120, 90, 119),
    ]
    df = frame(rows, "2026-11-16 00:00+00:00", freq="6h")
    levels = previous_day_levels(df, at=pd.Timestamp("2026-11-18 03:00+00:00"))
    assert (levels.high, levels.low) == (112.0, 95.0)
    assert levels.start == pd.Timestamp("2026-11-17 00:00+00:00")
    assert levels.end == pd.Timestamp("2026-11-18 00:00+00:00")


def test_previous_day_levels_ignore_the_current_day_and_need_history():
    days = _daily([(100, 105, 95, 101), (101, 110, 99, 108), (108, 130, 80, 90)])
    levels = previous_day_levels(days, at=pd.Timestamp("2026-11-16 12:00+00:00"))
    assert (levels.high, levels.low) == (110.0, 99.0)
    assert previous_day_levels(days, at=pd.Timestamp("2026-11-14 12:00+00:00")) is None


@pytest.mark.parametrize(
    "last,expected",
    [
        ((95, 102, 94, 101), "bullish"),  # closes above PDH 100
        ((95, 99, 89, 95), "bullish"),  # sweeps PDL 90, closes back inside
        ((95, 96, 88, 89), "bearish"),  # closes below PDL
        ((95, 101, 91, 98), "bearish"),  # sweeps PDH, fails to close above
        ((95, 99, 91, 95), "neutral"),  # inside day
        ((95, 101, 89, 95), "neutral"),  # sweeps both sides
    ],
)
def test_daily_bias_rules(last, expected):
    days = _daily([(92, 100, 90, 95), last, (200, 300, 1, 250)])
    assert daily_bias(days, at=pd.Timestamp("2026-11-16 10:00+00:00")) == expected


def test_daily_bias_needs_two_complete_days():
    days = _daily([(92, 100, 90, 95), (95, 102, 94, 101)])
    assert daily_bias(days, at=pd.Timestamp("2026-11-15 10:00+00:00")) == "neutral"


# --- dealing range -----------------------------------------------------------------------------


def test_premium_discount_zones():
    rng = DealingRange(high=110.0, low=100.0)
    assert rng.equilibrium == 105.0
    assert rng.zone(104.0) == "discount"
    assert rng.zone(106.0) == "premium"
    assert rng.zone(105.0) == "equilibrium"
    with pytest.raises(ValueError):
        DealingRange(high=99.0, low=100.0)


def test_dealing_range_uses_latest_swing_high_and_low():
    rng = dealing_range(frame(MSS_ROWS, T0), length=1)
    assert (rng.high, rng.low) == (105.0, 102.0)


# --- ATR / market structure shift ------------------------------------------------------------


def test_average_true_range_uses_previous_close():
    rows = [(10, 11, 9, 10), (10, 15, 10, 14), (14, 14.5, 12, 13)]
    atr = average_true_range(frame(rows, T0), period=2)
    assert list(atr) == pytest.approx([2.0, (2.0 + 5.0) / 2, (5.0 + 2.5) / 2])


def test_bullish_mss_needs_close_beyond_swing_with_displacement():
    df = frame(MSS_ROWS, T0)
    shifts = market_structure_shifts(df, length=1, k=1.5, atr_period=14)
    assert len(shifts) == 1
    mss = shifts[0]
    assert (mss.kind, mss.index, mss.level, mss.swing_index) == ("bullish", 8, 105.0, 3)
    assert mss.body == pytest.approx(3.1)
    assert mss.body >= 1.5 * mss.atr


def test_break_without_displacement_is_not_an_mss():
    assert market_structure_shifts(frame(MSS_ROWS, T0), length=1, k=4.0, atr_period=14) == []


def test_bearish_mss_is_the_mirror():
    shifts = market_structure_shifts(mirror(frame(MSS_ROWS, T0), 200.0), length=1, k=1.5, atr_period=14)
    assert [(s.kind, s.index, s.level) for s in shifts] == [("bearish", 8, 95.0)]


def test_only_the_first_close_beyond_a_swing_counts():
    rows = MSS_ROWS + [(106.2, 109.0, 106.0, 108.8)]
    shifts = market_structure_shifts(frame(rows, T0), length=1, k=1.5, atr_period=14)
    assert [s.index for s in shifts] == [8]


# --- killzones ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utc,expected",
    [
        ("2026-11-18 13:00", "ny_am"),  # 08:00 New York (EST)
        ("2026-11-18 07:00", "london"),  # 02:00
        ("2026-11-18 06:59", None),  # 01:59
        ("2026-11-18 15:00", "london_close"),  # 10:00
        ("2026-11-18 17:00", None),  # 12:00
        ("2026-11-18 18:29", None),  # 13:29
        ("2026-11-18 18:30", "ny_pm"),  # 13:30
        ("2026-11-18 21:00", None),  # 16:00
        ("2026-11-19 01:00", "asia"),  # 20:00
        ("2026-11-19 04:59", "asia"),  # 23:59
        ("2026-11-19 05:00", None),  # 00:00
        ("2026-07-15 12:00", "ny_am"),  # 08:00 New York (EDT)
    ],
)
def test_killzones_in_new_york_time(utc, expected):
    assert killzone(pd.Timestamp(utc, tz="UTC")) == expected


def test_killzone_accepts_datetime_and_rejects_naive():
    assert killzone(datetime(2026, 11, 18, 13, 0, tzinfo=timezone.utc)) == "ny_am"
    with pytest.raises(ValueError):
        killzone(datetime(2026, 11, 18, 13, 0))


def test_equal_highs_use_single_linkage_within_a_width_cap():
    # swing highs 100.00, 100.09, 100.18: each within 0.1% of its neighbour
    rows = [
        (99.0, 99.5, 98.0, 99.0),
        (99.0, 100.00, 98.5, 99.5),
        (99.5, 99.6, 98.0, 98.5),
        (98.5, 100.09, 98.2, 99.0),
        (99.0, 99.4, 98.1, 98.6),
        (98.6, 100.18, 98.3, 99.2),
        (99.2, 99.5, 98.4, 98.9),
    ]
    (pool,) = [p for p in equal_levels(frame(rows, T0), tolerance_pct=0.1) if p.kind == "EQH"]
    assert pool.price == 100.18 and pool.indices == (1, 3, 5)


def test_displacement_break_is_the_first_close_beyond_the_level():
    df = frame(MSS_ROWS, T0)
    shift = displacement_break(df, level=105.0, start=4, end=9, k=1.5, atr_period=14)
    assert (shift.kind, shift.index, shift.level) == ("bullish", 8, 105.0)
    assert displacement_break(df, level=105.0, start=4, end=9, k=4.0, atr_period=14) is None
    assert displacement_break(df, level=110.0, start=4, end=9, k=1.5, atr_period=14) is None
