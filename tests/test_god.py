from datetime import UTC, datetime

import pytest

from tradeagent import actions, cli, god
from tradeagent.brokers.base import Balance, OrderResult, Position
from tradeagent.config import ConfigError, load_config
from tradeagent.journal import Journal
from tradeagent.models import Side
from tests.test_cli import StubMarketData, write_config

GOAL = "goal:\n  start_capital: 100\n  target: 1000\n  since: '2026-11-26'\n"
LIVE = "crypto:\n  exchange: binanceusdm\n  testnet: false\n"


def settings(**overrides):
    values = dict(brake_pct=50.0, post_target_tolerance_pct=10.0, trail_pct=12.0, stretch_target=0.0)
    values.update(overrides)
    return god.GodConfig(**values)


# --- the floor ----------------------------------------------------------------------------------------------


def test_before_the_target_the_floor_is_the_brake():
    state = god.GodState(on=True, start_capital=100.0, target=1000.0, peak=180.0, floor=0.0)
    assert god.update(state, 180.0, settings()).floor == pytest.approx(50.0)


def test_reaching_the_target_lifts_the_floor_to_the_tolerance():
    state = god.update(god.GodState(on=True, start_capital=100.0, target=1000.0), 1000.0, settings())
    assert state.target_reached and state.floor == pytest.approx(900.0)


@pytest.mark.parametrize("peak,floor", [(1300.0, 1144.0), (1700.0, 1496.0), (2390.0, 2103.2)])
def test_after_the_target_the_floor_trails_the_peak(peak, floor):
    state = god.update(god.GodState(on=True, start_capital=100.0, target=1000.0), 1000.0, settings())
    state = god.update(state, peak, settings())
    assert state.floor == pytest.approx(floor)


def test_the_floor_never_goes_down():
    state = god.update(god.GodState(on=True, start_capital=100.0, target=1000.0), 1000.0, settings())
    state = god.update(state, 1700.0, settings())
    lower = god.update(state, 1600.0, settings())
    assert lower.floor == pytest.approx(1496.0) and lower.peak == 1700.0


@pytest.mark.parametrize("equity,verdict", [(49.0, "brake"), (51.0, None)])
def test_brake_verdict_before_the_target(equity, verdict):
    state = god.GodState(on=True, start_capital=100.0, target=1000.0)
    assert god.verdict(god.update(state, equity, settings()), equity, settings()) == verdict


def test_profit_lock_verdict_after_the_target():
    state = god.update(god.GodState(on=True, start_capital=100.0, target=1000.0), 1700.0, settings())
    assert god.verdict(state, 1490.0, settings()) == "lock"
    assert god.verdict(state, 1500.0, settings()) is None


def test_stretch_target_takes_the_profit():
    state = god.update(god.GodState(on=True, start_capital=100.0, target=1000.0), 2500.0, settings(stretch_target=2500.0))
    assert god.verdict(state, 2500.0, settings(stretch_target=2500.0)) == "stretch"


def test_god_settings_are_validated(tmp_path):
    cfg = load_config(write_config(tmp_path, extra="god:\n  brake_pct: 30\n  trail_pct: 15\n"))
    assert (cfg.god.brake_pct, cfg.god.trail_pct) == (30.0, 15.0)
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, extra="god:\n  brake_pct: 0\n"))


# --- switching it on and off from the panel ------------------------------------------------------------------


def test_god_mode_needs_allocated_capital(tmp_path):
    cfg = load_config(write_config(tmp_path))
    with pytest.raises(actions.ActionError) as excinfo:
        actions.god_mode(cfg, False, {"on": True, "accept": True})
    assert "sermaye" in excinfo.value.message


def test_god_mode_on_real_money_needs_explicit_acceptance(tmp_path):
    cfg = load_config(write_config(tmp_path, mode="live", extra=GOAL + LIVE))
    with pytest.raises(actions.ActionError):
        actions.god_mode(cfg, True, {"on": True})
    body = actions.god_mode(cfg, True, {"on": True, "accept": True})
    assert body["god"]["on"] is True and body["god"]["real_money"] is True
    assert god.load(cfg).on is True
    assert actions.god_mode(cfg, True, {"on": False})["god"]["on"] is False


def test_god_state_is_in_the_panel(tmp_path):
    from tradeagent import panel

    cfg = load_config(write_config(tmp_path, extra=GOAL))
    actions.god_mode(cfg, False, {"on": True, "accept": True})
    state = panel.build_state(cfg)
    assert state["god"]["on"] is True and state["god"]["floor"] == pytest.approx(50.0)


# --- what the watch does with it ------------------------------------------------------------------------------


class RealMoneyBroker:
    name, environment, is_demo = "ccxt:binanceusdm", "CANLI (gerçek para)", False

    def __init__(self, unrealized=0.0):
        self.positions = [Position(symbol="ETH/USDT:USDT", side=Side.LONG, qty=1.0, entry_price=100.0,
                                   unrealized_pnl=unrealized)]
        self.closed = False

    def get_balance(self):
        return Balance(equity=5000.0, free=5000.0, currency="USDT")

    def get_positions(self):
        return [] if self.closed else self.positions

    def close_all(self):
        self.closed = True
        return [OrderResult(id="c", symbol="ETH/USDT:USDT", side="sell", role="close", type="market", qty=1.0,
                            price=None, status="closed")]

    def get_open_orders(self, symbol=None):
        return []

    def cancel_orders(self, symbol):
        return 0

    def reconcile_ticket(self, ticket):
        return "open"


class Box:
    name = "test"

    def __init__(self):
        self.alerts = []

    def send(self, alert):
        self.alerts.append(alert)


def test_brake_closes_everything_and_switches_god_mode_off(tmp_path):
    cfg = load_config(write_config(tmp_path, mode="live", extra=GOAL + LIVE))
    actions.god_mode(cfg, True, {"on": True, "accept": True})
    broker, box = RealMoneyBroker(unrealized=-55.0), Box()  # 100 - 55 = 45 < floor 50
    journal = Journal(cfg.journal_path)
    try:
        assert cli.god_check(cfg, broker, journal, [box]) == "brake"
    finally:
        journal.close()
    assert broker.closed is True
    assert god.load(cfg).on is False
    assert any("God mode" in a.title for a in box.alerts)


def test_god_check_keeps_going_above_the_floor(tmp_path):
    cfg = load_config(write_config(tmp_path, mode="live", extra=GOAL + LIVE))
    actions.god_mode(cfg, True, {"on": True, "accept": True})
    broker = RealMoneyBroker(unrealized=30.0)
    journal = Journal(cfg.journal_path)
    try:
        assert cli.god_check(cfg, broker, journal, []) is None
    finally:
        journal.close()
    assert broker.closed is False and god.load(cfg).peak == pytest.approx(130.0)


def test_unattended_send_reaches_real_money_only_in_god_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "MarketData", StubMarketData)
    cfg = load_config(write_config(tmp_path, mode="live", extra=GOAL + LIVE))
    journal = Journal(cfg.journal_path)
    try:
        assert cli.submit_unattended("t-x", RealMoneyBroker(), journal, cfg).status == "refused"
        outcome = cli.submit_unattended("t-x", RealMoneyBroker(), journal, cfg, god_mode=True)
        assert outcome.status != "refused" or "durumda değil" in outcome.lines[0]
    finally:
        journal.close()
