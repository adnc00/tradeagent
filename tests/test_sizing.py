import json
import math

import pytest

from tradeagent import cli
from tradeagent.brokers.paper import PaperBroker
from tradeagent.config import load_config
from tradeagent.models import AccountState, OpenRisk, OrderTicket, RiskLimits, Side
from tradeagent.risk import check_ticket
from tests.test_cli import StubMarketData, write_config


def futures_ticket(**overrides) -> OrderTicket:
    values = dict(
        id="t-f", setup_id="s-f", symbol="SOL/USDT:USDT", side="long", entry=100.0, stop=99.0, targets=[104.0],
        qty=1.0, leverage=1.0, risk_pct=5.0, fee_rate=0.0005, market="futures",
    )
    values.update(overrides)
    return OrderTicket(**values)


# --- correlated group cap ------------------------------------------------------------------------------


def limits(**overrides) -> RiskLimits:
    values = dict(risk_pct=5.0, max_risk_pct=5.0, max_open_risk_pct=15.0, daily_loss_pct=10.0, weekly_loss_pct=25.0,
                  max_leverage=20.0, allowlist=("*/USDT:USDT",))
    values.update(overrides)
    return RiskLimits(**values)


def codes(ticket, state, lim):
    return {v.code for v in check_ticket(ticket, state, lim)}


def test_group_cap_defaults_to_the_single_trade_cap():
    t = futures_ticket(qty=4.0, leverage=10.0)
    state = AccountState(equity=100.0, open_risks=(OpenRisk("ETH/USDT:USDT", Side.LONG, 4.0),))
    assert "correlated_risk_limit" in codes(t, state, limits())


def test_group_cap_can_allow_several_correlated_positions():
    t = futures_ticket(qty=4.0, leverage=10.0)
    state = AccountState(equity=100.0, open_risks=(OpenRisk("ETH/USDT:USDT", Side.LONG, 4.0),))
    assert "correlated_risk_limit" not in codes(t, state, limits(max_group_risk_pct=15.0))
    full = AccountState(equity=100.0, open_risks=(OpenRisk("ETH/USDT:USDT", Side.LONG, 12.0),))
    assert "correlated_risk_limit" in codes(t, full, limits(max_group_risk_pct=15.0))


def test_group_cap_must_not_be_below_the_trade_cap():
    with pytest.raises(ValueError):
        limits(max_group_risk_pct=2.0)


# --- automatic leverage ----------------------------------------------------------------------------------


def test_leverage_is_raised_so_one_trade_uses_at_most_its_margin_share():
    lim = limits(margin_per_trade_pct=33.0)
    sized = cli.resize_for_account(futures_ticket(), 100.0, PaperBroker.__new__(PaperBroker), lim)
    notional = sized.qty * sized.entry
    assert sized.leverage == math.ceil(notional / 33.0)
    assert notional / sized.leverage <= 33.0 + 1e-9


def test_leverage_is_capped_at_the_maximum():
    lim = limits(margin_per_trade_pct=33.0, max_leverage=3.0)
    sized = cli.resize_for_account(futures_ticket(), 100.0, PaperBroker.__new__(PaperBroker), lim)
    assert sized.leverage == 3.0


def test_spot_and_disabled_auto_leverage_keep_the_ticket_leverage():
    broker = PaperBroker.__new__(PaperBroker)
    spot = futures_ticket(symbol="SOL/USDT", market="spot")
    assert cli.resize_for_account(spot, 100.0, broker, limits(margin_per_trade_pct=33.0)).leverage == 1.0
    assert cli.resize_for_account(futures_ticket(), 100.0, broker, limits()).leverage == 1.0


# --- several timeframes ----------------------------------------------------------------------------------


def test_timeframes_parse_and_the_watch_interval_follows_the_shortest():
    assert cli.parse_timeframes("5m, 15m") == ["5m", "15m"]
    assert cli.default_interval(["15m", "5m"]) == 5
    with pytest.raises(ValueError):
        cli.parse_timeframes("5m,7m")


def test_scan_runs_every_timeframe_and_fetches_the_bias_once(tmp_path, monkeypatch):
    calls = []

    class CountingData(StubMarketData):
        def get_ohlcv(self, symbol, timeframe, limit=500):
            calls.append(timeframe)
            return super().get_ohlcv(symbol, timeframe, limit)

    monkeypatch.setattr(StubMarketData, "price", None)
    monkeypatch.setattr(cli, "MarketData", CountingData)
    path = write_config(tmp_path)
    assert cli.main(["--config", str(path), "scan", "--symbols", "BTC/USDT", "--tf", "5m,15m"]) == 0
    assert calls.count("1d") == 1
    assert calls.count("5m") == 1 and calls.count("15m") == 1


def test_scanner_applies_automatic_leverage_before_its_risk_check():
    import pandas as pd
    from tradeagent.scanner import ScannerParams, scan_symbol
    from tests.synthetic import long_setup_frames

    ltf, daily = long_setup_frames()
    lim = limits(max_group_risk_pct=15.0, margin_per_trade_pct=33.0, min_rr=1.0)
    (setup,) = scan_symbol(
        "SOL/USDT:USDT", ltf, daily, timeframe="15m", account=AccountState(equity=10_000.0), limits=lim,
        params=ScannerParams(), fee_rate=0.0005, market="futures", now=ltf.index[-1].to_pydatetime(),
    )
    assert setup.ticket is not None, setup.reasons
    notional = setup.ticket.qty * setup.ticket.entry
    assert setup.ticket.leverage == math.ceil(notional / 3_300.0)
