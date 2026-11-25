"""Broker interface shared by the paper simulator and the live connectors."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from tradeagent.models import OrderTicket, Side


class BrokerError(RuntimeError):
    """A broker refused or failed an operation. The message is user-facing (Turkish).

    Raised only when nothing is left live at the venue (or what was left is
    stated in the message); see ``OrderStateUnknown`` otherwise.
    """


class OrderStateUnknown(BrokerError):
    """The venue's state after an order attempt could not be verified.

    The CLI marks the ticket ``unknown``, keeps counting it as open risk and
    tells the user to check the venue.
    """


@dataclass(frozen=True, slots=True)
class Balance:
    equity: float
    free: float
    currency: str


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    side: Side
    qty: float
    entry_price: float | None
    stop: float | None = None
    take_profit: float | None = None
    unrealized_pnl: float | None = None
    ticket_id: str | None = None


@dataclass(frozen=True, slots=True)
class OrderResult:
    """One order as acknowledged by a broker.

    ``role`` is ``entry``, ``stop``, ``take_profit``, ``bracket`` or ``close``.
    """

    id: str
    symbol: str
    side: str
    role: str
    type: str
    qty: float
    price: float | None
    status: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class Fill:
    """An execution. ``pnl`` and ``r`` are set on exits (fees of both legs included)."""

    id: str
    order_id: str
    ticket_id: str
    ts: datetime
    symbol: str
    side: str
    kind: str
    qty: float
    price: float
    fee: float
    pnl: float | None = None
    r: float | None = None


class Broker(ABC):
    """Common interface. ``name`` identifies the venue, ``environment`` describes it in Turkish."""

    name: str
    environment: str
    # True only for venues where no real money moves (local paper, exchange demo/paper accounts).
    is_demo: bool = False

    @abstractmethod
    def get_price(self, symbol: str) -> float: ...

    @abstractmethod
    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame: ...

    @abstractmethod
    def get_balance(self) -> Balance: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_open_orders(self, symbol: str | None = None) -> list[OrderResult]: ...

    @abstractmethod
    def place_bracket(self, ticket: OrderTicket) -> list[OrderResult]:
        """Send entry + protective stop + take-profit, or raise ``BrokerError`` without
        leaving an unprotected entry behind."""

    @abstractmethod
    def cancel_all(self) -> int:
        """Cancel every working order; return how many were cancelled."""

    @abstractmethod
    def cancel_orders(self, symbol: str) -> int:
        """Cancel every working order on ``symbol``; return how many were cancelled."""

    @abstractmethod
    def close_all(self) -> list[OrderResult]:
        """Close every open position at market."""

    def market_type(self, symbol: str, configured: str) -> str:
        """Venue kind (spot/futures/equity) the ticket will actually trade on.

        The default trusts the configured kind; live connectors derive it from
        the venue itself.
        """
        return configured

    def normalize_qty(self, symbol: str, qty: float) -> float:
        """Round ``qty`` down to what the venue accepts (default: unchanged)."""
        return qty

    def normalize_prices(
        self, symbol: str, side: Side, entry: float, stop: float, targets: list[float]
    ) -> tuple[float, float, list[float]]:
        """Prices exactly as the venue will receive them (default: unchanged).

        Implementations round the entry to the nearest tick and the stop and
        targets toward the entry, so rounding can only reduce risk and R:R.
        """
        return entry, stop, list(targets)

    def reconcile_ticket(self, ticket: OrderTicket) -> str:
        """``open`` while the venue still holds a position or working order on the ticket's
        symbol, else ``closed``. Connectors with per-leg order ids refine this."""
        if any(p.symbol == ticket.symbol for p in self.get_positions()):
            return "open"
        return "open" if self.get_open_orders(ticket.symbol) else "closed"

    def cancel_entry(self, ticket: OrderTicket) -> str | None:
        """Cancel the ticket's entry if it has not filled.

        Returns ``"canceled"`` (entry and its protective legs are gone), ``"filled"``
        (a position exists or the entry already filled: protection is left in place)
        or ``None`` when the connector cannot do this safely.
        """
        return None

    def venue_problems(self, ticket: OrderTicket) -> list[str]:
        """Venue-specific reasons the ticket cannot be sent (minimum size etc.)."""
        return []
