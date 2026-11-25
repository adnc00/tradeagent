"""Core domain models: candles, setups, order tickets, risk limits and account state."""

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

# Venue kinds a ticket can be routed to:
#   spot    - cash market (crypto spot, BIST): no short selling, no leverage, no liquidation
#   futures - linear perpetuals/futures: short allowed, leverage, liquidation price
#   equity  - US stocks via a margin broker (Alpaca): short allowed subject to the broker
MARKET_TYPES = ("spot", "futures", "equity")


# Accepted ranges for every risk limit (inclusive). Outside them the config is refused.
RISK_LIMIT_RANGES: dict[str, tuple[float, float]] = {
    "risk_pct": (0.01, 5.0),
    "max_risk_pct": (0.01, 5.0),
    "daily_loss_pct": (0.1, 20.0),
    "weekly_loss_pct": (0.1, 50.0),
    "max_open_risk_pct": (0.01, 50.0),
    "max_leverage": (1.0, 125.0),
    "min_rr": (0.1, 20.0),
    "mmr": (0.0, 0.5),
    "liq_stop_multiple": (1.0, 20.0),
}


class Side(StrEnum):
    """Trade direction."""

    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        """+1 for long, -1 for short; multiply a price move by it to get P&L per unit."""
        return 1 if self is Side.LONG else -1

    @property
    def entry_action(self) -> str:
        """Order side that opens the position."""
        return "buy" if self is Side.LONG else "sell"

    @property
    def exit_action(self) -> str:
        """Order side that closes the position."""
        return "sell" if self is Side.LONG else "buy"


@dataclass(frozen=True, slots=True)
class Candle:
    """One OHLCV bar; ``ts`` is the bar's open time and must be tz-aware."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Account-level risk rules. Percentages are of account equity."""

    risk_pct: float = 0.5
    max_risk_pct: float = 1.0
    daily_loss_pct: float = 2.0
    weekly_loss_pct: float = 5.0
    max_open_risk_pct: float = 3.0
    max_leverage: float = 5.0
    min_rr: float = 2.0
    mmr: float = 0.01
    liq_stop_multiple: float = 2.0
    allowlist: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowlist", tuple(self.allowlist))
        for name, (low, high) in RISK_LIMIT_RANGES.items():
            value = getattr(self, name)
            if not (isinstance(value, (int, float)) and math.isfinite(value) and low <= value <= high):
                raise ValueError(f"risk.{name} {low} ile {high} arasında olmalı (verilen {value!r})")
        if self.risk_pct > self.max_risk_pct:
            raise ValueError("risk.risk_pct, risk.max_risk_pct değerini aşamaz")
        if self.max_risk_pct > self.max_open_risk_pct:
            raise ValueError("risk.max_risk_pct, risk.max_open_risk_pct değerini aşamaz")
        if self.daily_loss_pct > self.weekly_loss_pct:
            raise ValueError("risk.daily_loss_pct, risk.weekly_loss_pct değerini aşamaz")


@dataclass(frozen=True, slots=True)
class OpenRisk:
    """Money at risk (to the stop) of a position or working order already on the book."""

    symbol: str
    side: Side
    amount: float


@dataclass(frozen=True, slots=True)
class AccountState:
    """Snapshot the risk engine checks against.

    ``day_start_equity`` / ``week_start_equity`` default to ``equity`` (no loss
    so far in the period) when unknown.
    """

    equity: float
    day_start_equity: float | None = None
    week_start_equity: float | None = None
    open_risks: tuple[OpenRisk, ...] = ()


@dataclass(slots=True)
class OrderTicket:
    """A fully specified order proposal: limit entry, protective stop and targets.

    ``targets[0]`` is the take-profit that is actually placed; further targets
    are informational. ``risk_pct`` is the intended risk used for sizing;
    the effective risk is always recomputed from qty/entry/stop/fees.
    """

    id: str
    setup_id: str
    symbol: str
    side: Side
    entry: float
    stop: float | None
    targets: list[float]
    qty: float
    leverage: float = 1.0
    risk_pct: float = 0.0
    fee_rate: float = 0.0
    market: str = "spot"
    timeframe: str = ""
    reason: str = ""
    created_at: datetime | None = None
    status: str = "new"

    def __post_init__(self) -> None:
        self.side = Side(self.side)
        if self.market not in MARKET_TYPES:
            raise ValueError(f"unknown market type {self.market!r}; expected one of {MARKET_TYPES}")
        self.targets = [float(t) for t in self.targets]

    @property
    def take_profit(self) -> float | None:
        return self.targets[0] if self.targets else None

    @property
    def notional(self) -> float:
        return self.qty * self.entry


@dataclass(slots=True)
class Setup:
    """A detected ICT setup candidate and, if the risk engine accepted it, its ticket.

    ``reasons`` holds the risk engine's rejection messages; it is empty when
    ``ticket`` is set.
    """

    id: str
    symbol: str
    timeframe: str
    side: Side
    detected_at: datetime
    bias: str
    sweep_kind: str
    sweep_level: float
    sweep_extreme: float
    mss_at: datetime
    mss_level: float
    fvg_top: float
    fvg_bottom: float
    entry: float
    stop: float
    targets: list[float]
    target_kinds: list[str]
    range_high: float
    range_low: float
    killzone: str | None
    narrative: str = ""
    reasons: list[str] = field(default_factory=list)
    ticket: OrderTicket | None = None

    def __post_init__(self) -> None:
        self.side = Side(self.side)
