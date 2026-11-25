import pytest

from tradeagent.models import AccountState, OpenRisk, OrderTicket, RiskLimits, Side
from tradeagent.risk import (
    check_ticket,
    correlation_group,
    floor_to_step,
    liquidation_price,
    position_size,
    rr,
    ticket_risk_amount,
)

LIMITS = RiskLimits(allowlist=("BTC/USDT", "ETH/USDT", "SOL/USDT", "BTC/USDT:USDT", "AAPL", "THYAO.IS"))
NAN = float("nan")
INF = float("inf")


def make_ticket(**overrides) -> OrderTicket:
    """A clean spot long BTC ticket risking exactly 0.5% of 10,000 (no fees)."""
    base = dict(
        id="t-test",
        setup_id="s-test",
        symbol="BTC/USDT",
        side=Side.LONG,
        entry=60_000.0,
        stop=59_400.0,
        targets=[61_500.0, 62_400.0],
        qty=50 / 600,
        leverage=1.0,
        risk_pct=0.5,
        fee_rate=0.0,
        market="spot",
    )
    base.update(overrides)
    return OrderTicket(**base)


def futures_ticket(**overrides) -> OrderTicket:
    return make_ticket(**{"symbol": "BTC/USDT:USDT", "market": "futures", **overrides})


def make_state(**overrides) -> AccountState:
    base = dict(equity=10_000.0)
    base.update(overrides)
    return AccountState(**base)


def codes(violations) -> set[str]:
    return {v.code for v in violations}


# --- position_size -----------------------------------------------------------------


def test_position_size_matches_reference_example():
    # risk-portfolio.md section 1: 10,000 equity, 1% risk, BTC 60,000 / stop 59,400 -> 0.1667
    assert position_size(10_000, 1.0, 60_000, 59_400) == pytest.approx(100 / 600)


def test_position_size_includes_round_trip_fees_on_notional():
    qty = position_size(10_000, 1.0, 60_000, 59_400, fee_rate=0.0004)
    assert qty == pytest.approx(100 / (600 + 2 * 0.0004 * 60_000))
    total_risk = qty * 600 + 2 * 0.0004 * qty * 60_000
    assert total_risk == pytest.approx(100)


def test_position_size_short_uses_absolute_distance():
    assert position_size(10_000, 0.5, 100.0, 102.0) == pytest.approx(25.0)


@pytest.mark.parametrize(
    "override",
    [
        {"equity": 0},
        {"equity": -5},
        {"equity": NAN},
        {"risk_pct": 0},
        {"risk_pct": NAN},
        {"entry": 0},
        {"entry": NAN},
        {"entry": INF},
        {"stop": -1},
        {"stop": NAN},
        {"stop": 60_000},
        {"fee_rate": -0.001},
        {"fee_rate": NAN},
    ],
)
def test_position_size_rejects_degenerate_input(override):
    args = dict(equity=10_000, risk_pct=1.0, entry=60_000, stop=59_400, fee_rate=0.0)
    args.update(override)
    with pytest.raises(ValueError):
        position_size(**args)


def test_floor_to_step_never_rounds_up():
    assert floor_to_step(12.9, 1.0) == 12.0
    assert floor_to_step(0.123456, 0.001) == pytest.approx(0.123)
    assert floor_to_step(3.0, 1.0) == 3.0
    assert floor_to_step(0.5, None) == 0.5


# --- liquidation / rr --------------------------------------------------------------------


def test_liquidation_price_long_and_short_formula():
    assert liquidation_price(60_000, 10, "long", 0.004) == pytest.approx(60_000 * (1 - 0.1 + 0.004))
    assert liquidation_price(60_000, 10, Side.SHORT, 0.004) == pytest.approx(60_000 * (1 + 0.1 - 0.004))


@pytest.mark.parametrize(
    "entry,leverage,mmr",
    [
        (100.0, 0, 0.01),
        (100.0, -2, 0.01),
        (100.0, 0.5, 0.01),
        (100.0, 5, -0.01),
        (100.0, 5, 1.0),
        (0.0, 5, 0.01),
        (NAN, 5, 0.01),
        (100.0, NAN, 0.01),
    ],
)
def test_liquidation_price_rejects_degenerate_input(entry, leverage, mmr):
    with pytest.raises(ValueError):
        liquidation_price(entry, leverage, "long", mmr)


def test_rr_long_short_and_wrong_side_target():
    assert rr(100, 98, 106) == pytest.approx(3.0)
    assert rr(100, 102, 94) == pytest.approx(3.0)
    assert rr(100, 98, 99) == pytest.approx(-0.5)


@pytest.mark.parametrize(
    "entry,stop,target",
    [(100, 100, 105), (NAN, 98, 105), (100, 98, NAN), (0, 98, 105), (100, -1, 105)],
)
def test_rr_rejects_degenerate_input(entry, stop, target):
    with pytest.raises(ValueError):
        rr(entry, stop, target)


def test_ticket_risk_amount_adds_round_trip_fees():
    ticket = make_ticket(qty=0.2, fee_rate=0.001)
    assert ticket_risk_amount(ticket) == pytest.approx(0.2 * 600 + 2 * 0.001 * 0.2 * 60_000)


# --- correlation groups ----------------------------------------------------------------


def test_correlation_groups_by_quote_and_direction():
    assert correlation_group("BTC/USDT", Side.LONG) == "crypto:long"
    assert correlation_group("SOL/USDT:USDT", "long") == "crypto:long"
    assert correlation_group("ETH/USDC", Side.LONG) == "crypto:long"
    assert correlation_group("BTC/USDT", Side.SHORT) == "crypto:short"
    assert correlation_group("BTC/TRY", Side.LONG) == "crypto-try:long"
    assert correlation_group("THYAO.IS", Side.LONG) == "bist:long"
    assert correlation_group("AAPL", Side.LONG) == "us-equity:long"


# --- ticket model ------------------------------------------------------------------------


def test_ticket_rejects_unknown_market_and_coerces_side():
    assert make_ticket(side="short", market="futures").side is Side.SHORT
    with pytest.raises(ValueError):
        make_ticket(market="otc")


# --- check_ticket ------------------------------------------------------------------------


def test_clean_ticket_passes():
    assert check_ticket(make_ticket(), make_state(), LIMITS) == []


def test_missing_stop_is_rejected_without_follow_up_noise():
    assert codes(check_ticket(make_ticket(stop=None), make_state(), LIMITS)) == {"missing_stop"}


def test_nan_stop_counts_as_missing():
    assert codes(check_ticket(make_ticket(stop=NAN), make_state(), LIMITS)) == {"missing_stop"}


@pytest.mark.parametrize(
    "override",
    [{"entry": 0.0}, {"entry": NAN}, {"entry": -5.0}, {"stop": -1.0}, {"targets": [NAN]}, {"targets": [0.0]}],
)
def test_degenerate_prices_are_rejected(override):
    assert "invalid_price" in codes(check_ticket(make_ticket(**override), make_state(), LIMITS))


def test_stop_on_wrong_side_long():
    assert "stop_wrong_side" in codes(check_ticket(make_ticket(stop=60_500.0), make_state(), LIMITS))


def test_stop_on_wrong_side_short():
    ticket = futures_ticket(side=Side.SHORT, stop=59_400.0, targets=[58_000.0])
    assert codes(check_ticket(ticket, make_state(), LIMITS)) == {"stop_wrong_side"}


def test_stop_equal_to_entry_is_rejected():
    assert codes(check_ticket(make_ticket(stop=60_000.0), make_state(), LIMITS)) == {"stop_wrong_side"}


def test_short_on_spot_is_rejected():
    ticket = make_ticket(side=Side.SHORT, stop=60_600.0, targets=[58_500.0])
    assert codes(check_ticket(ticket, make_state(), LIMITS)) == {"short_not_supported"}


def test_short_on_futures_passes():
    ticket = futures_ticket(side=Side.SHORT, stop=60_600.0, targets=[58_500.0])
    assert check_ticket(ticket, make_state(), LIMITS) == []


def test_short_on_us_equity_is_left_to_the_broker():
    ticket = make_ticket(
        symbol="AAPL", market="equity", side=Side.SHORT, entry=200.0, stop=202.0, targets=[195.0], qty=25.0
    )
    assert check_ticket(ticket, make_state(), LIMITS) == []


def test_risk_above_max_is_rejected():
    qty = position_size(10_000, 1.5, 60_000, 59_400)
    assert codes(check_ticket(futures_ticket(qty=qty, leverage=2.0), make_state(), LIMITS)) == {"risk_too_high"}


def test_risk_exactly_at_max_passes():
    qty = position_size(10_000, 1.0, 60_000, 59_400, fee_rate=0.001)
    assert check_ticket(make_ticket(qty=qty, fee_rate=0.001, targets=[62_000.0]), make_state(), LIMITS) == []


def test_fees_count_towards_risk():
    # 0.95% without fees, above 1% once round-trip fees are added
    qty = position_size(10_000, 0.95, 60_000, 59_400)
    ticket = make_ticket(qty=qty, fee_rate=0.001)
    assert "risk_too_high" in codes(check_ticket(ticket, make_state(), LIMITS))


def test_daily_loss_limit_hit():
    state = make_state(equity=9_800.0, day_start_equity=10_000.0, week_start_equity=10_000.0)
    assert codes(check_ticket(make_ticket(), state, LIMITS)) == {"daily_loss_limit"}


def test_daily_loss_below_limit_passes():
    state = make_state(equity=9_850.0, day_start_equity=10_000.0, week_start_equity=10_000.0)
    assert check_ticket(make_ticket(), state, LIMITS) == []


def test_weekly_loss_limit_hit():
    state = make_state(equity=9_500.0, day_start_equity=9_500.0, week_start_equity=10_000.0)
    assert codes(check_ticket(make_ticket(), state, LIMITS)) == {"weekly_loss_limit"}


def test_total_open_risk_limit():
    state = make_state(open_risks=(OpenRisk("AAPL", Side.LONG, 280.0),))
    assert codes(check_ticket(make_ticket(), state, LIMITS)) == {"open_risk_limit"}


def test_correlated_crypto_longs_count_as_one_position():
    state = make_state(open_risks=(OpenRisk("ETH/USDT", Side.LONG, 60.0),))
    assert codes(check_ticket(make_ticket(), state, LIMITS)) == {"correlated_risk_limit"}


def test_correlated_group_ignores_opposite_direction_and_other_assets():
    state = make_state(
        open_risks=(
            OpenRisk("ETH/USDT", Side.SHORT, 60.0),
            OpenRisk("AAPL", Side.LONG, 60.0),
        )
    )
    assert check_ticket(make_ticket(), state, LIMITS) == []


def test_leverage_above_max():
    assert codes(check_ticket(futures_ticket(leverage=10.0), make_state(), LIMITS)) == {"leverage_too_high"}


@pytest.mark.parametrize("leverage", [0.5, 0.0, NAN])
def test_leverage_below_one_is_invalid(leverage):
    assert "invalid_leverage" in codes(check_ticket(futures_ticket(leverage=leverage), make_state(), LIMITS))


def test_leverage_on_spot_is_rejected():
    assert codes(check_ticket(make_ticket(leverage=3.0), make_state(), LIMITS)) == {"leverage_not_supported"}


def test_liquidation_closer_than_twice_the_stop_distance():
    # 5x, mmr 1%: liquidation at 48,600 -> 11,400 away; stop 6,000 away needs 12,000
    qty = position_size(10_000, 0.5, 60_000, 54_000)
    ticket = futures_ticket(stop=54_000.0, targets=[75_000.0], qty=qty, leverage=5.0)
    assert codes(check_ticket(ticket, make_state(), LIMITS)) == {"liquidation_too_close"}


def test_liquidation_far_enough_passes():
    assert check_ticket(futures_ticket(leverage=5.0), make_state(), LIMITS) == []


def test_liquidation_rule_only_applies_to_futures():
    # Same geometry as the failing futures case, but spot has no liquidation.
    qty = position_size(10_000, 0.5, 60_000, 54_000)
    ticket = make_ticket(stop=54_000.0, targets=[75_000.0], qty=qty)
    assert check_ticket(ticket, make_state(), LIMITS) == []


def test_symbol_not_in_allowlist():
    assert codes(check_ticket(make_ticket(symbol="DOGE/USDT"), make_state(), LIMITS)) == {"symbol_not_allowed"}


def test_empty_allowlist_rejects_everything():
    assert "symbol_not_allowed" in codes(check_ticket(make_ticket(), make_state(), RiskLimits()))


def test_rr_below_minimum():
    assert codes(check_ticket(make_ticket(targets=[60_900.0]), make_state(), LIMITS)) == {"rr_too_low"}


def test_missing_target():
    assert codes(check_ticket(make_ticket(targets=[]), make_state(), LIMITS)) == {"missing_target"}


@pytest.mark.parametrize("qty", [0.0, -1.0, NAN, INF])
def test_degenerate_quantity(qty):
    assert "invalid_qty" in codes(check_ticket(make_ticket(qty=qty), make_state(), LIMITS))


@pytest.mark.parametrize("equity", [0.0, -100.0, NAN])
def test_degenerate_equity(equity):
    assert "no_equity" in codes(check_ticket(make_ticket(), make_state(equity=equity), LIMITS))


def test_several_violations_are_all_reported():
    ticket = make_ticket(symbol="DOGE/USDT", leverage=10.0, targets=[60_100.0])
    found = codes(check_ticket(ticket, make_state(), LIMITS))
    assert {"symbol_not_allowed", "leverage_not_supported", "leverage_too_high", "rr_too_low"} <= found


def test_violation_text_is_the_message():
    (violation,) = check_ticket(make_ticket(stop=None), make_state(), LIMITS)
    assert str(violation) == violation.message
    assert "Stop" in violation.message


# --- adversarial review fixes ---------------------------------------------------------------

from tradeagent.risk import rr_after_fees  # noqa: E402


def test_rr_after_fees_matches_realised_r():
    # entry 100.65, stop 99.45025, T1 103.5, fee 0.1%: realised R at T1 is about 1.89
    ratio = rr_after_fees(100.65, 99.45025, 103.5, 0.001)
    assert ratio == pytest.approx((103.5 - 100.65 - 0.001 * (100.65 + 103.5)) / (100.65 - 99.45025 + 2 * 0.001 * 100.65))
    assert ratio < rr(100.65, 99.45025, 103.5)


def test_rr_check_uses_fees():
    # gross R:R 2.08, net of 0.1% fees below 2.0
    ticket = make_ticket(entry=100.0, stop=99.0, targets=[102.08], qty=5.0, fee_rate=0.001)
    assert "rr_too_low" in codes(check_ticket(ticket, make_state(), LIMITS))


def test_margin_above_equity_is_rejected():
    qty = 25_000 / 60_000  # notional 25,000 on 10,000 equity at 1x
    ticket = make_ticket(qty=qty, stop=59_940.0, targets=[60_300.0])
    assert "insufficient_margin" in codes(check_ticket(ticket, make_state(), LIMITS))


def test_futures_margin_uses_leverage():
    qty = 25_000 / 60_000
    ticket = futures_ticket(qty=qty, stop=59_940.0, targets=[60_300.0], leverage=5.0)
    assert "insufficient_margin" not in codes(check_ticket(ticket, make_state(), LIMITS))


def test_liquidation_on_the_wrong_side_of_entry_is_rejected():
    # mmr 0.3 at 5x: long liquidation 60,000 * (1 - 0.2 + 0.3) = 66,000, above the entry
    limits = RiskLimits(allowlist=LIMITS.allowlist, mmr=0.3)
    ticket = futures_ticket(leverage=5.0, stop=59_940.0, targets=[60_300.0], qty=50 / 60)
    assert "liquidation_too_close" in codes(check_ticket(ticket, make_state(), limits))


@pytest.mark.parametrize("fee", [NAN, -0.001, 0.5])
def test_invalid_fee_rate_is_rejected(fee):
    assert "invalid_fee" in codes(check_ticket(make_ticket(fee_rate=fee), make_state(), LIMITS))


@pytest.mark.parametrize(
    "override",
    [
        {"risk_pct": 6.0, "max_risk_pct": 6.0},
        {"max_leverage": float("inf")},
        {"max_leverage": 200.0},
        {"daily_loss_pct": 10.0, "weekly_loss_pct": 5.0},
        {"max_open_risk_pct": 0.5},
        {"min_rr": NAN},
        {"liq_stop_multiple": 50.0},
    ],
)
def test_risk_limits_are_range_checked(override):
    with pytest.raises(ValueError):
        RiskLimits(**override)


def test_position_size_errors_are_turkish():
    with pytest.raises(ValueError, match="farklı olmalı"):
        position_size(10_000, 1.0, 100.0, 100.0)
