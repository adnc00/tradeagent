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


@pytest.mark.parametrize("word", ["", "onay", "ONAY ", "evet"])
def test_execute_needs_the_exact_confirmation_word(server, config_path, word):
    ticket_id = scanned_ticket_id(config_path)
    code, body = post(server, "/api/execute", {"ticket_id": ticket_id, "confirm": word})
    assert code == 400 and "ONAY" in body["error"]
    assert ticket_status(config_path, ticket_id) == "new"
    assert PaperBroker(load_config(config_path).paper_state_path).brackets == []


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


def test_kill_needs_onay_and_then_closes_everything(server, config_path):
    ticket_id = scanned_ticket_id(config_path)
    post(server, "/api/execute", {"ticket_id": ticket_id, "confirm": "ONAY"})
    broker = PaperBroker(load_config(config_path).paper_state_path)
    for candle in follow_through_candles()[:2]:
        broker.feed_candle("BTC/USDT", candle)
    assert len(broker.get_positions()) == 1
    assert post(server, "/api/kill", {"confirm": "evet"})[0] == 400
    assert len(PaperBroker(load_config(config_path).paper_state_path).get_positions()) == 1
    code, body = post(server, "/api/kill", {"confirm": "ONAY"})
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
