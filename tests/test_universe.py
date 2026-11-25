import json

import pytest

from tradeagent import cli, notify, universe
from tradeagent.config import ConfigError, load_config
from tradeagent.models import AccountState, OrderTicket, RiskLimits
from tradeagent.risk import check_ticket, symbol_allowed
from tests.test_cli import StubMarketData, write_config
from tests.test_watch import Inbox


# --- allowlist patterns -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "symbol,allowed",
    [
        ("SOL/USDT:USDT", True),
        ("btc/usdt:usdt", True),
        ("SOL/USDT", False),
        ("SOL/USDC:USDC", False),
        ("AAPL", True),
        ("MSFT", False),
    ],
)
def test_allowlist_accepts_exact_symbols_and_wildcards(symbol, allowed):
    assert symbol_allowed(symbol, ("*/USDT:USDT", "AAPL")) is allowed


def test_risk_engine_uses_allowlist_patterns():
    ticket = OrderTicket(
        id="t-1", setup_id="s-1", symbol="SOL/USDT:USDT", side="long", entry=100.0, stop=99.0,
        targets=[104.0], qty=1.0, risk_pct=0.5, fee_rate=0.001, market="futures",
    )
    state = AccountState(equity=10_000.0)
    codes = {v.code for v in check_ticket(ticket, state, RiskLimits(allowlist=("*/USDT:USDT",)))}
    assert "symbol_not_allowed" not in codes
    codes = {v.code for v in check_ticket(ticket, state, RiskLimits(allowlist=("BTC/USDT:USDT",)))}
    assert "symbol_not_allowed" in codes


# --- universe -------------------------------------------------------------------------------------------


class FakeExchange:
    def __init__(self):
        self.markets = {
            "BTC/USDT:USDT": dict(symbol="BTC/USDT:USDT", base="BTC", quote="USDT", settle="USDT", swap=True, linear=True, active=True, spot=False),
            "ETH/USDT:USDT": dict(symbol="ETH/USDT:USDT", base="ETH", quote="USDT", settle="USDT", swap=True, linear=True, active=True, spot=False),
            "DOGE/USDT:USDT": dict(symbol="DOGE/USDT:USDT", base="DOGE", quote="USDT", settle="USDT", swap=True, linear=True, active=True, spot=False),
            "OLD/USDT:USDT": dict(symbol="OLD/USDT:USDT", base="OLD", quote="USDT", settle="USDT", swap=True, linear=True, active=False, spot=False),
            "BTC/USDC:USDC": dict(symbol="BTC/USDC:USDC", base="BTC", quote="USDC", settle="USDC", swap=True, linear=True, active=True, spot=False),
            "USDC/USDT:USDT": dict(symbol="USDC/USDT:USDT", base="USDC", quote="USDT", settle="USDT", swap=True, linear=True, active=True, spot=False),
            "BTC/USDT:USDT-261225": dict(symbol="BTC/USDT:USDT-261225", base="BTC", quote="USDT", settle="USDT", swap=False, future=True, linear=True, active=True, spot=False),
            "SOL/USDT": dict(symbol="SOL/USDT", base="SOL", quote="USDT", settle=None, swap=False, linear=None, active=True, spot=True),
        }
        self.volumes = {"BTC/USDT:USDT": 9e9, "ETH/USDT:USDT": 5e9, "DOGE/USDT:USDT": 5e6, "USDC/USDT:USDT": 8e9, "SOL/USDT": 1e9}

    def load_markets(self):
        return self.markets

    def fetch_tickers(self, symbols=None):
        return {s: {"symbol": s, "quoteVolume": self.volumes.get(s)} for s in (symbols or self.markets)}


def test_universe_lists_active_usdt_perpetuals_by_volume():
    symbols = universe.top_symbols(FakeExchange(), derivatives=True, quote="USDT", top=0, min_quote_volume=0)
    assert symbols == ["BTC/USDT:USDT", "ETH/USDT:USDT", "DOGE/USDT:USDT"]


def test_universe_applies_top_min_volume_and_exclude():
    exchange = FakeExchange()
    assert universe.top_symbols(exchange, derivatives=True, quote="USDT", top=1, min_quote_volume=0) == ["BTC/USDT:USDT"]
    assert universe.top_symbols(exchange, derivatives=True, quote="USDT", top=0, min_quote_volume=1e7) == [
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
    ]
    assert universe.top_symbols(
        exchange, derivatives=True, quote="USDT", top=0, min_quote_volume=0, exclude=("ETH/USDT:USDT", "DOGE")
    ) == ["BTC/USDT:USDT"]


def test_universe_spot_mode_lists_spot_pairs():
    assert universe.top_symbols(FakeExchange(), derivatives=False, quote="USDT", top=0, min_quote_volume=0) == ["SOL/USDT"]


def test_universe_config_section(tmp_path):
    cfg = load_config(write_config(tmp_path, extra="universe:\n  top: 30\n  min_quote_volume: 5000000\n  exclude: [PEPE]\n"))
    assert (cfg.universe.top, cfg.universe.min_quote_volume, cfg.universe.exclude) == (30, 5_000_000.0, ("PEPE",))
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, extra="universe:\n  top: -1\n"))


@pytest.fixture
def stub_env(tmp_path, monkeypatch):
    monkeypatch.setattr(StubMarketData, "price", None)
    monkeypatch.setattr(cli, "MarketData", StubMarketData)
    box = Inbox()
    monkeypatch.setattr(cli.notify, "notifiers_from_env", lambda *a, **k: [box])
    return box


def test_watch_universe_scans_every_listed_symbol(tmp_path, monkeypatch, stub_env):
    seen = {}

    def fake_top(client, **kwargs):
        seen.update(kwargs)
        return ["BTC/USDT", "NOTALLOWED/USDT"]

    monkeypatch.setattr(cli.universe, "top_symbols", fake_top)
    monkeypatch.setattr(StubMarketData, "ccxt_client", lambda self: object(), raising=False)
    path = write_config(tmp_path, extra="universe:\n  top: 7\n")
    assert cli.main(["--config", str(path), "watch", "--universe", "--once"]) == 0
    assert seen["top"] == 7
    assert [a for a in stub_env.alerts if a.title.startswith("LONG BTC/USDT")]
    assert not [a for a in stub_env.alerts if "NOTALLOWED" in a.title]


def test_watch_needs_symbols_or_universe(tmp_path, stub_env):
    with pytest.raises(SystemExit):
        cli.main(["--config", str(write_config(tmp_path)), "watch", "--once"])


# --- telegram helpers ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_telegram_chats_lists_ids_from_updates():
    updates = {
        "ok": True,
        "result": [
            {"message": {"chat": {"id": 42, "first_name": "Ada", "type": "private"}}},
            {"message": {"chat": {"id": 42, "first_name": "Ada", "type": "private"}}},
            {"channel_post": {"chat": {"id": -100, "title": "Sinyaller", "type": "channel"}}},
        ],
    }
    chats = notify.telegram_chats("1:SECRET", opener=lambda request, timeout: FakeResponse(updates))
    assert chats == [("42", "Ada (private)"), ("-100", "Sinyaller (channel)")]


def test_telegram_chats_error_hides_token():
    def opener(request, timeout):
        raise OSError(request.full_url)

    with pytest.raises(notify.NotifyError) as excinfo:
        notify.telegram_chats("1:SECRET", opener=opener)
    assert "SECRET" not in str(excinfo.value)


# --- check / notify-test --------------------------------------------------------------------------------


def test_check_reports_setup_without_revealing_secrets(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path)
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=1:TOPSECRET\nTELEGRAM_CHAT_ID=42\n", encoding="utf-8")
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["--config", str(path), "check"]) == 0
    out = capsys.readouterr().out
    assert "TOPSECRET" not in out
    assert "Telegram" in out and "tanımlı" in out
    assert "paper" in out


def test_notify_test_sends_a_sample_alert(tmp_path, stub_env):
    assert cli.main(["--config", str(write_config(tmp_path)), "notify-test"]) == 0
    assert any("test" in a.title.lower() for a in stub_env.alerts)
