import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from tradeagent import cli, notify, panel
from tradeagent.config import load_config
from tradeagent.journal import Journal
from tests.test_cli import StubMarketData, write_config
from tests.test_watch import Inbox


@pytest.fixture
def cfg(tmp_path):
    return load_config(write_config(tmp_path))


# --- alert log ----------------------------------------------------------------------------------------


def test_file_notifier_appends_alerts_as_json_lines(tmp_path):
    log = tmp_path / "alerts.jsonl"
    channel = notify.FileNotifier(log)
    channel.send(notify.Alert("LONG X", "gövde"))
    channel.send(notify.Alert("SHORT Y", "gövde 2"))
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [r["title"] for r in rows] == ["LONG X", "SHORT Y"]
    assert all("at" in r for r in rows)


def test_file_notifier_keeps_the_log_bounded(tmp_path):
    log = tmp_path / "alerts.jsonl"
    channel = notify.FileNotifier(log, keep=5)
    for i in range(12):
        channel.send(notify.Alert(f"a{i}", ""))
    titles = [json.loads(line)["title"] for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(titles) <= 10
    assert titles[-1] == "a11"


# --- watch writes its heartbeat ------------------------------------------------------------------------


def test_watch_records_status_and_alerts_for_the_panel(tmp_path, monkeypatch):
    monkeypatch.setattr(StubMarketData, "price", None)
    monkeypatch.setattr(cli, "MarketData", StubMarketData)
    monkeypatch.setattr(cli.notify, "notifiers_from_env", lambda *a, **k: [Inbox()])
    path = write_config(tmp_path)
    assert cli.main(["--config", str(path), "watch", "--symbols", "BTC/USDT", "--once"]) == 0
    data_dir = load_config(path).data_dir
    status = json.loads((data_dir / "watch_status.json").read_text(encoding="utf-8"))
    assert status["cycles"] == 1
    assert status["last_error"] is None
    assert status["targets"] == "BTC/USDT"
    alerts = (data_dir / "alerts.jsonl").read_text(encoding="utf-8")
    assert "izleme başladı" in alerts
    assert "LONG BTC/USDT" in alerts


# --- state ----------------------------------------------------------------------------------------------


def _seed(cfg):
    journal = Journal(cfg.journal_path)
    try:
        conn = journal._conn
        now = datetime.now(UTC)
        rows = [
            ("t-open", "BTC/USDT:USDT", "long", "open", None, None, now - timedelta(minutes=30), None),
            ("t-win", "ETH/USDT:USDT", "short", "closed", 2.1, 105.0, now - timedelta(hours=5), now - timedelta(hours=3)),
            ("t-loss", "SOL/USDT:USDT", "long", "closed", -1.0, -50.0, now - timedelta(hours=9), now - timedelta(hours=8)),
            ("t-new", "XRP/USDT:USDT", "long", "new", None, None, now - timedelta(minutes=5), None),
        ]
        with conn:
            for tid, sym, side, status, r, pnl, created, closed in rows:
                conn.execute(
                    "INSERT INTO tickets (id, setup_id, created_at, symbol, timeframe, side, market, entry, stop, targets, qty, "
                    "leverage, risk_pct, fee_rate, risk_amount, rr, broker, mode, status, submitted_at, closed_at, result_r, pnl) "
                    "VALUES (?, ?, ?, ?, '15m', ?, 'futures', 100, 99, '[104]', 1, 1, 0.5, 0.001, 50, 2.4, 'paper', 'paper', ?, ?, ?, ?, ?)",
                    (tid, "s" + tid[1:], created.isoformat(), sym, side, status, created.isoformat(),
                     closed.isoformat() if closed else None, r, pnl),
                )
            for hours, equity in ((10, 10_000.0), (4, 9_950.0), (1, 10_055.0)):
                conn.execute(
                    "INSERT INTO equity_snapshots (ts, broker, equity) VALUES (?, 'paper', ?)",
                    ((now - timedelta(hours=hours)).isoformat(), equity),
                )
    finally:
        journal.close()


def test_state_summarises_journal(cfg):
    _seed(cfg)
    state = panel.build_state(cfg)
    assert [t["id"] for t in state["open_tickets"]] == ["t-open"]
    assert {t["id"] for t in state["recent_tickets"]} >= {"t-win", "t-loss", "t-new"}
    assert state["stats"]["trades"] == 2
    assert state["stats"]["total_r"] == pytest.approx(1.1)
    assert [p["r"] for p in state["r_curve"]] == pytest.approx([-1.0, 1.1])
    assert state["equity"][-1]["equity"] == 10_055.0
    assert state["watch"]["alive"] is False
    risk = state["risk"]
    assert risk["equity"] == 10_055.0
    assert risk["open_risk"] == pytest.approx(50.0)
    assert risk["open_risk_pct"] == pytest.approx(50.0 / 10_055.0 * 100)
    assert risk["max_open_risk_pct"] == 3.0
    assert risk["daily_loss_limit_pct"] == 2.0


def test_state_without_journal_is_empty_not_an_error(cfg):
    state = panel.build_state(cfg)
    assert state["open_tickets"] == [] and state["stats"]["trades"] == 0


def test_state_never_writes_to_the_journal(cfg):
    _seed(cfg)
    before = cfg.journal_path.read_bytes()
    panel.build_state(cfg)
    assert cfg.journal_path.read_bytes() == before


def test_watch_is_alive_when_heartbeat_is_recent(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "watch_status.json").write_text(
        json.dumps({"last_cycle_at": datetime.now(UTC).isoformat(), "interval_minutes": 15, "cycles": 3}),
        encoding="utf-8",
    )
    assert panel.build_state(cfg)["watch"]["alive"] is True


# --- http server ------------------------------------------------------------------------------------------


@pytest.fixture
def server(cfg):
    httpd = panel.make_server(cfg, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_server_serves_page_and_state(server):
    with urllib.request.urlopen(server + "/") as response:
        assert response.headers["Content-Type"].startswith("text/html")
        assert b"<html" in response.read().lower()
    with urllib.request.urlopen(server + "/api/state") as response:
        assert "stats" in json.loads(response.read())


def test_server_is_read_only(server):
    request = urllib.request.Request(server + "/api/state", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request)
    assert excinfo.value.code == 405


def test_server_rejects_foreign_host_header(server):
    request = urllib.request.Request(server + "/api/state", headers={"Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request)
    assert excinfo.value.code == 403


def test_server_unknown_path_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(server + "/../config.yaml")
    assert excinfo.value.code == 404


def test_server_binds_to_localhost_only(cfg):
    httpd = panel.make_server(cfg, port=0)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.server_close()
