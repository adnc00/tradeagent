from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from tradeagent import actions, cli
from tradeagent.brokers.ccxt_broker import client_order_ids
from tradeagent.config import load_config
from tradeagent.journal import Journal
from tradeagent.models import OrderTicket
from tests.test_ccxt_broker import SYMBOL, FakeExchange, broker_with, make_ticket
from tests.test_cli import StubMarketData, scanned_ticket_id, write_config

SENT = datetime(2026, 11, 25, 23, 22, tzinfo=UTC)


# --- expiry: only price action after the entry was sent counts ----------------------------------------


class Minutes:
    def __init__(self, lows):
        index = pd.date_range(SENT - timedelta(minutes=3), periods=len(lows), freq="1min")
        self.frame = pd.DataFrame({"open": 1.0, "high": 1.2, "low": lows, "close": 1.1, "volume": 1.0}, index=index)
        self.timeframes = []

    def get_ohlcv(self, symbol, timeframe, limit=500):
        self.timeframes.append(timeframe)
        return self.frame


def short_ticket():
    return OrderTicket(id="t-on", setup_id="s-on", symbol="ON/USDT:USDT", side="short", entry=1.10, stop=1.12,
                       targets=[1.07], qty=1.0, market="futures")


def test_a_touch_before_the_order_was_sent_does_not_cancel_it():
    data = Minutes([1.06, 1.08, 1.09, 1.09, 1.09])  # target 1.07 hit at 23:19, sent at 23:22
    assert cli._target_touched(short_ticket(), SENT, data) is False
    assert data.timeframes == ["1m"]


def test_a_touch_after_the_order_was_sent_cancels_it():
    data = Minutes([1.09, 1.09, 1.09, 1.09, 1.065])  # 23:23
    assert cli._target_touched(short_ticket(), SENT, data) is True


# --- fast preview ------------------------------------------------------------------------------------------


class SlowReconcileBroker:
    name, environment, is_demo = "ccxt:binanceusdm", "demo trading", True

    def reconcile_ticket(self, ticket):
        raise AssertionError("a preview must not reconcile every open ticket")

    def get_balance(self):
        from tradeagent.brokers.base import Balance
        return Balance(equity=5000.0, free=5000.0, currency="USDT")

    def get_price(self, symbol):
        return 100.65 * 1.001

    def market_type(self, symbol, configured):
        return configured

    def normalize_prices(self, symbol, side, entry, stop, targets):
        return entry, stop, list(targets)

    def normalize_qty(self, symbol, qty):
        return qty

    def venue_problems(self, ticket):
        return []


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.setattr(StubMarketData, "price", None)
    monkeypatch.setattr(cli, "MarketData", StubMarketData)
    actions._BROKERS.clear()
    return write_config(tmp_path)


def test_quick_prepare_skips_the_venue_reconciliation(config_path):
    ticket_id = scanned_ticket_id(config_path)
    cfg = load_config(config_path)
    journal = Journal(cfg.journal_path)
    try:
        with journal._conn:
            journal._conn.execute("UPDATE tickets SET broker='ccxt:binanceusdm', status='submitted' WHERE id != ?", (ticket_id,))
        prepared, state, reasons = cli.prepare_ticket(journal.get_ticket(ticket_id), SlowReconcileBroker(), journal, cfg, quick=True)
        assert state.equity == 5000.0
    finally:
        journal.close()


def test_panel_reuses_one_broker_connection(config_path, monkeypatch):
    built = []

    def counting(cfg, symbol, live):  # a remote (non-paper) account: its connection is kept
        built.append(symbol)
        return SlowReconcileBroker()

    monkeypatch.setattr(cli, "make_broker", counting)
    ticket_id = scanned_ticket_id(config_path)
    cfg = load_config(config_path)
    actions.preview(cfg, False, {"ticket_id": ticket_id})
    actions.preview(cfg, False, {"ticket_id": ticket_id})
    assert len(built) == 1


# --- positions on the venue -------------------------------------------------------------------------------


def holding(fake, side="long", contracts=1.234, **extra):
    fake.positions = [{"symbol": SYMBOL, "side": side, "contracts": contracts, "contractSize": 1.0, "entryPrice": 100.0,
                       "unrealizedPnl": 3.5, "leverage": 10, "liquidationPrice": 91.0, "markPrice": 102.8, **extra}]


def test_positions_carry_leverage_liquidation_and_mark_price():
    fake = FakeExchange()
    holding(fake)
    (p,) = broker_with(fake).get_positions()
    assert (p.leverage, p.liquidation, p.mark_price) == (10.0, 91.0, 102.8)


def test_close_position_sends_a_reduce_only_market_order_for_the_fraction():
    fake = FakeExchange()
    holding(fake)
    result = broker_with(fake).close_position(SYMBOL, 0.5)
    (call,) = [c for c in fake.created() if c[1] == "close"]
    assert call[3:5] == ("market", "sell") and call[5] == pytest.approx(0.617)
    assert call[7]["reduceOnly"] is True
    assert result.status != "failed"


def test_closing_the_whole_position_cancels_its_leftover_orders():
    fake = FakeExchange()
    broker = broker_with(fake)
    broker.place_bracket(make_ticket())
    holding(fake)

    def flat_after_close(*args, **kwargs):
        fake.positions = []

    original = fake.create_order

    def create(symbol, type, side, amount, price=None, params=None):
        order = original(symbol, type, side, amount, price, params)
        if (params or {}).get("reduceOnly") and "stopLossPrice" not in (params or {}) and "takeProfitPrice" not in (params or {}):
            flat_after_close()
        return order

    fake.create_order = create
    broker.close_position(SYMBOL, 1.0)
    assert all(o["status"] == "canceled" for o in fake.orders.values() if o["trigger"])


def test_protection_is_placed_before_the_old_one_is_removed():
    fake = FakeExchange()
    broker = broker_with(fake)
    broker.place_bracket(make_ticket())
    holding(fake)
    old_stop = client_order_ids("t-ccxt01")["stop"]
    broker.set_protection(SYMBOL, stop=99.0, take_profit=110.0)
    created = [c for c in fake.calls if c[0] in ("create_order", "cancel_order")]
    new_stop_at = next(i for i, c in enumerate(created) if c[0] == "create_order" and c[1] == "stop" and c[7].get("stopLossPrice") == 99.0)
    old_cancel_at = next(i for i, c in enumerate(created) if c[0] == "cancel_order" and c[1] == fake.orders[old_stop]["id"])
    assert new_stop_at < old_cancel_at
    live_stops = [o for o in fake.orders.values() if o["status"] == "open" and o["trigger"]]
    assert sorted(o["side"] for o in live_stops) == ["sell", "sell"]  # one stop + one take-profit


def test_protection_needs_a_position_and_sane_levels():
    fake = FakeExchange()
    broker = broker_with(fake)
    with pytest.raises(cli.BrokerError):
        broker.set_protection(SYMBOL, stop=99.0, take_profit=None)
    holding(fake)
    with pytest.raises(cli.BrokerError):
        broker.set_protection(SYMBOL, stop=105.0, take_profit=None)  # a long's stop above the mark price


def test_orphan_protective_orders_are_cleaned_up():
    fake = FakeExchange()
    broker = broker_with(fake)
    holding(fake)
    broker.set_protection(SYMBOL, stop=99.0, take_profit=110.0)
    fake.positions = []
    assert broker.cancel_orphans() == [SYMBOL]
    assert all(o["status"] == "canceled" for o in fake.orders.values())


def test_a_pending_entry_is_not_an_orphan():
    fake = FakeExchange()
    broker = broker_with(fake)
    broker.place_bracket(make_ticket())
    assert broker.cancel_orphans() == []


# --- panel actions for positions --------------------------------------------------------------------------


class AccountStub:
    name, environment, is_demo = "ccxt:binanceusdm", "demo trading", True

    def __init__(self):
        from tradeagent.brokers.base import Position
        from tradeagent.models import Side
        self.positions = [Position(symbol=SYMBOL, side=Side.LONG, qty=1.0, entry_price=100.0, unrealized_pnl=2.0,
                                   leverage=10.0, liquidation=91.0, mark_price=102.0)]
        self.closed = []

    def get_positions(self):
        return self.positions

    def get_open_orders(self, symbol=None):
        from tradeagent.brokers.base import OrderResult
        return [OrderResult(id="x-s", symbol=SYMBOL, side="sell", role="stop", type="stop_market", qty=1.0, price=98.0, status="open")]

    def close_position(self, symbol, fraction):
        from tradeagent.brokers.base import OrderResult
        self.closed.append((symbol, fraction))
        return OrderResult(id="c1", symbol=symbol, side="sell", role="close", type="market", qty=fraction, price=None, status="closed")


def test_positions_action_lists_venue_positions_with_their_protection(config_path, monkeypatch):
    stub = AccountStub()
    monkeypatch.setattr(actions, "_account_brokers", lambda cfg, live: [stub])
    body = actions.positions(load_config(config_path), True, {})
    (p,) = body["positions"]
    assert p["symbol"] == SYMBOL and p["stop"] == 98.0 and p["take_profit"] is None
    assert p["unrealized_pnl"] == 2.0 and p["leverage"] == 10.0


def test_close_action_is_a_click_on_any_account(config_path, monkeypatch):
    stub = AccountStub()
    monkeypatch.setattr(actions, "_account_brokers", lambda cfg, live: [stub])
    cfg = load_config(config_path)
    assert actions.close(cfg, True, {"symbol": SYMBOL, "fraction": 0.5})["status"] == "closed"
    stub.is_demo = False
    assert actions.close(cfg, True, {"symbol": SYMBOL, "fraction": 1.0})["status"] == "closed"
    assert stub.closed == [(SYMBOL, 0.5), (SYMBOL, 1.0)]


@pytest.mark.parametrize("fraction", [0, -1, 1.5, "abc"])
def test_close_fraction_must_be_sane(config_path, monkeypatch, fraction):
    monkeypatch.setattr(actions, "_account_brokers", lambda cfg, live: [AccountStub()])
    with pytest.raises(actions.ActionError):
        actions.close(load_config(config_path), True, {"symbol": SYMBOL, "fraction": fraction})


def test_candles_action_returns_ohlc_for_the_chart(config_path):
    body = actions.candles(load_config(config_path), False, {"symbol": "BTC/USDT", "timeframe": "15m"})
    assert len(body["candles"]) > 10 and len(body["candles"][0]) == 5


def test_leverage_falls_back_to_notional_over_initial_margin():
    fake = FakeExchange()
    holding(fake, leverage=None, notional=11_747.5, initialMargin=93.98)
    (p,) = broker_with(fake).get_positions()
    assert p.leverage == pytest.approx(125.0, abs=0.1)


def test_risk_card_shows_the_group_cap(tmp_path):
    from tradeagent import panel

    path = write_config(tmp_path)
    extra_limits = "risk:\n  max_risk_pct: 1.0\n  max_open_risk_pct: 15.0\n  max_group_risk_pct: 15.0\n"
    path.write_text(path.read_text(encoding="utf-8").replace("risk:\n", extra_limits, 1), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.risk.group_cap_pct == 15.0
    state = panel.build_state(cfg)
    assert state["risk"]["group_cap_pct"] == cfg.risk.group_cap_pct


# --- zero noise: no alerts for symbols that cannot be traded right now ------------------------------------


def test_a_symbol_already_in_use_is_a_preparation_reason_not_a_send_failure():
    fake = FakeExchange()
    broker = broker_with(fake)
    broker.place_bracket(make_ticket())
    problems = broker.venue_problems(make_ticket(id="t-second"))
    assert any("zaten" in p for p in problems)


def test_a_market_missing_on_this_account_is_reported_not_raised():
    fake = FakeExchange()
    broker = broker_with(fake)
    assert broker.has_market(SYMBOL) is True
    assert broker.has_market("SLX/USDT:USDT") is False


class MissingMarketBroker(SlowReconcileBroker):
    def market_type(self, symbol, configured):
        raise cli.BrokerError(f"binanceusdm borsasında {symbol} piyasası yok (ör. BTC/USDT:USDT).")


def test_prepare_turns_a_missing_market_into_a_reason(config_path):
    ticket_id = scanned_ticket_id(config_path)
    cfg = load_config(config_path)
    journal = Journal(cfg.journal_path)
    try:
        _, _, reasons = cli.prepare_ticket(journal.get_ticket(ticket_id), MissingMarketBroker(), journal, cfg, quick=True)
    finally:
        journal.close()
    assert any("piyasası yok" in r for r in reasons)


def test_scan_list_keeps_only_symbols_the_account_can_trade():
    class Account:
        def has_market(self, symbol):
            return symbol != "SLX/USDT:USDT"

    assert cli.tradable(["BTC/USDT:USDT", "SLX/USDT:USDT"], Account()) == ["BTC/USDT:USDT"]
    assert cli.tradable(["BTC/USDT"], object()) == ["BTC/USDT"]  # brokers without market lists keep everything
