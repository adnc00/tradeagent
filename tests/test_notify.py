import json
from datetime import UTC, datetime

import pytest

from tradeagent import notify
from tradeagent.journal import Stats
from tradeagent.models import OrderTicket

CREATED = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)


def long_ticket(**overrides) -> OrderTicket:
    values = dict(
        id="t-abc",
        setup_id="s-abc",
        symbol="BTC/USDT:USDT",
        side="long",
        entry=100.65,
        stop=99.45,
        targets=[104.0, 105.0],
        qty=40.0,
        leverage=2.0,
        risk_pct=0.5,
        fee_rate=0.001,
        market="futures",
        timeframe="15m",
        created_at=CREATED,
    )
    values.update(overrides)
    return OrderTicket(**values)


def stats(trades: int, expectancy: float = 0.4) -> Stats:
    return Stats(
        trades=trades,
        wins=0,
        losses=0,
        win_rate=0.5,
        avg_r=expectancy,
        expectancy=expectancy,
        profit_factor=1.5,
        max_drawdown_r=2.0,
        total_r=expectancy * trades,
    )


def alert_text(**kwargs) -> str:
    defaults = dict(
        equity=10_000.0,
        mmr=0.01,
        stats=stats(0),
        narrative="SSL süpürüldü, MSS + FVG",
        killzone="ny_am",
        valid_minutes=60,
        action="Onay için: tradeagent execute t-abc --live",
    )
    defaults.update(kwargs)
    alert = notify.ticket_alert(long_ticket(), **defaults)
    return alert.title + "\n" + alert.body


def test_alert_states_direction_levels_and_size():
    text = alert_text()
    assert "LONG" in text and "BTC/USDT:USDT" in text
    assert "100.65" in text and "99.45" in text and "104" in text
    assert "Miktar" in text and "40" in text
    assert "2x" in text


def test_alert_states_risk_amount_and_percent_of_equity():
    ticket = long_ticket()
    risk = 40 * (100.65 - 99.45) + 2 * 0.001 * 40 * 100.65
    text = alert_text()
    assert f"{risk:,.2f} USDT" in text
    assert f"%{risk / 10_000 * 100:.2f}" in text
    assert ticket.risk_pct == 0.5


def test_alert_states_gain_at_take_profit_net_of_fees():
    risk = 40 * (100.65 - 99.45) + 2 * 0.001 * 40 * 100.65
    reward = 40 * ((104.0 - 100.65) - 0.001 * (100.65 + 104.0))
    text = alert_text()
    assert f"{reward:,.2f} USDT" in text
    assert f"%{reward / 10_000 * 100:.2f}" in text
    assert f"{reward / risk:.2f}R" in text


def test_alert_states_break_even_win_rate():
    risk = 40 * (100.65 - 99.45) + 2 * 0.001 * 40 * 100.65
    reward = 40 * ((104.0 - 100.65) - 0.001 * (100.65 + 104.0))
    break_even = 1 / (1 + reward / risk) * 100
    assert f"%{break_even:.1f}" in alert_text()


def test_alert_has_no_expected_value_with_too_little_history():
    text = alert_text(stats=stats(12))
    assert "12/30" in text
    assert "beklenen" in text.lower()


def test_alert_shows_expected_value_from_journal_history():
    risk = 40 * (100.65 - 99.45) + 2 * 0.001 * 40 * 100.65
    text = alert_text(stats=stats(40, expectancy=0.4))
    assert "+0.40R" in text
    assert f"{0.4 * risk:,.2f} USDT" in text
    assert "40 işlem" in text


def test_alert_shows_validity_in_turkey_time_and_the_action():
    text = alert_text()
    assert "17:00" in text  # 13:00 UTC + 60 min = 14:00 UTC = 17:00 Istanbul
    assert "tradeagent execute t-abc --live" in text
    assert notify.DISCLAIMER in text


def test_alert_shows_liquidation_for_futures():
    assert "Likidasyon" in alert_text()


def test_short_alert_says_short():
    ticket = long_ticket(side="short", entry=100.0, stop=101.0, targets=[96.0])
    alert = notify.ticket_alert(
        ticket, equity=10_000, mmr=0.01, stats=stats(0), narrative="", killzone=None, valid_minutes=60, action=""
    )
    assert "SHORT" in alert.title


# --- channels ------------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_telegram_posts_title_and_body_to_the_chat():
    requests = []

    def opener(request, timeout):
        requests.append((request, timeout))
        return FakeResponse({"ok": True})

    notify.TelegramNotifier("123:SECRET", "42", opener=opener).send(notify.Alert("Başlık", "Gövde"))
    (request, timeout), = requests
    assert request.full_url == "https://api.telegram.org/bot123:SECRET/sendMessage"
    payload = json.loads(request.data)
    assert payload["chat_id"] == "42"
    assert payload["text"] == "Başlık\n\nGövde"
    assert timeout > 0


def test_telegram_error_never_reveals_the_token():
    def opener(request, timeout):
        raise OSError(f"connection failed for {request.full_url}")

    with pytest.raises(notify.NotifyError) as excinfo:
        notify.TelegramNotifier("123:SECRET", "42", opener=opener).send(notify.Alert("a", "b"))
    assert "SECRET" not in str(excinfo.value)


def test_telegram_reports_api_refusal():
    def opener(request, timeout):
        return FakeResponse({"ok": False, "description": "chat not found"})

    with pytest.raises(notify.NotifyError, match="chat not found"):
        notify.TelegramNotifier("1:X", "42", opener=opener).send(notify.Alert("a", "b"))


def test_telegram_long_message_is_cut_to_the_api_limit():
    sent = []

    def opener(request, timeout):
        sent.append(json.loads(request.data)["text"])
        return FakeResponse({"ok": True})

    notify.TelegramNotifier("1:X", "42", opener=opener).send(notify.Alert("a", "x" * 5000))
    assert len(sent[0]) <= 4096


def test_desktop_passes_text_through_environment_not_the_script():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))

        class Done:
            returncode = 0
            stderr = ""

        return Done()

    alert = notify.Alert("Başlık'; Remove-Item C:\\ #", "Gövde")
    notify.DesktopNotifier(runner=runner).send(alert)
    (argv, kwargs), = calls
    assert all("Remove-Item" not in part for part in argv)
    assert kwargs["env"]["TOAST_TITLE"] == alert.title
    assert kwargs["env"]["TOAST_BODY"] == "Gövde"


def test_notifiers_from_env_builds_configured_channels():
    env = {"TELEGRAM_BOT_TOKEN": "1:X", "TELEGRAM_CHAT_ID": "42"}
    names = [n.name for n in notify.notifiers_from_env(env, platform="win32")]
    assert names == ["konsol", "Telegram", "Windows bildirimi"]


def test_notifiers_from_env_skips_unconfigured_and_disabled_channels():
    names = [n.name for n in notify.notifiers_from_env({}, platform="win32", desktop=False)]
    assert names == ["konsol"]
    assert [n.name for n in notify.notifiers_from_env({}, platform="linux")] == ["konsol"]


def test_broadcast_returns_errors_and_still_uses_other_channels():
    delivered = []

    class Broken:
        name = "bozuk"

        def send(self, alert):
            raise notify.NotifyError("kapalı")

    class Working:
        name = "çalışan"

        def send(self, alert):
            delivered.append(alert)

    alert = notify.Alert("a", "b")
    sent, errors = notify.broadcast([Broken(), Working()], alert)
    assert delivered == [alert]
    assert sent == ["çalışan"]
    assert errors == ["bozuk: kapalı"]
