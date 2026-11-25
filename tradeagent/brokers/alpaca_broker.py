"""US stocks via Alpaca (alpaca-py).

Keys come only from ``ALPACA_API_KEY`` / ``ALPACA_API_SECRET``. ``paper=True``
(the default) targets Alpaca's paper-trading endpoint; real money needs
``paper=False``, which the CLI only passes when ``alpaca.paper: false`` is set
and the live gate is open.

Brackets use Alpaca's native ``OrderClass.BRACKET`` (limit entry with an
attached stop-loss and take-profit); alpaca-py itself refuses a bracket
without both legs. Bracket orders need whole shares, so quantities are
floored.
"""

import math
import os
from collections.abc import Mapping
from typing import Any

import pandas as pd
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, StopLossRequest, TakeProfitRequest

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from tradeagent.brokers.base import Balance, Broker, BrokerError, OrderResult, OrderStateUnknown, Position
from tradeagent.brokers.data import fetch_yfinance
from tradeagent.models import OrderTicket, Side
from tradeagent.risk import is_positive

KEY_VAR, SECRET_VAR = "ALPACA_API_KEY", "ALPACA_API_SECRET"


def _price(value: float) -> float:
    """US equities trade in cents above $1 and in 1/100 cents below."""
    return round(value, 2) if value >= 1 else round(value, 4)


def _round_price(value: float, rounding: str) -> float:
    step = Decimal("0.01") if value >= 1 else Decimal("0.0001")
    return float((Decimal(repr(value)) / step).to_integral_value(rounding=rounding) * step)


def client_order_id(ticket_id: str) -> str:
    """Deterministic client order id of a ticket's bracket order."""
    return f"{ticket_id}-b"


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value))


class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(self, *, paper: bool = True, env: Mapping[str, str] | None = None) -> None:
        source = os.environ if env is None else env
        if not source.get(KEY_VAR) or not source.get(SECRET_VAR):
            raise BrokerError(f"API anahtarı bulunamadı: {KEY_VAR} ve {SECRET_VAR} ortam değişkenlerini (veya .env) tanımlayın.")
        self._trading = TradingClient(source[KEY_VAR], source[SECRET_VAR], paper=paper)
        self._data = StockHistoricalDataClient(source[KEY_VAR], source[SECRET_VAR])
        self.paper = paper
        self.is_demo = bool(paper)
        self.environment = "Alpaca paper hesabı" if paper else "CANLI (gerçek para)"

    def __repr__(self) -> str:
        return f"AlpacaBroker(paper={self.paper})"

    def market_type(self, symbol: str, configured: str) -> str:
        return "equity"

    def normalize_qty(self, symbol: str, qty: float) -> float:
        return float(math.floor(qty + 1e-9))

    def normalize_prices(
        self, symbol: str, side: Side, entry: float, stop: float, targets: list[float]
    ) -> tuple[float, float, list[float]]:
        long = side is Side.LONG
        return (
            _round_price(entry, ROUND_HALF_UP),
            _round_price(stop, ROUND_CEILING if long else ROUND_FLOOR),
            [_round_price(t, ROUND_FLOOR if long else ROUND_CEILING) for t in targets],
        )

    def get_price(self, symbol: str) -> float:
        trades: Any = self._data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
        return float(trades[symbol].price)

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame:
        return fetch_yfinance(symbol, timeframe, limit)

    def get_balance(self) -> Balance:
        account: Any = self._trading.get_account()
        return Balance(equity=float(account.equity), free=float(account.buying_power), currency=account.currency or "USD")

    def get_positions(self) -> list[Position]:
        positions = []
        raw_positions: Any = self._trading.get_all_positions()
        for raw in raw_positions:
            positions.append(
                Position(
                    symbol=raw.symbol,
                    side=Side.SHORT if _enum_text(raw.side) == "short" else Side.LONG,
                    qty=abs(float(raw.qty)),
                    entry_price=float(raw.avg_entry_price),
                    unrealized_pnl=float(raw.unrealized_pl) if raw.unrealized_pl is not None else None,
                )
            )
        return positions

    def get_open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol] if symbol else None, nested=True)
        raw_orders: Any = self._trading.get_orders(request)
        results: list[OrderResult] = []
        for raw in raw_orders:
            for order, role in [(raw, "entry"), *[(leg, "leg") for leg in (raw.legs or [])]]:
                order_type = _enum_text(order.order_type or order.type)
                if role == "leg":
                    role = "stop" if order.stop_price is not None else "take_profit"
                price = order.limit_price if order.limit_price is not None else order.stop_price
                results.append(
                    OrderResult(
                        id=str(order.id),
                        symbol=order.symbol,
                        side=_enum_text(order.side),
                        role=role,
                        type=order_type,
                        qty=float(order.qty or 0),
                        price=float(price) if price is not None else None,
                        status=_enum_text(order.status),
                    )
                )
        return results

    def place_bracket(self, ticket: OrderTicket) -> list[OrderResult]:
        stop, take_profit = ticket.stop, ticket.take_profit
        if stop is None or not is_positive(stop) or take_profit is None or not is_positive(take_profit):
            raise BrokerError("Stop ve hedef (take-profit) olmadan emir gönderilmez.")
        sign = ticket.side.sign
        if (ticket.entry - stop) * sign <= 0 or (take_profit - ticket.entry) * sign <= 0:
            raise BrokerError("Stop veya hedef yanlış tarafta; emir gönderilmedi.")
        qty = int(self.normalize_qty(ticket.symbol, ticket.qty))
        if qty < 1:
            raise BrokerError("Alpaca bracket emirleri tam hisse ister; miktar 1'in altında, emir gönderilmedi.")
        side = OrderSide.BUY if ticket.side is Side.LONG else OrderSide.SELL
        request = LimitOrderRequest(
            symbol=ticket.symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.BRACKET,
            limit_price=_price(ticket.entry),
            take_profit=TakeProfitRequest(limit_price=_price(take_profit)),
            stop_loss=StopLossRequest(stop_price=_price(stop)),
            client_order_id=client_order_id(ticket.id),
        )
        try:
            order: Any = self._trading.submit_order(request)
        except APIError as exc:
            raise BrokerError(f"Alpaca emri reddetti: {exc}. Hiçbir emir gönderilmedi.") from exc
        except requests.exceptions.RequestException as exc:  # timeout: the order may or may not exist
            order = self._lookup(client_order_id(ticket.id), exc)
        return [
            OrderResult(
                id=str(order.id),
                symbol=ticket.symbol,
                side=side.value,
                role="bracket",
                type="limit",
                qty=float(qty),
                price=_price(ticket.entry),
                status=_enum_text(order.status),
                note=f"stop {_price(stop)}, take-profit {_price(take_profit)}",
            )
        ]

    def _lookup(self, client_id: str, cause: Exception) -> Any:
        try:
            return self._trading.get_order_by_client_id(client_id)
        except APIError as exc:
            raise BrokerError(
                f"Alpaca'ya bağlantı koptu ({cause}) ve {client_id} kimlikli emir borsada yok; emir gönderilmedi."
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise OrderStateUnknown(
                f"Alpaca emrinin durumu doğrulanamadı ({cause}; {exc}). {client_id} kimlikli emri Alpaca'da "
                "kontrol edin; bilet açık risk olarak sayılmaya devam ediyor."
            ) from exc

    def cancel_orders(self, symbol: str) -> int:
        orders = [o for o in self.get_open_orders(symbol) if o.role == "entry"]
        for order in orders:
            self._trading.cancel_order_by_id(order.id)
        return len(orders)

    def cancel_all(self) -> int:
        responses: Any = self._trading.cancel_orders()
        return len(responses) if isinstance(responses, list) else 0

    def close_all(self) -> list[OrderResult]:
        responses: Any = self._trading.close_all_positions(cancel_orders=True)
        results = []
        for raw in responses if isinstance(responses, list) else []:
            ok = isinstance(raw.status, int) and 200 <= raw.status < 300
            results.append(
                OrderResult(
                    id=str(raw.order_id) if raw.order_id else f"failed:{raw.symbol}",
                    symbol=raw.symbol,
                    side="",
                    role="close",
                    type="market",
                    qty=0.0,
                    price=None,
                    status="submitted" if ok else "failed",
                    note="" if ok else f"kapatılamadı (HTTP {raw.status}): {getattr(raw, 'body', '')}",
                )
            )
        return results
