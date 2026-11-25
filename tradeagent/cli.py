"""Command line interface. User-facing text is Turkish.

Safety rules enforced here:

* A live broker is constructed only when the config says ``mode: live`` AND
  the command line has ``--live``. In every other case the local PaperBroker
  is used, even if API keys exist.
* A live confirmation is only read from an interactive terminal (TTY); piped
  or scripted input cannot approve a live order or a live kill.
* ``execute`` rounds prices and quantity exactly as the venue will receive
  them, re-sizes for the executing account, runs the risk engine and the
  stale-ticket guard (current price vs. stop and entry, ticket age), prints
  the full ticket and sends it only if the user types exactly ``ONAY``. It
  then claims the ticket atomically ('submitting') and runs the risk engine
  and the stale-ticket guard again on exactly what will be sent.
* ``kill`` (also ``ONAY``) closes positions first, verifies they are flat and
  only then cancels the remaining orders; protective orders of a position
  that could not be closed are kept.
* No flag, environment variable or config option can answer the
  confirmation on the user's behalf.
"""

import argparse
import math
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from tradeagent import notify
from tradeagent.brokers.base import Broker, BrokerError, OrderResult, OrderStateUnknown
from tradeagent.brokers.data import MarketData, asset_class, market_type
from tradeagent.brokers.paper import PaperBroker
from tradeagent.config import DEFAULT_ENV_PATH, Config, ConfigError, load_config, load_dotenv
from tradeagent.journal import Journal, JournalError
from tradeagent.models import AccountState, OpenRisk, OrderTicket, Setup
from tradeagent.notify import DISCLAIMER, ISTANBUL, money, num, quote_currency
from tradeagent.risk import (
    check_ticket,
    floor_to_step,
    liquidation_price,
    lot_step,
    position_size,
    ticket_metrics,
    ticket_risk_amount,
)
from tradeagent.scanner import scan_symbol

CONFIRM_WORD = "ONAY"
EXECUTE_PROMPT = f"Göndermek için tam olarak {CONFIRM_WORD} yazın (başka her şey iptal eder): "
KILL_PROMPT = f"Hepsini kapatıp iptal etmek için tam olarak {CONFIRM_WORD} yazın (başka her şey iptal eder): "
TTY_REFUSAL = (
    "CANLI onay yalnızca etkileşimli bir terminalde (TTY) verilebilir; girdi bir dosyadan, borudan ya da "
    "başka bir programdan geliyor. Hiçbir emir GÖNDERİLMEDİ."
)
EXIT_OK, EXIT_ABORTED, EXIT_REJECTED, EXIT_UNKNOWN = 0, 1, 2, 3
MAX_CANDLES = 1000
KILL_VERIFY_ATTEMPTS = 3
_TIMEFRAMES = {
    "1m": timedelta(minutes=1),
    "2m": timedelta(minutes=2),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "1wk": timedelta(weeks=1),
}


# --- formatting -------------------------------------------------------------------------------------


def confirm(prompt: str, *, live: bool) -> bool:
    """True only when the user types exactly the confirmation word.

    EOF counts as no. A live confirmation is refused unless stdin is an
    interactive terminal.
    """
    if live and not sys.stdin.isatty():
        print(TTY_REFUSAL)
        return False
    try:
        reply = input(prompt)
    except EOFError:
        reply = ""
    if not sys.stdin.isatty():
        print()  # piped answers are not echoed; keep the transcript readable
    return reply == CONFIRM_WORD


# --- broker selection (live gate) ---------------------------------------------------------------------


def live_enabled(cfg: Config, live_flag: bool) -> bool:
    return cfg.mode == "live" and live_flag


def gate_notice(cfg: Config, live_flag: bool) -> str | None:
    if live_flag and cfg.mode != "live":
        return "Uyarı: --live verildi ama ayarlarda mode: paper; paper kullanılıyor."
    if cfg.mode == "live" and not live_flag:
        return "Bilgi: ayarlarda mode: live ama --live verilmedi; paper kullanılıyor."
    return None


def _env_path(cfg: Config) -> Path:
    return cfg.source.parent / ".env" if cfg.source is not None else DEFAULT_ENV_PATH


def paper_broker(cfg: Config) -> PaperBroker:
    data = MarketData(cfg.crypto.exchange) if cfg.paper.sync else None
    return PaperBroker(
        cfg.paper_state_path,
        initial_equity=cfg.paper.equity,
        market_data=data,
        sync_timeframe=cfg.paper.sync_timeframe,
    )


def make_broker(cfg: Config, symbol: str, live_flag: bool) -> Broker:
    """The broker that would trade ``symbol``; live only with ``mode: live`` AND ``--live``."""
    if not live_enabled(cfg, live_flag):
        return paper_broker(cfg)
    kind = asset_class(symbol)
    if kind == "bist":
        raise BrokerError(
            "BIST için canlı emir desteklenmiyor: aracı kurumların herkese açık bir emir API'si yok. "
            "BIST yalnızca yfinance verisi + paper ile kullanılır."
        )
    load_dotenv(_env_path(cfg))
    if kind == "crypto":
        from tradeagent.brokers import ccxt_broker

        return ccxt_broker.CcxtBroker(cfg.crypto.exchange, testnet=cfg.crypto.testnet, allowlist=cfg.risk.allowlist)
    from tradeagent.brokers import alpaca_broker

    return alpaca_broker.AlpacaBroker(paper=cfg.alpaca.paper)


def account_brokers(cfg: Config, live_flag: bool) -> list[Broker]:
    """Brokers whose account a command (positions/kill) works on."""
    if not live_enabled(cfg, live_flag):
        return [paper_broker(cfg)]
    load_dotenv(_env_path(cfg))
    from tradeagent.brokers import alpaca_broker, ccxt_broker

    brokers: list[Broker] = []
    for label, build in (
        (
            "kripto",
            lambda: ccxt_broker.CcxtBroker(cfg.crypto.exchange, testnet=cfg.crypto.testnet, allowlist=cfg.risk.allowlist),
        ),
        ("Alpaca", lambda: alpaca_broker.AlpacaBroker(paper=cfg.alpaca.paper)),
    ):
        try:
            brokers.append(build())
        except BrokerError as exc:
            print(f"{label} hesabı atlandı: {exc}")
    return brokers


# --- journal synchronisation / account state ------------------------------------------------------------


def journal_paper_state(broker: PaperBroker, journal: Journal) -> None:
    """Copy paper fills into the journal and mirror bracket status onto tickets."""
    for fill in broker.fills:
        journal.record_fill(fill)
    for bracket in broker.brackets:
        row = journal.ticket_row(bracket.ticket_id)
        if row is None or row["status"] == "closed":
            continue
        target = {"pending": "submitted", "open": "open", "closed": "closed", "canceled": "canceled"}[bracket.status]
        if target == row["status"]:
            continue
        if target == "closed":
            journal.set_ticket_status(
                bracket.ticket_id,
                "closed",
                result_r=bracket.r,
                pnl=bracket.pnl,
                closed_at=bracket.closed_at,
                note=f"kapandı ({bracket.exit_reason}) {num(bracket.exit_price)}, R {num(bracket.r)}",
            )
        else:
            journal.set_ticket_status(bracket.ticket_id, target, note=f"paper durumu: {bracket.status}")


def reconcile_live_tickets(broker: Broker, journal: Journal) -> None:
    """Close tickets the venue no longer holds; mark unverifiable ones 'unknown'."""
    for ticket in journal.open_tickets(broker.name):
        try:
            outcome = broker.reconcile_ticket(ticket)
        except OrderStateUnknown as exc:
            journal.set_ticket_status(ticket.id, "unknown", note=str(exc))
            print(f"DİKKAT: {exc}")
            continue
        if outcome == "closed":
            journal.set_ticket_status(
                ticket.id,
                "closed",
                note="borsada pozisyon ya da korumalı çalışan giriş yok; kalan emirler temizlendi (sonuç R bilinmiyor)",
            )


def refresh(broker: Broker, journal: Journal) -> None:
    if isinstance(broker, PaperBroker):
        try:
            broker.sync()
        except BrokerError:
            raise
        except Exception as exc:  # paper only: a data outage must not block the simulator
            print(f"Uyarı: paper fiyat senkronizasyonu yapılamadı ({exc}); son bilinen durum kullanılıyor.")
        for warning in broker.sync_warnings:
            print(f"Uyarı: {warning}")
        journal_paper_state(broker, journal)
    else:
        reconcile_live_tickets(broker, journal)


def account_state(broker: Broker, journal: Journal, exclude: str | None = None) -> AccountState:
    refresh(broker, journal)
    equity = broker.get_balance().equity
    day_start, week_start = journal.equity_baselines(broker.name, equity)
    open_risks = tuple(
        OpenRisk(t.symbol, t.side, ticket_risk_amount(t))
        for t in journal.open_tickets(broker.name)
        if t.stop is not None and t.id != exclude
    )
    return AccountState(equity=equity, day_start_equity=day_start, week_start_equity=week_start, open_risks=open_risks)


def resize_for_account(ticket: OrderTicket, equity: float, broker: Broker) -> OrderTicket:
    """Re-size to the executing account's equity at the ticket's intended risk %."""
    if ticket.stop is None:
        return ticket
    try:
        qty = position_size(equity, ticket.risk_pct, ticket.entry, ticket.stop, ticket.fee_rate)
    except ValueError:
        return ticket
    return replace(ticket, qty=broker.normalize_qty(ticket.symbol, floor_to_step(qty, lot_step(ticket.symbol))))


def current_price(broker: Broker, cfg: Config, symbol: str) -> float | None:
    """Latest price from the broker (paper falls back to market data); ``None`` if unavailable."""
    try:
        return float(broker.get_price(symbol))
    except Exception:  # any data failure means the guard cannot pass
        if not isinstance(broker, PaperBroker):
            return None
    try:
        return float(MarketData(cfg.crypto.exchange).get_price(symbol))
    except Exception:
        return None


def stale_reasons(ticket: OrderTicket, price: float | None, cfg: Config, now: datetime) -> list[str]:
    """Why a ticket is too old or the market has moved away from it."""
    reasons = []
    limits = cfg.execution
    if price is None:
        reasons.append("Güncel fiyat alınamadı; bayat bilet kontrolü yapılamadığından emir gönderilmez.")
    else:
        if ticket.stop is not None and (price - ticket.stop) * ticket.side.sign <= 0:
            reasons.append(f"Fiyat ({num(price)}) stop seviyesini ({num(ticket.stop)}) zaten geçmiş; kurulum bozuldu.")
        deviation = abs(price - ticket.entry) / ticket.entry * 100.0
        if deviation > limits.ticket_max_deviation_pct:
            reasons.append(
                f"Fiyat ({num(price)}) girişten %{deviation:.2f} sapma gösteriyor "
                f"(sınır %{limits.ticket_max_deviation_pct:.2f}); bilet bayat, yeniden tarayın."
            )
    created = ticket.created_at
    max_age = timedelta(minutes=limits.ticket_max_age_minutes)
    if created is None or now - created > max_age:
        age = "bilinmiyor" if created is None else f"{(now - created).total_seconds() / 60:.0f} dk"
        reasons.append(
            f"Bilet çok eski (yaş {age}, sınır {limits.ticket_max_age_minutes} dk); yeniden tarayın."
        )
    return reasons


def prepare_ticket(
    ticket: OrderTicket, broker: Broker, journal: Journal, cfg: Config
) -> tuple[OrderTicket, AccountState, list[str]]:
    """Venue-rounded, re-sized ticket plus every reason it must not be sent."""
    state = account_state(broker, journal)
    prepared = replace(ticket, market=broker.market_type(ticket.symbol, ticket.market))
    if prepared.stop is not None and prepared.targets:
        entry, stop, targets = broker.normalize_prices(
            prepared.symbol, prepared.side, prepared.entry, prepared.stop, prepared.targets
        )
        prepared = replace(prepared, entry=entry, stop=stop, targets=targets)
    prepared = resize_for_account(prepared, state.equity, broker)
    reasons = [v.message for v in check_ticket(prepared, state, cfg.risk)]
    if not reasons:
        reasons.extend(broker.venue_problems(prepared))
    reasons.extend(stale_reasons(prepared, current_price(broker, cfg, prepared.symbol), cfg, datetime.now(UTC)))
    return prepared, state, reasons


def recheck_after_confirmation(ticket: OrderTicket, broker: Broker, journal: Journal, cfg: Config) -> list[str]:
    """Risk engine and stale-ticket guard on exactly the confirmed ticket, with fresh account state."""
    state = account_state(broker, journal, exclude=ticket.id)
    reasons = [v.message for v in check_ticket(ticket, state, cfg.risk)]
    reasons.extend(stale_reasons(ticket, current_price(broker, cfg, ticket.symbol), cfg, datetime.now(UTC)))
    return reasons


# --- rendering -----------------------------------------------------------------------------------------------


def render_ticket(ticket: OrderTicket, state: AccountState, cfg: Config, broker: Broker, live: bool) -> str:
    metrics = ticket_metrics(ticket, state.equity, cfg.risk.mmr)
    currency = quote_currency(ticket.symbol)
    targets = "  ".join(
        f"{i}) {num(t)}{'  <- take-profit emri' if i == 1 else ' (bilgi)'}" for i, t in enumerate(ticket.targets, 1)
    )
    if metrics.liquidation is None:
        liquidation = "yok (spot/hisse, kaldıraçsız)"
    else:
        liquidation = f"{num(metrics.liquidation)} (izole, mmr %{cfg.risk.mmr * 100:.2f} ile tahmini)"
    side_text = "LONG (alış)" if ticket.side.value == "long" else "SHORT (açığa satış)"
    mode_text = "CANLI - emir broker hesabına gider" if live else "PAPER (simülasyon, gerçek emir yok)"
    rows = [
        ("Bilet", f"{ticket.id}  (kurulum {ticket.setup_id})"),
        ("Sembol", f"{ticket.symbol}  [{ticket.market}]"),
        ("Yön", side_text),
        ("Miktar", num(ticket.qty)),
        ("Pozisyon değeri", f"{money(metrics.notional)} {currency}"),
        ("Giriş (limit)", num(ticket.entry)),
        ("Stop", num(ticket.stop)),
        ("Hedefler", targets),
        (
            "Risk",
            f"{money(metrics.risk_amount)} {currency} = %{metrics.risk_pct or 0:.2f} "
            f"(özsermaye {money(state.equity)}, komisyon dahil)",
        ),
        ("R:R (hedef 1)", f"{metrics.rr:.2f} (komisyon dahil)" if metrics.rr is not None else "-"),
        ("Kaldıraç", f"{num(ticket.leverage)}x  (marjin {money(metrics.margin)} {currency})"),
        ("Likidasyon", liquidation),
        ("Broker", f"{broker.name} ({broker.environment})"),
        ("Mod", mode_text),
    ]
    line = "=" * 72
    body = "\n".join(f" {label:<16}: {value}" for label, value in rows)
    return f"{line}\n EMİR ONAYI\n{line}\n{body}\n{'-' * 72}\n {DISCLAIMER}\n{line}"


def print_orders(orders: list[OrderResult]) -> None:
    for order in orders:
        note = f"  ({order.note})" if order.note else ""
        print(
            f"  {order.role:<12} {order.side:<5} {num(order.qty):>14} @ {num(order.price):<12} "
            f"{order.status:<9} id {order.id}{note}"
        )


def print_setup(setup: Setup) -> None:
    targets = " / ".join(num(t) for t in setup.targets) or "-"
    print(
        f"  [{setup.id}] {setup.side.value.upper():<5} giriş {num(setup.entry)}  stop {num(setup.stop)}  "
        f"hedef {targets}  killzone {setup.killzone or '-'}"
    )
    if setup.ticket is not None:
        print(
            f"      -> BİLET {setup.ticket.id}  miktar {num(setup.ticket.qty)}  risk %{setup.ticket.risk_pct:.2f}  "
            f"(onaya sunmak için: execute {setup.ticket.id})"
        )
    else:
        print("      -> REDDEDİLDİ (bilet yok):")
        for reason in setup.reasons:
            print(f"         - {reason}")


def closed_candles(df: pd.DataFrame, timeframe: str, now: datetime) -> pd.DataFrame:
    """Drop the newest candle only while it is still forming (unknown timeframe: drop it)."""
    if df.empty:
        return df
    duration = _TIMEFRAMES.get(timeframe)
    if duration is None or df.index[-1].to_pydatetime() + duration > now:
        return df.iloc[:-1]
    return df


# --- commands --------------------------------------------------------------------------------------------------


def run_scan(symbols: list[str], tf: str, htf: str, limit: int, cfg: Config, journal: Journal) -> list[Setup]:
    """Scan, journal and print every setup; returns the ones evaluated now (not already acted on)."""
    data = MarketData(cfg.crypto.exchange)
    broker = paper_broker(cfg)
    state = account_state(broker, journal)
    evaluated: list[Setup] = []
    for symbol in symbols:
        try:
            ltf = data.get_ohlcv(symbol, tf, limit)
            htf_df = data.get_ohlcv(symbol, htf, limit)
        except Exception as exc:  # one symbol's data outage must not stop the scan
            print(f"{symbol}: veri alınamadı ({exc})")
            continue
        now = datetime.now(UTC)
        try:
            setups = scan_symbol(
                symbol,
                closed_candles(ltf, tf, now),
                htf_df,
                timeframe=tf,
                account=state,
                limits=cfg.risk,
                params=cfg.scanner,
                fee_rate=cfg.fee_rate,
                market=market_type(symbol, cfg.crypto.exchange),
                now=now,
            )
        except ValueError as exc:
            print(f"{symbol}: veri reddedildi ({exc})")
            continue
        if not setups:
            print(f"{symbol} {tf}: kurulum yok.")
            continue
        print(f"{symbol} {tf}: {len(setups)} kurulum")
        for setup in setups:
            existing = journal.get_ticket("t-" + setup.id.removeprefix("s-"))
            journal.save_setup(setup)
            if existing is not None and existing.status != "new":
                print(f"  [{setup.id}] bilet {existing.id} zaten '{existing.status}' durumunda; yeniden değerlendirilmedi.")
                continue
            print_setup(setup)
            evaluated.append(setup)
    return evaluated


def parse_symbols(text: str) -> list[str]:
    return [s.strip() for s in text.split(",") if s.strip()]


def cmd_scan(args: argparse.Namespace, cfg: Config) -> int:
    if not 1 <= args.limit <= MAX_CANDLES:
        print(f"Hata: --limit 1 ile {MAX_CANDLES} arasında olmalı (borsa tek istekte en fazla {MAX_CANDLES} mum verir).")
        return EXIT_REJECTED
    symbols = parse_symbols(args.symbols)
    if not cfg.risk.allowlist:
        print("Uyarı: risk.allowlist boş; hiçbir kurulum bilete dönüşmez (config.example.yaml'a bakın).")
    if cfg.mode == "live":
        print("Not: tarama paper hesabına göre boyutlandırır; execute --live miktarı canlı hesaba göre yeniden hesaplar.")
    journal = Journal(cfg.journal_path)
    try:
        run_scan(symbols, args.tf, args.htf, args.limit, cfg, journal)
        print(DISCLAIMER)
        return EXIT_OK
    finally:
        journal.close()


def cmd_plan(args: argparse.Namespace, cfg: Config) -> int:
    journal = Journal(cfg.journal_path)
    try:
        setup = journal.get_setup(args.setup_id.strip().lower())
        if setup is None:
            print(f"Kurulum bulunamadı: {args.setup_id}")
            return EXIT_ABORTED
        print(f"Kurulum {setup.id}  ({setup.symbol} {setup.timeframe}, {setup.side.value.upper()})")
        print(f"  {setup.narrative}")
        print(f"  Tespit             : {setup.detected_at:%Y-%m-%d %H:%M} UTC")
        print(f"  Süpürülen seviye   : {num(setup.sweep_level)} ({setup.sweep_kind}), uç {num(setup.sweep_extreme)}")
        print(f"  MSS                : {num(setup.mss_level)} kırıldı, {setup.mss_at:%Y-%m-%d %H:%M} UTC")
        print(f"  FVG                : {num(setup.fvg_bottom)} - {num(setup.fvg_top)}")
        print(f"  Dealing range      : {num(setup.range_low)} - {num(setup.range_high)}")
        kinds = ", ".join(f"{num(t)} ({k})" for t, k in zip(setup.targets, setup.target_kinds)) or "-"
        print(f"  Giriş / stop       : {num(setup.entry)} / {num(setup.stop)}")
        print(f"  Hedefler           : {kinds}")
        if setup.ticket is not None:
            ticket = journal.get_ticket(setup.ticket.id) or setup.ticket
            metrics = ticket_metrics(ticket, None, cfg.risk.mmr)
            rr_text = f"{metrics.rr:.2f}" if metrics.rr is not None else "-"
            print(
                f"  Bilet              : {ticket.id} [{ticket.status}] miktar {num(ticket.qty)}, "
                f"risk {money(metrics.risk_amount)} {quote_currency(ticket.symbol)} (hedef %{ticket.risk_pct:.2f}), "
                f"R:R {rr_text} (komisyon dahil)"
            )
        else:
            print("  Bilet              : yok - risk motoru reddetti:")
            for reason in setup.reasons:
                print(f"    - {reason}")
        print(DISCLAIMER)
        return EXIT_OK
    finally:
        journal.close()


@dataclass(slots=True)
class SendOutcome:
    """Result of handing a ticket to a broker. ``lines`` are the user-facing messages."""

    status: str  # sent | busy | rejected | failed | unknown | refused
    exit_code: int
    lines: list[str]
    orders: list[OrderResult] = field(default_factory=list)
    ticket: OrderTicket | None = None
    equity: float | None = None


def send_confirmed(prepared: OrderTicket, broker: Broker, journal: Journal, cfg: Config, mode: str) -> SendOutcome:
    """Claim, re-check and send an already approved ticket. Never asks for anything."""
    if not journal.claim_for_submission(prepared.id, broker=broker.name, mode=mode):
        return SendOutcome(
            "busy", EXIT_ABORTED, ["Bilet başka bir işlem tarafından gönderiliyor ya da gönderildi. Emir GÖNDERİLMEDİ."]
        )
    reasons = recheck_after_confirmation(prepared, broker, journal, cfg)
    if reasons:
        journal.release_claim(prepared.id, "onay sonrası kontrol reddetti: " + " | ".join(reasons))
        return SendOutcome(
            "rejected",
            EXIT_REJECTED,
            ["Onaydan sonra koşullar değişti; emir GÖNDERİLMEDİ:", *(f"  - {r}" for r in reasons)],
            ticket=prepared,
        )
    try:
        orders = broker.place_bracket(prepared)
    except OrderStateUnknown as exc:
        journal.set_ticket_status(prepared.id, "unknown", note=f"durum belirsiz: {exc}")
        return SendOutcome(
            "unknown",
            EXIT_UNKNOWN,
            [
                f"DİKKAT: emir durumu doğrulanamadı: {exc}",
                "Bilet 'unknown' olarak işaretlendi ve açık risk sayılıyor. Broker'da pozisyon ve emirleri kontrol edin.",
            ],
            ticket=prepared,
        )
    except BrokerError as exc:
        journal.release_claim(prepared.id, f"broker reddi: {exc}")
        return SendOutcome("failed", EXIT_ABORTED, [f"Emir gönderilemedi: {exc}"], ticket=prepared)
    except BaseException:
        journal.set_ticket_status(prepared.id, "unknown", note="gönderim yarıda kesildi; durum belirsiz")
        print("DİKKAT: gönderim yarıda kesildi; bilet 'unknown'. Broker'da pozisyon ve emirleri kontrol edin.")
        raise
    try:
        journal.mark_submitted(prepared, broker=broker.name, mode=mode, orders=orders)
    except Exception as exc:  # the orders are live; the ticket must not fall back to 'new'
        return SendOutcome(
            "unknown",
            EXIT_UNKNOWN,
            [
                f"DİKKAT: emirler gönderildi ama journal'a yazılamadı ({exc}).",
                "Bilet 'submitting' durumunda kalıyor ve açık risk sayılıyor. Gönderilen emirler:",
            ],
            orders,
            ticket=prepared,
        )
    return SendOutcome("sent", EXIT_OK, [f"Emir gönderildi ({broker.name}, {broker.environment}):"], orders, ticket=prepared)


def cmd_execute(args: argparse.Namespace, cfg: Config) -> int:
    journal = Journal(cfg.journal_path)
    try:
        ticket = journal.get_ticket(args.ticket_id.strip().lower())
        if ticket is None:
            print(f"Bilet bulunamadı: {args.ticket_id}")
            return EXIT_ABORTED
        if ticket.status != "new":
            print(f"Bilet {ticket.id} durumu '{ticket.status}'; yalnızca 'new' biletler gönderilebilir.")
            return EXIT_ABORTED
        notice = gate_notice(cfg, args.live)
        if notice:
            print(notice)
        live = live_enabled(cfg, args.live)
        if live and not sys.stdin.isatty():
            print(TTY_REFUSAL)
            journal.add_ticket_note(ticket.id, "canlı onay TTY dışından istendi; reddedildi")
            return EXIT_ABORTED
        mode = "live" if live else "paper"
        broker = make_broker(cfg, ticket.symbol, args.live)
        prepared, state, reasons = prepare_ticket(ticket, broker, journal, cfg)
        if reasons:
            print("EMİR ONAYA SUNULMADI:")
            for reason in reasons:
                print(f"  - {reason}")
            journal.add_ticket_note(ticket.id, "execute reddi: " + " | ".join(reasons))
            return EXIT_REJECTED
        print(render_ticket(prepared, state, cfg, broker, live))
        if not confirm(EXECUTE_PROMPT, live=live):
            print("Onaylanmadı. Emir GÖNDERİLMEDİ.")
            journal.add_ticket_note(ticket.id, "kullanıcı onaylamadı; emir gönderilmedi")
            return EXIT_ABORTED
        outcome = send_confirmed(prepared, broker, journal, cfg, mode)
        for line in outcome.lines:
            print(line)
        if outcome.orders:
            print_orders(outcome.orders)
        return outcome.exit_code
    finally:
        journal.close()


def cmd_positions(args: argparse.Namespace, cfg: Config) -> int:
    notice = gate_notice(cfg, args.live)
    if notice:
        print(notice)
    brokers = account_brokers(cfg, args.live)
    if not brokers:
        print("Kullanılabilir canlı hesap yok (API anahtarlarını .env'de tanımlayın).")
        return EXIT_ABORTED
    journal = Journal(cfg.journal_path)
    failed = False
    try:
        for broker in brokers:
            try:
                refresh(broker, journal)
                balance = broker.get_balance()
                print(
                    f"[{broker.name} - {broker.environment}] özsermaye {money(balance.equity)} {balance.currency}, "
                    f"serbest {money(balance.free)}"
                )
                positions = broker.get_positions()
                if not positions:
                    print("  Açık pozisyon yok.")
                for p in positions:
                    print(
                        f"  {p.symbol:<16} {p.side.value.upper():<5} miktar {num(p.qty)}  giriş {num(p.entry_price)}  "
                        f"stop {num(p.stop)}  hedef {num(p.take_profit)}  K/Z {num(p.unrealized_pnl)}"
                    )
                orders = broker.get_open_orders()
                if orders:
                    print("  Açık emirler:")
                    print_orders(orders)
            except Exception as exc:  # report and continue with the next account
                failed = True
                print(f"[{broker.name}] okunamadı: {exc}")
        return EXIT_ABORTED if failed else EXIT_OK
    finally:
        journal.close()


def _kill_broker(broker: Broker, journal: Journal, live: bool) -> list[str]:
    """Close positions, verify flat, then cancel orders of flat symbols. Returns what is still open."""
    problems: list[str] = []
    closed = broker.close_all()
    journal.record_orders(None, broker=broker.name, mode="live" if live else "paper", orders=closed)
    remaining = broker.get_positions()
    attempts = 1 if isinstance(broker, PaperBroker) else KILL_VERIFY_ATTEMPTS
    for _ in range(attempts - 1):
        if not remaining:
            break
        time.sleep(1.0)
        remaining = broker.get_positions()
    still_open = {p.symbol for p in remaining}
    order_symbols = {o.symbol for o in broker.get_open_orders()}
    cancelled = sum(broker.cancel_orders(symbol) for symbol in sorted(order_symbols - still_open))
    if isinstance(broker, PaperBroker):
        journal_paper_state(broker, journal)
    else:
        for ticket in journal.open_tickets(broker.name):
            if ticket.symbol not in still_open:
                journal.set_ticket_status(ticket.id, "closed", note="kill: pozisyon kapandı (doğrulandı), emirler iptal")
    done = [o for o in closed if o.status != "failed"]
    print(f"[{broker.name}] kapatılan pozisyon: {len(done)}, iptal edilen emir: {cancelled}")
    for failed in (o for o in closed if o.status == "failed"):
        print(f"  DİKKAT: {failed.symbol} {failed.note}")
    for position in remaining:
        problems.append(
            f"{broker.name}: {position.symbol} {position.side.value.upper()} miktar {num(position.qty)} hâlâ açık; "
            "bu sembolün koruyucu emirleri iptal edilmedi. Broker'da elle kapatın."
        )
    leftover = [o for o in broker.get_open_orders() if o.symbol not in still_open]
    if leftover:
        problems.append(
            f"{broker.name}: {len(leftover)} emir hâlâ açık görünüyor ({', '.join(sorted({o.symbol for o in leftover}))})."
        )
    return problems


def cmd_kill(args: argparse.Namespace, cfg: Config) -> int:
    notice = gate_notice(cfg, args.live)
    if notice:
        print(notice)
    live = live_enabled(cfg, args.live)
    brokers = account_brokers(cfg, args.live)
    if not brokers:
        print("Kullanılabilir canlı hesap yok (API anahtarlarını .env'de tanımlayın).")
        return EXIT_ABORTED
    journal = Journal(cfg.journal_path)
    try:
        for broker in brokers:
            try:
                refresh(broker, journal)
                positions, orders = broker.get_positions(), broker.get_open_orders()
                print(f"[{broker.name} - {broker.environment}] açık pozisyon: {len(positions)}, açık emir: {len(orders)}")
                for p in positions:
                    print(f"  {p.symbol:<16} {p.side.value.upper():<5} miktar {num(p.qty)}")
            except Exception as exc:  # still offer the kill; the account may be partly reachable
                print(f"[{broker.name}] durum okunamadı: {exc}")
        print("KILL: tüm pozisyonlar piyasa emriyle kapatılacak, kapandığı doğrulanan sembollerin emirleri iptal edilecek.")
        if not confirm(KILL_PROMPT, live=live):
            print("Onaylanmadı. Hiçbir pozisyon kapatılmadı, hiçbir emir iptal edilmedi.")
            return EXIT_ABORTED
        problems: list[str] = []
        for broker in brokers:
            try:
                problems.extend(_kill_broker(broker, journal, live))
            except Exception as exc:  # report and continue with the next account
                problems.append(f"{broker.name}: {exc}")
        if problems:
            print("KILL TAMAMLANAMADI - hâlâ açık olanlar / hatalar:")
            for problem in problems:
                print(f"  - {problem}")
            return EXIT_ABORTED
        print("KILL tamamlandı: pozisyonlar kapalı, emirler iptal edildi.")
        return EXIT_OK
    finally:
        journal.close()


def cmd_stats(args: argparse.Namespace, cfg: Config) -> int:
    journal = Journal(cfg.journal_path)
    try:
        if cfg.paper_state_path.exists():
            journal_paper_state(PaperBroker(cfg.paper_state_path, initial_equity=cfg.paper.equity), journal)
        stats = journal.stats()
        profit_factor = "sonsuz (kayıp yok)" if stats.profit_factor == float("inf") else f"{stats.profit_factor:.2f}"
        rows = [
            ("İşlem sayısı", f"{stats.trades} (kazanç {stats.wins}, kayıp {stats.losses})"),
            ("Kazanma oranı", f"%{stats.win_rate * 100:.1f}"),
            ("Ortalama R", f"{stats.avg_r:.2f}"),
            ("Beklenti (R)", f"{stats.expectancy:.2f}"),
            ("Profit factor", profit_factor),
            ("Maks. drawdown", f"{stats.max_drawdown_r:.2f} R"),
            ("Toplam", f"{stats.total_r:.2f} R"),
        ]
        for label, value in rows:
            print(f"{label:<18}: {value}")
        if stats.trades < 30:
            print("Not: 30'dan az işlem var; bu istatistikler henüz anlamlı değil (30-50+, tercihen 100+ işlem).")
        return EXIT_OK
    finally:
        journal.close()


def cmd_risk_calc(args: argparse.Namespace, cfg: Config) -> int:
    if not math.isfinite(args.leverage) or args.leverage < 1:
        print(f"Hata: kaldıraç geçersiz: {args.leverage!r} (en az 1x olmalı).")
        return EXIT_REJECTED
    implied = "long" if args.stop < args.entry else "short"
    if args.side and args.side != implied:
        print(f"Hata: stop {num(args.stop)} giriş {num(args.entry)} ile '{args.side}' tarafına uymuyor ({implied} olmalı).")
        return EXIT_REJECTED
    try:
        qty = position_size(args.equity, args.risk, args.entry, args.stop, cfg.fee_rate)
        liquidation = liquidation_price(args.entry, args.leverage, implied, cfg.risk.mmr) if args.leverage > 1 else None
    except ValueError as exc:
        print(f"Hata: {exc}")
        return EXIT_REJECTED
    notional = qty * args.entry
    stop_distance = abs(args.entry - args.stop)
    print(f"Yön               : {implied.upper()}")
    print(f"Miktar            : {num(qty)}")
    print(f"Pozisyon değeri   : {money(notional)}")
    print(f"Risk tutarı       : {money(args.equity * args.risk / 100)} (komisyon %{cfg.fee_rate * 100:.3f} x2 dahil)")
    print(f"Stop mesafesi     : {num(stop_distance)} (%{stop_distance / args.entry * 100:.2f})")
    print(f"Gerekli marjin    : {money(notional / args.leverage)} ({num(args.leverage)}x)")
    if liquidation is not None:
        liq_distance = (args.entry - liquidation) * (1 if implied == "long" else -1)
        enough = liq_distance >= cfg.risk.liq_stop_multiple * stop_distance
        verdict = "yeterli" if enough else "ÇOK YAKIN - kaldıracı düşürün"
        print(f"Likidasyon        : {num(liquidation)} (tahmini)  mesafe {num(liq_distance)} -> {verdict}")
    if notional / args.leverage > args.equity:
        print("Uyarı: gereken teminat özsermayeyi aşıyor; risk motoru bu bileti reddeder.")
    if args.risk > cfg.risk.max_risk_pct:
        print(f"Uyarı: risk %{args.risk} ayardaki üst sınırı (%{cfg.risk.max_risk_pct}) aşıyor.")
    if args.leverage > cfg.risk.max_leverage:
        print(f"Uyarı: kaldıraç {num(args.leverage)}x ayardaki üst sınırı ({num(cfg.risk.max_leverage)}x) aşıyor.")
    return EXIT_OK


# --- watch (24/7 scanning, alerts, demo-only automation) -------------------------------------------------

WAKE_DELAY_SECONDS = 20
_sleep = time.sleep


def next_wake(now: datetime, interval_minutes: int) -> datetime:
    """The next interval boundary (candle close) plus a short delay for the exchange to publish it."""
    step = interval_minutes * 60
    base = math.floor((now.timestamp() - WAKE_DELAY_SECONDS) / step) * step + step + WAKE_DELAY_SECONDS
    return datetime.fromtimestamp(base, UTC)


def auto_problems(cfg: Config, symbols: list[str], live_flag: bool) -> list[str]:
    """Why ``--demo-auto`` must not run for these symbols: any real-money target is a problem."""
    if not live_enabled(cfg, live_flag):
        return []  # local paper simulation: no money can move
    problems = []
    for symbol in symbols:
        kind = asset_class(symbol)
        if kind == "bist":
            problems.append(f"{symbol}: BIST için demo/canlı emir yolu yok; yalnızca paper (--live olmadan).")
        elif kind == "crypto" and not cfg.crypto.testnet:
            problems.append(f"{symbol}: crypto.testnet: false = GERÇEK PARA hesabı; otomatik mod yalnızca demo hesapta çalışır.")
        elif kind not in ("crypto", "bist") and not cfg.alpaca.paper:
            problems.append(f"{symbol}: alpaca.paper: false = GERÇEK PARA hesabı; otomatik mod yalnızca paper hesapta çalışır.")
    return problems


def submit_unattended(ticket_id: str, broker: Broker, journal: Journal, cfg: Config, mode: str = "paper") -> SendOutcome:
    """Send a ticket without asking: only ever on a demo/paper broker, with every risk and stale check."""
    if not getattr(broker, "is_demo", False):
        return SendOutcome(
            "refused",
            EXIT_REJECTED,
            [f"Otomatik gönderim yalnızca demo/paper hesapta yapılır; {broker.name} ({broker.environment}) değil. Emir GÖNDERİLMEDİ."],
        )
    ticket = journal.get_ticket(ticket_id)
    if ticket is None or ticket.status != "new":
        return SendOutcome("refused", EXIT_ABORTED, [f"Bilet {ticket_id} gönderilebilir durumda değil."])
    prepared, state, reasons = prepare_ticket(ticket, broker, journal, cfg)
    if reasons:
        journal.add_ticket_note(ticket.id, "otomatik gönderim reddi: " + " | ".join(reasons))
        return SendOutcome(
            "rejected",
            EXIT_REJECTED,
            ["Otomatik gönderilmedi (risk/bayat bilet kontrolü):", *(f"  - {r}" for r in reasons)],
            ticket=prepared,
            equity=state.equity,
        )
    journal.add_ticket_note(ticket.id, f"watch --demo-auto: onaysız gönderim ({broker.name}, {broker.environment})")
    outcome = send_confirmed(prepared, broker, journal, cfg, mode)
    outcome.equity = state.equity
    return outcome


def _order_lines(orders: list[OrderResult]) -> list[str]:
    return [f"  {o.role} {o.side} {num(o.qty)} @ {num(o.price)} [{o.status}]" for o in orders]


def announce_updates(journal: Journal, channels: list) -> None:
    """Tell the user when an announced ticket's entry fills or the trade ends."""
    for row in journal.tickets_to_announce("open", ("open",)):
        if not journal.claim_notification(row["id"], "open"):
            continue
        alert = notify.Alert(
            f"{row['side'].upper()} {row['symbol']} - giriş doldu, pozisyon açık",
            f"Giriş: {num(row['entry'])}  stop: {num(row['stop'])}\nBilet: {row['id']}\n"
            "Stop ve take-profit emirleri borsada bekliyor.",
        )
        _send(channels, alert)
    for row in journal.tickets_to_announce("closed", ("closed", "canceled")):
        if not journal.claim_notification(row["id"], "closed"):
            continue
        stats = journal.stats()
        result = f"{row['result_r']:+.2f}R" if row["result_r"] is not None else "R bilinmiyor"
        pnl = f"{row['pnl']:+,.2f} {quote_currency(row['symbol'])}" if row["pnl"] is not None else "bilinmiyor"
        verb = "kapandı" if row["status"] == "closed" else "iptal edildi"
        alert = notify.Alert(
            f"{row['side'].upper()} {row['symbol']} - {verb} ({result})",
            f"Sonuç: {result}\nKâr/zarar: {pnl}\nBilet: {row['id']}\n"
            f"Toplam: {stats.trades} işlem, kazanma %{stats.win_rate * 100:.1f}, beklenti {stats.expectancy:+.2f}R, "
            f"toplam {stats.total_r:+.2f}R",
        )
        _send(channels, alert)


def _send(channels: list, alert: notify.Alert) -> None:
    _, errors = notify.broadcast(channels, alert)
    for error in errors:
        print(f"Uyarı: bildirim gönderilemedi - {error}")


def watch_cycle(args: argparse.Namespace, cfg: Config, journal: Journal, channels: list) -> None:
    """One wake-up: scan, report fills/closures, then alert (or demo-auto-send) every new ticket."""
    live = live_enabled(cfg, args.live)
    stamp = datetime.now(UTC).astimezone(ISTANBUL)
    print(f"--- tarama {stamp:%d.%m.%Y %H:%M} (Türkiye saati) ---")
    fresh = run_scan(parse_symbols(args.symbols), args.tf, args.htf, args.limit, cfg, journal)
    brokers: dict[str, Broker] = {}

    def live_for(symbol: str) -> bool:
        return live and asset_class(symbol) != "bist"  # BIST has no live/demo route: it stays on paper

    def broker_for(symbol: str) -> Broker:
        key = asset_class(symbol) if live_for(symbol) else "paper"
        if key not in brokers:
            brokers[key] = make_broker(cfg, symbol, live_for(symbol))
        return brokers[key]

    for symbol in parse_symbols(args.symbols):
        if live_for(symbol):
            broker_for(symbol)
    for broker in [b for key, b in brokers.items() if key != "paper"]:
        refresh(broker, journal)
    announce_updates(journal, channels)
    stats = journal.stats()
    for setup in fresh:
        if setup.ticket is None:
            continue
        current = journal.get_ticket(setup.ticket.id)
        if current is None or current.status != "new":
            continue
        broker = broker_for(current.symbol)
        details = dict(
            mmr=cfg.risk.mmr,
            stats=stats,
            narrative=setup.narrative,
            killzone=setup.killzone,
            valid_minutes=cfg.execution.ticket_max_age_minutes,
        )
        if args.demo_auto:
            outcome = submit_unattended(current.id, broker, journal, cfg, "live" if live_for(current.symbol) else "paper")
            if outcome.status == "rejected":
                for line in outcome.lines:
                    print(line)
                continue  # retried next wake-up while the ticket is still fresh
            if not journal.claim_notification(current.id, "new"):
                continue
            if outcome.status == "sent" and outcome.ticket is not None:
                alert = notify.ticket_alert(
                    outcome.ticket,
                    equity=outcome.equity or 0.0,
                    headline="demo hesapta otomatik gönderildi",
                    action="\n".join(
                        [f"Hesap: {broker.name} ({broker.environment})", "Gönderilen emirler:", *_order_lines(outcome.orders),
                         "Hepsini kapatmak için: tradeagent kill" + (" --live" if live_for(current.symbol) else "")]
                    ),
                    **details,
                )
            else:
                alert = notify.Alert(
                    f"DİKKAT {current.symbol} - otomatik emir sorunu ({outcome.status})",
                    "\n".join([*outcome.lines, *_order_lines(outcome.orders), f"Bilet: {current.id}"]),
                )
            for line in outcome.lines:
                print(line)
            _send(channels, alert)
            continue
        prepared, state, reasons = prepare_ticket(current, broker, journal, cfg)
        if reasons:
            print(f"  [{current.id}] şu an onaya uygun değil:")
            for reason in reasons:
                print(f"    - {reason}")
            continue
        if not journal.claim_notification(current.id, "new"):
            continue
        command = f"tradeagent execute {current.id}" + (" --live" if live_for(current.symbol) else "")
        action = (
            "Onaylamak için bilgisayarınızda şu komutu çalıştırın, ekrandaki son kontrolden sonra ONAY yazın:\n"
            f"  {command}\nOnaylamazsanız hiçbir emir gönderilmez."
        )
        alert = notify.ticket_alert(prepared, equity=state.equity, action=action, **details)
        _send(channels, alert)


def cmd_watch(args: argparse.Namespace, cfg: Config) -> int:
    interval = args.interval
    if interval is None:
        frame = _TIMEFRAMES.get(args.tf)
        interval = int(frame.total_seconds() // 60) if frame is not None else 15
    if not 1 <= interval <= 1440:
        print("Hata: --interval 1 ile 1440 dakika arasında olmalı.")
        return EXIT_REJECTED
    if not 1 <= args.limit <= MAX_CANDLES:
        print(f"Hata: --limit 1 ile {MAX_CANDLES} arasında olmalı.")
        return EXIT_REJECTED
    symbols = parse_symbols(args.symbols)
    if not symbols:
        print("Hata: en az bir sembol verin (--symbols).")
        return EXIT_REJECTED
    notice = gate_notice(cfg, args.live)
    if notice:
        print(notice)
    live = live_enabled(cfg, args.live)
    if args.demo_auto:
        problems = auto_problems(cfg, symbols, args.live)
        if problems:
            print("OTOMATİK MOD REDDEDİLDİ - yalnızca demo/paper hesaplarda çalışır:")
            for problem in problems:
                print(f"  - {problem}")
            return EXIT_REJECTED
    load_dotenv(_env_path(cfg))  # Telegram credentials (and API keys for --live)
    channels = notify.notifiers_from_env(desktop=not args.no_desktop)
    if args.demo_auto:
        behaviour = "DEMO OTOMATİK: uygun bilet onay beklemeden demo/paper hesaba gönderilir"
    else:
        behaviour = "BİLDİRİM: bilet bildirilir, emir yalnızca sizin execute + ONAY onayınızla gider"
    account = ("canlı ayar: " + ("kripto demo" if cfg.crypto.testnet else "kripto GERÇEK PARA")) if live else "yerel paper"
    _send(
        channels,
        notify.Alert(
            "tradeagent izleme başladı",
            f"Semboller: {', '.join(symbols)}\nZaman dilimi: {args.tf} (bias {args.htf}), her {interval} dk\n"
            f"Mod: {behaviour}\nHesap: {account}\nKanallar: {', '.join(c.name for c in channels)}",
        ),
    )
    journal = Journal(cfg.journal_path)
    failing = False
    try:
        while True:
            try:
                watch_cycle(args, cfg, journal, channels)
                failing = False
            except Exception as exc:  # one bad wake-up must not end a 24/7 watch
                print(f"Tarama hatası ({type(exc).__name__}): {exc}")
                if not failing:
                    _send(
                        channels,
                        notify.Alert(
                            "tradeagent izleme hatası",
                            f"{type(exc).__name__}: {exc}\nİzleme devam ediyor; bir sonraki taramada yeniden denenecek.",
                        ),
                    )
                failing = True
            if args.once:
                return EXIT_ABORTED if failing else EXIT_OK
            now = datetime.now(UTC)
            wake = next_wake(now, interval)
            print(f"Sonraki tarama: {wake.astimezone(ISTANBUL):%H:%M:%S} (Türkiye saati). Durdurmak için Ctrl+C.")
            _sleep(max(0.0, (wake - now).total_seconds()))
    except KeyboardInterrupt:
        print("İzleme durduruldu.")
        return EXIT_OK
    finally:
        journal.close()


# --- entry point --------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tradeagent",
        description="Onaylı ICT/SMC trade ajanı. Yatırım tavsiyesi değildir; emirler yalnızca ONAY ile gider.",
    )
    parser.add_argument("--config", type=Path, help="ayar dosyası (varsayılan: bulunduğunuz klasördeki config.yaml, yoksa ~/trading-agent/config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="sembolleri tarayıp kurulum ve bilet üretir")
    scan.add_argument("--symbols", required=True, help="virgülle ayrılmış, ör. BTC/USDT,ETH/USDT,THYAO.IS,AAPL")
    scan.add_argument("--tf", default="15m", help="giriş zaman dilimi (varsayılan 15m)")
    scan.add_argument("--htf", default="1d", help="bias zaman dilimi (varsayılan 1d)")
    scan.add_argument("--limit", type=int, default=300, help=f"indirilecek mum sayısı (1-{MAX_CANDLES}, varsayılan 300)")

    plan = sub.add_parser("plan", help="bir kurulumun planını ve biletini gösterir")
    plan.add_argument("setup_id")

    execute = sub.add_parser("execute", help="bileti risk kontrolü ve ONAY ekranıyla gönderir")
    execute.add_argument("ticket_id")
    execute.add_argument("--live", action="store_true", help="canlı broker (ayrıca config'de mode: live gerekir)")

    positions = sub.add_parser("positions", help="pozisyonları ve açık emirleri gösterir")
    positions.add_argument("--live", action="store_true", help="canlı hesap (ayrıca config'de mode: live gerekir)")

    kill = sub.add_parser("kill", help="pozisyonları kapatıp emirleri iptal eder (ONAY ister)")
    kill.add_argument("--live", action="store_true", help="canlı hesap (ayrıca config'de mode: live gerekir)")

    sub.add_parser("stats", help="journal'dan R bazlı istatistikler")

    watch = sub.add_parser("watch", help="7/24 izleme: periyodik tarama + bildirim (demo'da isteğe bağlı otomatik)")
    watch.add_argument("--symbols", required=True, help="virgülle ayrılmış, ör. BTC/USDT:USDT,ETH/USDT:USDT")
    watch.add_argument("--tf", default="15m", help="giriş zaman dilimi (varsayılan 15m)")
    watch.add_argument("--htf", default="1d", help="bias zaman dilimi (varsayılan 1d)")
    watch.add_argument("--limit", type=int, default=300, help=f"indirilecek mum sayısı (1-{MAX_CANDLES})")
    watch.add_argument("--interval", type=int, help="tarama aralığı, dakika (varsayılan: --tf süresi)")
    watch.add_argument("--once", action="store_true", help="tek tarama yapıp çık (Görev Zamanlayıcı için)")
    watch.add_argument("--no-desktop", action="store_true", help="Windows masaüstü bildirimini kapat")
    watch.add_argument("--live", action="store_true", help="hesap olarak config'deki canlı/demo broker'ı kullan")
    watch.add_argument(
        "--demo-auto",
        action="store_true",
        help="YALNIZCA demo/paper hesapta: uygun bileti onay beklemeden gönderir (gerçek para hesabında reddedilir)",
    )

    risk = sub.add_parser("risk-calc", help="pozisyon büyüklüğü ve likidasyon hesabı")
    risk.add_argument("--equity", type=float, required=True, help="hesap özsermayesi")
    risk.add_argument("--risk", type=float, required=True, help="işlem başına risk, yüzde (ör. 0.5)")
    risk.add_argument("--entry", type=float, required=True, help="giriş fiyatı")
    risk.add_argument("--stop", type=float, required=True, help="stop fiyatı")
    risk.add_argument("--leverage", type=float, default=1.0, help="kaldıraç (varsayılan 1)")
    risk.add_argument("--side", choices=["long", "short"], help="yön (verilmezse stop'a göre bulunur)")
    return parser


COMMANDS = {
    "scan": cmd_scan,
    "plan": cmd_plan,
    "execute": cmd_execute,
    "positions": cmd_positions,
    "kill": cmd_kill,
    "stats": cmd_stats,
    "watch": cmd_watch,
    "risk-calc": cmd_risk_calc,
}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        if stream.isatty():
            reconfigure(errors="replace")
        else:
            # Pipes and mintty/Git Bash expect UTF-8, not the Windows ANSI code page.
            reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    config_path = args.config
    if config_path is None and (Path.cwd() / "config.yaml").is_file():
        config_path = Path.cwd() / "config.yaml"
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"Ayar hatası: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    try:
        return COMMANDS[args.command](args, cfg)
    except KeyboardInterrupt:
        print("İptal edildi (Ctrl+C).", file=sys.stderr)
        return 130
    except (BrokerError, ConfigError, JournalError, ValueError) as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return EXIT_ABORTED
    except Exception as exc:  # last resort: a Turkish message instead of a traceback
        print(f"Beklenmeyen hata ({type(exc).__name__}): {exc}", file=sys.stderr)
        return EXIT_ABORTED


if __name__ == "__main__":
    raise SystemExit(main())
