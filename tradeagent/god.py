"""God mode: the agent trades the allocated capital on its own until the goal is met.

Switched on and off from the panel (never by a flag, an environment variable or a
config value). While it is on, ``watch`` sends every ticket that passes the hard
rules without waiting for approval, on demo and on real money. Three guards stay:

* every position has its stop (the bracket always places it first);
* only the allocated capital (``goal.start_capital`` plus its realized result) is
  traded, never more than the account's real balance;
* a floor under the allocated equity (realized + open P&L):
  - before the target: the bankruptcy brake, ``start * (1 - brake_pct)``;
  - after the target: a profit lock, ``max(target * (1 - tolerance), peak * (1 - trail))``,
    which only ever rises.
  Equity at or below the floor closes every position, cancels the orders and
  switches god mode off; so does reaching the optional stretch target.
"""

import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATE_FILE = "god_mode.json"


@dataclass(frozen=True, slots=True)
class GodConfig:
    brake_pct: float = 50.0
    post_target_tolerance_pct: float = 10.0
    trail_pct: float = 12.0
    stretch_target: float = 0.0

    def __post_init__(self) -> None:
        checks = (
            ("brake_pct", 1.0, 95.0),
            ("post_target_tolerance_pct", 0.0, 50.0),
            ("trail_pct", 1.0, 50.0),
        )
        for name, low, high in checks:
            value = getattr(self, name)
            if not (math.isfinite(value) and low <= value <= high):
                raise ValueError(f"{name} {low:g} ile {high:g} arasında olmalı (verilen {value!r})")
        if not (math.isfinite(self.stretch_target) and self.stretch_target >= 0):
            raise ValueError("stretch_target 0 (kapalı) veya pozitif olmalı")


@dataclass(frozen=True, slots=True)
class GodState:
    on: bool = False
    account: str | None = None
    real_money: bool = False
    start_capital: float = 0.0
    target: float = 0.0
    peak: float = 0.0
    floor: float = 0.0
    target_reached: bool = False
    since: str | None = None
    stopped_reason: str | None = None
    updated_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def update(state: GodState, equity: float, settings: GodConfig) -> GodState:
    """New peak and floor after seeing ``equity``; the profit-lock floor never goes down."""
    peak = max(state.peak, equity)
    reached = state.target_reached or (state.target > 0 and peak >= state.target)
    if reached:
        floor = max(
            state.target * (1.0 - settings.post_target_tolerance_pct / 100.0),
            peak * (1.0 - settings.trail_pct / 100.0),
        )
        if state.target_reached:
            floor = max(floor, state.floor)
    else:
        floor = state.start_capital * (1.0 - settings.brake_pct / 100.0)
    return replace(state, peak=peak, floor=floor, target_reached=reached,
                   updated_at=datetime.now(UTC).isoformat())


def verdict(state: GodState, equity: float, settings: GodConfig) -> str | None:
    """``stretch`` (stretch target reached), ``lock`` (profit lock hit), ``brake`` (bankruptcy brake) or None."""
    if settings.stretch_target and equity >= settings.stretch_target:
        return "stretch"
    if equity <= state.floor:
        return "lock" if state.target_reached else "brake"
    return None


def _path(cfg) -> Path:
    return Path(cfg.data_dir) / STATE_FILE


def load(cfg) -> GodState:
    try:
        data = json.loads(_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return GodState()
    known = {k: v for k, v in data.items() if k in GodState.__dataclass_fields__}
    return GodState(**known)


def save(cfg, state: GodState) -> None:
    path = _path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state.as_dict(), ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
