import json
import re
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from tradeagent import cli, panel
from tradeagent.brokers.paper import PaperBroker
from tradeagent.config import load_config
from tradeagent.journal import Journal
from tests.synthetic import follow_through_candles
from tests.test_cli import StubMarketData, scanned_ticket_id, ticket_status, write_config


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.setattr(StubMarketData, "price", None)
    monkeypatch.setattr(cli, "MarketData", StubMarketData)
    return write_config(tmp_path)


@pytest.fixture
def server(config_path):
    httpd = panel.make_server(load_config(config_path), port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    with urllib.request.urlopen(base + "/") as response:
        page = response.read().decode("utf-8")
    token = re.search(r'name="panel-token" content="([^"]+)"', page).group(1)
    yield base, token
    httpd.shutdown()
    httpd.server_close()


def post(server, path, body, *, token=None, origin=None, content_type="application/json"):
    base, real_token = server
    headers = {"Content-Type": content_type, "Origin": origin or base, "X-Panel-Token": real_token if token is None else token}
    request = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload)
        except ValueError:
            return exc.code, {"raw": payload.decode("utf-8", "replace")}


# --- request guards -------------------------------------------------------------------------------------


def test_every_page_load_gets_a_secret_token(server):
    base, token = server
    assert len(token) >= 32


def test_action_without_the_token_is_refused(server, config_path):
    ticket_id = scanned_ticket_id(config_path)
    code, _ = post(server, "/api/execute", {"ticket_id": ticket_id, "confirm": "ONAY"}, token="wrong")
    assert code == 403
    assert ticket_status(config_path, ticket_id) == "new"


def test_action_from_another_origin_is_refused(server, config_path):
    ticket_id = scanned_ticket_id(config_path)
    code, _ = post(server, "/api/execute", {"ticket_id": ticket_id, "confirm": "ONAY"}, origin="https://evil.example")
    assert code == 403
    assert ticket_status(config_path, ticket_id) == "new"


def test_form_posts_are_refused(server, config_path):
    ticket_id = scanned_ticket_id(config_path)
    code, _ = post(server, "/api/execute", {"ticket_id": ticket_id, "confirm": "ONAY"}, content_type="text/plain")
    assert code == 415
    assert ticket_status(config_path, ticket_id) == "new"


def test_unknown_action_is_404(server):
    assert post(server, "/api/withdraw", {})[0] == 404


# --- approve -------------------------------------------------------------------------------------------------


def test_preview_shows_the_ticket_as_it_would_be_sent(server, config_path):
    ticket_id = scanned_ticket_id(config_path)
    code, body = post(server, "/api/preview", {"ticket_id": ticket_id})
    assert code == 200
    assert body["reasons"] == []
    assert body["ticket"]["id"] == ticket_id and body["ticket"]["side"] == "long"
    assert body["metrics"]["risk_pct"] == pytest.approx(0.5, abs=0.05)
    assert body["account"]["real_money"] is False
    assert ticket_status(config_path, ticket_id) == "new"


def test_execute_is_a_single_click(server, config_path):
    # The user decided (2026-11-26): the panel's Approve button sends; no typed word.
    ticket_id = scanned_ticket_id(config_path)
    code, body = post(server, "/api/execute", {"ticket_id": ticket_id})
    assert code == 200 and body["status"] == "sent"
    assert ticket_status(config_path, ticket_id) == "submitted"


def test_execute_with_onay_sends_the_bracket(server, config_path):
    ticket_id = scanned_ticket_id(config_path)
    code, body = post(server, "/api/execute", {"ticket_id": ticket_id, "confirm": "ONAY"})
    assert code == 200 and body["status"] == "sent"
    assert {o["role"] for o in body["orders"]} == {"entry", "stop", "take_profit"}
    assert ticket_status(config_path, ticket_id) == "submitted"
    journal = Journal(load_config(config_path).journal_path)
    try:
        assert "panel" in journal.get_ticket_notes(ticket_id)
    finally:
        journal.close()


def test_execute_runs_every_check_again(server, config_path, monkeypatch):
    ticket_id = scanned_ticket_id(config_path)
    monkeypatch.setattr(StubMarketData, "price", 99.0)  # through the stop: the setup is broken
    code, body = post(server, "/api/execute", {"ticket_id": ticket_id, "confirm": "ONAY"})
    assert code == 409 and any("stop" in r for r in body["reasons"])
    assert ticket_status(config_path, ticket_id) == "new"


def test_a_ticket_is_sent_only_once(server, config_path):
    ticket_id = scanned_ticket_id(config_path)
    assert post(server, "/api/execute", {"ticket_id": ticket_id, "confirm": "ONAY"})[0] == 200
    code, _ = post(server, "/api/execute", {"ticket_id": ticket_id, "confirm": "ONAY"})
    assert code == 409
    assert len(PaperBroker(load_config(config_path).paper_state_path).brackets) == 1


# --- dismiss, cancel, kill --------------------------------------------------------------------------------


def test_dismiss_retires_a_ticket_without_sending(server, config_path):
    ticket_id = scanned_ticket_id(config_path)
    code, _ = post(server, "/api/dismiss", {"ticket_id": ticket_id})
    assert code == 200
    assert ticket_status(config_path, ticket_id) == "canceled"
    assert PaperBroker(load_config(config_path).paper_state_path).brackets == []


def test_cancel_removes_an_unfilled_entry(server, config_path):
    ticket_id = scanned_ticket_id(config_path)
    post(server, "/api/execute", {"ticket_id": ticket_id, "confirm": "ONAY"})
    code, body = post(server, "/api/cancel", {"ticket_id": ticket_id})
    assert code == 200 and body["status"] == "canceled"
    assert ticket_status(config_path, ticket_id) == "canceled"


def test_kill_closes_everything_on_click(server, config_path):
    ticket_id = scanned_ticket_id(config_path)
    post(server, "/api/execute", {"ticket_id": ticket_id})
    broker = PaperBroker(load_config(config_path).paper_state_path)
    for candle in follow_through_candles()[:2]:
        broker.feed_candle("BTC/USDT", candle)
    assert len(broker.get_positions()) == 1
    code, body = post(server, "/api/kill", {})
    assert code == 200 and body["problems"] == []
    assert PaperBroker(load_config(config_path).paper_state_path).get_positions() == []


# --- what the panel offers --------------------------------------------------------------------------------


def test_state_marks_only_fresh_new_tickets_as_awaiting_approval(config_path):
    fresh = scanned_ticket_id(config_path)
    cfg = load_config(config_path)
    journal = Journal(cfg.journal_path)
    try:
        old = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        with journal._conn:
            journal._conn.execute(
                "INSERT INTO tickets (id, setup_id, created_at, symbol, side, market, entry, stop, targets, qty, leverage, "
                "risk_pct, fee_rate, status) VALUES ('t-old', 's-old', ?, 'ETH/USDT', 'long', 'spot', 10, 9, '[13]', 1, 1, 0.5, 0.001, 'new')",
                (old,),
            )
    finally:
        journal.close()
    state = panel.build_state(cfg)
    assert [t["id"] for t in state["awaiting"]] == [fresh]
    by_id = {t["id"]: t for t in state["recent_tickets"]}
    assert by_id["t-old"]["expired"] is True and by_id[fresh]["expired"] is False
    assert state["account"]["actions"] is True


def test_state_names_the_account_actions_go_to(tmp_path, monkeypatch):
    path = write_config(tmp_path, mode="live", extra="crypto:\n  exchange: binanceusdm\n  testnet: false\n")
    account = panel.build_state(load_config(path), live=True)["account"]
    assert account["real_money"] is True and account["live"] is True


def test_queue_shows_rr_after_fees(config_path):
    ticket_id = scanned_ticket_id(config_path)
    (ticket,) = panel.build_state(load_config(config_path))["awaiting"]
    assert ticket["id"] == ticket_id
    assert ticket["rr_net"] == pytest.approx(2.24, abs=0.01)  # the synthetic setup's R:R after 0.1% fees


def test_page_and_state_carry_the_same_version(server, config_path):
    import tradeagent

    base, _ = server
    with urllib.request.urlopen(base + "/") as response:
        page = response.read().decode("utf-8")
    assert f'name="panel-version" content="{tradeagent.__version__}"' in page
    assert panel.build_state(load_config(config_path))["version"] == tradeagent.__version__


# --- edit a ticket before approving it -----------------------------------------------------------------


def test_modify_changes_levels_risk_and_leverage(server, config_path):
    ticket_id = scanned_ticket_id(config_path)
    code, body = post(server, "/api/modify", {"ticket_id": ticket_id, "entry": 100.5, "stop": 99.2, "target": 104.5,
                                              "risk_pct": 0.3, "leverage": 2})
    assert code == 200, body
    t = body["ticket"]
    assert (t["entry"], t["stop"], t["targets"][0]) == (100.5, 99.2, 104.5)
    assert body["metrics"]["risk_pct"] == pytest.approx(0.3, abs=0.03)
    journal = Journal(load_config(config_path).journal_path)
    try:
        stored = journal.get_ticket(ticket_id)
        assert (stored.entry, stored.stop, stored.risk_pct) == (100.5, 99.2, 0.3)
    finally:
        journal.close()


def test_a_rescan_does_not_overwrite_the_users_edit(server, config_path):
    ticket_id = scanned_ticket_id(config_path)
    post(server, "/api/modify", {"ticket_id": ticket_id, "entry": 100.5, "stop": 99.2, "target": 104.5, "risk_pct": 0.3})
    assert cli.main(["--config", str(config_path), "scan", "--symbols", "BTC/USDT"]) == 0
    journal = Journal(load_config(config_path).journal_path)
    try:
        assert journal.get_ticket(ticket_id).entry == 100.5
    finally:
        journal.close()


@pytest.mark.parametrize("levels", [
    {"entry": 100.0, "stop": 101.0, "target": 104.0},   # stop above a long entry
    {"entry": 100.0, "stop": 99.0, "target": 98.0},     # target below a long entry
    {"entry": -1, "stop": 99.0, "target": 104.0},
    {"entry": "abc", "stop": 99.0, "target": 104.0},
])
def test_modify_refuses_inconsistent_levels(server, config_path, levels):
    ticket_id = scanned_ticket_id(config_path)
    code, body = post(server, "/api/modify", {"ticket_id": ticket_id, "risk_pct": 0.5, **levels})
    assert code == 400 and body["error"]


def test_modify_allows_risk_above_the_cap_but_warns(server, config_path):
    ticket_id = scanned_ticket_id(config_path)
    code, body = post(server, "/api/modify", {"ticket_id": ticket_id, "entry": 100.5, "stop": 99.2, "target": 104.5,
                                              "risk_pct": 3})
    assert code == 200 and body["reasons"] == []
    assert any("risk" in w.lower() for w in body["warnings"])


# --- open your own long/short --------------------------------------------------------------------------


def test_price_lookup_for_the_order_form(server):
    code, body = post(server, "/api/price", {"symbol": "BTC/USDT"})
    assert code == 200 and body["price"] > 0


def test_manual_trade_becomes_a_ticket_that_goes_through_approval(server, config_path):
    code, body = post(server, "/api/manual", {"symbol": "BTC/USDT", "side": "long", "entry": 100.9, "stop": 99.9,
                                              "target": 104.0, "risk_pct": 0.5, "leverage": 1})
    assert code == 200, body
    ticket_id = body["ticket"]["id"]
    assert ticket_id.startswith("t-m") and body["reasons"] == []
    assert ticket_status(config_path, ticket_id) == "new"
    assert post(server, "/api/execute", {"ticket_id": ticket_id, "confirm": "ONAY"})[0] == 200
    assert ticket_status(config_path, ticket_id) == "submitted"


def test_manual_short_on_spot_is_refused_by_the_risk_engine(server):
    code, body = post(server, "/api/manual", {"symbol": "BTC/USDT", "side": "short", "entry": 100.9, "stop": 101.9,
                                              "target": 97.0, "risk_pct": 0.5})
    assert code == 200 and any("spot" in r.lower() or "short" in r.lower() for r in body["reasons"])


def test_manual_trade_outside_the_allowlist_is_a_warning(server):
    code, body = post(server, "/api/manual", {"symbol": "DOGE/USDT", "side": "long", "entry": 1.0, "stop": 0.9,
                                              "target": 1.5, "risk_pct": 0.5})
    assert code == 200 and body["reasons"] == []
    assert any("izin" in w for w in body["warnings"])


def test_queue_says_why_a_ticket_was_not_sent_automatically(config_path):
    ticket_id = scanned_ticket_id(config_path)
    cfg = load_config(config_path)
    journal = Journal(cfg.journal_path)
    try:
        journal.add_ticket_note(ticket_id, "otomatik gönderim reddi: BTC/USDT için zaten açık pozisyon veya emir var")
    finally:
        journal.close()
    (ticket,) = panel.build_state(cfg)["awaiting"]
    assert "zaten açık pozisyon" in ticket["why"]
    assert not ticket["why"][:4].isdigit()  # no timestamp



# --- risk is shown, not imposed, on the user's own trades ------------------------------------------------


def test_user_trade_below_min_rr_is_a_warning_and_still_sends(server, config_path):
    code, body = post(server, "/api/manual", {"symbol": "BTC/USDT", "side": "long", "entry": 100.9, "stop": 99.9,
                                              "target": 101.5, "risk_pct": 0.5})
    assert code == 200 and body["reasons"] == []
    assert any("R:R" in w for w in body["warnings"])
    code, sent = post(server, "/api/execute", {"ticket_id": body["ticket"]["id"]})
    assert code == 200 and sent["status"] == "sent"


def test_a_broken_setup_is_still_blocked(server, config_path, monkeypatch):
    ticket_id = scanned_ticket_id(config_path)
    monkeypatch.setattr(StubMarketData, "price", 99.0)  # price already through the stop
    code, body = post(server, "/api/execute", {"ticket_id": ticket_id})
    assert code == 409 and any("stop" in r for r in body["reasons"])


def test_preview_explains_the_trade_for_a_beginner(server, config_path):
    ticket_id = scanned_ticket_id(config_path)
    code, body = post(server, "/api/preview", {"ticket_id": ticket_id})
    titles = [title for title, _ in body["explanation"]]
    assert any("Genel yön" in t for t in titles) and any("Stop" in t for t in titles)
