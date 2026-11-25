import ccxt
import pytest

from tradeagent.brokers.base import BrokerError, OrderStateUnknown
from tradeagent.brokers.ccxt_broker import CcxtBroker, api_key_names, client_order_ids
from tradeagent.models import OrderTicket, Side

DUMMY_KEY = "dummy-key-not-real"
DUMMY_SECRET = "dummy-secret-not-real"
SYMBOL = "BTC/USDT:USDT"


def env_for(exchange_id: str) -> dict[str, str]:
    key_var, secret_var = api_key_names(exchange_id)
    return {key_var: DUMMY_KEY, secret_var: DUMMY_SECRET}


class FakeExchange:
    """Stateful stand-in for ccxt.binanceusdm; never touches the network."""

    id = "binanceusdm"
    precisionMode = ccxt.TICK_SIZE

    def __init__(self, *, fail=None, hedged=False, lookup_fails=False, cancel_fails=(), margin_already_set=False):
        self.fail = fail or {}
        self.hedged = hedged
        self.lookup_fails = lookup_fails
        self.cancel_fails = set(cancel_fails)
        self.margin_already_set = margin_already_set
        self.calls: list[tuple] = []
        self.orders: dict[str, dict] = {}
        self.positions: list[dict] = []
        self.balance = {"total": {"USDT": 1000.0}, "free": {"USDT": 800.0}}
        self.markets = {
            SYMBOL: {
                "symbol": SYMBOL, "contract": True, "linear": True,
                "precision": {"amount": 0.001, "price": 0.1},
                "limits": {"amount": {"min": 0.001}, "cost": {"min": 5.0}},
            },
            "BTC/USDT": {"symbol": "BTC/USDT", "contract": False, "linear": None, "precision": {}, "limits": {}},
        }

    def load_markets(self):
        return self.markets

    def market(self, symbol):
        if symbol not in self.markets:
            raise ccxt.BadSymbol(symbol)
        return self.markets[symbol]

    def amount_to_precision(self, symbol, amount):
        value = int(amount * 1000 + 1e-9) / 1000
        if value <= 0:
            raise ccxt.InvalidOrder("amount below precision")
        return str(value)

    def fetch_position_mode(self, symbol=None):
        return {"hedged": self.hedged}

    def set_margin_mode(self, mode, symbol):
        self.calls.append(("set_margin_mode", mode, symbol))
        if self.margin_already_set:
            raise ccxt.MarginModeAlreadySet("No need to change margin type.")

    def set_leverage(self, leverage, symbol):
        self.calls.append(("set_leverage", leverage, symbol))

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        params = dict(params or {})
        if "stopLossPrice" in params:
            role = "stop"
        elif "takeProfitPrice" in params:
            role = "take_profit"
        elif params.get("reduceOnly") or "positionSide" in params:
            role = "close"
        else:
            role = "entry"
        self.calls.append(("create_order", role, symbol, type, side, amount, price, params))
        effect = self.fail.get(role)
        order = {
            "id": f"x{len(self.calls)}", "clientOrderId": params.get("clientOrderId"), "symbol": symbol,
            "status": "open", "type": type, "side": side, "amount": amount, "price": price,
            "trigger": role in ("stop", "take_profit"),
        }
        if effect == "interrupt":
            raise KeyboardInterrupt
        if effect == "timeout_absent":
            raise ccxt.RequestTimeout("timed out")
        if effect == "timeout_landed":
            self.orders[order["clientOrderId"]] = order
            raise ccxt.RequestTimeout("timed out")
        if isinstance(effect, Exception):
            raise effect
        if order["clientOrderId"]:
            self.orders[order["clientOrderId"]] = order
        return order

    def fetch_order(self, id, symbol=None, params=None):
        self.calls.append(("fetch_order", (params or {}).get("clientOrderId"), bool((params or {}).get("trigger"))))
        if self.lookup_fails:
            raise ccxt.NetworkError("still down")
        client_id = (params or {}).get("clientOrderId")
        if client_id not in self.orders:
            raise ccxt.OrderNotFound(client_id)
        return self.orders[client_id]

    def cancel_order(self, id, symbol=None, params=None):
        self.calls.append(("cancel_order", id, dict(params or {})))
        order = next(o for o in self.orders.values() if o["id"] == id)
        if order["clientOrderId"] in self.cancel_fails:
            raise ccxt.ExchangeError("cannot cancel")
        order["status"] = "canceled"
        return order

    def _open(self, symbol, trigger):
        return [
            o for o in self.orders.values()
            if o["status"] == "open" and o["trigger"] == trigger and (symbol is None or o["symbol"] == symbol)
        ]

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        return self._open(symbol, bool((params or {}).get("trigger")))

    def cancel_all_orders(self, symbol=None, params=None):
        trigger = bool((params or {}).get("trigger"))
        self.calls.append(("cancel_all_orders", symbol, trigger))
        for order in self._open(symbol, trigger):
            order["status"] = "canceled"
        return []

    def fetch_positions(self, symbols=None, params=None):
        return self.positions

    def fetch_balance(self, params=None):
        return self.balance

    def fetch_ticker(self, symbol):
        return {"last": 100.0}

    def created(self):
        return [c for c in self.calls if c[0] == "create_order"]

    def cancels(self):
        return [c for c in self.calls if c[0] == "cancel_order"]


def make_ticket(**overrides) -> OrderTicket:
    base = dict(
        id="t-ccxt01", setup_id="s-ccxt01", symbol=SYMBOL, side=Side.LONG, entry=100.0, stop=98.0,
        targets=[106.0], qty=1.234, leverage=3.0, risk_pct=0.5, fee_rate=0.0005, market="futures",
    )
    base.update(overrides)
    return OrderTicket(**base)


def broker_with(fake: FakeExchange) -> CcxtBroker:
    broker = CcxtBroker("binanceusdm", testnet=True, allowlist=(SYMBOL,), env=env_for("binanceusdm"))
    broker._exchange = fake
    return broker


# --- construction -------------------------------------------------------------------------------


@pytest.mark.parametrize("exchange_id", ["binance", "btcturk", "kraken", "coinbase", "binanceus", "binancecoinm"])
def test_only_binanceusdm_is_allowed_for_live_crypto(exchange_id):
    with pytest.raises(BrokerError) as info:
        CcxtBroker(exchange_id, testnet=True, env=env_for(exchange_id))
    assert "binanceusdm" in str(info.value)


def test_missing_keys_name_the_variables_without_values():
    with pytest.raises(BrokerError) as info:
        CcxtBroker("binanceusdm", testnet=True, env={"BINANCEUSDM_API_KEY": DUMMY_KEY})
    assert "BINANCEUSDM_API_SECRET" in str(info.value) and DUMMY_KEY not in str(info.value)


def test_keys_are_not_exposed_and_testnet_means_demo_trading():
    broker = CcxtBroker("binanceusdm", testnet=True, env=env_for("binanceusdm"))
    for text in (repr(broker), str(broker), broker.name, broker.environment):
        assert DUMMY_KEY not in text and DUMMY_SECRET not in text
    assert all(DUMMY_SECRET not in str(v) for k, v in vars(broker).items() if k != "_exchange")
    assert "demo" in broker.environment and broker._exchange.options.get("enableDemoTrading") is True
    assert "CANLI" in CcxtBroker("binanceusdm", testnet=False, env=env_for("binanceusdm")).environment


def test_spot_symbols_are_refused_on_the_futures_venue():
    with pytest.raises(BrokerError):
        broker_with(FakeExchange()).market_type("BTC/USDT", "spot")
    with pytest.raises(BrokerError):
        broker_with(FakeExchange()).market_type("DOGE/USDT:USDT", "futures")


# --- bracket placement ------------------------------------------------------------------------------


def test_protective_legs_exist_before_the_entry():
    fake = FakeExchange()
    results = broker_with(fake).place_bracket(make_ticket())
    assert ("set_margin_mode", "isolated", SYMBOL) in fake.calls and ("set_leverage", 3, SYMBOL) in fake.calls
    stop, take_profit, entry = fake.created()
    assert [stop[1], take_profit[1], entry[1]] == ["stop", "take_profit", "entry"]
    ids = client_order_ids("t-ccxt01")
    # Binance refuses closePosition legs without a position (-4509); reduce-only legs with the
    # ticket quantity are accepted (verified on Demo Trading) and can never flip the position.
    assert stop[5] == 1.234 and stop[7] == {"stopLossPrice": 98.0, "reduceOnly": True, "clientOrderId": ids["stop"]}
    assert take_profit[5] == 1.234
    assert take_profit[7] == {"takeProfitPrice": 106.0, "reduceOnly": True, "clientOrderId": ids["take_profit"]}
    assert entry[3:7] == ("limit", "buy", 1.234, 100.0) and entry[7] == {"clientOrderId": ids["entry"]}
    assert [(r.role, r.id) for r in results] == [
        ("entry", ids["entry"]), ("stop", ids["stop"]), ("take_profit", ids["take_profit"])
    ]


def test_short_bracket_exits_with_buys():
    fake = FakeExchange()
    broker_with(fake).place_bracket(make_ticket(side=Side.SHORT, stop=102.0, targets=[94.0]))
    assert [c[4] for c in fake.created()] == ["buy", "buy", "sell"]


def test_margin_mode_already_set_is_fine():
    fake = FakeExchange(margin_already_set=True)
    assert len(broker_with(fake).place_bracket(make_ticket())) == 3


def test_hedge_mode_and_existing_exposure_are_refused_before_any_order():
    fake = FakeExchange(hedged=True)
    with pytest.raises(BrokerError, match="hedge"):
        broker_with(fake).place_bracket(make_ticket())
    fake = FakeExchange()
    fake.positions = [{"symbol": SYMBOL, "side": "long", "contracts": 1.0, "contractSize": 1.0}]
    with pytest.raises(BrokerError, match="zaten"):
        broker_with(fake).place_bracket(make_ticket())
    assert fake.created() == []


def test_rejected_entry_cancels_the_protective_legs_through_the_algo_endpoint():
    fake = FakeExchange(fail={"entry": ccxt.InsufficientFunds("margin")})
    with pytest.raises(BrokerError) as info:
        broker_with(fake).place_bracket(make_ticket())
    assert not isinstance(info.value, OrderStateUnknown)
    assert "iptal edildi" in str(info.value)
    assert [c[2] for c in fake.cancels()] == [{"trigger": True}, {"trigger": True}]
    assert fake._open(SYMBOL, True) == []


def test_rejected_stop_means_nothing_was_sent():
    fake = FakeExchange(fail={"stop": ccxt.InvalidOrder("bad trigger")})
    with pytest.raises(BrokerError, match="Hiçbir emir gönderilmedi"):
        broker_with(fake).place_bracket(make_ticket())
    assert len(fake.created()) == 1 and fake.cancels() == []


def test_failed_cleanup_is_reported_as_unknown_state():
    ids = client_order_ids("t-ccxt01")
    fake = FakeExchange(fail={"entry": ccxt.InsufficientFunds("margin")}, cancel_fails={ids["stop"]})
    with pytest.raises(OrderStateUnknown, match="EDİLEMEDİ"):
        broker_with(fake).place_bracket(make_ticket())


def test_entry_timeout_that_landed_is_resolved_by_client_id():
    fake = FakeExchange(fail={"entry": "timeout_landed"})
    results = broker_with(fake).place_bracket(make_ticket())
    assert [r.role for r in results] == ["entry", "stop", "take_profit"]
    assert ("fetch_order", client_order_ids("t-ccxt01")["entry"], False) in fake.calls


def test_entry_timeout_that_did_not_land_rolls_back():
    fake = FakeExchange(fail={"entry": "timeout_absent"})
    with pytest.raises(BrokerError, match="iptal edildi"):
        broker_with(fake).place_bracket(make_ticket())
    assert fake._open(SYMBOL, True) == []


def test_unverifiable_entry_keeps_the_protection():
    fake = FakeExchange(fail={"entry": "timeout_absent"}, lookup_fails=True)
    with pytest.raises(OrderStateUnknown):
        broker_with(fake).place_bracket(make_ticket())
    assert fake.cancels() == [] and len(fake._open(SYMBOL, True)) == 2


def test_ctrl_c_before_the_entry_removes_the_legs():
    fake = FakeExchange(fail={"take_profit": "interrupt"})
    with pytest.raises(KeyboardInterrupt):
        broker_with(fake).place_bracket(make_ticket())
    assert fake._open(SYMBOL, True) == []


def test_ctrl_c_during_the_entry_keeps_the_legs():
    fake = FakeExchange(fail={"entry": "interrupt"})
    with pytest.raises(KeyboardInterrupt):
        broker_with(fake).place_bracket(make_ticket())
    assert fake.cancels() == [] and len(fake._open(SYMBOL, True)) == 2


@pytest.mark.parametrize(
    "override", [{"qty": 1.23456}, {"leverage": 2.5}, {"stop": None}, {"targets": []}, {"stop": 101.0}]
)
def test_invalid_or_unrounded_brackets_are_refused_before_any_order(override):
    fake = FakeExchange()
    with pytest.raises(BrokerError):
        broker_with(fake).place_bracket(make_ticket(**override))
    assert fake.created() == []


# --- rounding / venue limits -------------------------------------------------------------------------


def test_prices_are_rounded_toward_less_risk():
    broker = broker_with(FakeExchange())
    assert broker.normalize_prices(SYMBOL, Side.LONG, 100.04, 98.03, [106.07]) == (100.0, 98.1, [106.0])
    assert broker.normalize_prices(SYMBOL, Side.SHORT, 100.06, 102.07, [94.03]) == (100.1, 102.0, [94.1])
    assert broker.normalize_qty(SYMBOL, 1.23456) == 1.234


def test_venue_minimums_are_reported():
    broker = broker_with(FakeExchange())
    assert broker.venue_problems(make_ticket(qty=0.001, entry=100.0)) != []  # 0.10 USDT < 5 USDT minimum
    assert broker.venue_problems(make_ticket()) == []


# --- reconciliation / kill ---------------------------------------------------------------------------


def test_reconcile_keeps_working_brackets_and_cleans_up_finished_ones():
    fake = FakeExchange()
    broker = broker_with(fake)
    ticket = make_ticket()
    broker.place_bracket(ticket)
    assert broker.reconcile_ticket(ticket) == "open"
    ids = client_order_ids(ticket.id)
    fake.orders[ids["entry"]]["status"] = "closed"  # filled, then the stop closed the position
    fake.orders[ids["stop"]]["status"] = "closed"
    assert broker.reconcile_ticket(ticket) == "closed"
    assert fake.orders[ids["take_profit"]]["status"] == "canceled"


def test_reconcile_keeps_a_ticket_with_a_position():
    fake = FakeExchange()
    fake.positions = [{"symbol": SYMBOL, "side": "long", "contracts": 1.0, "contractSize": 1.0}]
    assert broker_with(fake).reconcile_ticket(make_ticket()) == "open"


def test_open_orders_cover_both_scopes_and_label_roles():
    fake = FakeExchange()
    broker = broker_with(fake)
    broker.place_bracket(make_ticket())
    assert sorted(o.role for o in broker.get_open_orders()) == ["entry", "stop", "take_profit"]
    assert broker.cancel_all() == 3
    assert ("cancel_all_orders", SYMBOL, True) in fake.calls and broker.get_open_orders() == []


def test_close_all_uses_reduce_only_or_position_side_and_reports_failures():
    fake = FakeExchange()
    fake.positions = [
        {"symbol": SYMBOL, "side": "long", "contracts": 2.0, "contractSize": 1.0},
        {"symbol": "ETH/USDT:USDT", "side": "short", "contracts": 3.0, "contractSize": 1.0},
        {"symbol": "SOL/USDT:USDT", "side": "long", "contracts": 0.0, "contractSize": 1.0},
    ]
    broker_with(fake).close_all()
    assert [(c[2], c[4], c[5], c[7]) for c in fake.created()] == [
        (SYMBOL, "sell", 2.0, {"reduceOnly": True}),
        ("ETH/USDT:USDT", "buy", 3.0, {"reduceOnly": True}),
    ]
    hedged = FakeExchange(hedged=True, fail={"close": ccxt.ExchangeError("rejected")})
    hedged.positions = fake.positions[:2]
    results = broker_with(hedged).close_all()
    assert [c[7] for c in hedged.created()] == [{"positionSide": "LONG"}, {"positionSide": "SHORT"}]
    assert [r.status for r in results] == ["failed", "failed"] and len({r.id for r in results}) == 2


def test_signed_requests_compensate_for_clock_drift():
    broker = CcxtBroker("binanceusdm", testnet=True, env=env_for("binanceusdm"))
    assert broker._exchange.options.get("adjustForTimeDifference") is True
    assert broker._exchange.options.get("recvWindow") == 10_000


def test_leverage_above_the_symbol_maximum_is_lowered_to_it():
    fake = FakeExchange()
    calls = []

    def set_leverage(leverage, symbol):
        calls.append(leverage)
        if leverage > 10:
            raise ccxt.BadRequest('binanceusdm {"code":-4028,"msg":"Leverage 14 is not valid"}')

    fake.set_leverage = set_leverage
    fake.fetch_market_leverage_tiers = lambda symbol, params=None: [{"maxLeverage": 10}, {"maxLeverage": 5}]
    results = broker_with(fake).place_bracket(make_ticket(leverage=14.0))
    assert calls == [14, 10]
    assert {r.role for r in results} == {"entry", "stop", "take_profit"}
