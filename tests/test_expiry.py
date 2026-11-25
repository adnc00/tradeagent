from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from tradeagent import cli
from tradeagent.brokers.ccxt_broker import client_order_ids
from tradeagent.brokers.paper import PaperBroker
from tradeagent.config import load_config
from tradeagent.journal import Journal
from tradeagent.models import OrderTicket
from tests.test_ccxt_broker import FakeExchange, broker_with, make_ticket
from tests.test_cli import StubMarketData, answer, scanned_ticket_id, ticket_status, write_config
from tests.test_watch import Inbox

NOW = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)


def ticket(side="long", **overrides) -> OrderTicket:
    values = dict(
        id="t-1", setup_id="s-1", symbol="BTC/USDT", side=side, entry=100.0,
        stop=99.0 if side == "long" else 101.0, targets=[104.0] if side == "long" else [96.0],
        qty=1.0, risk_pct=0.5, fee_rate=0.001, created_at=NOW,
    )
    values.update(overrides)
    return OrderTicket(**values)


@pytest.fixture
def cfg(tmp_path):
    return load_config(write_config(tmp_path))


# --- stale guard: a resting limit may wait on the target side ----------------------------------------


@pytest.mark.parametrize("side,price", [("long", 103.0), ("long", 100.3), ("short", 97.0), ("short", 99.7)])
def test_price_on_the_target_side_of_entry_is_not_stale(cfg, side, price):
    assert cli.stale_reasons(ticket(side), price, cfg, NOW) == []


@pytest.mark.parametrize("side,price", [("long", 104.0), ("long", 104.5), ("short", 96.0), ("short", 95.0)])
def test_price_at_or_beyond_target_means_the_move_is_gone(cfg, side, price):
    reasons = cli.stale_reasons(ticket(side), price, cfg, NOW)
    assert any("hedef" in r for r in reasons)


@pytest.mark.parametrize("side,price", [("long", 99.4), ("short", 100.6)])
def test_price_through_entry_towards_stop_is_stale(cfg, side, price):
    reasons = cli.stale_reasons(ticket(side), price, cfg, NOW)
    assert any("sapma" in r for r in reasons)


@pytest.mark.parametrize("side,price", [("long", 99.8), ("short", 100.2)])
def test_small_move_through_entry_is_tolerated(cfg, side, price):
    assert cli.stale_reasons(ticket(side), price, cfg, NOW) == []


def test_entry_expiry_setting(tmp_path):
    assert load_config(write_config(tmp_path)).execution.entry_expiry_minutes == 120
    cfg = load_config(write_config(tmp_path, extra="execution:\n  entry_expiry_minutes: 45\n"))
    assert cfg.execution.entry_expiry_minutes == 45


# --- unfilled entries expire ----------------------------------------------------------------------------


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.setattr(StubMarketData, "price", None)
    monkeypatch.setattr(cli, "MarketData", StubMarketData)
    return write_config(tmp_path)


def _submit(config_path, monkeypatch, *, minutes_ago: float) -> str:
    ticket_id = scanned_ticket_id(config_path)
    answer(monkeypatch, "ONAY")
    assert cli.main(["--config", str(config_path), "execute", ticket_id]) == 0
    journal = Journal(load_config(config_path).journal_path)
    try:
        moment = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()
        with journal._conn:
            journal._conn.execute("UPDATE tickets SET submitted_at = ? WHERE id = ?", (moment, ticket_id))
    finally:
        journal.close()
    return ticket_id


def _expire(config_path):
    cfg = load_config(config_path)
    broker = PaperBroker(cfg.paper_state_path, initial_equity=cfg.paper.equity)
    journal = Journal(cfg.journal_path)
    try:
        return cli.expire_entries(broker, journal, cfg, StubMarketData(), datetime.now(UTC))
    finally:
        journal.close()


def test_unfilled_entry_is_cancelled_after_the_expiry(config_path, monkeypatch):
    ticket_id = _submit(config_path, monkeypatch, minutes_ago=121)
    (expired,) = _expire(config_path)
    assert expired[0] == ticket_id and "dolmadı" in expired[1]
    assert ticket_status(config_path, ticket_id) == "canceled"
    cfg = load_config(config_path)
    assert [b.status for b in PaperBroker(cfg.paper_state_path).brackets] == ["canceled"]


def test_fresh_unfilled_entry_keeps_waiting(config_path, monkeypatch):
    ticket_id = _submit(config_path, monkeypatch, minutes_ago=10)
    assert _expire(config_path) == []
    assert ticket_status(config_path, ticket_id) == "submitted"


def test_entry_is_cancelled_when_price_reached_target_first(config_path, monkeypatch):
    ticket_id = _submit(config_path, monkeypatch, minutes_ago=10)
    original = StubMarketData.get_ohlcv

    def spiked(self, symbol, timeframe, limit=500):
        df = original(self, symbol, timeframe, limit).copy()
        if timeframe == "1m":
            df.index = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=len(df), freq="1min")
            df.iloc[-1, df.columns.get_loc("high")] = 110.0
        return df

    monkeypatch.setattr(StubMarketData, "get_ohlcv", spiked)
    (expired,) = _expire(config_path)
    assert expired[0] == ticket_id and "hedef" in expired[1]
    assert ticket_status(config_path, ticket_id) == "canceled"


def test_watch_cancels_expired_entries_and_says_why(config_path, monkeypatch):
    ticket_id = _submit(config_path, monkeypatch, minutes_ago=200)
    journal = Journal(load_config(config_path).journal_path)
    try:
        journal.claim_notification(ticket_id, "new")
    finally:
        journal.close()
    box = Inbox()
    monkeypatch.setattr(cli.notify, "notifiers_from_env", lambda *a, **k: [box])
    assert cli.main(["--config", str(config_path), "watch", "--symbols", "BTC/USDT", "--once"]) == 0
    assert ticket_status(config_path, ticket_id) == "canceled"
    (alert,) = [a for a in box.alerts if "iptal" in a.title]
    assert "dolmadı" in alert.body


# --- ccxt: cancel only an unfilled entry -----------------------------------------------------------------


def test_ccxt_cancel_entry_cancels_the_working_entry_and_its_legs():
    fake = FakeExchange()
    broker = broker_with(fake)
    t = make_ticket()
    broker.place_bracket(t)
    assert broker.cancel_entry(t) == "canceled"
    assert {o["status"] for o in fake.orders.values()} == {"canceled"}


def test_ccxt_cancel_entry_leaves_a_filled_entry_alone():
    fake = FakeExchange()
    broker = broker_with(fake)
    t = make_ticket()
    broker.place_bracket(t)
    ids = client_order_ids(t.id)
    fake.orders[ids["entry"]]["status"] = "closed"
    fake.positions = [{"symbol": t.symbol, "side": "long", "contracts": 1.0, "contractSize": 1.0}]
    assert broker.cancel_entry(t) == "filled"
    assert fake.orders[ids["stop"]]["status"] == "open"
    assert fake.cancels() == []


def test_ccxt_entry_filling_during_cancel_keeps_protection():
    fake = FakeExchange()
    broker = broker_with(fake)
    t = make_ticket()
    broker.place_bracket(t)
    ids = client_order_ids(t.id)
    original = fake.cancel_order

    def fill_then_cancel(id, symbol=None, params=None):
        fake.positions = [{"symbol": t.symbol, "side": "long", "contracts": 1.0, "contractSize": 1.0}]
        fake.orders[ids["entry"]]["status"] = "closed"
        raise __import__("ccxt").OrderNotFound("already filled")

    fake.cancel_order = fill_then_cancel
    assert broker.cancel_entry(t) == "filled"
    assert fake.orders[ids["stop"]]["status"] == "open"
    fake.cancel_order = original


# --- a ticket being submitted belongs to the submitting process ------------------------------------


class RejectingLiveBroker:
    """A live-like broker: no position, no orders, and it refuses the bracket."""

    name, environment, is_demo = "ccxt:binanceusdm", "demo trading", True

    def __init__(self):
        self.reconciled = []

    def reconcile_ticket(self, ticket):
        self.reconciled.append(ticket.id)
        return "closed"

    def get_balance(self):
        from tradeagent.brokers.base import Balance
        return Balance(equity=10_000.0, free=10_000.0, currency="USDT")

    def get_price(self, symbol):
        return 100.65 * 1.001

    def place_bracket(self, ticket):
        raise cli.BrokerError("stop reddedildi: -4509")


def test_reconcile_never_touches_a_ticket_being_submitted(config_path, monkeypatch):
    ticket_id = scanned_ticket_id(config_path)
    cfg = load_config(config_path)
    broker = RejectingLiveBroker()
    journal = Journal(cfg.journal_path)
    try:
        assert journal.claim_for_submission(ticket_id, broker=broker.name, mode="live")
        cli.reconcile_live_tickets(broker, journal)
        assert broker.reconciled == []
        assert journal.get_ticket(ticket_id).status == "submitting"
    finally:
        journal.close()


def test_a_rejected_bracket_returns_the_ticket_to_new(config_path, monkeypatch):
    ticket_id = scanned_ticket_id(config_path)
    cfg = load_config(config_path)
    broker = RejectingLiveBroker()
    journal = Journal(cfg.journal_path)
    try:
        prepared = journal.get_ticket(ticket_id)
        outcome = cli.send_confirmed(prepared, broker, journal, cfg, "live")
        assert outcome.status == "failed"
        assert journal.get_ticket(ticket_id).status == "new"
    finally:
        journal.close()
