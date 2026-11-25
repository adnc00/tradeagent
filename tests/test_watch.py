from datetime import UTC, datetime

import pytest

from tradeagent import cli, notify
from tradeagent.brokers.paper import PaperBroker
from tradeagent.config import load_config
from tradeagent.journal import Journal
from tests.synthetic import follow_through_candles
from tests.test_cli import StubMarketData, write_config


class Inbox:
    """Records alerts like a phone would."""

    name = "test"

    def __init__(self):
        self.alerts: list[notify.Alert] = []

    def send(self, alert):
        self.alerts.append(alert)

    def text(self) -> str:
        return "\n".join(a.title + "\n" + a.body for a in self.alerts)


@pytest.fixture
def inbox(monkeypatch):
    box = Inbox()
    monkeypatch.setattr(cli.notify, "notifiers_from_env", lambda *a, **k: [box])
    return box


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.setattr(StubMarketData, "price", None)
    monkeypatch.setattr(cli, "MarketData", StubMarketData)
    for name in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCEUSDM_API_KEY", "BINANCEUSDM_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    return write_config(tmp_path)


def watch_once(config_path, *extra: str) -> int:
    return cli.main(["--config", str(config_path), "watch", "--symbols", "BTC/USDT", "--once", *extra])


def tickets(config_path) -> list[dict]:
    journal = Journal(load_config(config_path).journal_path)
    try:
        return journal.latest_tickets()
    finally:
        journal.close()


def trade_alerts(box: Inbox) -> list[notify.Alert]:
    return [a for a in box.alerts if "BTC/USDT" in a.title]


# --- scheduling ------------------------------------------------------------------------------------


def test_next_wake_is_just_after_the_next_candle_close():
    now = datetime(2026, 11, 25, 13, 7, 30, tzinfo=UTC)
    assert cli.next_wake(now, 15) == datetime(2026, 11, 25, 13, 15, 20, tzinfo=UTC)


def test_next_wake_on_a_boundary_moves_to_the_following_one():
    now = datetime(2026, 11, 25, 13, 15, 20, tzinfo=UTC)
    assert cli.next_wake(now, 15) == datetime(2026, 11, 25, 13, 30, 20, tzinfo=UTC)


# --- notify mode -----------------------------------------------------------------------------------


def test_watch_alerts_a_new_setup_with_risk_and_gain(config_path, inbox):
    assert watch_once(config_path) == 0
    (alert,) = trade_alerts(inbox)
    assert alert.title.startswith("LONG BTC/USDT")
    assert "Risk (stop olursa)" in alert.body
    assert "Hedefte kazanç" in alert.body
    (row,) = tickets(config_path)
    assert row["status"] == "new"
    assert f"tradeagent execute {row['id']}" in alert.body


def test_watch_never_sends_an_order_without_auto(config_path, inbox):
    assert watch_once(config_path) == 0
    assert PaperBroker(load_config(config_path).paper_state_path).get_open_orders() == []


def test_watch_does_not_alert_the_same_ticket_twice(config_path, inbox):
    assert watch_once(config_path) == 0
    assert watch_once(config_path) == 0
    assert len(trade_alerts(inbox)) == 1


def test_watch_announces_that_it_started(config_path, inbox):
    assert watch_once(config_path) == 0
    assert any("izleme" in a.title.lower() for a in inbox.alerts)


# --- demo auto mode --------------------------------------------------------------------------------


def test_auto_sends_paper_orders_without_a_prompt(config_path, inbox, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("auto mode must not prompt"))
    assert watch_once(config_path, "--demo-auto") == 0
    (row,) = tickets(config_path)
    assert row["status"] == "submitted"
    (alert,) = trade_alerts(inbox)
    assert "otomatik" in alert.title.lower()
    assert PaperBroker(load_config(config_path).paper_state_path).get_open_orders()


def test_auto_reports_closed_trade_with_result(config_path, inbox):
    assert watch_once(config_path, "--demo-auto") == 0
    broker = PaperBroker(load_config(config_path).paper_state_path)
    for candle in follow_through_candles():
        broker.feed_candle("BTC/USDT", candle)
    assert watch_once(config_path, "--demo-auto") == 0
    closed = [a for a in inbox.alerts if "kapandı" in a.title.lower()]
    assert len(closed) == 1
    assert "R" in closed[0].body
    assert watch_once(config_path, "--demo-auto") == 0
    assert len([a for a in inbox.alerts if "kapandı" in a.title.lower()]) == 1


def test_auto_refuses_a_real_money_crypto_account(tmp_path, monkeypatch, inbox):
    path = write_config(tmp_path, mode="live", extra="crypto:\n  exchange: binanceusdm\n  testnet: false\n")
    monkeypatch.setattr(cli, "make_broker", lambda *a, **k: pytest.fail("no broker may be built"))
    assert cli.main(["--config", str(path), "watch", "--symbols", "BTC/USDT:USDT", "--once", "--demo-auto", "--live"]) == 2
    assert trade_alerts(inbox) == []


def test_auto_refuses_a_real_money_alpaca_account(tmp_path, monkeypatch, inbox):
    path = write_config(tmp_path, mode="live", extra="alpaca:\n  paper: false\n")
    monkeypatch.setattr(cli, "make_broker", lambda *a, **k: pytest.fail("no broker may be built"))
    assert cli.main(["--config", str(path), "watch", "--symbols", "AAPL", "--once", "--demo-auto", "--live"]) == 2


def test_auto_allows_demo_accounts(tmp_path):
    cfg = load_config(write_config(tmp_path, mode="live", extra="crypto:\n  exchange: binanceusdm\n  testnet: true\n"))
    assert cli.auto_problems(cfg, ["BTC/USDT:USDT", "AAPL"], live_flag=True) == []


def test_auto_rejects_bist_live(tmp_path):
    cfg = load_config(write_config(tmp_path, mode="live"))
    assert cli.auto_problems(cfg, ["THYAO.IS"], live_flag=True)


def test_unattended_submission_refuses_a_non_demo_broker(config_path, monkeypatch):
    cfg = load_config(config_path)
    broker = PaperBroker(cfg.paper_state_path)
    monkeypatch.setattr(PaperBroker, "is_demo", False)
    journal = Journal(cfg.journal_path)
    try:
        outcome = cli.submit_unattended("t-whatever", broker, journal, cfg)
    finally:
        journal.close()
    assert outcome.status == "refused"


# --- robustness -----------------------------------------------------------------------------------


def test_a_failing_cycle_does_not_stop_the_loop(config_path, inbox, monkeypatch):
    calls = []

    def flaky_cycle(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("borsa yanıt vermedi")

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "watch_cycle", flaky_cycle)
    monkeypatch.setattr(cli, "_sleep", fake_sleep)
    assert cli.main(["--config", str(config_path), "watch", "--symbols", "BTC/USDT"]) == 0
    assert len(calls) == 2
    assert any("borsa yanıt vermedi" in a.body for a in inbox.alerts)


def test_watch_validates_interval(config_path, inbox):
    assert watch_once(config_path, "--interval", "0") == 2


def test_live_watch_keeps_bist_on_paper(tmp_path, monkeypatch, inbox):
    monkeypatch.setattr(StubMarketData, "price", None)
    monkeypatch.setattr(cli, "MarketData", StubMarketData)
    path = write_config(tmp_path, mode="live")
    assert cli.main(["--config", str(path), "watch", "--symbols", "THYAO.IS", "--once", "--live"]) == 0
    (alert,) = [a for a in inbox.alerts if "THYAO.IS" in a.title]
    assert "--live" not in alert.body


def test_redirected_output_is_line_buffered(monkeypatch, tmp_path):
    calls = []

    class Stream:
        def isatty(self):
            return False

        def reconfigure(self, **kwargs):
            calls.append(kwargs)

        def write(self, text):
            return len(text)

        def flush(self):
            pass

    monkeypatch.setattr(cli.sys, "stdout", Stream())
    monkeypatch.setattr(cli.sys, "stderr", Stream())
    cli.main(["--config", str(write_config(tmp_path)), "stats"])
    assert calls and all(c.get("line_buffering") is True for c in calls)
