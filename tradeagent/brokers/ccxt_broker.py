"""Live crypto broker: Binance USDⓈ-M futures via ccxt (``binanceusdm`` only).

Why only binanceusdm: a spot stop-sell needs the coins already, so no spot
venue reachable through ccxt's unified API can protect an unfilled limit
entry with an exchange-side stop. Every other exchange id is refused here,
explicitly, not through ccxt ``has`` flags.

API keys come only from ``BINANCEUSDM_API_KEY`` / ``BINANCEUSDM_API_SECRET``.
They are handed to ccxt and never stored on this object, logged or printed.

Bracket order of operations (verified against the installed ccxt 4.5.93):

1. Refuse hedge (dual-side) mode and any existing position or order on the
   symbol (one ticket per symbol: the exits below close the whole position).
2. Isolated margin and the ticket's leverage.
3. Protective stop: STOP_MARKET with ``closePosition`` (ccxt passes the flag
   through and drops the quantity), then the take-profit the same way. Both
   exist BEFORE the entry, so a filled entry is never unprotected.
4. The limit entry.

Every leg carries a deterministic client order id (``<ticket>-s/-t/-e``). A
network error or timeout is resolved by looking the order up by that id;
a state that still cannot be verified raises ``OrderStateUnknown`` and the
protective legs are left in place. A definitive rejection cancels the legs
already placed (conditional legs through the algo endpoint) and the error
says exactly what could and could not be cancelled.
"""

import os
from collections.abc import Callable, Iterable, Mapping
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

import ccxt
import pandas as pd

from tradeagent.brokers.base import Balance, Broker, BrokerError, OrderResult, OrderStateUnknown, Position
from tradeagent.brokers.data import fetch_ccxt
from tradeagent.models import OrderTicket, Side
from tradeagent.risk import is_positive

LIVE_CRYPTO_EXCHANGE = "binanceusdm"
_LEG_SUFFIX = {"stop": "s", "take_profit": "t", "entry": "e"}
_CONDITIONAL_ROLES = ("stop", "take_profit")
_OPEN_STATUSES = ("open", "new", "partially_filled")


def api_key_names(exchange_id: str) -> tuple[str, str]:
    """Environment variable names that hold the key pair for ``exchange_id``."""
    prefix = exchange_id.upper()
    return f"{prefix}_API_KEY", f"{prefix}_API_SECRET"


def client_order_ids(ticket_id: str) -> dict[str, str]:
    """Deterministic client order id per bracket leg (Binance allows [A-Za-z0-9._:/-], <= 36 chars)."""
    return {role: f"{ticket_id}-{suffix}" for role, suffix in _LEG_SUFFIX.items()}


def _role_of(order: dict[str, Any]) -> str:
    client_id = str(order.get("clientOrderId") or "")
    for role, suffix in _LEG_SUFFIX.items():
        if client_id.endswith(f"-{suffix}"):
            return role
    kind = str(order.get("type") or "").lower()
    if order.get("stopLossPrice") is not None or "stop" in kind:
        return "stop"
    if order.get("takeProfitPrice") is not None or "take_profit" in kind:
        return "take_profit"
    return "entry"


def _order_price(order: dict[str, Any]) -> float | None:
    for key in ("price", "stopLossPrice", "takeProfitPrice", "triggerPrice", "stopPrice"):
        value = order.get(key)
        if value is not None:
            return float(value)
    return None


def _to_tick(value: float, tick: float, rounding: str) -> float:
    step = Decimal(repr(tick))
    return float((Decimal(repr(value)) / step).to_integral_value(rounding=rounding) * step)


class _LegRejected(Exception):
    """A leg was definitively not placed (the venue rejected it or it is verifiably absent)."""


class CcxtBroker(Broker):
    def __init__(
        self,
        exchange_id: str,
        *,
        testnet: bool,
        allowlist: Iterable[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> None:
        if exchange_id != LIVE_CRYPTO_EXCHANGE:
            raise BrokerError(
                f"Canlı kripto emri yalnızca {LIVE_CRYPTO_EXCHANGE} (Binance USDⓈ-M futures) ile destekleniyor; "
                f"'{exchange_id}' için canlı emir yok. Spot piyasada dolmamış bir girişi borsa tarafında stop ile "
                "korumak mümkün değil. Spot sembolleri paper ile kullanın."
            )
        source = os.environ if env is None else env
        key_var, secret_var = api_key_names(exchange_id)
        if not source.get(key_var) or not source.get(secret_var):
            raise BrokerError(
                f"API anahtarı bulunamadı: {key_var} ve {secret_var} ortam değişkenlerini (veya .env) tanımlayın."
            )
        self._exchange = ccxt.binanceusdm(
            {
                "apiKey": source[key_var],
                "secret": source[secret_var],
                "enableRateLimit": True,
                # Signed requests carry a timestamp; compensate for a drifting PC clock (Binance error -1021).
                "options": {
                    "warnOnFetchOpenOrdersWithoutSymbol": False,
                    "adjustForTimeDifference": True,
                    "recvWindow": 10_000,
                },
            }
        )
        self.name = f"ccxt:{exchange_id}"
        self.is_demo = bool(testnet)
        if testnet:
            # ccxt 4.5.x rejects private calls on the deprecated futures testnet; demo trading replaces it.
            self._exchange.enable_demo_trading(True)
            self.environment = "demo trading (Binance futures test ortamı)"
        else:
            self.environment = "CANLI (gerçek para)"
        self._allowlist = tuple(s for s in allowlist if "/" in s)
        self._markets_loaded = False

    def __repr__(self) -> str:
        return f"CcxtBroker(name={self.name!r}, environment={self.environment!r})"

    # --- markets ---------------------------------------------------------------------------------

    def _market(self, symbol: str) -> dict[str, Any]:
        if not self._markets_loaded:
            self._exchange.load_markets()
            self._markets_loaded = True
        try:
            market = self._exchange.market(symbol)
        except ccxt.BadSymbol as exc:
            raise BrokerError(f"{self._exchange.id} borsasında {symbol} piyasası yok (ör. BTC/USDT:USDT).") from exc
        if not (market.get("contract") and market.get("linear")):
            raise BrokerError(f"{symbol} lineer USDⓈ-M futures piyasası değil; BTC/USDT:USDT biçimini kullanın.")
        return market

    def market_type(self, symbol: str, configured: str) -> str:
        self._market(symbol)
        return "futures"

    def normalize_qty(self, symbol: str, qty: float) -> float:
        self._market(symbol)
        try:
            return float(self._exchange.amount_to_precision(symbol, qty))
        except ccxt.InvalidOrder:
            return 0.0

    def normalize_prices(
        self, symbol: str, side: Side, entry: float, stop: float, targets: list[float]
    ) -> tuple[float, float, list[float]]:
        precision = self._market(symbol).get("precision", {}).get("price")
        if precision is None:
            return entry, stop, list(targets)
        tick = precision if self._exchange.precisionMode == ccxt.TICK_SIZE else 10.0 ** -precision
        toward_entry_from_below = ROUND_CEILING  # long stop / short target
        toward_entry_from_above = ROUND_FLOOR  # long target / short stop
        long = side is Side.LONG
        return (
            _to_tick(entry, tick, ROUND_HALF_UP),
            _to_tick(stop, tick, toward_entry_from_below if long else toward_entry_from_above),
            [_to_tick(t, tick, toward_entry_from_above if long else toward_entry_from_below) for t in targets],
        )

    def venue_problems(self, ticket: OrderTicket) -> list[str]:
        limits = self._market(ticket.symbol).get("limits", {})
        problems = []
        min_amount = (limits.get("amount") or {}).get("min")
        min_cost = (limits.get("cost") or {}).get("min")
        if min_amount is not None and ticket.qty < float(min_amount):
            problems.append(f"Miktar {ticket.qty} borsanın en düşük miktarının ({min_amount}) altında.")
        if min_cost is not None and ticket.qty * ticket.entry < float(min_cost):
            problems.append(f"Pozisyon değeri borsanın en düşük emir tutarının ({min_cost}) altında.")
        return problems

    def _is_hedge_mode(self, symbol: str | None = None) -> bool:
        mode = self._exchange.fetch_position_mode(symbol)
        return bool(mode.get("hedged"))

    # --- reads -----------------------------------------------------------------------------------

    def get_price(self, symbol: str) -> float:
        last = self._exchange.fetch_ticker(symbol).get("last")
        if last is None:
            raise BrokerError(f"{symbol} için son fiyat alınamadı.")
        return float(last)

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame:
        return fetch_ccxt(self._exchange, symbol, timeframe, limit)

    def get_balance(self) -> Balance:
        balance = self._exchange.fetch_balance()
        total, free = balance.get("total") or {}, balance.get("free") or {}
        return Balance(equity=float(total.get("USDT") or 0.0), free=float(free.get("USDT") or 0.0), currency="USDT")

    def get_positions(self) -> list[Position]:
        positions = []
        for raw in self._exchange.fetch_positions():
            contracts = float(raw.get("contracts") or 0.0)
            if contracts == 0:
                continue
            positions.append(
                Position(
                    symbol=raw["symbol"],
                    side=Side.LONG if raw.get("side") == "long" else Side.SHORT,
                    qty=contracts * float(raw.get("contractSize") or 1.0),
                    entry_price=float(raw["entryPrice"]) if raw.get("entryPrice") is not None else None,
                    unrealized_pnl=float(raw["unrealizedPnl"]) if raw.get("unrealizedPnl") is not None else None,
                )
            )
        return positions

    def _raw_open_orders(self, symbol: str | None) -> list[tuple[dict[str, Any], bool]]:
        """(order, is_conditional) for regular and conditional (algo) orders."""
        orders = []
        for conditional in (False, True):
            params = {"trigger": True} if conditional else {}
            orders.extend((raw, conditional) for raw in self._exchange.fetch_open_orders(symbol, None, None, params))
        return orders

    def get_open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        return [
            OrderResult(
                id=str(raw.get("clientOrderId") or raw["id"]),
                symbol=raw.get("symbol") or symbol or "",
                side=raw.get("side") or "",
                role=_role_of(raw),
                type=raw.get("type") or "",
                qty=float(raw.get("amount") or 0.0),
                price=_order_price(raw),
                status=raw.get("status") or "open",
            )
            for raw, _ in self._raw_open_orders(symbol)
        ]

    # --- order helpers ---------------------------------------------------------------------------

    def _lookup(self, symbol: str, client_id: str, conditional: bool) -> dict[str, Any] | None:
        """The order with ``client_id``, ``None`` if the venue says it does not exist.

        Raises ``OrderStateUnknown`` when the lookup itself fails.
        """
        params: dict[str, Any] = {"clientOrderId": client_id}
        if conditional:
            params["trigger"] = True
        try:
            return self._exchange.fetch_order(None, symbol, params)
        except ccxt.OrderNotFound:
            return None
        except ccxt.BaseError as exc:
            raise OrderStateUnknown(
                f"{client_id} kimlikli emrin durumu doğrulanamadı ({exc}). Borsada elle kontrol edin; "
                "bilet açık risk olarak sayılmaya devam ediyor."
            ) from exc

    def _submit(self, symbol: str, role: str, client_id: str, send: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        conditional = role in _CONDITIONAL_ROLES
        try:
            return send()
        except ccxt.NetworkError as exc:  # timeouts included: the order may or may not exist
            found = self._lookup(symbol, client_id, conditional)
            if found is None:
                raise _LegRejected(f"{role} gönderilemedi (ağ hatası, borsada kaydı yok: {exc})") from exc
            return found
        except ccxt.BaseError as exc:
            raise _LegRejected(f"{role} reddedildi: {exc}") from exc

    def _cancel_leg(self, symbol: str, role: str, raw: dict[str, Any]) -> bool:
        params: dict[str, Any] = {"trigger": True} if role in _CONDITIONAL_ROLES else {}
        try:
            self._exchange.cancel_order(raw.get("id"), symbol, params)
            return True
        except ccxt.OrderNotFound:
            return True  # already gone
        except ccxt.BaseError:
            return False

    def _rollback(self, symbol: str, placed: list[tuple[str, dict[str, Any]]], reason: str) -> None:
        failed = [role for role, raw in placed if not self._cancel_leg(symbol, role, raw)]
        if failed:
            raise OrderStateUnknown(
                f"{reason}. Önceden gönderilen {', '.join(failed)} emri iptal EDİLEMEDİ; borsada elle iptal edin."
            )
        if placed:
            raise BrokerError(
                f"{reason}. Önceden gönderilen {', '.join(r for r, _ in placed)} emri borsa tarafından iptal edildi."
            )
        raise BrokerError(f"{reason}. Hiçbir emir gönderilmedi.")

    def _has_exposure(self, symbol: str) -> bool:
        return any(p.symbol == symbol for p in self.get_positions()) or bool(self._raw_open_orders(symbol))

    # --- orders ------------------------------------------------------------------------------------

    def place_bracket(self, ticket: OrderTicket) -> list[OrderResult]:
        exchange, symbol = self._exchange, ticket.symbol
        stop, take_profit = ticket.stop, ticket.take_profit
        if stop is None or not is_positive(stop) or take_profit is None or not is_positive(take_profit):
            raise BrokerError("Stop ve hedef (take-profit) olmadan emir gönderilmez.")
        sign = ticket.side.sign
        if (ticket.entry - stop) * sign <= 0 or (take_profit - ticket.entry) * sign <= 0:
            raise BrokerError("Stop veya hedef yanlış tarafta; emir gönderilmedi.")
        if not float(ticket.leverage).is_integer():
            raise BrokerError(f"Kaldıraç tam sayı olmalı (bilet {ticket.leverage}x); emir gönderilmedi.")
        self._market(symbol)
        qty = self.normalize_qty(symbol, ticket.qty)
        if qty <= 0 or qty != ticket.qty:
            raise BrokerError(f"Miktar borsa hassasiyetiyle uyuşmuyor ({ticket.qty} -> {qty}); emir gönderilmedi.")
        if self._is_hedge_mode(symbol):
            raise BrokerError("Hesap hedge (çift yönlü) modunda; yalnızca tek yönlü (one-way) mod desteklenir.")
        if self._has_exposure(symbol):
            raise BrokerError(
                f"{symbol} için zaten açık pozisyon veya emir var. Koruyucu emirler tüm pozisyonu kapattığından "
                "aynı sembolde aynı anda tek bilet çalışır; emir gönderilmedi."
            )
        try:
            exchange.set_margin_mode("isolated", symbol)
        except ccxt.MarginModeAlreadySet:
            pass
        except ccxt.BaseError as exc:
            raise BrokerError(f"İzole marjin ayarlanamadı ({exc}); emir gönderilmedi.") from exc
        try:
            exchange.set_leverage(int(ticket.leverage), symbol)
        except ccxt.BaseError as exc:
            raise BrokerError(f"Kaldıraç ayarlanamadı ({exc}); emir gönderilmedi.") from exc

        ids = client_order_ids(ticket.id)
        entry_side, exit_side = ticket.side.entry_action, ticket.side.exit_action
        sends: list[tuple[str, Callable[[], dict[str, Any]]]] = [
            (
                "stop",
                lambda: exchange.create_order(
                    symbol, "market", exit_side, qty, None,
                    {"stopLossPrice": stop, "closePosition": True, "clientOrderId": ids["stop"]},
                ),
            ),
            (
                "take_profit",
                lambda: exchange.create_order(
                    symbol, "market", exit_side, qty, None,
                    {"takeProfitPrice": take_profit, "closePosition": True, "clientOrderId": ids["take_profit"]},
                ),
            ),
            (
                "entry",
                lambda: exchange.create_order(
                    symbol, "limit", entry_side, qty, ticket.entry, {"clientOrderId": ids["entry"]}
                ),
            ),
        ]
        placed: list[tuple[str, dict[str, Any]]] = []
        attempted: list[str] = []
        try:
            for role, send in sends:
                attempted.append(role)
                placed.append((role, self._submit(symbol, role, ids[role], send)))
        except _LegRejected as exc:
            self._rollback(symbol, placed, str(exc))
        except OrderStateUnknown:
            raise  # protective legs already placed stay in place
        except BaseException:
            # Ctrl+C or an unexpected error: without an entry the protective legs are removed;
            # once the entry request went out the entry may exist, so the legs stay.
            if "entry" not in attempted:
                for role, raw in placed:
                    self._cancel_leg(symbol, role, raw)
            raise
        prices = {"stop": stop, "take_profit": take_profit, "entry": ticket.entry}
        types = {"stop": "stop_market", "take_profit": "take_profit_market", "entry": "limit"}
        by_role = dict(placed)
        return [
            OrderResult(
                id=ids[role],
                symbol=symbol,
                side=entry_side if role == "entry" else exit_side,
                role=role,
                type=types[role],
                qty=qty,
                price=prices[role],
                status=str(by_role[role].get("status") or "open"),
                note="closePosition" if role != "entry" else "",
            )
            for role in ("entry", "stop", "take_profit")
        ]

    def reconcile_ticket(self, ticket: OrderTicket) -> str:
        """``open`` while the ticket has a position or a protected working entry; otherwise its
        leftover legs are cancelled and ``closed`` is returned."""
        if any(p.symbol == ticket.symbol for p in self.get_positions()):
            return "open"
        ids = client_order_ids(ticket.id)
        legs = {role: self._lookup(ticket.symbol, cid, role in _CONDITIONAL_ROLES) for role, cid in ids.items()}

        def working(role: str) -> bool:
            raw = legs[role]
            return raw is not None and str(raw.get("status")) in _OPEN_STATUSES

        if working("entry") and working("stop"):
            return "open"
        leftovers = [(role, raw) for role, raw in legs.items() if raw is not None and working(role)]
        failed = [role for role, raw in leftovers if not self._cancel_leg(ticket.symbol, role, raw)]
        if failed:
            raise OrderStateUnknown(f"{ticket.id}: artık emirler ({', '.join(failed)}) iptal edilemedi; borsada kontrol edin.")
        return "closed"

    def cancel_entry(self, ticket: OrderTicket) -> str | None:
        """Cancel an unfilled entry; then drop its legs, unless a position exists (it filled meanwhile)."""
        ids = client_order_ids(ticket.id)
        entry = self._lookup(ticket.symbol, ids["entry"], False)
        if entry is not None:
            status = str(entry.get("status"))
            if status == "closed":
                return "filled"
            if status in _OPEN_STATUSES and not self._cancel_leg(ticket.symbol, "entry", entry):
                raise OrderStateUnknown(f"{ticket.id}: giriş emri iptal edilemedi; borsada kontrol edin.")
        return "filled" if self.reconcile_ticket(ticket) == "open" else "canceled"

    def cancel_orders(self, symbol: str) -> int:
        exchange = self._exchange
        cancelled = 0
        for conditional in (False, True):
            params = {"trigger": True} if conditional else {}
            open_orders = exchange.fetch_open_orders(symbol, None, None, params)
            if open_orders:
                exchange.cancel_all_orders(symbol, params)
                cancelled += len(open_orders)
        return cancelled

    def cancel_all(self) -> int:
        symbols = sorted({str(raw.get("symbol")) for raw, _ in self._raw_open_orders(None) if raw.get("symbol")})
        return sum(self.cancel_orders(symbol) for symbol in symbols)

    def close_all(self) -> list[OrderResult]:
        hedged = self._is_hedge_mode()
        results: list[OrderResult] = []
        for position in self.get_positions():
            side = position.side.exit_action
            params = {"positionSide": position.side.value.upper()} if hedged else {"reduceOnly": True}
            try:
                raw = self._exchange.create_order(position.symbol, "market", side, position.qty, None, params)
                results.append(
                    OrderResult(
                        id=str(raw.get("id")), symbol=position.symbol, side=side, role="close", type="market",
                        qty=position.qty, price=None, status=str(raw.get("status") or "open"),
                    )
                )
            except ccxt.BaseError as exc:
                results.append(
                    OrderResult(
                        id=f"failed:{position.symbol}:{position.side.value}", symbol=position.symbol, side=side,
                        role="close", type="market", qty=position.qty, price=None, status="failed",
                        note=f"kapatılamadı: {exc}",
                    )
                )
        return results
