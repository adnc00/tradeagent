"""Rule-based ICT setup scanner.

This is a deterministic heuristic that encodes one common ICT entry model. It
describes what the candles show; it is not a forecast and not a guarantee.

Long rule (evaluated at the last candle of the lower-timeframe frame):

1. Daily bias is bullish (``ict.daily_bias``).
2. Sell-side sweep: a candle trades below a confirmed swing low (or the
   previous day's low) and a close back above that level follows within
   ``reclaim_bars`` candles (the sweep candle included).
3. The first bullish MSS with displacement at or after that reclaim, no more
   than ``sweep_lookback`` candles after the sweep started.
   The level to break is the last swing high that led into the sweep; the
   first close above it must itself be a displacement candle.
4. A bullish FVG left by the displacement leg (middle candle after the sweep
   extreme, up to the MSS candle) that is still unfilled and whose entry
   price has not been traded since (otherwise the setup has played out).
5. The entry (FVG CE, or the FVG top with ``entry="top"``) is in discount of
   the dealing range [sweep extreme, highest high since].
6. Stop = sweep extreme minus ``stop_buffer_pct`` %. Target 1 = the nearest
   untaken buy-side liquidity above the entry (swing high / EQH / PDH; the
   PDH counts only if today's HTF candle has not traded above it), target
   2 = the next one.
7. Dropped when: price traded below the sweep extreme after the MSS, the MSS
   is older than ``max_setup_age`` candles, or (if ``killzones`` is set) the
   MSS candle is outside the allowed killzones.

Short is the exact mirror: the same rule runs on negated prices and the
levels are mapped back. A ticket is attached only when ``risk.check_ticket``
returns no violations; otherwise the setup carries the violation messages.
"""

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from tradeagent.ict import (
    KILLZONE_NAMES,
    NEW_YORK,
    DealingRange,
    PreviousDay,
    SwingPoint,
    daily_bias,
    equal_levels,
    find_fvgs,
    displacement_break,
    killzone,
    previous_day_levels,
    swing_points,
    validate_ohlc,
)
from tradeagent.models import AccountState, OrderTicket, RiskLimits, Setup, Side
from tradeagent.risk import check_ticket, floor_to_step, fmt, lot_step, position_size

ENTRY_MODES = ("ce", "top")
MIN_CANDLES = 3
_MIRRORED_KIND = {
    "swing_low": "swing_high",
    "swing_high": "swing_low",
    "PDL": "PDH",
    "PDH": "PDL",
    "EQH": "EQL",
    "EQL": "EQH",
}
_TARGET_RANK = {"PDH": 0, "EQH": 1, "swing_high": 2}


@dataclass(frozen=True, slots=True)
class ScannerParams:
    """Tunable thresholds of the heuristic (see the module docstring)."""

    swing_length: int = 1
    eqh_tolerance_pct: float = 0.1
    displacement_k: float = 1.5
    atr_period: int = 14
    sweep_lookback: int = 30
    reclaim_bars: int = 2
    max_setup_age: int = 20
    entry: str = "ce"
    stop_buffer_pct: float = 0.05
    killzones: tuple[str, ...] = ()
    leverage: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "killzones", tuple(self.killzones))
        if self.entry not in ENTRY_MODES:
            raise ValueError(f"entry must be one of {ENTRY_MODES}, got {self.entry!r}")
        unknown = [k for k in self.killzones if k not in KILLZONE_NAMES]
        if unknown:
            raise ValueError(f"unknown killzones {unknown}; expected names from {KILLZONE_NAMES}")
        for name in ("swing_length", "atr_period", "sweep_lookback", "reclaim_bars"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
        if self.max_setup_age < 0:
            raise ValueError("max_setup_age must be >= 0")
        if self.displacement_k <= 0:
            raise ValueError("displacement_k must be > 0")
        for name in ("eqh_tolerance_pct", "stop_buffer_pct", "displacement_k", "leverage"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} sonlu bir sayı olmalı")
        if not 0 <= self.eqh_tolerance_pct <= 5 or not 0 <= self.stop_buffer_pct <= 5:
            raise ValueError("eqh_tolerance_pct ve stop_buffer_pct 0 ile 5 arasında olmalı")
        if not 1 <= self.leverage <= 125:
            raise ValueError("leverage 1 ile 125 arasında olmalı")


@dataclass(frozen=True, slots=True)
class _Sweep:
    kind: str
    level: float
    start: int
    reclaim: int


@dataclass(frozen=True, slots=True)
class _Geometry:
    sweep_kind: str
    sweep_level: float
    sweep_extreme: float
    mss_ts: pd.Timestamp
    mss_level: float
    fvg_top: float
    fvg_bottom: float
    entry: float
    stop: float
    targets: tuple[float, ...]
    target_kinds: tuple[str, ...]
    range_high: float
    range_low: float

    def unmirrored(self) -> "_Geometry":
        """Map a geometry found on negated prices back to real prices."""
        return _Geometry(
            sweep_kind=_MIRRORED_KIND[self.sweep_kind],
            sweep_level=-self.sweep_level,
            sweep_extreme=-self.sweep_extreme,
            mss_ts=self.mss_ts,
            mss_level=-self.mss_level,
            fvg_top=-self.fvg_bottom,
            fvg_bottom=-self.fvg_top,
            entry=-self.entry,
            stop=-self.stop,
            targets=tuple(-t for t in self.targets),
            target_kinds=tuple(_MIRRORED_KIND[k] for k in self.target_kinds),
            range_high=-self.range_low,
            range_low=-self.range_high,
        )


def _negated(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {"open": -df["open"], "high": -df["low"], "low": -df["high"], "close": -df["close"]},
        index=df.index,
    )


def _require_positive_prices(df: pd.DataFrame, name: str) -> None:
    validate_ohlc(df)
    if (df.loc[:, ["open", "high", "low", "close"]].to_numpy(dtype=float) <= 0).any():
        raise ValueError(f"{name}: prices must be > 0")


def _first_index_at_or_after(df: pd.DataFrame, moment: pd.Timestamp) -> int:
    return int(df.index.searchsorted(moment.tz_convert(df.index.tz)))


def _sell_side_sweeps(
    ltf: pd.DataFrame, swings: list[SwingPoint], prev_day: PreviousDay | None, reclaim_bars: int
) -> list[_Sweep]:
    low = ltf["low"].to_numpy(dtype=float)
    close = ltf["close"].to_numpy(dtype=float)
    levels: list[tuple[str, float, int]] = [
        ("swing_low", s.price, s.confirmed_index + 1) for s in swings if s.kind == "low"
    ]
    if prev_day is not None:
        levels.append(("PDL", prev_day.low, _first_index_at_or_after(ltf, prev_day.end)))
    sweeps: list[_Sweep] = []
    for kind, level, first in levels:
        breaches = np.flatnonzero(low[first:] < level)
        if not breaches.size:
            continue
        start = first + int(breaches[0])
        reclaims = np.flatnonzero(close[start : start + reclaim_bars] > level)
        if reclaims.size:
            sweeps.append(_Sweep(kind, level, start, start + int(reclaims[0])))
    # Most recent sweep first; for the same candle prefer the deepest level.
    return sorted(sweeps, key=lambda s: (-s.start, s.level))


def _buy_side_targets(
    ltf: pd.DataFrame,
    swings: list[SwingPoint],
    params: ScannerParams,
    prev_day: PreviousDay | None,
    entry: float,
    today_high: float | None,
) -> list[tuple[float, str]]:
    high = ltf["high"].to_numpy(dtype=float)

    def untaken(level: float, after: int) -> bool:
        later = high[after + 1 :]
        return not later.size or later.max() <= level

    candidates = [(s.price, "swing_high") for s in swings if s.kind == "high" and s.price > entry and untaken(s.price, s.index)]
    for pool in equal_levels(ltf, params.swing_length, params.eqh_tolerance_pct):
        if pool.kind == "EQH" and pool.price > entry and untaken(pool.price, max(pool.indices)):
            candidates.append((pool.price, "EQH"))
    # The PDH is only resting liquidity if neither the LTF window nor today's
    # HTF candle (which also covers the hours before the window) traded above it.
    pdh_open = today_high is None or today_high <= (prev_day.high if prev_day is not None else 0.0)
    if prev_day is not None and prev_day.high > entry and pdh_open:
        first_today = _first_index_at_or_after(ltf, prev_day.end)
        if untaken(prev_day.high, first_today - 1):
            candidates.append((prev_day.high, "PDH"))

    targets: list[tuple[float, str]] = []
    for price, kind in sorted(candidates, key=lambda c: (c[0], _TARGET_RANK[c[1]])):
        if targets and abs(price - targets[-1][0]) <= abs(targets[-1][0]) * params.eqh_tolerance_pct / 100.0:
            continue
        targets.append((price, kind))
    return targets[:2]


def _today_high(daily: pd.DataFrame, prev_day: PreviousDay | None, at: pd.Timestamp) -> float | None:
    """Highest price traded so far today according to the HTF frame (``None`` if unknown)."""
    if prev_day is None:
        return None
    start = prev_day.end.tz_convert(daily.index.tz)
    today = daily.loc[(daily.index >= start) & (daily.index <= at), "high"]
    return float(today.max()) if not today.empty else None


def _long_geometry(ltf: pd.DataFrame, daily: pd.DataFrame, params: ScannerParams) -> _Geometry | None:
    """The most recent valid long setup in ``ltf``, or ``None``."""
    at = ltf.index[-1]
    if daily_bias(daily, at) != "bullish":
        return None
    prev_day = previous_day_levels(daily, at)
    today_high = _today_high(daily, prev_day, at)
    swings = swing_points(ltf, params.swing_length)
    gaps = [g for g in find_fvgs(ltf) if g.kind == "bullish" and not g.filled]
    high = ltf["high"].to_numpy(dtype=float)
    low = ltf["low"].to_numpy(dtype=float)
    last = len(ltf) - 1

    for sweep in _sell_side_sweeps(ltf, swings, prev_day, params.reclaim_bars):
        # The structure to shift is the last swing high that led into the sweep,
        # not whichever minor high formed afterwards.
        lower_high = next(
            (s for s in reversed(swings) if s.kind == "high" and s.index < sweep.start and s.confirmed_index <= sweep.start),
            None,
        )
        if lower_high is None:
            continue
        mss = displacement_break(
            ltf,
            lower_high.price,
            sweep.reclaim,
            min(sweep.start + params.sweep_lookback, last),
            params.displacement_k,
            params.atr_period,
            swing_index=lower_high.index,
        )
        if mss is None or mss.index < last - params.max_setup_age:
            continue
        if params.killzones and killzone(mss.ts) not in params.killzones:
            continue
        extreme_index = sweep.start + int(np.argmin(low[sweep.start : mss.index + 1]))
        extreme = float(low[extreme_index])
        if mss.index < last and low[mss.index + 1 :].min() < extreme:
            continue
        dealing = DealingRange(high=float(high[extreme_index:].max()), low=extreme)
        leg_gaps = [
            (gap, gap.ce if params.entry == "ce" else gap.top)
            for gap in gaps
            if extreme_index < gap.index <= mss.index
        ]
        # A gap whose entry price was already traded after the gap formed has played out.
        candidates = [
            (gap, entry)
            for gap, entry in leg_gaps
            if dealing.zone(entry) == "discount" and not (low[gap.index + 2 :] <= entry).any()
        ]
        if not candidates:
            continue
        gap, entry = max(candidates, key=lambda item: item[0].index)
        stop = extreme - abs(extreme) * params.stop_buffer_pct / 100.0
        targets = _buy_side_targets(ltf, swings, params, prev_day, entry, today_high)
        return _Geometry(
            sweep_kind=sweep.kind,
            sweep_level=sweep.level,
            sweep_extreme=extreme,
            mss_ts=mss.ts,
            mss_level=mss.level,
            fvg_top=gap.top,
            fvg_bottom=gap.bottom,
            entry=entry,
            stop=stop,
            targets=tuple(price for price, _ in targets),
            target_kinds=tuple(kind for _, kind in targets),
            range_high=dealing.high,
            range_low=dealing.low,
        )
    return None


_KIND_TR = {
    "swing_low": "swing low",
    "swing_high": "swing high",
    "PDL": "PDL (önceki gün dibi)",
    "PDH": "PDH (önceki gün tepesi)",
    "EQH": "EQH",
    "EQL": "EQL",
}


def _narrative(symbol: str, timeframe: str, side: Side, geom: _Geometry, bias: str, zone_name: str | None) -> str:
    long = side is Side.LONG
    targets = ", ".join(f"{fmt(t)} ({_KIND_TR[k]})" for t, k in zip(geom.targets, geom.target_kinds)) or "yok"
    mss_local = geom.mss_ts.tz_convert(NEW_YORK).strftime("%Y-%m-%d %H:%M")
    equilibrium = (geom.range_high + geom.range_low) / 2.0
    return (
        f"{side.value.upper()} {symbol} {timeframe}: günlük bias {bias}; "
        f"{fmt(geom.sweep_level)} {_KIND_TR[geom.sweep_kind]} süpürüldü "
        f"({'dip' if long else 'tepe'} {fmt(geom.sweep_extreme)}) ve seviyenin {'üstünde' if long else 'altında'} kapanış geldi; "
        f"{fmt(geom.mss_level)} {'swing high' if long else 'swing low'} displacement ile kırıldı "
        f"(MSS {mss_local} New York, {zone_name or 'killzone dışı'}); "
        f"{'bullish' if long else 'bearish'} FVG {fmt(geom.fvg_bottom)}-{fmt(geom.fvg_top)} "
        f"{'discount' if long else 'premium'} bölgesinde (denge {fmt(equilibrium)}). "
        f"Giriş {fmt(geom.entry)}, stop {fmt(geom.stop)}, hedefler {targets}. "
        "Kural tabanlı sezgisel kurulum; garanti değildir."
    )


def _setup_digest(symbol: str, timeframe: str, side: Side, geom: _Geometry) -> str:
    key = f"{symbol}|{timeframe}|{side.value}|{geom.mss_ts.isoformat()}|{geom.sweep_level!r}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def scan_symbol(
    symbol: str,
    ltf: pd.DataFrame,
    htf: pd.DataFrame,
    *,
    timeframe: str,
    account: AccountState,
    limits: RiskLimits,
    params: ScannerParams = ScannerParams(),
    fee_rate: float = 0.0,
    market: str = "spot",
    now: datetime | None = None,
) -> list[Setup]:
    """Scan one symbol and return at most one long and one short setup candidate.

    ``ltf`` is the entry timeframe, ``htf`` provides the daily candles for the
    bias and PDH/PDL (intraday ``htf`` frames are aggregated to days). Each
    setup carries an ``OrderTicket`` sized with ``limits.risk_pct`` if the risk
    engine accepts it, otherwise the rejection reasons. The ticket's
    ``created_at`` is ``now`` (the scan time) when given, else the last candle.
    """
    _require_positive_prices(ltf, "ltf")
    _require_positive_prices(htf, "htf")
    if len(ltf) < MIN_CANDLES or htf.empty:
        return []
    at = ltf.index[-1]
    setups: list[Setup] = []
    for side in (Side.LONG, Side.SHORT):
        if side is Side.LONG:
            geom = _long_geometry(ltf, htf, params)
        else:
            mirrored = _long_geometry(_negated(ltf), _negated(htf), params)
            geom = mirrored.unmirrored() if mirrored is not None else None
        if geom is None:
            continue
        digest = _setup_digest(symbol, timeframe, side, geom)
        zone_name = killzone(geom.mss_ts)
        bias = "bullish" if side is Side.LONG else "bearish"
        narrative = _narrative(symbol, timeframe, side, geom, bias, zone_name)
        setup = Setup(
            id=f"s-{digest}",
            symbol=symbol,
            timeframe=timeframe,
            side=side,
            detected_at=at,
            bias=bias,
            sweep_kind=geom.sweep_kind,
            sweep_level=geom.sweep_level,
            sweep_extreme=geom.sweep_extreme,
            mss_at=geom.mss_ts,
            mss_level=geom.mss_level,
            fvg_top=geom.fvg_top,
            fvg_bottom=geom.fvg_bottom,
            entry=geom.entry,
            stop=geom.stop,
            targets=list(geom.targets),
            target_kinds=list(geom.target_kinds),
            range_high=geom.range_high,
            range_low=geom.range_low,
            killzone=zone_name,
            narrative=narrative,
        )
        try:
            qty = floor_to_step(
                position_size(account.equity, limits.risk_pct, geom.entry, geom.stop, fee_rate), lot_step(symbol)
            )
        except ValueError:
            qty = 0.0
        ticket = OrderTicket(
            id=f"t-{digest}",
            setup_id=setup.id,
            symbol=symbol,
            side=side,
            entry=geom.entry,
            stop=geom.stop,
            targets=list(geom.targets),
            qty=qty,
            leverage=params.leverage if market == "futures" else 1.0,
            risk_pct=limits.risk_pct,
            fee_rate=fee_rate,
            market=market,
            timeframe=timeframe,
            reason=narrative,
            created_at=now if now is not None else at.to_pydatetime(),
        )
        violations = check_ticket(ticket, account, limits)
        if violations:
            setup.reasons = [v.message for v in violations]
        else:
            setup.ticket = ticket
        setups.append(setup)
    return setups
