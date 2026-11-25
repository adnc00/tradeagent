"""Local paper-trading simulator with JSON persistence.

Fill model (deliberately conservative):

* Limit entries fill when a candle trades through the entry price, at the
  entry or at the candle open if price gapped beyond it.
* On the candle that fills the entry only the stop is checked (the order of
  highs and lows inside a candle is unknown, so the worse case is assumed).
* When one candle touches both the stop and the take-profit, the stop wins.
* A stop gapped through fills at the candle open (slippage); a take-profit
  gapped through fills at the open (price improvement).
* Fees are ``ticket.fee_rate`` on the notional of every fill.

The paper account holds one currency-agnostic equity number; there is no FX
conversion between symbols quoted in different currencies.
"""

import json
import os
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from tradeagent.brokers.base import Balance, Broker, BrokerError, Fill, OrderResult, Position
from tradeagent.models import Candle, OrderTicket, Side
from tradeagent.risk import is_positive, ticket_risk_amount

STATE_VERSION = 1
_ACTIVE = ("pending", "open")


class MarketDataSource(Protocol):
    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame: ...

    def get_price(self, symbol: str) -> float: ...


if sys.platform == "win32":
    import msvcrt

    def _try_lock(handle) -> None:  # type: ignore[no-untyped-def]
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:  # type: ignore[no-untyped-def]
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle) -> None:  # type: ignore[no-untyped-def]
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:  # type: ignore[no-untyped-def]
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def state_lock(state_path: Path, timeout: float = 10.0) -> Iterator[None]:
    """Exclusive inter-process lock on ``<state>.lock`` (msvcrt on Windows, flock on POSIX)."""
    lock_path = Path(state_path).with_name(Path(state_path).name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with open(lock_path, "a+b") as handle:
        while True:
            try:
                _try_lock(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise BrokerError(
                        f"Paper durum dosyası başka bir işlem tarafından kilitli: {state_path}. "
                        "Diğer tradeagent komutunun bitmesini bekleyin."
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(handle)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _parse(moment: str | None) -> datetime | None:
    return datetime.fromisoformat(moment) if moment is not None else None


@dataclass(slots=True)
class PaperBracket:
    """Entry + stop + take-profit simulated as one unit."""

    id: str
    ticket_id: str
    symbol: str
    side: Side
    qty: float
    entry: float
    stop: float
    take_profit: float
    leverage: float
    market: str
    fee_rate: float
    risk_amount: float
    created_at: datetime
    status: str = "pending"
    fill_price: float | None = None
    filled_at: datetime | None = None
    entry_fee: float = 0.0
    exit_price: float | None = None
    exit_reason: str | None = None
    closed_at: datetime | None = None
    pnl: float | None = None
    r: float | None = None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["side"] = self.side.value
        for key in ("created_at", "filled_at", "closed_at"):
            data[key] = _iso(getattr(self, key))
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "PaperBracket":
        values = dict(data)
        values["side"] = Side(values["side"])
        for key in ("created_at", "filled_at", "closed_at"):
            values[key] = _parse(values.get(key))
        return cls(**values)


def _fill_to_json(fill: Fill) -> dict[str, Any]:
    data = asdict(fill)
    data["ts"] = fill.ts.isoformat()
    return data


def _fill_from_json(data: dict[str, Any]) -> Fill:
    return Fill(**{**data, "ts": datetime.fromisoformat(data["ts"])})


class PaperBroker(Broker):
    """Simulated broker driven by fed candles/prices; state survives restarts."""

    name = "paper"
    environment = "yerel simülasyon"
    is_demo = True

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        initial_equity: float = 10_000.0,
        market_data: MarketDataSource | None = None,
        sync_timeframe: str = "5m",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(state_path)
        self._market_data = market_data
        self._sync_timeframe = sync_timeframe
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._initial_equity = float(initial_equity)
        self._cash = float(initial_equity)
        self._brackets: list[PaperBracket] = []
        self._fills: list[Fill] = []
        self._last_prices: dict[str, float] = {}
        self._synced_until: dict[str, datetime] = {}
        self.sync_warnings: list[str] = []
        self._reload()

    # --- persistence ---------------------------------------------------------------------

    def _reload(self) -> None:
        """Re-read the state file (writes are atomic, so a reader always sees a whole file)."""
        if self._path.exists():
            self._load()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Lock, reload what other processes wrote, mutate, save atomically, unlock."""
        with state_lock(self._path):
            self._reload()
            yield
            self._save()

    def _load(self) -> None:
        try:
            state = json.loads(self._path.read_text(encoding="utf-8"))
            if state.get("version") != STATE_VERSION:
                raise ValueError(f"desteklenmeyen sürüm {state.get('version')!r}")
            self._initial_equity = float(state["initial_equity"])
            self._cash = float(state["cash"])
            self._brackets = [PaperBracket.from_json(b) for b in state["brackets"]]
            self._fills = [_fill_from_json(f) for f in state["fills"]]
            self._last_prices = {k: float(v) for k, v in state["last_prices"].items()}
            self._synced_until = {k: datetime.fromisoformat(v) for k, v in state["synced_until"].items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise BrokerError(
                f"Paper durum dosyası okunamadı: {self._path} ({type(exc).__name__}: {exc}). "
                "Dosyayı yedekten geri yükleyin ya da silin (paper hesabı sıfırlanır)."
            ) from exc

    def _save(self) -> None:
        state = {
            "version": STATE_VERSION,
            "initial_equity": self._initial_equity,
            "cash": self._cash,
            "brackets": [b.to_json() for b in self._brackets],
            "fills": [_fill_to_json(f) for f in self._fills],
            "last_prices": self._last_prices,
            "synced_until": {k: v.isoformat() for k, v in self._synced_until.items()},
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    # --- read-only views -------------------------------------------------------------------

    @property
    def brackets(self) -> list[PaperBracket]:
        self._reload()
        return [replace(b) for b in self._brackets]

    @property
    def fills(self) -> list[Fill]:
        self._reload()
        return list(self._fills)

    def get_price(self, symbol: str) -> float:
        self._reload()
        if self._market_data is not None:
            price = float(self._market_data.get_price(symbol))
            self._last_prices[symbol] = price
            return price
        if symbol in self._last_prices:
            return self._last_prices[symbol]
        raise BrokerError(f"{symbol} için paper fiyatı yok: fiyat beslenmedi ve veri kaynağı kapalı.")

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame:
        if self._market_data is None:
            raise BrokerError("Paper broker'da veri kaynağı kapalı (paper.sync: false).")
        return self._market_data.get_ohlcv(symbol, timeframe, limit)

    def _unrealized(self, bracket: PaperBracket) -> float:
        if bracket.status != "open" or bracket.fill_price is None:
            return 0.0
        last = self._last_prices.get(bracket.symbol, bracket.fill_price)
        return (last - bracket.fill_price) * bracket.qty * bracket.side.sign

    def get_balance(self) -> Balance:
        self._reload()
        equity = self._cash + sum(self._unrealized(b) for b in self._brackets)
        margin = sum(
            b.qty * (b.fill_price if b.fill_price is not None else b.entry) / b.leverage
            for b in self._brackets
            if b.status in _ACTIVE
        )
        return Balance(equity=equity, free=equity - margin, currency="PAPER")

    def get_positions(self) -> list[Position]:
        self._reload()
        return [
            Position(
                symbol=b.symbol,
                side=b.side,
                qty=b.qty,
                entry_price=b.fill_price,
                stop=b.stop,
                take_profit=b.take_profit,
                unrealized_pnl=self._unrealized(b),
                ticket_id=b.ticket_id,
            )
            for b in self._brackets
            if b.status == "open"
        ]

    def get_open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        self._reload()
        orders: list[OrderResult] = []
        for b in self._brackets:
            if symbol is not None and b.symbol != symbol:
                continue
            if b.status == "pending":
                orders.append(self._order(b, "entry", "pending"))
            elif b.status == "open":
                orders.append(self._order(b, "stop", "working"))
                orders.append(self._order(b, "take_profit", "working"))
        return orders

    @staticmethod
    def _order(bracket: PaperBracket, role: str, status: str) -> OrderResult:
        if role == "entry":
            return OrderResult(
                f"{bracket.id}-entry", bracket.symbol, bracket.side.entry_action, "entry", "limit",
                bracket.qty, bracket.entry, status,
            )
        if role == "stop":
            return OrderResult(
                f"{bracket.id}-stop", bracket.symbol, bracket.side.exit_action, "stop", "stop_market",
                bracket.qty, bracket.stop, status,
            )
        return OrderResult(
            f"{bracket.id}-tp", bracket.symbol, bracket.side.exit_action, "take_profit", "limit",
            bracket.qty, bracket.take_profit, status,
        )

    # --- order entry -----------------------------------------------------------------------

    def place_bracket(self, ticket: OrderTicket) -> list[OrderResult]:
        stop, take_profit = ticket.stop, ticket.take_profit
        if stop is None or not is_positive(stop):
            raise BrokerError("Stop olmadan emir verilmez (paper).")
        if take_profit is None or not is_positive(take_profit):
            raise BrokerError("Take-profit (hedef 1) olmadan bracket kurulmaz (paper).")
        if not is_positive(ticket.qty) or not is_positive(ticket.entry):
            raise BrokerError("Miktar ve giriş fiyatı sıfırdan büyük olmalı (paper).")
        sign = ticket.side.sign
        if (ticket.entry - stop) * sign <= 0:
            raise BrokerError("Stop yanlış tarafta; bracket kurulmadı (paper).")
        if (take_profit - ticket.entry) * sign <= 0:
            raise BrokerError("Hedef yanlış tarafta; bracket kurulmadı (paper).")
        if ticket.side is Side.SHORT and ticket.market == "spot":
            raise BrokerError("Spot piyasada short açılamaz (paper, spot olarak yapılandırılmış).")
        bracket = PaperBracket(
            id=f"p-{uuid.uuid4().hex[:8]}",
            ticket_id=ticket.id,
            symbol=ticket.symbol,
            side=ticket.side,
            qty=float(ticket.qty),
            entry=float(ticket.entry),
            stop=float(stop),
            take_profit=float(take_profit),
            leverage=float(ticket.leverage),
            market=ticket.market,
            fee_rate=float(ticket.fee_rate),
            risk_amount=ticket_risk_amount(ticket),
            created_at=self._clock(),
        )
        with self._transaction():
            self._brackets.append(bracket)
        return [
            self._order(bracket, "entry", "pending"),
            self._order(bracket, "stop", "attached"),
            self._order(bracket, "take_profit", "attached"),
        ]

    def cancel_all(self) -> int:
        return self._cancel(None)

    def cancel_orders(self, symbol: str) -> int:
        return self._cancel(symbol)

    def _cancel(self, symbol: str | None) -> int:
        now = self._clock()
        cancelled = 0
        with self._transaction():
            for b in self._brackets:
                if b.status == "pending" and (symbol is None or b.symbol == symbol):
                    b.status, b.closed_at = "canceled", now
                    cancelled += 1
        return cancelled

    def close_all(self) -> list[OrderResult]:
        now = self._clock()
        results: list[OrderResult] = []
        with self._transaction():
            for b in self._brackets:
                if b.status != "open":
                    continue
                price, note = self._close_price(b)
                self._exit(b, price, "close", now)
                results.append(
                    OrderResult(
                        f"{b.id}-close", b.symbol, b.side.exit_action, "close", "market", b.qty, price, "filled", note
                    )
                )
        return results

    def _close_price(self, bracket: PaperBracket) -> tuple[float, str]:
        if bracket.symbol in self._last_prices:
            return self._last_prices[bracket.symbol], ""
        fallback = bracket.fill_price if bracket.fill_price is not None else bracket.entry
        if self._market_data is not None:
            try:
                return float(self._market_data.get_price(bracket.symbol)), ""
            except Exception as exc:  # the kill switch must close even when data is down
                return fallback, f"fiyat alınamadı ({exc}); giriş fiyatından kapatıldı"
        return fallback, "fiyat bilinmiyor; giriş fiyatından kapatıldı"

    # --- simulation ------------------------------------------------------------------------

    def feed_price(self, symbol: str, price: float, ts: datetime | None = None) -> list[Fill]:
        """Feed one traded price (treated as a flat candle)."""
        moment = ts if ts is not None else self._clock()
        return self.feed_candle(symbol, Candle(ts=moment, open=price, high=price, low=price, close=price))

    def feed_candle(self, symbol: str, candle: Candle) -> list[Fill]:
        """Process one candle for ``symbol`` and return the fills it produced."""
        with self._transaction():
            fills = self._process(symbol, candle)
            self._last_prices[symbol] = float(candle.close)
        return fills

    def sync(self) -> list[Fill]:
        """Replay closed candles from the market-data source since each order was placed.

        A bracket only sees candles that opened after it was placed, the
        still-forming last candle is skipped and each candle is processed at
        most once. If the data does not reach back far enough, a warning is
        added to ``sync_warnings``.
        """
        self.sync_warnings = []
        if self._market_data is None:
            return []
        fills: list[Fill] = []
        with self._transaction():
            for symbol in sorted({b.symbol for b in self._brackets if b.status in _ACTIVE}):
                fills.extend(self._sync_symbol(symbol, self._market_data))
        return fills

    def _sync_symbol(self, symbol: str, market_data: MarketDataSource) -> list[Fill]:
        df = market_data.get_ohlcv(symbol, self._sync_timeframe, 500)
        if df.empty:
            return []
        fills: list[Fill] = []
        earliest = min(b.created_at for b in self._brackets if b.symbol == symbol and b.status in _ACTIVE)
        done_until = self._synced_until.get(symbol)
        reference = max(earliest, done_until) if done_until is not None else earliest
        if pd.Timestamp(df.index[0]).to_pydatetime() > reference:
            self.sync_warnings.append(
                f"{symbol}: veri {df.index[0]} tarihinden başlıyor, {reference} sonrası kısmen simüle edilemedi."
            )
        for ts, row in df.iloc[:-1].iterrows():
            moment = pd.Timestamp(ts).to_pydatetime()
            if moment < earliest or (done_until is not None and moment <= done_until):
                continue
            candle = Candle(
                ts=moment,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
            )
            fills.extend(self._process(symbol, candle, only_after_placement=True))
            self._synced_until[symbol] = moment
        self._last_prices[symbol] = float(df["close"].iloc[-1])
        return fills

    def _process(self, symbol: str, candle: Candle, only_after_placement: bool = False) -> list[Fill]:
        fills: list[Fill] = []
        for b in self._brackets:
            if b.symbol != symbol or b.status not in _ACTIVE:
                continue
            if only_after_placement and candle.ts < b.created_at:
                continue
            long = b.side is Side.LONG
            stop_hit = candle.low <= b.stop if long else candle.high >= b.stop
            if b.status == "pending":
                touched = candle.low <= b.entry if long else candle.high >= b.entry
                if not touched:
                    continue
                price = min(b.entry, candle.open) if long else max(b.entry, candle.open)
                fills.append(self._fill_entry(b, price, candle.ts))
                if stop_hit:
                    exit_price = min(b.stop, price) if long else max(b.stop, price)
                    fills.append(self._exit(b, exit_price, "stop", candle.ts))
                continue
            target_hit = candle.high >= b.take_profit if long else candle.low <= b.take_profit
            if stop_hit:
                price = min(b.stop, candle.open) if long else max(b.stop, candle.open)
                fills.append(self._exit(b, price, "stop", candle.ts))
            elif target_hit:
                price = max(b.take_profit, candle.open) if long else min(b.take_profit, candle.open)
                fills.append(self._exit(b, price, "take_profit", candle.ts))
        return fills

    def _fill_entry(self, bracket: PaperBracket, price: float, ts: datetime) -> Fill:
        fee = bracket.qty * price * bracket.fee_rate
        self._cash -= fee
        bracket.status, bracket.fill_price, bracket.filled_at, bracket.entry_fee = "open", price, ts, fee
        fill = Fill(
            id=f"{bracket.id}-entry",
            order_id=f"{bracket.id}-entry",
            ticket_id=bracket.ticket_id,
            ts=ts,
            symbol=bracket.symbol,
            side=bracket.side.entry_action,
            kind="entry",
            qty=bracket.qty,
            price=price,
            fee=fee,
        )
        self._fills.append(fill)
        return fill

    def _exit(self, bracket: PaperBracket, price: float, reason: str, ts: datetime) -> Fill:
        entry_price = bracket.fill_price if bracket.fill_price is not None else bracket.entry
        fee = bracket.qty * price * bracket.fee_rate
        gross = (price - entry_price) * bracket.qty * bracket.side.sign
        self._cash += gross - fee
        pnl = gross - bracket.entry_fee - fee
        r_multiple = pnl / bracket.risk_amount if bracket.risk_amount > 0 else None
        bracket.status, bracket.exit_price, bracket.exit_reason = "closed", price, reason
        bracket.closed_at, bracket.pnl, bracket.r = ts, pnl, r_multiple
        order_suffix = {"stop": "stop", "take_profit": "tp", "close": "close"}[reason]
        fill = Fill(
            id=f"{bracket.id}-exit",
            order_id=f"{bracket.id}-{order_suffix}",
            ticket_id=bracket.ticket_id,
            ts=ts,
            symbol=bracket.symbol,
            side=bracket.side.exit_action,
            kind=reason,
            qty=bracket.qty,
            price=price,
            fee=fee,
            pnl=pnl,
            r=r_multiple,
        )
        self._fills.append(fill)
        return fill
