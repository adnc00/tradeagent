"""Panel actions: approve, cancel, dismiss and kill through the local panel.

They run exactly the terminal's path: the ticket is venue-rounded and re-sized,
the risk engine and the stale-ticket guard run, the ticket is claimed atomically
and both checks run once more on what will be sent. Sending and the kill need
the same confirmation word as the terminal (``ONAY``), typed by the user in the
panel. Cancelling an unfilled entry and dismissing an unsent ticket only reduce
risk and need no word. Nothing here runs without a request from the panel page.
"""

import math
import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from tradeagent import cli
from tradeagent.brokers.data import market_type
from tradeagent.journal import USER_PREFIX, Journal
from tradeagent.models import OrderTicket
from tradeagent.risk import ticket_metrics


@dataclass
class ActionError(Exception):
    code: int
    message: str
    extra: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {"error": self.message, **self.extra}


def _ticket_dict(ticket: OrderTicket) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "symbol": ticket.symbol,
        "side": ticket.side.value,
        "market": ticket.market,
        "timeframe": ticket.timeframe,
        "entry": ticket.entry,
        "stop": ticket.stop,
        "targets": list(ticket.targets),
        "qty": ticket.qty,
        "leverage": ticket.leverage,
        "status": ticket.status,
    }


def _orders(orders) -> list[dict[str, Any]]:
    return [
        {"role": o.role, "side": o.side, "qty": o.qty, "price": o.price, "status": o.status, "id": o.id, "note": o.note}
        for o in orders
    ]


def _require_confirmation(confirm: object) -> None:
    if confirm != cli.CONFIRM_WORD:
        raise ActionError(400, f"Onay için tam olarak {cli.CONFIRM_WORD} yazın; hiçbir işlem yapılmadı.")


def _load(journal: Journal, ticket_id: object, *, status: str) -> OrderTicket:
    ticket = journal.get_ticket(str(ticket_id or "").strip().lower())
    if ticket is None:
        raise ActionError(404, f"Bilet bulunamadı: {ticket_id}")
    if ticket.status != status:
        raise ActionError(409, f"Bilet {ticket.id} durumu '{ticket.status}'; bu işlem yalnızca '{status}' biletlerde yapılır.")
    return ticket


# One connection per account for the panel's lifetime: rebuilding a ccxt client reloads ~1000
# markets and made every preview take ~15 s. Paper brokers are rebuilt (their state is a file).
_BROKERS: dict[tuple, Any] = {}


def _broker(cfg, symbol: str, live: bool):
    from tradeagent.brokers.data import asset_class
    from tradeagent.brokers.paper import PaperBroker

    key = (str(cfg.source), cli.live_enabled(cfg, live), asset_class(symbol) if cli.live_enabled(cfg, live) else "paper")
    cached = _BROKERS.get(key)
    if cached is not None:
        return cached
    broker = cli.make_broker(cfg, symbol, live)
    if not isinstance(broker, PaperBroker):
        _BROKERS[key] = broker
    return broker


def _account_brokers(cfg, live: bool) -> list:
    key = (str(cfg.source), cli.live_enabled(cfg, live), "account")
    if key not in _BROKERS:
        _BROKERS[key] = cli.account_brokers(cfg, live)
    return _BROKERS[key]


def _prepared(cfg, live: bool, journal: Journal, ticket: OrderTicket):
    broker = _broker(cfg, ticket.symbol, live)
    prepared, state, reasons = cli.prepare_ticket(ticket, broker, journal, cfg, quick=True)
    return broker, prepared, state, reasons


def _preview_body(cfg, live: bool, broker, prepared: OrderTicket, state, reasons: list[str]) -> dict[str, Any]:
    metrics = ticket_metrics(prepared, state.equity, cfg.risk.mmr)
    gain = metrics.rr * metrics.risk_amount if metrics.rr is not None else None
    return {
        "ticket": _ticket_dict(prepared),
        "metrics": {
            "notional": metrics.notional,
            "margin": metrics.margin,
            "risk_amount": metrics.risk_amount,
            "risk_pct": metrics.risk_pct,
            "rr": metrics.rr,
            "gain": gain,
            "gain_pct": gain / state.equity * 100.0 if gain is not None and state.equity else None,
            "liquidation": metrics.liquidation,
            "equity": state.equity,
            "currency": cli.quote_currency(prepared.symbol),
        },
        "account": {"broker": broker.name, "environment": broker.environment, "real_money": not getattr(broker, "is_demo", False)},
        "reasons": reasons,
    }


def preview(cfg, live: bool, body: dict[str, Any]) -> dict[str, Any]:
    journal = Journal(cfg.journal_path)
    try:
        ticket = _load(journal, body.get("ticket_id"), status="new")
        broker, prepared, state, reasons = _prepared(cfg, live, journal, ticket)
        return _preview_body(cfg, live, broker, prepared, state, reasons)
    finally:
        journal.close()


def execute(cfg, live: bool, body: dict[str, Any]) -> dict[str, Any]:
    _require_confirmation(body.get("confirm"))
    journal = Journal(cfg.journal_path)
    try:
        ticket = _load(journal, body.get("ticket_id"), status="new")
        broker, prepared, state, reasons = _prepared(cfg, live, journal, ticket)
        if reasons:
            journal.add_ticket_note(ticket.id, "panel onayı reddedildi: " + " | ".join(reasons))
            raise ActionError(409, "Kontroller emri reddetti; hiçbir emir gönderilmedi.", {"reasons": reasons})
        journal.add_ticket_note(ticket.id, f"panelden onaylandı ({cli.CONFIRM_WORD} yazıldı)")
        mode = "live" if cli.live_enabled(cfg, live) else "paper"
        outcome = cli.send_confirmed(prepared, broker, journal, cfg, mode, quick=True)
        result = {"status": outcome.status, "lines": outcome.lines, "orders": _orders(outcome.orders)}
        if outcome.status == "sent":
            return result
        codes = {"busy": 409, "rejected": 409, "failed": 502, "unknown": 500}
        raise ActionError(codes.get(outcome.status, 500), " ".join(outcome.lines), result)
    finally:
        journal.close()


def dismiss(cfg, live: bool, body: dict[str, Any]) -> dict[str, Any]:
    journal = Journal(cfg.journal_path)
    try:
        ticket = _load(journal, body.get("ticket_id"), status="new")
        journal.set_ticket_status(ticket.id, "canceled", note="panelden reddedildi; gönderilmedi")
        return {"status": "canceled"}
    finally:
        journal.close()


def cancel(cfg, live: bool, body: dict[str, Any]) -> dict[str, Any]:
    journal = Journal(cfg.journal_path)
    try:
        ticket = _load(journal, body.get("ticket_id"), status="submitted")
        broker = _broker(cfg, ticket.symbol, live)
        outcome = broker.cancel_entry(ticket)
        if outcome is None:
            raise ActionError(501, f"{broker.name} için girişi tek başına iptal etme desteklenmiyor; borsadan iptal edin.")
        if outcome == "filled":
            journal.set_ticket_status(ticket.id, "open", note="iptal istendi ama giriş dolmuştu; koruyucu emirler yerinde")
            return {"status": "filled", "message": "Giriş zaten dolmuş; pozisyon açık, stop ve hedef yerinde."}
        journal.set_ticket_status(ticket.id, "canceled", note="panelden iptal edildi (giriş dolmamıştı)")
        return {"status": "canceled"}
    finally:
        journal.close()


def kill(cfg, live: bool, body: dict[str, Any]) -> dict[str, Any]:
    _require_confirmation(body.get("confirm"))
    enabled = cli.live_enabled(cfg, live)
    brokers = _account_brokers(cfg, live)
    if not brokers:
        raise ActionError(503, "Kullanılabilir hesap yok (API anahtarlarını .env'de tanımlayın).")
    journal = Journal(cfg.journal_path)
    problems: list[str] = []
    try:
        for broker in brokers:
            try:
                cli.refresh(broker, journal)
                problems.extend(cli._kill_broker(broker, journal, enabled))
            except Exception as exc:  # report and continue with the next account
                problems.append(f"{broker.name}: {exc}")
    finally:
        journal.close()
    return {"problems": problems}


# --- edit a ticket / open one by hand -----------------------------------------------------------------


def _number(body: dict[str, Any], key: str, label: str) -> float:
    value = body.get(key)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ActionError(400, f"{label} geçerli bir sayı olmalı.") from None
    if not math.isfinite(number) or number <= 0:
        raise ActionError(400, f"{label} pozitif bir sayı olmalı.")
    return number


def _levels(side: str, body: dict[str, Any]) -> tuple[float, float, float]:
    entry = _number(body, "entry", "Giriş")
    stop = _number(body, "stop", "Stop")
    target = _number(body, "target", "Hedef")
    if side == "long" and not stop < entry < target:
        raise ActionError(400, "Long için stop < giriş < hedef olmalı.")
    if side == "short" and not target < entry < stop:
        raise ActionError(400, "Short için hedef < giriş < stop olmalı.")
    return entry, stop, target


def _risk_pct(cfg, body: dict[str, Any]) -> float:
    if body.get("risk_pct") in (None, ""):
        return cfg.risk.risk_pct
    value = _number(body, "risk_pct", "Risk yüzdesi")
    if value > cfg.risk.max_risk_pct:
        raise ActionError(400, f"Risk yüzdesi %{value:g}, üst sınır %{cfg.risk.max_risk_pct:g} (config: risk.max_risk_pct).")
    return value


def _leverage(cfg, body: dict[str, Any], default: float) -> float:
    if body.get("leverage") in (None, "", "auto"):
        return default
    value = _number(body, "leverage", "Kaldıraç")
    if not value.is_integer() or not 1 <= value <= cfg.risk.max_leverage:
        raise ActionError(400, f"Kaldıraç 1 ile {cfg.risk.max_leverage:g} arasında tam sayı olmalı.")
    return value


def modify(cfg, live: bool, body: dict[str, Any]) -> dict[str, Any]:
    journal = Journal(cfg.journal_path)
    try:
        ticket = _load(journal, body.get("ticket_id"), status="new")
        entry, stop, target = _levels(ticket.side.value, body)
        sign = ticket.side.sign
        targets = [target] + [t for t in ticket.targets[1:] if (t - target) * sign > 0]
        edited = replace(
            ticket, entry=entry, stop=stop, targets=targets, risk_pct=_risk_pct(cfg, body),
            leverage=_leverage(cfg, body, ticket.leverage), reason=f"{USER_PREFIX} panelden düzenlendi",
        )
        journal.save_user_ticket(edited)
        journal.add_ticket_note(
            ticket.id, f"panelden düzenlendi: giriş {entry:g}, stop {stop:g}, hedef {target:g}, "
            f"risk %{edited.risk_pct:g}, kaldıraç {edited.leverage:g}x"
        )
        broker, prepared, state, reasons = _prepared(cfg, live, journal, journal.get_ticket(ticket.id))
        return _preview_body(cfg, live, broker, prepared, state, reasons)
    finally:
        journal.close()


_SYMBOL = re.compile(r"^[A-Z0-9]{1,20}(/[A-Z0-9]{1,10}(:[A-Z0-9]{1,10})?|\.IS)?$")


def _symbol(body: dict[str, Any]) -> str:
    symbol = str(body.get("symbol") or "").strip().upper()
    if not _SYMBOL.match(symbol):
        raise ActionError(400, "Sembol biçimi: BTC/USDT:USDT (vadeli), BTC/USDT (spot), AAPL veya THYAO.IS.")
    return symbol


def manual(cfg, live: bool, body: dict[str, Any]) -> dict[str, Any]:
    symbol = _symbol(body)
    side = str(body.get("side") or "").lower()
    if side not in ("long", "short"):
        raise ActionError(400, "Yön long ya da short olmalı.")
    entry, stop, target = _levels(side, body)
    ticket = OrderTicket(
        id="t-m" + secrets.token_hex(4), setup_id="manual", symbol=symbol, side=side, entry=entry, stop=stop,
        targets=[target], qty=0.0, leverage=_leverage(cfg, body, cfg.scanner.leverage), risk_pct=_risk_pct(cfg, body),
        fee_rate=cfg.fee_rate, market=market_type(symbol, cfg.crypto.exchange), timeframe="elle",
        reason=f"{USER_PREFIX} panelden elle açıldı", created_at=datetime.now(UTC),
    )
    journal = Journal(cfg.journal_path)
    try:
        journal.save_user_ticket(ticket)
        journal.add_ticket_note(ticket.id, "panelden elle oluşturuldu")
        broker, prepared, state, reasons = _prepared(cfg, live, journal, ticket)
        return _preview_body(cfg, live, broker, prepared, state, reasons)
    finally:
        journal.close()


def price(cfg, live: bool, body: dict[str, Any]) -> dict[str, Any]:
    symbol = _symbol(body)
    broker = _broker(cfg, symbol, live)
    value = cli.current_price(broker, cfg, symbol)
    if value is None:
        raise ActionError(502, f"{symbol} için fiyat alınamadı.")
    return {"symbol": symbol, "price": value, "market": market_type(symbol, cfg.crypto.exchange),
            "risk_pct": cfg.risk.risk_pct, "max_risk_pct": cfg.risk.max_risk_pct, "max_leverage": cfg.risk.max_leverage}


# --- positions on the venue ---------------------------------------------------------------------------------


def positions(cfg, live: bool, body: dict[str, Any]) -> dict[str, Any]:
    """Every open position of the panel's account(s), with its protective orders and its ticket."""
    journal = Journal(cfg.journal_path)
    out = []
    try:
        for broker in _account_brokers(cfg, live):
            tickets = {t.symbol: t.id for t in journal.open_tickets(broker.name)}
            for p in broker.get_positions():
                stop, take_profit = p.stop, p.take_profit
                if stop is None or take_profit is None:
                    for order in broker.get_open_orders(p.symbol):
                        if order.role == "stop" and stop is None:
                            stop = order.price
                        elif order.role == "take_profit" and take_profit is None:
                            take_profit = order.price
                out.append({
                    "broker": broker.name, "environment": broker.environment,
                    "real_money": not getattr(broker, "is_demo", False),
                    "symbol": p.symbol, "side": p.side.value, "qty": p.qty, "entry": p.entry_price,
                    "mark": p.mark_price, "unrealized_pnl": p.unrealized_pnl, "leverage": p.leverage,
                    "liquidation": p.liquidation, "stop": stop, "take_profit": take_profit,
                    "ticket": tickets.get(p.symbol), "currency": cli.quote_currency(p.symbol),
                })
    finally:
        journal.close()
    return {"positions": out}


def _holder(cfg, live: bool, symbol: str):
    for broker in _account_brokers(cfg, live):
        if any(p.symbol == symbol for p in broker.get_positions()):
            return broker
    raise ActionError(404, f"{symbol} için açık pozisyon yok.")


def _require_word_for_real_money(broker, body: dict[str, Any]) -> None:
    if not getattr(broker, "is_demo", False) and body.get("confirm") != cli.CONFIRM_WORD:
        raise ActionError(400, f"Gerçek para hesabında bu işlem için tam olarak {cli.CONFIRM_WORD} yazın.")


def close(cfg, live: bool, body: dict[str, Any]) -> dict[str, Any]:
    symbol = _symbol(body)
    try:
        fraction = float(body.get("fraction", 1.0))
    except (TypeError, ValueError):
        raise ActionError(400, "Kapatılacak oran bir sayı olmalı (ör. 0.5 = yarısı).") from None
    if not (math.isfinite(fraction) and 0 < fraction <= 1):
        raise ActionError(400, "Kapatılacak oran 0'dan büyük ve en fazla 1 olmalı.")
    broker = _holder(cfg, live, symbol)
    _require_word_for_real_money(broker, body)
    closer = getattr(broker, "close_position", None)
    if closer is None:
        raise ActionError(501, f"{broker.name} için tek pozisyon kapatma desteklenmiyor; Acil durdur'u kullanın.")
    try:
        result = closer(symbol, fraction)
    except cli.BrokerError as exc:
        raise ActionError(409, str(exc)) from None
    return {"status": result.status, "qty": result.qty, "symbol": symbol, "fraction": fraction}


def protect(cfg, live: bool, body: dict[str, Any]) -> dict[str, Any]:
    symbol = _symbol(body)
    levels = {}
    for key, label in (("stop", "Stop"), ("take_profit", "Hedef")):
        levels[key] = None if body.get(key) in (None, "") else _number(body, key, label)
    broker = _holder(cfg, live, symbol)
    _require_word_for_real_money(broker, body)
    setter = getattr(broker, "set_protection", None)
    if setter is None:
        raise ActionError(501, f"{broker.name} için stop/hedef değiştirme desteklenmiyor.")
    try:
        legs = setter(symbol, stop=levels["stop"], take_profit=levels["take_profit"])
    except cli.BrokerError as exc:
        raise ActionError(409, str(exc)) from None
    return {"orders": _orders(legs)}


def candles(cfg, live: bool, body: dict[str, Any]) -> dict[str, Any]:
    symbol = _symbol(body)
    timeframe = str(body.get("timeframe") or "15m")
    if timeframe not in cli._TIMEFRAMES:
        raise ActionError(400, "Zaman dilimi geçersiz.")
    try:
        frame = cli.MarketData(cfg.crypto.exchange).get_ohlcv(symbol, timeframe, 120)
    except Exception as exc:
        raise ActionError(502, f"{symbol} mum verisi alınamadı ({exc}).") from None
    rows = [
        [int(ts.timestamp() * 1000), float(r.open), float(r.high), float(r.low), float(r.close)]
        for ts, r in frame.tail(120).iterrows()
    ]
    return {"symbol": symbol, "timeframe": timeframe, "candles": rows}


ACTIONS = {
    "/api/preview": preview,
    "/api/execute": execute,
    "/api/dismiss": dismiss,
    "/api/cancel": cancel,
    "/api/kill": kill,
    "/api/modify": modify,
    "/api/manual": manual,
    "/api/price": price,
    "/api/positions": positions,
    "/api/close": close,
    "/api/protect": protect,
    "/api/candles": candles,
}
