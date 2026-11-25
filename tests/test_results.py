from datetime import UTC, datetime, timedelta

import pytest

from tradeagent import cli
from tradeagent.brokers.base import TicketResult
from tradeagent.journal import Journal
from tradeagent.models import Side
from tests.test_ccxt_broker import SYMBOL, FakeExchange, broker_with, make_ticket

SENT = datetime(2026, 11, 25, 17, 0, tzinfo=UTC)


def trade(minutes, side, amount, price, pnl="0", fee=0.0, fee_ccy="USDT", symbol=SYMBOL):
    ts = SENT + timedelta(minutes=minutes)
    return {
        "symbol": symbol, "timestamp": int(ts.timestamp() * 1000), "datetime": ts.isoformat(), "side": side,
        "amount": amount, "price": price, "fee": {"cost": fee, "currency": fee_ccy},
        "info": {"realizedPnl": pnl},
    }


def with_trades(trades) -> FakeExchange:
    fake = FakeExchange()
    fake.fetch_my_trades = lambda symbol=None, since=None, limit=None, params=None: [
        t for t in trades if t["symbol"] == symbol and (since is None or t["timestamp"] >= since)
    ]
    return fake


# --- ccxt: result from the venue's own fills ---------------------------------------------------------


def test_stopped_long_gives_negative_r_with_fees():
    # long 1.234 @ 100, stop 98: realized -2.468, fees 0.06 + 0.06
    fake = with_trades([
        trade(1, "buy", 1.234, 100.0, fee=0.06),
        trade(5, "sell", 1.234, 98.0, pnl="-2.468", fee=0.06),
    ])
    result = broker_with(fake).ticket_result(make_ticket(), SENT)
    assert result.status == "closed"
    assert result.pnl == pytest.approx(-2.468 - 0.12)
    assert result.exit_reason == "stop"
    assert result.exit_price == pytest.approx(98.0)
    assert result.closed_at == SENT + timedelta(minutes=5)


def test_take_profit_in_two_fills_is_one_result():
    fake = with_trades([
        trade(1, "buy", 1.234, 100.0, fee=0.06),
        trade(9, "sell", 1.0, 106.0, pnl="6.0", fee=0.05),
        trade(9, "sell", 0.234, 106.0, pnl="1.404", fee=0.01),
    ])
    result = broker_with(fake).ticket_result(make_ticket(), SENT)
    assert result.status == "closed" and result.exit_reason == "take_profit"
    assert result.pnl == pytest.approx(7.404 - 0.12)


def test_no_entry_fill_means_unfilled():
    result = broker_with(with_trades([])).ticket_result(make_ticket(), SENT)
    assert result.status == "unfilled"


def test_trades_before_the_ticket_and_other_fees_are_ignored():
    fake = with_trades([
        trade(-30, "sell", 5.0, 120.0, pnl="9.0", fee=1.0),  # an older trade on the symbol
        trade(1, "buy", 1.234, 100.0, fee=0.06),
        trade(5, "sell", 1.234, 98.0, pnl="-2.468", fee=0.001, fee_ccy="BNB"),
    ])
    result = broker_with(fake).ticket_result(make_ticket(), SENT)
    assert result.pnl == pytest.approx(-2.468 - 0.06)


def test_position_still_open_is_not_a_result():
    fake = with_trades([trade(1, "buy", 1.234, 100.0, fee=0.06), trade(4, "sell", 0.5, 98.0, pnl="-1.0")])
    assert broker_with(fake).ticket_result(make_ticket(), SENT) is None


def test_short_exit_reason_and_sign():
    ticket = make_ticket(side=Side.SHORT, entry=100.0, stop=102.0, targets=[94.0])
    fake = with_trades([trade(1, "sell", 1.234, 100.0, fee=0.06), trade(3, "buy", 1.234, 102.0, pnl="-2.468", fee=0.06)])
    result = broker_with(fake).ticket_result(ticket, SENT)
    assert result.exit_reason == "stop" and result.pnl < 0


# --- journal ---------------------------------------------------------------------------------------------------


class ResultBroker:
    name, environment, is_demo = "ccxt:binanceusdm", "demo trading", True

    def __init__(self, result, reconcile="closed"):
        self.result, self.reconcile = result, reconcile

    def reconcile_ticket(self, ticket):
        return self.reconcile

    def ticket_result(self, ticket, since):
        return self.result


def _sent_ticket(tmp_path, risk_amount=2.5):
    journal = Journal(tmp_path / "j.sqlite")
    ticket = make_ticket()
    journal._write_ticket(ticket, only_if_new=True)
    with journal._conn:
        journal._conn.execute(
            "UPDATE tickets SET status='submitted', broker='ccxt:binanceusdm', mode='live', submitted_at=?, risk_amount=? WHERE id=?",
            (SENT.isoformat(), risk_amount, ticket.id),
        )
    return journal, ticket


def test_reconcile_records_r_and_pnl_from_the_venue(tmp_path):
    journal, ticket = _sent_ticket(tmp_path)
    result = TicketResult("closed", pnl=-2.588, exit_price=98.0, exit_reason="stop", closed_at=SENT + timedelta(minutes=5))
    try:
        cli.reconcile_live_tickets(ResultBroker(result), journal)
        row = journal.ticket_row(ticket.id)
        assert row["status"] == "closed"
        assert row["pnl"] == pytest.approx(-2.588)
        assert row["result_r"] == pytest.approx(-2.588 / 2.5)
        assert journal.stats().trades == 1
    finally:
        journal.close()


def test_reconcile_marks_an_unfilled_ticket_canceled(tmp_path):
    journal, ticket = _sent_ticket(tmp_path)
    try:
        cli.reconcile_live_tickets(ResultBroker(TicketResult("unfilled")), journal)
        assert journal.ticket_row(ticket.id)["status"] == "canceled"
    finally:
        journal.close()


def test_reconcile_without_result_keeps_the_old_note(tmp_path):
    journal, ticket = _sent_ticket(tmp_path)
    try:
        cli.reconcile_live_tickets(ResultBroker(None), journal)
        row = journal.ticket_row(ticket.id)
        assert row["status"] == "closed" and row["result_r"] is None
    finally:
        journal.close()


def test_backfill_fills_in_closed_tickets_without_r(tmp_path):
    journal, ticket = _sent_ticket(tmp_path)
    result = TicketResult("closed", pnl=5.0, exit_price=106.0, exit_reason="take_profit", closed_at=SENT + timedelta(hours=1))
    try:
        journal.set_ticket_status(ticket.id, "closed", note="sonuç R bilinmiyor")
        assert cli.backfill_results(ResultBroker(result), journal) == [ticket.id]
        assert journal.ticket_row(ticket.id)["result_r"] == pytest.approx(2.0)
        assert cli.backfill_results(ResultBroker(result), journal) == []
    finally:
        journal.close()


# --- equity of the account the panel acts on ---------------------------------------------------------


def _snapshots(path, rows):
    journal = Journal(path)
    try:
        with journal._conn:
            for ts, broker, equity in rows:
                journal._conn.execute("INSERT INTO equity_snapshots (ts, broker, equity) VALUES (?, ?, ?)", (ts, broker, equity))
    finally:
        journal.close()


def test_live_panel_shows_the_live_account_equity(tmp_path):
    from tradeagent import panel
    from tradeagent.config import load_config
    from tests.test_cli import write_config

    cfg = load_config(write_config(tmp_path, mode="live", extra="crypto:\n  exchange: binanceusdm\n  testnet: true\n"))
    now = datetime.now(UTC)
    _snapshots(cfg.journal_path, [
        ((now - timedelta(minutes=30)).isoformat(), "ccxt:binanceusdm", 4978.43),
        ((now - timedelta(minutes=5)).isoformat(), "paper", 10000.0),
    ])
    assert panel.build_state(cfg, live=True)["risk"]["equity"] == 4978.43
    assert panel.build_state(cfg, live=False)["risk"]["equity"] == 10000.0


class EquityBroker(ResultBroker):
    def __init__(self):
        super().__init__(None, reconcile="open")

    def get_balance(self):
        from tradeagent.brokers.base import Balance
        return Balance(equity=4978.43, free=4978.43, currency="USDT")


def test_watch_records_live_equity_every_cycle(tmp_path):
    journal = Journal(tmp_path / "j.sqlite")
    try:
        cli.record_equity(EquityBroker(), journal)
        row = journal._conn.execute("SELECT broker, equity FROM equity_snapshots").fetchone()
        assert (row["broker"], row["equity"]) == ("ccxt:binanceusdm", 4978.43)
    finally:
        journal.close()
