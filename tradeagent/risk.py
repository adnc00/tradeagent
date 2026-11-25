"""Pure risk functions: sizing, liquidation estimate, R:R and pre-trade checks.

Formulas follow risk-portfolio.md (sections 1-4 and 8) of the trading-smc-ict
skill:

* qty = risk amount / (|entry - stop| + 2 * fee_rate * entry)  (round-trip fees)
* isolated linear perp liquidation: long entry * (1 - 1/L + mmr),
  short entry * (1 + 1/L - mmr)
* correlated positions count as one position.
"""

import fnmatch
import math
import numbers
from dataclasses import dataclass

from tradeagent.models import AccountState, OrderTicket, RiskLimits, Side

_PCT_TOL = 1e-9
_USD_QUOTES = frozenset({"USDT", "USDC", "USD", "BUSD", "FDUSD", "TUSD", "DAI"})


@dataclass(frozen=True, slots=True)
class RiskViolation:
    """One reason the risk engine refuses a ticket (``message`` is user-facing Turkish)."""

    code: str
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class TicketMetrics:
    """Derived numbers shown on the confirmation screen and stored in the journal."""

    notional: float
    risk_amount: float
    risk_pct: float | None
    rr: float | None
    liquidation: float | None
    margin: float


def _as_float(value: object) -> float | None:
    """``float(value)`` for real numbers (numpy scalars included); ``None`` otherwise."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    return float(value)


def _finite(value: object) -> bool:
    number = _as_float(value)
    return number is not None and math.isfinite(number)


def is_positive(value: object) -> bool:
    """True for a finite real number > 0 (NaN, inf, None, bool and strings are rejected)."""
    number = _as_float(value)
    return number is not None and math.isfinite(number) and number > 0


MAX_FEE_RATE = 0.01


def valid_fee_rate(value: object) -> bool:
    """Fee rate per fill: a finite number in [0, MAX_FEE_RATE]."""
    number = _as_float(value)
    return number is not None and math.isfinite(number) and 0 <= number <= MAX_FEE_RATE


def fmt(value: float) -> str:
    """Compact number formatting for messages (8 significant digits, thousands separator)."""
    return f"{value:,.8g}"


def position_size(equity: float, risk_pct: float, entry: float, stop: float, fee_rate: float = 0.0) -> float:
    """Units to buy/sell so that a stop-out costs ``risk_pct`` % of ``equity``.

    The risk includes round-trip fees on the entry notional
    (``2 * fee_rate * entry`` per unit). Raises ``ValueError`` for degenerate
    input (non-positive or non-finite numbers, entry == stop, negative fees).
    """
    for name, value in (("özsermaye", equity), ("risk %", risk_pct), ("giriş", entry), ("stop", stop)):
        if not is_positive(value):
            raise ValueError(f"{name} pozitif ve sonlu bir sayı olmalı (verilen {value!r})")
    if not valid_fee_rate(fee_rate):
        raise ValueError(f"komisyon oranı 0 ile {MAX_FEE_RATE} arasında olmalı (verilen {fee_rate!r})")
    if entry == stop:
        raise ValueError("giriş ve stop farklı olmalı")
    risk_amount = equity * risk_pct / 100.0
    per_unit = abs(entry - stop) + 2.0 * fee_rate * entry
    return risk_amount / per_unit


def floor_to_step(qty: float, step: float | None) -> float:
    """Round ``qty`` down to a multiple of ``step`` (never up, so risk never grows)."""
    if step is None:
        return qty
    if not is_positive(step):
        raise ValueError(f"adım pozitif olmalı (verilen {step!r})")
    units = math.floor(qty / step + 1e-9)
    return round(units * step, 12)


def liquidation_price(entry: float, leverage: float, side: Side | str, mmr: float) -> float:
    """Approximate liquidation price of an isolated-margin linear perpetual position."""
    if not is_positive(entry):
        raise ValueError(f"giriş pozitif ve sonlu bir sayı olmalı (verilen {entry!r})")
    if not _finite(leverage) or leverage < 1:
        raise ValueError(f"kaldıraç en az 1 olmalı (verilen {leverage!r})")
    if not _finite(mmr) or not 0 <= mmr < 1:
        raise ValueError(f"mmr 0 ile 1 arasında olmalı (verilen {mmr!r})")
    if Side(side) is Side.LONG:
        return entry * (1 - 1 / leverage + mmr)
    return entry * (1 + 1 / leverage - mmr)


def rr(entry: float, stop: float, target: float) -> float:
    """Reward-to-risk ratio; negative when the target is on the stop's side."""
    for name, value in (("giriş", entry), ("stop", stop), ("hedef", target)):
        if not is_positive(value):
            raise ValueError(f"{name} pozitif ve sonlu bir sayı olmalı (verilen {value!r})")
    if entry == stop:
        raise ValueError("giriş ve stop farklı olmalı")
    direction = 1.0 if stop < entry else -1.0
    return (target - entry) * direction / abs(entry - stop)


def rr_after_fees(entry: float, stop: float, target: float, fee_rate: float) -> float:
    """R:R net of fees: the R actually realised if the target fills.

    Reward = |target - entry| minus fees on both legs (entry and target notional);
    risk = |entry - stop| plus round-trip fees, the same risk ``position_size`` uses.
    """
    rr(entry, stop, target)  # validates the prices
    if not valid_fee_rate(fee_rate):
        raise ValueError(f"komisyon oranı 0 ile {MAX_FEE_RATE} arasında olmalı (verilen {fee_rate!r})")
    direction = 1.0 if stop < entry else -1.0
    reward = (target - entry) * direction - fee_rate * (entry + target)
    risk = abs(entry - stop) + 2.0 * fee_rate * entry
    return reward / risk


def ticket_risk_amount(ticket: OrderTicket) -> float:
    """Money lost if the ticket is stopped out, round-trip fees included."""
    if ticket.stop is None:
        raise ValueError("ticket has no stop")
    return ticket.qty * abs(ticket.entry - ticket.stop) + 2.0 * ticket.fee_rate * ticket.qty * ticket.entry


def ticket_metrics(ticket: OrderTicket, equity: float | None, mmr: float) -> TicketMetrics:
    """Notional, risk, R:R, liquidation and margin for display and journaling."""
    risk_amount = ticket_risk_amount(ticket)
    risk_pct = risk_amount / equity * 100.0 if is_positive(equity) else None
    take_profit = ticket.take_profit
    ratio = (
        rr_after_fees(ticket.entry, ticket.stop, take_profit, ticket.fee_rate)
        if ticket.stop is not None and take_profit is not None
        else None
    )
    liquidation = (
        liquidation_price(ticket.entry, ticket.leverage, ticket.side, mmr) if ticket.market == "futures" else None
    )
    notional = ticket.notional
    return TicketMetrics(
        notional=notional,
        risk_amount=risk_amount,
        risk_pct=risk_pct,
        rr=ratio,
        liquidation=liquidation,
        margin=notional / ticket.leverage,
    )


def asset_group(symbol: str) -> str:
    """Correlation bucket of a symbol: USD-quoted crypto share one bucket per quote family."""
    upper = symbol.upper()
    if upper.endswith(".IS"):
        return "bist"
    if "/" in upper:
        quote = upper.split("/", 1)[1].split(":", 1)[0]
        return "crypto" if quote in _USD_QUOTES else f"crypto-{quote.lower()}"
    return "us-equity"


def lot_step(symbol: str) -> float | None:
    """Quantity increment used for sizing: whole shares for stocks, unrestricted for crypto.

    Live crypto brokers additionally round to the exchange's precision before
    the confirmation screen.
    """
    return 1.0 if asset_group(symbol) in ("bist", "us-equity") else None


def correlation_group(symbol: str, side: Side | str) -> str:
    """Group whose open risk is summed as if it were a single position."""
    return f"{asset_group(symbol)}:{Side(side).value}"


def _loss_pct(start_equity: float | None, equity: float) -> float:
    start = _as_float(start_equity)
    if start is None or not math.isfinite(start) or start <= 0:
        return 0.0
    return max(0.0, (start - equity) / start * 100.0)


def auto_leverage(qty: float, entry: float, equity: float, leverage: float, limits: RiskLimits) -> float:
    """Leverage for a futures position so its margin stays within ``limits.margin_per_trade_pct``
    of equity: at least ``leverage``, at most ``limits.max_leverage``, a whole number."""
    share = limits.margin_per_trade_pct
    if share <= 0 or qty <= 0 or entry <= 0 or equity <= 0:
        return leverage
    needed = math.ceil(qty * entry / (equity * share / 100.0) - 1e-9)
    return float(math.floor(min(max(leverage, needed), limits.max_leverage)))


def symbol_allowed(symbol: str, allowlist: tuple[str, ...] | list[str]) -> bool:
    """Exact (case-insensitive) match or a shell-style pattern, e.g. ``*/USDT:USDT`` = every USDT-M perpetual."""
    upper = symbol.upper()
    return any(fnmatch.fnmatchcase(upper, entry.strip().upper()) for entry in allowlist if entry.strip())


def check_ticket(ticket: OrderTicket, account_state: AccountState, limits: RiskLimits) -> list[RiskViolation]:
    """Return every reason the ticket must not be sent; an empty list means it passes.

    Checks: allowlist, valid prices, stop present and on the correct side,
    no shorts on spot, valid quantity and leverage (leverage only on
    futures, <= max), per-trade risk incl. fees, daily and weekly loss limits,
    total open risk, correlated-group risk (a group counts as one position, so
    it is capped at ``max_risk_pct``), liquidation distance >= ``liq_stop_multiple``
    x stop distance (futures only) and minimum R:R to the first target.
    """
    violations: list[RiskViolation] = []

    def reject(code: str, message: str) -> None:
        violations.append(RiskViolation(code, message))

    if not symbol_allowed(ticket.symbol, limits.allowlist):
        reject("symbol_not_allowed", f"{ticket.symbol} izin listesinde (allowlist) değil.")

    entry_ok = is_positive(ticket.entry)
    if not entry_ok:
        reject("invalid_price", f"Giriş fiyatı geçersiz: {ticket.entry!r} (pozitif ve sonlu olmalı).")

    raw_stop = _as_float(ticket.stop)
    stop_ok = False
    if raw_stop is None or math.isnan(raw_stop):
        reject("missing_stop", "Stop seviyesi yok: stopsuz emir gönderilmez.")
    elif not is_positive(raw_stop):
        reject("invalid_price", f"Stop fiyatı geçersiz: {ticket.stop!r} (pozitif ve sonlu olmalı).")
    else:
        stop_ok = True
    stop = raw_stop if stop_ok and raw_stop is not None else math.nan

    bad_targets = [t for t in ticket.targets if not is_positive(t)]
    if bad_targets:
        reject("invalid_price", f"Hedef fiyatı geçersiz: {bad_targets} (pozitif ve sonlu olmalı).")

    geometry_ok = entry_ok and stop_ok
    if geometry_ok and (ticket.entry - stop) * ticket.side.sign <= 0:
        reject(
            "stop_wrong_side",
            f"Stop yanlış tarafta: long'da girişin altında, short'ta üstünde olmalı "
            f"(giriş {fmt(ticket.entry)}, stop {fmt(stop)}).",
        )
        geometry_ok = False

    if ticket.side is Side.SHORT and ticket.market == "spot":
        reject("short_not_supported", "Spot piyasada açığa satış (short) yapılamaz; short için futures piyasası gerekir.")

    qty_ok = is_positive(ticket.qty)
    if not qty_ok:
        reject("invalid_qty", f"Miktar geçersiz: {ticket.qty!r} (sıfırdan büyük olmalı).")

    leverage_ok = _finite(ticket.leverage) and ticket.leverage >= 1
    if not leverage_ok:
        reject("invalid_leverage", f"Kaldıraç geçersiz: {ticket.leverage!r} (en az 1x olmalı).")
    else:
        if ticket.market != "futures" and ticket.leverage != 1:
            reject(
                "leverage_not_supported",
                f"{ticket.market} piyasasında kaldıraç kullanılmaz (1x olmalı, bilet {fmt(ticket.leverage)}x).",
            )
        if ticket.leverage > limits.max_leverage:
            reject(
                "leverage_too_high",
                f"Kaldıraç {fmt(ticket.leverage)}x, üst sınır {fmt(limits.max_leverage)}x.",
            )

    fee_ok = valid_fee_rate(ticket.fee_rate)
    if not fee_ok:
        reject("invalid_fee", f"Komisyon oranı geçersiz: {ticket.fee_rate!r} (0 ile {MAX_FEE_RATE} arasında olmalı).")

    equity = account_state.equity
    equity_ok = is_positive(equity)
    if not equity_ok:
        reject("no_equity", f"Hesap özsermayesi geçersiz: {equity!r}.")
    else:
        daily_loss = _loss_pct(account_state.day_start_equity, equity)
        if daily_loss >= limits.daily_loss_pct - _PCT_TOL:
            reject(
                "daily_loss_limit",
                f"Günlük kayıp limiti doldu: bugün %{daily_loss:.2f} kayıp (limit %{limits.daily_loss_pct:.2f}). "
                "Bugün yeni işlem yok.",
            )
        weekly_loss = _loss_pct(account_state.week_start_equity, equity)
        if weekly_loss >= limits.weekly_loss_pct - _PCT_TOL:
            reject(
                "weekly_loss_limit",
                f"Haftalık kayıp limiti doldu: bu hafta %{weekly_loss:.2f} kayıp "
                f"(limit %{limits.weekly_loss_pct:.2f}). Hafta kapandı; journal'ı inceleyin.",
            )

    if geometry_ok and qty_ok and equity_ok and leverage_ok:
        margin = ticket.qty * ticket.entry / ticket.leverage
        if margin > equity:
            reject(
                "insufficient_margin",
                f"Pozisyon için gereken teminat {fmt(margin)} özsermayeyi ({fmt(equity)}) aşıyor "
                f"(pozisyon değeri {fmt(ticket.qty * ticket.entry)}, kaldıraç {fmt(ticket.leverage)}x).",
            )

    if geometry_ok and qty_ok and equity_ok and fee_ok:
        risk_pct = ticket_risk_amount(ticket) / equity * 100.0
        if risk_pct > limits.max_risk_pct + _PCT_TOL:
            reject(
                "risk_too_high",
                f"İşlem riski %{risk_pct:.2f} (komisyon dahil), üst sınır %{limits.max_risk_pct:.2f}.",
            )
        open_pct = sum(r.amount for r in account_state.open_risks) / equity * 100.0
        if open_pct + risk_pct > limits.max_open_risk_pct + _PCT_TOL:
            reject(
                "open_risk_limit",
                f"Toplam açık risk %{open_pct:.2f} + yeni %{risk_pct:.2f} = %{open_pct + risk_pct:.2f}, "
                f"sınır %{limits.max_open_risk_pct:.2f}.",
            )
        group = correlation_group(ticket.symbol, ticket.side)
        group_pct = (
            sum(r.amount for r in account_state.open_risks if correlation_group(r.symbol, r.side) == group)
            / equity
            * 100.0
        )
        if group_pct > 0 and group_pct + risk_pct > limits.group_cap_pct + _PCT_TOL:
            reject(
                "correlated_risk_limit",
                f"Korelasyonlu grup '{group}' açık riski %{group_pct:.2f} + yeni %{risk_pct:.2f} = "
                f"%{group_pct + risk_pct:.2f}; korelasyonlu grubun toplam riski sınırlı "
                f"(sınır %{limits.group_cap_pct:.2f}).",
            )

    if ticket.market == "futures" and geometry_ok and leverage_ok:
        liquidation = liquidation_price(ticket.entry, ticket.leverage, ticket.side, limits.mmr)
        # Signed: a liquidation price on the wrong side of the entry gives a negative distance.
        liq_distance = (ticket.entry - liquidation) * ticket.side.sign
        stop_distance = abs(ticket.entry - stop)
        if liq_distance < limits.liq_stop_multiple * stop_distance:
            reject(
                "liquidation_too_close",
                f"Likidasyon ({fmt(liquidation)}) girişe çok yakın: mesafe {fmt(liq_distance)}, "
                f"stop mesafesinin {fmt(limits.liq_stop_multiple)} katı ({fmt(limits.liq_stop_multiple * stop_distance)}) "
                "gerekir. Kaldıracı düşürün.",
            )

    if not ticket.targets:
        reject("missing_target", "Hedef (take-profit) yok; R:R hesaplanamaz.")
    elif geometry_ok and not bad_targets and fee_ok:
        ratio = rr_after_fees(ticket.entry, stop, ticket.targets[0], ticket.fee_rate)
        if ratio < limits.min_rr - _PCT_TOL:
            reject(
                "rr_too_low",
                f"R:R {ratio:.2f} (hedef 1'e göre, komisyon dahil), en az {limits.min_rr:.2f} olmalı.",
            )

    return violations
