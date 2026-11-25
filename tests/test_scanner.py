import pandas as pd
import pytest

from tradeagent.models import AccountState, RiskLimits, Side
from tradeagent.risk import position_size
from tradeagent.scanner import ScannerParams, scan_symbol
from tests.synthetic import LONG_SETUP_DAILY_ROWS, frame, long_setup_frames, mirror

ALLOW = ("BTC/USDT", "BTC/USDT:USDT", "AAPL")
LIMITS = RiskLimits(allowlist=ALLOW)
ACCOUNT = AccountState(equity=10_000.0)
STOP = 99.5 * (1 - 0.05 / 100)


def scan(ltf, daily, *, symbol="BTC/USDT", params=ScannerParams(), limits=LIMITS, market="spot", fee_rate=0.001):
    return scan_symbol(
        symbol,
        ltf,
        daily,
        timeframe="15m",
        account=ACCOUNT,
        limits=limits,
        params=params,
        fee_rate=fee_rate,
        market=market,
    )


def test_long_setup_geometry_and_ticket():
    ltf, daily = long_setup_frames()
    (setup,) = scan(ltf, daily)
    assert setup.side is Side.LONG
    assert setup.bias == "bullish"
    assert (setup.sweep_kind, setup.sweep_level, setup.sweep_extreme) == ("swing_low", 100.0, 99.5)
    assert setup.mss_level == 100.7
    assert setup.mss_at == pd.Timestamp("2026-11-18 12:15+00:00")
    assert (setup.fvg_bottom, setup.fvg_top) == (100.35, 100.95)
    assert setup.entry == pytest.approx(100.65)
    assert setup.stop == pytest.approx(STOP)
    assert setup.targets == [104.0, 105.0]
    assert setup.target_kinds == ["swing_high", "PDH"]
    assert (setup.range_high, setup.range_low) == (104.0, 99.5)
    assert setup.killzone == "ny_am"
    assert setup.detected_at == ltf.index[-1]
    assert setup.reasons == []

    ticket = setup.ticket
    assert ticket is not None
    assert ticket.setup_id == setup.id
    assert ticket.id == "t-" + setup.id.removeprefix("s-")
    assert ticket.side is Side.LONG and ticket.market == "spot" and ticket.leverage == 1.0
    assert ticket.qty == pytest.approx(position_size(10_000, 0.5, 100.65, STOP, 0.001))
    assert ticket.targets == [104.0, 105.0]
    assert ticket.risk_pct == 0.5
    assert "sezgisel" in ticket.reason


def test_scan_is_deterministic():
    ltf, daily = long_setup_frames()
    first, second = scan(ltf, daily), scan(ltf, daily)
    assert [s.id for s in first] == [s.id for s in second]
    assert first[0].id.startswith("s-") and len(first[0].id) == 10


def test_entry_at_fvg_top_is_configurable_and_rejections_are_attached():
    ltf, daily = long_setup_frames()
    (setup,) = scan(ltf, daily, params=ScannerParams(entry="top"))
    assert setup.entry == 100.95
    assert setup.ticket is None
    assert any("R:R" in reason for reason in setup.reasons)


def test_symbol_outside_allowlist_keeps_the_setup_without_ticket():
    ltf, daily = long_setup_frames()
    (setup,) = scan(ltf, daily, symbol="ETH/USDT")
    assert setup.ticket is None
    assert any("izin listesinde" in reason for reason in setup.reasons)


def test_stock_quantity_is_whole_shares():
    ltf, daily = long_setup_frames()
    (setup,) = scan(ltf, daily, symbol="AAPL", market="equity", fee_rate=0.0)
    assert setup.ticket is not None
    assert setup.ticket.qty == float(int(position_size(10_000, 0.5, 100.65, STOP)))


def test_short_setup_is_the_mirror_on_futures():
    ltf, daily = long_setup_frames()
    params = ScannerParams(leverage=3.0)
    (setup,) = scan(mirror(ltf, 200.0), mirror(daily, 200.0), symbol="BTC/USDT:USDT", market="futures", params=params)
    assert setup.side is Side.SHORT
    assert setup.bias == "bearish"
    assert (setup.sweep_kind, setup.sweep_level, setup.sweep_extreme) == ("swing_high", 100.0, 100.5)
    assert setup.mss_level == pytest.approx(99.3)
    assert (setup.fvg_bottom, setup.fvg_top) == pytest.approx((99.05, 99.65))
    assert setup.entry == pytest.approx(99.35)
    assert setup.stop == pytest.approx(100.5 * (1 + 0.05 / 100))
    assert setup.targets == pytest.approx([96.0, 95.0])
    assert setup.target_kinds == ["swing_low", "PDL"]
    assert setup.ticket is not None
    assert setup.ticket.leverage == 3.0


def test_short_on_spot_is_rejected_by_the_risk_engine():
    ltf, daily = long_setup_frames()
    (setup,) = scan(mirror(ltf, 200.0), mirror(daily, 200.0), market="spot")
    assert setup.side is Side.SHORT
    assert setup.ticket is None
    assert any("short" in reason for reason in setup.reasons)


def test_no_setup_without_bullish_bias():
    ltf, _ = long_setup_frames()
    inside_day = [*LONG_SETUP_DAILY_ROWS[:2], (100.5, 100.9, 97.5, 99.0)]
    daily = frame(inside_day, "2026-11-15 00:00+00:00", freq="1D")
    assert scan(ltf, daily) == []


def test_killzone_filter():
    ltf, daily = long_setup_frames()
    assert scan(ltf, daily, params=ScannerParams(killzones=("london",))) == []
    assert len(scan(ltf, daily, params=ScannerParams(killzones=("ny_am",)))) == 1


def test_stale_setups_are_ignored():
    ltf, daily = long_setup_frames()
    assert scan(ltf, daily, params=ScannerParams(max_setup_age=5)) == []


def test_filled_fvg_invalidates_the_setup():
    ltf, daily = long_setup_frames()
    extra = frame([(101.60, 101.70, 100.30, 100.40)], ltf.index[-1] + pd.Timedelta(minutes=15))
    assert scan(pd.concat([ltf, extra]), daily) == []


def test_trading_below_the_sweep_low_invalidates_the_setup():
    ltf, daily = long_setup_frames()
    extra = frame([(101.60, 101.70, 99.40, 99.45)], ltf.index[-1] + pd.Timedelta(minutes=15))
    assert scan(pd.concat([ltf, extra]), daily) == []


def test_scan_rejects_non_positive_prices():
    ltf, daily = long_setup_frames()
    with pytest.raises(ValueError):
        scan(mirror(ltf, 50.0), daily)


@pytest.mark.parametrize("bad", [{"entry": "mid"}, {"swing_length": 0}, {"killzones": ("lunch",)}, {"stop_buffer_pct": -1}])
def test_scanner_params_are_validated(bad):
    with pytest.raises(ValueError):
        ScannerParams(**bad)


def test_too_few_candles_yield_no_setup():
    ltf, daily = long_setup_frames()
    assert scan(ltf.iloc[:2], daily) == []
    assert scan(ltf.iloc[:0], daily) == []


# --- adversarial review fixes ---------------------------------------------------------------

from datetime import UTC, datetime  # noqa: E402

from tests.synthetic import LONG_SETUP_ROWS, follow_through_candles  # noqa: E402


def _with_candles(ltf, candles):
    extra = pd.DataFrame(
        [(c.open, c.high, c.low, c.close, 1.0) for c in candles],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([c.ts for c in candles]),
    )
    return pd.concat([ltf, extra])


def test_setup_whose_entry_was_already_touched_is_dropped():
    ltf, daily = long_setup_frames()
    assert scan(_with_candles(ltf, follow_through_candles()[:2]), daily) == []
    assert scan(_with_candles(ltf, follow_through_candles()), daily) == []


def test_break_of_a_minor_high_after_the_sweep_is_not_an_mss():
    rows = list(LONG_SETUP_ROWS)
    rows[11] = (100.45, 101.40, 100.35, 100.55)  # lower high 101.40 leads into the sweep
    rows[14:15] = [
        (100.10, 100.60, 100.05, 100.40),  # minor high 100.60 after the sweep
        (100.40, 100.50, 100.20, 100.30),
    ]
    rows[16] = (100.30, 101.30, 100.25, 101.25)  # displacement closes above 100.60, below 101.40
    ltf = frame(rows, "2026-11-18 08:15+00:00")
    _, daily = long_setup_frames()
    assert scan(ltf, daily) == []


def test_pdh_already_taken_today_is_not_a_target():
    ltf, daily = long_setup_frames()
    taken = daily.copy()
    taken.loc[taken.index[-1], "high"] = 106.0  # PDH 105 traded through before the LTF window
    (setup,) = scan(ltf, taken)
    assert setup.targets == [104.0] and setup.target_kinds == ["swing_high"]


def test_ticket_creation_time_is_the_scan_time():
    ltf, daily = long_setup_frames()
    now = datetime(2026, 11, 18, 15, 1, tzinfo=UTC)
    (setup,) = scan_symbol(
        "BTC/USDT", ltf, daily, timeframe="15m", account=ACCOUNT, limits=LIMITS, fee_rate=0.001, now=now
    )
    assert setup.ticket.created_at == now
