from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce

from tradeagent.brokers.alpaca_broker import AlpacaBroker
from tradeagent.brokers.base import BrokerError
from tradeagent.models import OrderTicket, Side

ENV = {"ALPACA_API_KEY": "dummy-key-not-real", "ALPACA_API_SECRET": "dummy-secret-not-real"}


class FakeTrading:
    def __init__(self, fail: bool = False):
        self.submitted = []
        self.fail = fail
        self.cancelled = False
        self.closed = False

    def submit_order(self, request):
        if self.fail:
            raise APIError('{"message": "insufficient buying power"}')
        self.submitted.append(request)
        return SimpleNamespace(id="ord-1", status=SimpleNamespace(value="accepted"))

    def get_account(self):
        return SimpleNamespace(equity="10250.5", buying_power="20000", currency="USD")

    def get_all_positions(self):
        return [
            SimpleNamespace(symbol="AAPL", qty="10", side=SimpleNamespace(value="long"), avg_entry_price="190.0", unrealized_pl="12.5"),
            SimpleNamespace(symbol="TSLA", qty="-5", side=SimpleNamespace(value="short"), avg_entry_price="250.0", unrealized_pl="-3.0"),
        ]

    def cancel_orders(self):
        self.cancelled = True
        return [SimpleNamespace(id="a"), SimpleNamespace(id="b")]

    def close_all_positions(self, cancel_orders=None):
        self.closed = cancel_orders
        return [SimpleNamespace(symbol="AAPL", order_id="c-1", status=200)]


def make_ticket(**overrides) -> OrderTicket:
    base = dict(
        id="t-alp01",
        setup_id="s-alp01",
        symbol="AAPL",
        side=Side.LONG,
        entry=190.123,
        stop=187.456,
        targets=[196.789],
        qty=12.9,
        market="equity",
    )
    base.update(overrides)
    return OrderTicket(**base)


def broker_with(fake: FakeTrading) -> AlpacaBroker:
    broker = AlpacaBroker(env=ENV)
    broker._trading = fake
    return broker


def test_missing_keys_are_reported_by_name():
    with pytest.raises(BrokerError) as info:
        AlpacaBroker(env={})
    assert "ALPACA_API_KEY" in str(info.value) and "ALPACA_API_SECRET" in str(info.value)


def test_paper_endpoint_is_the_default():
    broker = AlpacaBroker(env=ENV)
    assert broker._trading._base_url.value == "https://paper-api.alpaca.markets"
    assert "paper" in broker.environment
    assert "dummy" not in repr(broker)


def test_live_endpoint_only_when_asked():
    broker = AlpacaBroker(paper=False, env=ENV)
    assert broker._trading._base_url.value == "https://api.alpaca.markets"
    assert "CANLI" in broker.environment


def test_bracket_request_has_whole_shares_stop_and_target():
    fake = FakeTrading()
    (result,) = broker_with(fake).place_bracket(make_ticket())
    (request,) = fake.submitted
    assert request.order_class == OrderClass.BRACKET
    assert request.side == OrderSide.BUY and request.time_in_force == TimeInForce.GTC
    assert request.qty == 12
    assert (request.limit_price, request.stop_loss.stop_price, request.take_profit.limit_price) == (190.12, 187.46, 196.79)
    assert (result.id, result.role) == ("ord-1", "bracket")


def test_short_bracket_sells():
    fake = FakeTrading()
    broker_with(fake).place_bracket(make_ticket(side=Side.SHORT, stop=193.0, targets=[184.0]))
    assert fake.submitted[0].side == OrderSide.SELL


@pytest.mark.parametrize("override", [{"qty": 0.7}, {"stop": None}, {"targets": []}, {"stop": 191.0}])
def test_invalid_brackets_are_refused(override):
    fake = FakeTrading()
    with pytest.raises(BrokerError):
        broker_with(fake).place_bracket(make_ticket(**override))
    assert fake.submitted == []


def test_api_rejection_becomes_broker_error():
    with pytest.raises(BrokerError):
        broker_with(FakeTrading(fail=True)).place_bracket(make_ticket())


def test_account_positions_and_kill_switch():
    fake = FakeTrading()
    broker = broker_with(fake)
    balance = broker.get_balance()
    assert (balance.equity, balance.free, balance.currency) == (10250.5, 20000.0, "USD")
    positions = broker.get_positions()
    assert [(p.symbol, p.side, p.qty) for p in positions] == [("AAPL", Side.LONG, 10.0), ("TSLA", Side.SHORT, 5.0)]
    assert broker.cancel_all() == 2 and fake.cancelled
    (closed,) = broker.close_all()
    assert fake.closed is True and (closed.symbol, closed.role) == ("AAPL", "close")
    assert broker.normalize_qty("AAPL", 7.99) == 7.0
    assert broker.market_type("AAPL", "spot") == "equity"


# --- adversarial review fixes ---------------------------------------------------------------

import requests  # noqa: E402

from tradeagent.brokers.base import OrderStateUnknown  # noqa: E402


class LookupTrading(FakeTrading):
    def __init__(self, landed: bool | None):
        super().__init__()
        self.landed = landed

    def submit_order(self, request):
        self.submitted.append(request)
        raise requests.exceptions.ReadTimeout("timed out")

    def get_order_by_client_id(self, client_id):
        if self.landed is None:
            raise requests.exceptions.ConnectionError("down")
        if not self.landed:
            raise APIError('{"code": 40410000, "message": "order not found"}', http_error=None)
        return SimpleNamespace(id="ord-9", status=SimpleNamespace(value="new"), client_order_id=client_id)


def test_bracket_carries_a_deterministic_client_order_id():
    fake = FakeTrading()
    broker_with(fake).place_bracket(make_ticket())
    assert fake.submitted[0].client_order_id == "t-alp01-b"


@pytest.mark.parametrize("landed,outcome", [(True, "ok"), (False, BrokerError), (None, OrderStateUnknown)])
def test_timeout_is_resolved_by_client_order_id(landed, outcome):
    broker = broker_with(LookupTrading(landed))
    if outcome == "ok":
        assert broker.place_bracket(make_ticket())[0].id == "ord-9"
    else:
        with pytest.raises(outcome) as info:
            broker.place_bracket(make_ticket())
        assert (type(info.value) is OrderStateUnknown) == (outcome is OrderStateUnknown)


def test_close_all_reports_failed_liquidations():
    fake = FakeTrading()
    fake.close_all_positions = lambda cancel_orders=None: [
        SimpleNamespace(symbol="AAPL", order_id="c-1", status=200, body=None),
        SimpleNamespace(symbol="TSLA", order_id=None, status=403, body={"message": "insufficient qty"}),
    ]
    ok, failed = broker_with(fake).close_all()
    assert ok.status == "submitted" and failed.status == "failed" and "insufficient" in failed.note


def test_prices_round_to_cents_toward_the_entry_and_cancel_is_per_symbol():
    fake = FakeTrading()
    cancelled = []
    fake.get_orders = lambda request: [
        SimpleNamespace(id="o1", symbol="AAPL", legs=None, order_type="limit", type="limit", stop_price=None,
                        limit_price="190", side="buy", qty="1", status="new"),
    ]
    fake.cancel_order_by_id = cancelled.append
    broker = broker_with(fake)
    assert broker.normalize_prices("AAPL", Side.LONG, 190.123, 187.451, [196.789]) == (190.12, 187.46, [196.78])
    assert broker.cancel_orders("AAPL") == 1 and cancelled == ["o1"]
