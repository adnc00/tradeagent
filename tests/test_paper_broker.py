from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from tradeagent.brokers.base import BrokerError
from tradeagent.brokers.paper import PaperBroker
from tradeagent.models import Candle, OrderTicket, Side
from tradeagent.risk import ticket_risk_amount

T0 = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)


def make_ticket(**overrides) -> OrderTicket:
    base = dict(
        id="t-paper01",
        setup_id="s-paper01",
        symbol="BTC/USDT",
        side=Side.LONG,
        entry=100.0,
        stop=98.0,
        targets=[106.0, 110.0],
        qty=10.0,
        fee_rate=0.001,
        market="spot",
    )
    base.update(overrides)
    return OrderTicket(**base)


def candle(minutes: int, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(ts=T0 + timedelta(minutes=minutes), open=o, high=h, low=low, close=c)


@pytest.fixture
def broker(tmp_path):
    return PaperBroker(tmp_path / "paper.json", initial_equity=10_000.0)


def test_place_bracket_creates_pending_entry_with_protection(broker):
    orders = broker.place_bracket(make_ticket())
    assert [o.role for o in orders] == ["entry", "stop", "take_profit"]
    assert [o.price for o in orders] == [100.0, 98.0, 106.0]
    assert [o.side for o in orders] == ["buy", "sell", "sell"]
    assert broker.get_positions() == []
    assert [o.role for o in broker.get_open_orders()] == ["entry"]
    assert broker.get_balance().equity == 10_000.0


def test_entry_fills_only_when_price_reaches_it(broker):
    broker.place_bracket(make_ticket())
    assert broker.feed_candle("BTC/USDT", candle(0, 101.0, 102.0, 100.5, 101.5)) == []
    fills = broker.feed_candle("BTC/USDT", candle(15, 101.0, 101.2, 99.8, 100.4))
    assert [(f.kind, f.price, f.qty) for f in fills] == [("entry", 100.0, 10.0)]
    assert fills[0].fee == pytest.approx(10 * 100.0 * 0.001)
    (position,) = broker.get_positions()
    assert (position.side, position.qty, position.entry_price, position.stop, position.take_profit) == (
        Side.LONG,
        10.0,
        100.0,
        98.0,
        106.0,
    )
    assert position.ticket_id == "t-paper01"


def test_take_profit_closes_with_r_multiple(broker):
    ticket = make_ticket()
    broker.place_bracket(ticket)
    broker.feed_candle("BTC/USDT", candle(0, 100.5, 100.6, 99.9, 100.2))
    fills = broker.feed_candle("BTC/USDT", candle(15, 104.0, 106.5, 103.9, 106.2))
    (exit_fill,) = fills
    expected_pnl = (106.0 - 100.0) * 10 - 10 * 100.0 * 0.001 - 10 * 106.0 * 0.001
    assert (exit_fill.kind, exit_fill.price) == ("take_profit", 106.0)
    assert exit_fill.pnl == pytest.approx(expected_pnl)
    assert exit_fill.r == pytest.approx(expected_pnl / ticket_risk_amount(ticket))
    assert broker.get_positions() == []
    assert broker.get_balance().equity == pytest.approx(10_000.0 + expected_pnl)


def test_stop_loss_costs_about_one_r(broker):
    ticket = make_ticket()
    broker.place_bracket(ticket)
    broker.feed_candle("BTC/USDT", candle(0, 100.5, 100.6, 99.9, 100.2))
    (exit_fill,) = broker.feed_candle("BTC/USDT", candle(15, 99.0, 99.5, 97.5, 97.8))
    assert (exit_fill.kind, exit_fill.price) == ("stop", 98.0)
    assert exit_fill.r == pytest.approx(-1.0, abs=0.01)
    assert exit_fill.r > -1.0


def test_gap_through_stop_fills_at_the_open(broker):
    broker.place_bracket(make_ticket())
    broker.feed_candle("BTC/USDT", candle(0, 100.5, 100.6, 99.9, 100.2))
    (exit_fill,) = broker.feed_candle("BTC/USDT", candle(15, 96.0, 96.5, 95.0, 95.5))
    assert exit_fill.price == 96.0
    assert exit_fill.r < -1.0


def test_entry_candle_checks_the_stop_but_not_the_target(broker):
    broker.place_bracket(make_ticket())
    fills = broker.feed_candle("BTC/USDT", candle(0, 101.0, 107.0, 97.0, 104.0))
    assert [f.kind for f in fills] == ["entry", "stop"]


def test_stop_wins_when_one_candle_touches_both_levels(broker):
    broker.place_bracket(make_ticket())
    broker.feed_candle("BTC/USDT", candle(0, 100.5, 100.6, 99.9, 100.2))
    (exit_fill,) = broker.feed_candle("BTC/USDT", candle(15, 100.0, 107.0, 97.0, 101.0))
    assert exit_fill.kind == "stop"


def test_short_bracket_on_futures(broker):
    ticket = make_ticket(side=Side.SHORT, stop=102.0, targets=[94.0], market="futures", leverage=2.0)
    orders = broker.place_bracket(ticket)
    assert [o.side for o in orders] == ["sell", "buy", "buy"]
    broker.feed_candle("BTC/USDT", candle(0, 99.0, 100.2, 98.8, 100.1))
    (exit_fill,) = broker.feed_candle("BTC/USDT", candle(15, 96.0, 96.2, 93.5, 93.8))
    assert exit_fill.kind == "take_profit"
    assert exit_fill.pnl > 0


def test_short_on_spot_is_refused(broker):
    with pytest.raises(BrokerError):
        broker.place_bracket(make_ticket(side=Side.SHORT, stop=102.0, targets=[94.0]))


@pytest.mark.parametrize(
    "override",
    [{"stop": None}, {"targets": []}, {"qty": 0.0}, {"stop": 101.0}, {"targets": [99.0]}],
)
def test_unprotected_or_inconsistent_brackets_are_refused(broker, override):
    with pytest.raises(BrokerError):
        broker.place_bracket(make_ticket(**override))
    assert broker.get_open_orders() == []


def test_state_persists_across_instances(tmp_path):
    path = tmp_path / "paper.json"
    first = PaperBroker(path, initial_equity=5_000.0)
    first.place_bracket(make_ticket())
    first.feed_candle("BTC/USDT", candle(0, 100.5, 100.6, 99.9, 100.2))
    second = PaperBroker(path, initial_equity=99_999.0)
    (position,) = second.get_positions()
    assert position.entry_price == 100.0
    assert second.get_price("BTC/USDT") == 100.2
    assert second.get_balance().equity == pytest.approx(5_000.0 - 1.0 + (100.2 - 100.0) * 10)


def test_cancel_all_and_close_all(broker):
    broker.place_bracket(make_ticket())
    broker.place_bracket(make_ticket(id="t-paper02", symbol="ETH/USDT", entry=50.0, stop=49.0, targets=[53.0]))
    broker.feed_candle("BTC/USDT", candle(0, 100.5, 100.6, 99.9, 100.2))
    broker.feed_price("BTC/USDT", 101.0)
    assert broker.cancel_all() == 1
    assert broker.get_open_orders("ETH/USDT") == []
    closed = broker.close_all()
    assert [(o.role, o.symbol, o.price) for o in closed] == [("close", "BTC/USDT", 101.0)]
    assert broker.get_positions() == []
    exit_fill = broker.fills[-1]
    assert exit_fill.kind == "close"
    assert exit_fill.pnl == pytest.approx(10.0 - 1.0 - 1.01)


def test_price_and_ohlcv_need_data(broker):
    with pytest.raises(BrokerError):
        broker.get_price("BTC/USDT")
    with pytest.raises(BrokerError):
        broker.get_ohlcv("BTC/USDT", "15m", 10)


class StubMarketData:
    def __init__(self, df: pd.DataFrame):
        self.df = df
        self.calls = 0

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame:
        self.calls += 1
        return self.df

    def get_price(self, symbol: str) -> float:
        return float(self.df["close"].iloc[-1])


def test_sync_feeds_closed_candles_after_the_order_once(tmp_path):
    index = pd.date_range(T0, periods=5, freq="5min")
    df = pd.DataFrame(
        {
            "open": [100.5, 100.5, 100.4, 104.0, 106.5],
            "high": [100.6, 100.6, 100.5, 106.4, 107.0],
            "low": [99.0, 99.9, 99.8, 103.9, 106.0],
            "close": [100.5, 100.4, 100.2, 106.2, 106.8],
            "volume": 1.0,
        },
        index=index,
    )
    data = StubMarketData(df)
    broker = PaperBroker(
        tmp_path / "paper.json",
        initial_equity=10_000.0,
        market_data=data,
        clock=lambda: T0 + timedelta(minutes=7),
    )
    broker.place_bracket(make_ticket())
    fills = broker.sync()
    # candles 0-1 opened before the order and candle 4 is still forming: only 2-3 count
    assert [(f.kind, f.price) for f in fills] == [("entry", 100.0), ("take_profit", 106.0)]
    assert broker.sync() == []
    assert broker.get_price("BTC/USDT") == 106.8


def test_sync_without_market_data_is_a_no_op(broker):
    broker.place_bracket(make_ticket())
    assert broker.sync() == []


# --- adversarial review fixes ---------------------------------------------------------------

import threading  # noqa: E402

from tradeagent.brokers.paper import state_lock  # noqa: E402


def test_stale_instances_merge_instead_of_overwriting(tmp_path):
    path = tmp_path / "paper.json"
    first, second = PaperBroker(path), PaperBroker(path)
    first.place_bracket(make_ticket(id="t-a"))
    second.place_bracket(make_ticket(id="t-b"))
    assert sorted(b.ticket_id for b in PaperBroker(path).brackets) == ["t-a", "t-b"]


def test_state_lock_is_exclusive_across_handles(tmp_path):
    path = tmp_path / "paper.json"
    errors = []
    with state_lock(path):
        worker = threading.Thread(target=lambda: _try_lock(path, errors))
        worker.start()
        worker.join()
    assert len(errors) == 1 and "kilitli" in errors[0]
    with state_lock(path, timeout=0.2):
        pass


def _try_lock(path, errors):
    try:
        with state_lock(path, timeout=0.2):
            pass
    except BrokerError as exc:
        errors.append(str(exc))


def test_sync_does_not_fill_a_bracket_on_candles_before_it_existed(tmp_path):
    index = pd.date_range(T0, periods=4, freq="5min")
    df = pd.DataFrame(
        {"open": [100.5] * 4, "high": [100.6] * 4, "low": [99.5, 100.4, 100.4, 100.4], "close": [100.4] * 4, "volume": 1.0},
        index=index,
    )
    now = {"t": T0 - timedelta(minutes=1)}
    broker = PaperBroker(tmp_path / "paper.json", market_data=StubMarketData(df), clock=lambda: now["t"])
    broker.place_bracket(make_ticket(id="t-old"))
    now["t"] = T0 + timedelta(minutes=2)
    broker.place_bracket(make_ticket(id="t-new"))
    fills = broker.sync()
    assert [(f.ticket_id, f.kind) for f in fills] == [("t-old", "entry")]


def test_sync_warns_when_data_does_not_reach_back_to_the_order(tmp_path):
    index = pd.date_range(T0 + timedelta(days=5), periods=3, freq="5min")
    df = pd.DataFrame({"open": 101.0, "high": 101.0, "low": 101.0, "close": 101.0, "volume": 1.0}, index=index)
    broker = PaperBroker(tmp_path / "paper.json", market_data=StubMarketData(df), clock=lambda: T0)
    broker.place_bracket(make_ticket())
    broker.sync()
    assert broker.sync_warnings and "BTC/USDT" in broker.sync_warnings[0]


def test_corrupt_state_gives_a_turkish_error_with_the_path(tmp_path):
    path = tmp_path / "paper.json"
    path.write_text('{"version": 1, "cash": ', encoding="utf-8")
    with pytest.raises(BrokerError) as info:
        PaperBroker(path)
    assert str(path) in str(info.value) and "okunamadı" in str(info.value)


def test_cancel_orders_is_per_symbol(broker):
    broker.place_bracket(make_ticket())
    broker.place_bracket(make_ticket(id="t-eth", symbol="ETH/USDT", entry=50.0, stop=49.0, targets=[53.0]))
    assert broker.cancel_orders("ETH/USDT") == 1
    assert [o.symbol for o in broker.get_open_orders()] == ["BTC/USDT"]
