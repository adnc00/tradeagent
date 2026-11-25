from datetime import UTC, datetime, timedelta

import pytest

from tradeagent import cli, panel
from tradeagent.brokers.base import Balance
from tradeagent.config import ConfigError, load_config
from tradeagent.journal import Journal
from tests.test_cli import write_config

LIVE = "crypto:\n  exchange: binanceusdm\n  testnet: true\n"
GOAL = "goal:\n  start_capital: 100\n  target: 1000\n  since: '2026-11-26'\n"


class DemoBroker:
    name, environment, is_demo = "ccxt:binanceusdm", "demo trading", True

    def __init__(self, equity=4978.43):
        self.equity = equity

    def get_balance(self):
        return Balance(equity=self.equity, free=self.equity, currency="USDT")

    def reconcile_ticket(self, ticket):
        return "open"


def closed(journal, ticket_id, pnl, r, when, broker="ccxt:binanceusdm"):
    with journal._conn:
        journal._conn.execute(
            "INSERT INTO tickets (id, setup_id, created_at, symbol, side, market, entry, stop, targets, qty, leverage, "
            "risk_pct, fee_rate, status, broker, mode, submitted_at, closed_at, result_r, pnl) VALUES "
            "(?, ?, ?, 'ENA/USDT:USDT', 'short', 'futures', 1, 1.1, '[0.8]', 1, 1, 0.5, 0.0005, 'closed', ?, 'live', ?, ?, ?, ?)",
            (ticket_id, "s" + ticket_id, when.isoformat(), broker, when.isoformat(), when.isoformat(), r, pnl),
        )


# --- config -------------------------------------------------------------------------------------------------


def test_goal_is_off_by_default(tmp_path):
    assert load_config(write_config(tmp_path)).goal.start_capital == 0


def test_goal_settings(tmp_path):
    goal = load_config(write_config(tmp_path, extra=GOAL)).goal
    assert (goal.start_capital, goal.target) == (100.0, 1000.0)
    assert goal.since_moment() == datetime(2026, 11, 25, 21, 0, tzinfo=UTC)  # 00:00 Istanbul


@pytest.mark.parametrize("bad", ["goal:\n  start_capital: -5\n", "goal:\n  start_capital: 100\n  target: 50\n",
                                 "goal:\n  since: 'dün'\n"])
def test_goal_validation(tmp_path, bad):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, extra=bad))


# --- virtual account ---------------------------------------------------------------------------------------


def test_realized_pnl_counts_closed_tickets_of_the_account_since_the_start(tmp_path):
    journal = Journal(tmp_path / "j.sqlite")
    start = datetime(2026, 11, 26, tzinfo=UTC)
    try:
        closed(journal, "t-old", -50.0, -1.0, start - timedelta(days=1))
        closed(journal, "t-1", -0.4, -0.8, start + timedelta(hours=1))
        closed(journal, "t-2", 1.2, 2.4, start + timedelta(hours=2))
        closed(journal, "t-paper", 99.0, 3.0, start + timedelta(hours=3), broker="paper")
        assert journal.realized_pnl("ccxt:binanceusdm", start) == pytest.approx(0.8)
    finally:
        journal.close()


def test_demo_account_is_sized_as_the_virtual_account(tmp_path):
    cfg = load_config(write_config(tmp_path, mode="live", extra=LIVE + GOAL))
    journal = Journal(cfg.journal_path)
    try:
        closed(journal, "t-1", -0.4, -0.8, datetime(2026, 11, 26, 9, tzinfo=UTC))
        state = cli.account_state(DemoBroker(), journal, goal=cfg.goal)
        assert state.equity == pytest.approx(99.6)
        assert journal._conn.execute(
            "SELECT COUNT(*) FROM equity_snapshots WHERE broker = 'ccxt:binanceusdm#sanal'"
        ).fetchone()[0] == 1
    finally:
        journal.close()


def test_virtual_account_never_exceeds_the_real_balance(tmp_path):
    cfg = load_config(write_config(tmp_path, mode="live", extra=LIVE + GOAL))
    journal = Journal(cfg.journal_path)
    try:
        assert cli.account_state(DemoBroker(equity=60.0), journal, goal=cfg.goal).equity == 60.0
    finally:
        journal.close()


def test_without_goal_the_real_balance_is_used(tmp_path):
    cfg = load_config(write_config(tmp_path, mode="live", extra=LIVE))
    journal = Journal(cfg.journal_path)
    try:
        assert cli.account_state(DemoBroker(), journal, goal=cfg.goal).equity == pytest.approx(4978.43)
    finally:
        journal.close()


# --- panel ----------------------------------------------------------------------------------------------------


def test_panel_goal_progress(tmp_path):
    cfg = load_config(write_config(tmp_path, mode="live", extra=LIVE + GOAL))
    journal = Journal(cfg.journal_path)
    try:
        closed(journal, "t-1", -0.4, -0.8, datetime(2026, 11, 26, 9, tzinfo=UTC))
        closed(journal, "t-2", 1.6, 3.2, datetime(2026, 11, 26, 11, tzinfo=UTC))
    finally:
        journal.close()
    goal = panel.build_state(cfg, live=True)["goal"]
    assert goal["enabled"] is True
    assert goal["equity"] == pytest.approx(101.2)
    assert goal["progress_pct"] == pytest.approx(1.2 / 900 * 100)
    assert goal["trades"] == 2 and goal["expectancy"] == pytest.approx(1.2)
    # ln(1000/101.2) / ln(1 + 0.005 * 1.2)
    assert goal["trades_needed"] == pytest.approx(383, abs=2)


def test_panel_goal_without_an_edge_has_no_estimate(tmp_path):
    cfg = load_config(write_config(tmp_path, mode="live", extra=LIVE + GOAL))
    journal = Journal(cfg.journal_path)
    try:
        closed(journal, "t-1", -0.4, -0.8, datetime(2026, 11, 26, 9, tzinfo=UTC))
    finally:
        journal.close()
    goal = panel.build_state(cfg, live=True)["goal"]
    assert goal["trades_needed"] is None


def test_panel_goal_off(tmp_path):
    assert panel.build_state(load_config(write_config(tmp_path)))["goal"]["enabled"] is False
