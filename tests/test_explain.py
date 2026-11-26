from datetime import UTC, datetime

from tradeagent import explain
from tradeagent.models import OrderTicket, Setup

AT = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)


def long_setup() -> Setup:
    ticket = OrderTicket(id="t-1", setup_id="s-1", symbol="ETH/USDT:USDT", side="long", entry=100.65, stop=99.45,
                         targets=[104.0, 105.0], qty=4.0, risk_pct=5.0, fee_rate=0.0005, market="futures", timeframe="15m")
    return Setup(
        id="s-1", symbol="ETH/USDT:USDT", timeframe="15m", side="long", detected_at=AT, bias="bullish",
        sweep_kind="swing_low", sweep_level=100.0, sweep_extreme=99.5, mss_at=AT, mss_level=100.7,
        fvg_top=100.95, fvg_bottom=100.35, entry=100.65, stop=99.45, targets=[104.0, 105.0],
        target_kinds=["swing_high", "PDH"], range_high=104.0, range_low=99.5, killzone="ny_am", ticket=ticket,
    )


def text(parts) -> str:
    return "\n".join(f"{title}: {body}" for title, body in parts)


def test_long_explanation_walks_through_the_idea_in_plain_words():
    setup = long_setup()
    out = text(explain.explain(setup.ticket, setup))
    for needle in ("yukarı", "100", "99,5", "100,7", "100,35", "100,95", "99,45", "104", "stop", "hedef", "R"):
        assert needle in out, needle
    assert "alış" in out.lower() or "long" in out.lower()
    assert "garanti" in out.lower()


def test_short_explanation_is_the_mirror():
    setup = long_setup()
    short_ticket = OrderTicket(id="t-2", setup_id="s-2", symbol="ETH/USDT:USDT", side="short", entry=100.0, stop=101.2,
                               targets=[96.0], qty=1.0, risk_pct=5.0, fee_rate=0.0005, market="futures")
    short = Setup(
        id="s-2", symbol="ETH/USDT:USDT", timeframe="5m", side="short", detected_at=AT, bias="bearish",
        sweep_kind="swing_high", sweep_level=101.0, sweep_extreme=101.1, mss_at=AT, mss_level=99.8, fvg_top=100.2,
        fvg_bottom=99.8, entry=100.0, stop=101.2, targets=[96.0], target_kinds=["swing_low"], range_high=101.1,
        range_low=96.0, killzone=None, ticket=short_ticket,
    )
    out = text(explain.explain(short_ticket, short)).lower()
    assert "aşağı" in out and "satış" in out


def test_hand_made_ticket_gets_a_general_explanation():
    ticket = OrderTicket(id="t-m1", setup_id="manual", symbol="BTC/USDT:USDT", side="long", entry=100.0, stop=98.0,
                         targets=[106.0], qty=1.0, risk_pct=5.0, fee_rate=0.0005, market="futures")
    out = text(explain.explain(ticket, None))
    assert "elle" in out.lower() and "98" in out and "106" in out


def test_rr_and_break_even_are_explained_with_numbers():
    setup = long_setup()
    out = text(explain.explain(setup.ticket, setup))
    assert "Başabaş" in out or "başabaş" in out
    assert "%" in out


def test_prices_are_rounded_for_reading():
    assert explain.tr(0.0034717999999999997) == "0,0034718"
    assert explain.tr(60097.53) == "60.097,53"
    assert explain.tr(1.5) == "1,5"
