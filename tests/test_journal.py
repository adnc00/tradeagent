import math
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from tradeagent.brokers.base import Fill, OrderResult
from tradeagent.journal import Journal
from tradeagent.models import OrderTicket, Setup, Side

T0 = datetime(2026, 11, 18, 14, 45, tzinfo=UTC)


def make_ticket(**overrides) -> OrderTicket:
    base = dict(
        id="t-0001",
        setup_id="s-0001",
        symbol="BTC/USDT",
        side=Side.LONG,
        entry=100.65,
        stop=99.45,
        targets=[103.5, 105.0],
        qty=35.5,
        leverage=1.0,
        risk_pct=0.5,
        fee_rate=0.001,
        market="spot",
        timeframe="15m",
        reason="LONG BTC/USDT 15m: test",
        created_at=T0,
    )
    base.update(overrides)
    return OrderTicket(**base)


def make_setup(ticket: OrderTicket | None = None, reasons: list[str] | None = None, **overrides) -> Setup:
    base = dict(
        id="s-0001",
        symbol="BTC/USDT",
        timeframe="15m",
        side=Side.LONG,
        detected_at=T0,
        bias="bullish",
        sweep_kind="swing_low",
        sweep_level=100.0,
        sweep_extreme=99.5,
        mss_at=T0 - timedelta(hours=2, minutes=30),
        mss_level=100.7,
        fvg_top=100.95,
        fvg_bottom=100.35,
        entry=100.65,
        stop=99.45,
        targets=[103.5, 105.0],
        target_kinds=["swing_high", "PDH"],
        range_high=103.5,
        range_low=99.5,
        killzone="ny_am",
        narrative="LONG BTC/USDT 15m: test",
        reasons=reasons or [],
        ticket=ticket,
    )
    base.update(overrides)
    return Setup(**base)


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "nested" / "journal.sqlite")
    yield j
    j.close()


def test_tables_have_reason_and_notes_columns(journal, tmp_path):
    conn = sqlite3.connect(tmp_path / "nested" / "journal.sqlite")
    for table in ("setups", "tickets", "orders", "fills"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert {"reason", "notes"} <= columns, table
    conn.close()


def test_setup_and_ticket_round_trip(journal):
    ticket = make_ticket()
    journal.save_setup(make_setup(ticket=ticket))
    loaded = journal.get_setup("s-0001")
    assert loaded.targets == [103.5, 105.0]
    assert loaded.target_kinds == ["swing_high", "PDH"]
    assert loaded.mss_at == T0 - timedelta(hours=2, minutes=30)
    assert loaded.ticket is not None and loaded.ticket.id == "t-0001"
    stored = journal.get_ticket("t-0001")
    assert (stored.symbol, stored.side, stored.market, stored.qty, stored.status) == ("BTC/USDT", Side.LONG, "spot", 35.5, "new")
    assert stored.targets == [103.5, 105.0]
    assert stored.created_at == T0


def test_rejected_setup_keeps_reasons_and_has_no_ticket(journal):
    journal.save_setup(make_setup(reasons=["R:R 1.70, en az 2.00 olmalı."]))
    loaded = journal.get_setup("s-0001")
    assert loaded.ticket is None
    assert loaded.reasons == ["R:R 1.70, en az 2.00 olmalı."]
    assert journal.get_ticket("t-0001") is None


def test_rescan_updates_only_new_tickets(journal):
    journal.save_setup(make_setup(ticket=make_ticket()))
    journal.save_setup(make_setup(ticket=make_ticket(qty=30.0)))
    assert journal.get_ticket("t-0001").qty == 30.0

    orders = [OrderResult("p-1-entry", "BTC/USDT", "buy", "entry", "limit", 30.0, 100.65, "pending")]
    journal.mark_submitted(make_ticket(qty=30.0), broker="paper", mode="paper", orders=orders)
    journal.save_setup(make_setup(ticket=make_ticket(qty=12.0)))
    ticket = journal.get_ticket("t-0001")
    assert (ticket.qty, ticket.status) == (30.0, "submitted")


def test_submitted_tickets_count_as_open_until_closed(journal):
    journal.save_setup(make_setup(ticket=make_ticket()))
    orders = [
        OrderResult("p-1-entry", "BTC/USDT", "buy", "entry", "limit", 35.5, 100.65, "pending"),
        OrderResult("p-1-stop", "BTC/USDT", "sell", "stop", "stop_market", 35.5, 99.45, "attached"),
    ]
    journal.mark_submitted(make_ticket(), broker="paper", mode="paper", orders=orders)
    assert [t.id for t in journal.open_tickets("paper")] == ["t-0001"]
    assert journal.open_tickets("ccxt:binance") == []
    assert [o["role"] for o in journal.orders_for("t-0001")] == ["entry", "stop"]

    journal.set_ticket_status("t-0001", "closed", result_r=2.1, pnl=105.0, closed_at=T0, note="TP")
    assert journal.open_tickets("paper") == []
    assert "TP" in journal.get_ticket_notes("t-0001")


def test_fills_are_idempotent(journal):
    fill = Fill("p-1-entry", "p-1-entry", "t-0001", T0, "BTC/USDT", "buy", "entry", 35.5, 100.65, 3.57)
    assert journal.record_fill(fill) is True
    assert journal.record_fill(fill) is False
    assert len(journal.fills_for("t-0001")) == 1


def test_notes_are_appended_with_timestamps(journal):
    journal.save_setup(make_setup(ticket=make_ticket()))
    journal.add_ticket_note("t-0001", "kullanıcı onaylamadı")
    journal.add_ticket_note("t-0001", "risk reddi: günlük limit")
    notes = journal.get_ticket_notes("t-0001")
    assert "kullanıcı onaylamadı" in notes and "risk reddi" in notes
    assert notes.count("\n") == 1


def _close(journal: Journal, ticket_id: str, r: float, when: datetime) -> None:
    journal.save_setup(make_setup(id=f"s-{ticket_id}", ticket=make_ticket(id=ticket_id, setup_id=f"s-{ticket_id}")))
    journal.mark_submitted(make_ticket(id=ticket_id, setup_id=f"s-{ticket_id}"), broker="paper", mode="paper", orders=[])
    journal.set_ticket_status(ticket_id, "closed", result_r=r, pnl=r * 50, closed_at=when)


def test_stats_win_rate_expectancy_profit_factor_and_drawdown(journal):
    for i, r in enumerate([2.0, -1.0, -1.0, 3.0, -1.0, 0.5]):
        _close(journal, f"t-{i:04d}", r, T0 + timedelta(hours=i))
    stats = journal.stats()
    assert (stats.trades, stats.wins, stats.losses) == (6, 3, 3)
    assert stats.win_rate == pytest.approx(0.5)
    assert stats.avg_r == pytest.approx(2.5 / 6)
    assert stats.expectancy == pytest.approx(0.5 * (5.5 / 3) - 0.5 * 1.0)
    assert stats.profit_factor == pytest.approx(5.5 / 3)
    assert stats.max_drawdown_r == pytest.approx(2.0)
    assert stats.total_r == pytest.approx(2.5)


def test_stats_breakeven_and_edge_cases(journal):
    assert journal.stats().trades == 0
    assert journal.stats().profit_factor == 0.0
    for i, r in enumerate([1.0, 0.0, -1.0]):
        _close(journal, f"t-{i:04d}", r, T0 + timedelta(hours=i))
    stats = journal.stats()
    assert stats.win_rate == pytest.approx(1 / 3)
    assert stats.expectancy == pytest.approx(0.0)


def test_profit_factor_without_losses_is_infinite(journal):
    _close(journal, "t-0001", 1.5, T0)
    assert math.isinf(journal.stats().profit_factor)


def test_equity_baselines_use_new_york_day_and_week(journal):
    sunday = datetime(2026, 11, 15, 20, 0, tzinfo=UTC)
    monday = datetime(2026, 11, 16, 14, 0, tzinfo=UTC)
    wednesday_morning = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    now = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)
    assert journal.equity_baselines("paper", 10_500.0, sunday) == (10_500.0, 10_500.0)
    journal.equity_baselines("paper", 10_000.0, monday)
    journal.equity_baselines("paper", 9_900.0, wednesday_morning)
    assert journal.equity_baselines("paper", 9_850.0, now) == (9_900.0, 10_000.0)
    assert journal.equity_baselines("ccxt:binance", 500.0, now) == (500.0, 500.0)


# --- adversarial review fixes ---------------------------------------------------------------

from tradeagent.journal import JournalError  # noqa: E402


def test_claim_is_atomic_and_counts_as_open_risk(journal):
    journal.save_setup(make_setup(ticket=make_ticket()))
    assert journal.claim_for_submission("t-0001", broker="paper", mode="paper") is True
    assert journal.claim_for_submission("t-0001", broker="paper", mode="paper") is False
    assert journal.get_ticket("t-0001").status == "submitting"
    assert [t.id for t in journal.open_tickets("paper")] == ["t-0001"]
    journal.release_claim("t-0001", "risk yeniden reddetti")
    assert journal.get_ticket("t-0001").status == "new"
    assert journal.open_tickets("paper") == []


def test_unknown_tickets_count_as_open_risk(journal):
    journal.save_setup(make_setup(ticket=make_ticket()))
    journal.claim_for_submission("t-0001", broker="ccxt:binanceusdm", mode="live")
    journal.set_ticket_status("t-0001", "unknown", note="zaman aşımı")
    assert [t.id for t in journal.open_tickets("ccxt:binanceusdm")] == ["t-0001"]


def test_rescanning_a_sent_setup_keeps_its_link_and_status(journal):
    journal.save_setup(make_setup(ticket=make_ticket()))
    journal.mark_submitted(make_ticket(), broker="paper", mode="paper", orders=[])
    journal.save_setup(make_setup(reasons=["Korelasyonlu grup riski"]))
    loaded = journal.get_setup("s-0001")
    assert loaded.ticket is not None and loaded.ticket.status == "submitted"
    assert loaded.reasons == []


def test_corrupt_journal_gives_a_turkish_error(tmp_path):
    path = tmp_path / "journal.sqlite"
    path.write_bytes(b"this is not a database" * 100)
    with pytest.raises(JournalError) as info:
        Journal(path)
    assert str(path) in str(info.value) and "okunamadı" in str(info.value)
