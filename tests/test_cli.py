import argparse
import builtins
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

import tradeagent.brokers.alpaca_broker as alpaca_module
import tradeagent.brokers.ccxt_broker as ccxt_module
from tradeagent import cli
from tradeagent.brokers.base import BrokerError
from tradeagent.brokers.paper import PaperBroker
from tradeagent.config import load_config
from tradeagent.ict import NEW_YORK
from tradeagent.journal import Journal
from tests.synthetic import follow_through_candles, long_setup_frames

PACKAGE = Path(cli.__file__).resolve().parent


def fresh_end() -> pd.Timestamp:
    """Open time of the last fully closed 15m candle."""
    return pd.Timestamp.now(tz="UTC").floor("15min") - pd.Timedelta(minutes=15)


class StubMarketData:
    """Serves the synthetic long scenario (ending at the last closed candle); no network.

    ``price`` overrides the current price returned by ``get_price``.
    """

    price: float | None = None

    def __init__(self, crypto_exchange: str = "binance", **_: object) -> None:
        self.ltf, self.daily = long_setup_frames(end=fresh_end())

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 500):
        return self.daily if timeframe == "1d" else self.ltf

    def get_price(self, symbol: str) -> float:
        return StubMarketData.price if StubMarketData.price is not None else float(self.ltf["close"].iloc[-1])


def write_config(tmp_path: Path, *, mode: str = "paper", fee_rate: float = 0.001, extra: str = "") -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
mode: {mode}
data_dir: {(tmp_path / "data").as_posix()}
fee_rate: {fee_rate}
risk:
  allowlist: [BTC/USDT, THYAO.IS, AAPL]
paper:
  equity: 10000
  sync: false
{extra}
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.setattr(StubMarketData, "price", None)
    monkeypatch.setattr(cli, "MarketData", StubMarketData)
    for name in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "ALPACA_API_KEY", "ALPACA_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    return write_config(tmp_path)


def answer(monkeypatch, *replies: str) -> list[str]:
    """Feed replies to input(); returns the list of prompts that were shown."""
    prompts: list[str] = []
    queue = list(replies)

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr(builtins, "input", fake_input)
    return prompts


def scanned_ticket_id(config_path: Path) -> str:
    assert cli.main(["--config", str(config_path), "scan", "--symbols", "BTC/USDT"]) == 0
    journal = Journal(load_config(config_path).journal_path)
    try:
        (row,) = journal.latest_tickets()
        return row["id"]
    finally:
        journal.close()


def ticket_status(config_path: Path, ticket_id: str) -> str:
    journal = Journal(load_config(config_path).journal_path)
    try:
        return journal.get_ticket(ticket_id).status
    finally:
        journal.close()


# --- live gate -------------------------------------------------------------------------------------


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self


@pytest.mark.parametrize("mode,flag", [("paper", False), ("paper", True), ("live", False)])
def test_paper_broker_unless_live_mode_and_flag(tmp_path, monkeypatch, mode, flag):
    monkeypatch.setenv("BINANCE_API_KEY", "dummy-key-not-real")
    monkeypatch.setenv("BINANCE_API_SECRET", "dummy-secret-not-real")
    ccxt_recorder, alpaca_recorder = Recorder(), Recorder()
    monkeypatch.setattr(ccxt_module, "CcxtBroker", ccxt_recorder)
    monkeypatch.setattr(alpaca_module, "AlpacaBroker", alpaca_recorder)
    cfg = load_config(write_config(tmp_path, mode=mode))
    broker = cli.make_broker(cfg, "BTC/USDT", live_flag=flag)
    assert isinstance(broker, PaperBroker)
    assert ccxt_recorder.calls == [] and alpaca_recorder.calls == []


def test_live_broker_needs_live_mode_and_flag(tmp_path, monkeypatch):
    ccxt_recorder, alpaca_recorder = Recorder(), Recorder()
    monkeypatch.setattr(ccxt_module, "CcxtBroker", ccxt_recorder)
    monkeypatch.setattr(alpaca_module, "AlpacaBroker", alpaca_recorder)
    cfg = load_config(write_config(tmp_path, mode="live"))
    assert cli.make_broker(cfg, "BTC/USDT", live_flag=True) is ccxt_recorder
    assert ccxt_recorder.calls[0][0] == ("binance",)
    assert ccxt_recorder.calls[0][1]["testnet"] is True
    assert cli.make_broker(cfg, "AAPL", live_flag=True) is alpaca_recorder
    assert alpaca_recorder.calls[0][1] == {"paper": True}


def test_bist_has_no_live_route(tmp_path):
    cfg = load_config(write_config(tmp_path, mode="live"))
    with pytest.raises(BrokerError) as info:
        cli.make_broker(cfg, "THYAO.IS", live_flag=True)
    assert "BIST" in str(info.value)


# --- execute -----------------------------------------------------------------------------------------

REQUIRED_TICKET_FIELDS = [
    "Sembol",
    "Yön",
    "Miktar",
    "Pozisyon değeri",
    "Giriş",
    "Stop",
    "Hedefler",
    "Risk",
    "R:R",
    "Kaldıraç",
    "Likidasyon",
    "Broker",
    "Mod",
]


def test_execute_prints_full_ticket_and_sends_only_on_onay(config_path, monkeypatch, capsys):
    ticket_id = scanned_ticket_id(config_path)
    capsys.readouterr()
    prompts = answer(monkeypatch, "ONAY")
    assert cli.main(["--config", str(config_path), "execute", ticket_id]) == 0
    out = capsys.readouterr().out
    for field in REQUIRED_TICKET_FIELDS:
        assert field in out, field
    for value in ("BTC/USDT", "LONG", "100.65", "99.45025", "104", "105", "%0.50", "2.24", "PAPER"):
        assert value in out, value
    assert len(prompts) == 1 and "ONAY" in prompts[0]
    assert ticket_status(config_path, ticket_id) == "submitted"
    (bracket,) = PaperBroker(load_config(config_path).paper_state_path).brackets
    assert (bracket.ticket_id, bracket.status) == (ticket_id, "pending")


@pytest.mark.parametrize("reply", ["", "onay", "Onay", " ONAY", "ONAY ", "ONAY\t", "EVET", "yes", "y", "ONAYY", "OK"])
def test_execute_aborts_on_anything_but_exact_onay(config_path, monkeypatch, capsys, reply):
    ticket_id = scanned_ticket_id(config_path)
    answer(monkeypatch, reply)
    assert cli.main(["--config", str(config_path), "execute", ticket_id]) == 1
    assert "GÖNDERİLMEDİ" in capsys.readouterr().out
    assert ticket_status(config_path, ticket_id) == "new"
    assert PaperBroker(load_config(config_path).paper_state_path).brackets == []


def test_execute_aborts_on_end_of_input(config_path, monkeypatch):
    ticket_id = scanned_ticket_id(config_path)
    answer(monkeypatch)  # no replies -> EOFError
    assert cli.main(["--config", str(config_path), "execute", ticket_id]) == 1
    assert ticket_status(config_path, ticket_id) == "new"


def test_execute_live_flag_without_live_mode_stays_paper(config_path, monkeypatch, capsys):
    ticket_id = scanned_ticket_id(config_path)
    answer(monkeypatch, "ONAY")
    assert cli.main(["--config", str(config_path), "execute", ticket_id, "--live"]) == 0
    out = capsys.readouterr().out
    assert "paper kullanılıyor" in out
    assert "Mod             : PAPER" in out


def test_execute_refuses_already_submitted_ticket_without_prompt(config_path, monkeypatch):
    ticket_id = scanned_ticket_id(config_path)
    answer(monkeypatch, "ONAY")
    assert cli.main(["--config", str(config_path), "execute", ticket_id]) == 0
    prompts = answer(monkeypatch, "ONAY")
    assert cli.main(["--config", str(config_path), "execute", ticket_id]) == 1
    assert prompts == []


def test_risk_rejection_never_reaches_the_prompt(config_path, monkeypatch, capsys):
    ticket_id = scanned_ticket_id(config_path)
    journal = Journal(load_config(config_path).journal_path)
    new_york_midnight = datetime.now(NEW_YORK).replace(hour=0, minute=0, second=0, microsecond=0)
    journal.equity_baselines("paper", 10_500.0, new_york_midnight)  # equity is down 4.8% since the day began
    journal.close()
    prompts = answer(monkeypatch, "ONAY")
    assert cli.main(["--config", str(config_path), "execute", ticket_id]) == 2
    assert prompts == []
    assert "Günlük kayıp limiti" in capsys.readouterr().out
    assert ticket_status(config_path, ticket_id) == "new"


def test_unknown_ticket(config_path, monkeypatch):
    prompts = answer(monkeypatch, "ONAY")
    assert cli.main(["--config", str(config_path), "execute", "t-doesnotexist"]) == 1
    assert prompts == []


# --- kill ---------------------------------------------------------------------------------------------


def _open_paper_position(config_path: Path, monkeypatch) -> str:
    ticket_id = scanned_ticket_id(config_path)
    answer(monkeypatch, "ONAY")
    assert cli.main(["--config", str(config_path), "execute", ticket_id]) == 0
    broker = PaperBroker(load_config(config_path).paper_state_path)
    for candle in follow_through_candles()[:2]:
        broker.feed_candle("BTC/USDT", candle)
    assert len(broker.get_positions()) == 1
    return ticket_id


@pytest.mark.parametrize("reply", ["", "evet", "onay", "ONAY "])
def test_kill_requires_onay(config_path, monkeypatch, reply):
    _open_paper_position(config_path, monkeypatch)
    prompts = answer(monkeypatch, reply)
    assert cli.main(["--config", str(config_path), "kill"]) == 1
    assert len(prompts) == 1
    assert len(PaperBroker(load_config(config_path).paper_state_path).get_positions()) == 1


def test_kill_with_onay_closes_everything_and_journals_it(config_path, monkeypatch, capsys):
    ticket_id = _open_paper_position(config_path, monkeypatch)
    answer(monkeypatch, "ONAY")
    assert cli.main(["--config", str(config_path), "kill"]) == 0
    assert PaperBroker(load_config(config_path).paper_state_path).get_positions() == []
    assert ticket_status(config_path, ticket_id) == "closed"
    assert "kapatılan pozisyon: 1" in capsys.readouterr().out


# --- there is no way around the confirmation ------------------------------------------------------


def _options(parser: argparse.ArgumentParser) -> set[str]:
    return {opt for action in parser._actions for opt in action.option_strings}


def test_parser_offers_no_confirmation_bypass():
    parser = cli.build_parser()
    assert _options(parser) == {"-h", "--help", "--config"}
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert _options(subparsers.choices["execute"]) == {"-h", "--help", "--live"}
    assert _options(subparsers.choices["kill"]) == {"-h", "--help", "--live"}
    every_option = set().union(*(_options(p) for p in subparsers.choices.values()))
    assert every_option <= {
        "-h", "--help", "--live", "--symbols", "--tf", "--htf", "--limit",
        "--equity", "--risk", "--entry", "--stop", "--leverage", "--side",
        "--interval", "--once", "--no-desktop", "--demo-auto", "--universe", "--top", "--port", "--no-browser",
    }
    # The only unattended path is watch --demo-auto, and it is demo-only (tests/test_watch.py).
    assert {name for name, p in subparsers.choices.items() if "--demo-auto" in _options(p)} == {"watch"}


FORBIDDEN_PATTERNS = [
    r"--(?:yes|y|force|no-?confirm|skip|auto|assume-yes|batch|non-?interactive)\b",
    r"(?i)\b(?:skip|bypass|auto|no|assume)[-_]?(?:confirm|confirmation|onay)\b",
    r"(?i)\byes[-_]?to[-_]?all\b",
    r"(?i)\bnon[-_]?interactive\b",
    r"TRADEAGENT_[A-Z_]+",
]


# The single sanctioned unattended path: ``watch --demo-auto`` (demo/paper accounts only; see
# auto_problems and submit_unattended, both tested in tests/test_watch.py). Nothing else may exist.
SANCTIONED = "--demo-auto"


def test_source_contains_no_bypass_switch():
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text(encoding="utf-8").replace(SANCTIONED, "")
        for pattern in FORBIDDEN_PATTERNS:
            offenders += [f"{path.name}: {m}" for m in re.findall(pattern, text)]
    assert offenders == []


def test_environment_is_only_read_for_api_keys():
    readers = {}
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"os\.environ|getenv\(", text):
            readers[path.relative_to(PACKAGE).as_posix()] = text
    assert set(readers) == {"config.py", "brokers/ccxt_broker.py", "brokers/alpaca_broker.py", "notify.py"}
    assert "getenv(" not in "".join(readers.values())
    # notify.py reads only the Telegram credentials (and passes the environment on to the toast process).
    notify_text = readers["notify.py"]
    assert set(re.findall(r'source\.get\("([A-Z0-9_]+)"', notify_text)) == {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
    assert not re.search(r"os\.environ\[|os\.environ\.get", notify_text)


def test_input_is_read_in_exactly_one_place():
    counts = {p.relative_to(PACKAGE).as_posix(): p.read_text(encoding="utf-8").count("input(") for p in PACKAGE.rglob("*.py")}
    assert {k: v for k, v in counts.items() if v} == {"cli.py": 1}
    assert cli.CONFIRM_WORD == "ONAY"


# --- other commands ---------------------------------------------------------------------------------


def test_scan_and_plan(config_path, monkeypatch, capsys):
    assert cli.main(["--config", str(config_path), "scan", "--symbols", "BTC/USDT,THYAO.IS", "--tf", "15m", "--htf", "1d"]) == 0
    out = capsys.readouterr().out
    assert "BTC/USDT" in out and "THYAO.IS" in out and "t-" in out
    setup_id = re.search(r"\b(s-[0-9a-f]{8})\b", out).group(1)
    assert cli.main(["--config", str(config_path), "plan", setup_id]) == 0
    plan = capsys.readouterr().out
    assert "100.35" in plan and "100.95" in plan and "sezgisel" in plan


def test_plan_unknown_setup(config_path):
    assert cli.main(["--config", str(config_path), "plan", "s-nothere0"]) == 1


def test_positions_and_stats_after_a_winning_paper_trade(config_path, monkeypatch, capsys):
    ticket_id = scanned_ticket_id(config_path)
    answer(monkeypatch, "ONAY")
    assert cli.main(["--config", str(config_path), "execute", ticket_id]) == 0
    broker = PaperBroker(load_config(config_path).paper_state_path)
    for candle in follow_through_candles():
        broker.feed_candle("BTC/USDT", candle)
    capsys.readouterr()
    assert cli.main(["--config", str(config_path), "positions"]) == 0
    assert "Açık pozisyon yok" in capsys.readouterr().out
    assert cli.main(["--config", str(config_path), "stats"]) == 0
    stats = capsys.readouterr().out
    assert "İşlem sayısı      : 1" in stats and "%100.0" in stats
    assert ticket_status(config_path, ticket_id) == "closed"


def test_risk_calc_reference_example(tmp_path, capsys):
    config = write_config(tmp_path, fee_rate=0.0)
    argv = ["--config", str(config), "risk-calc", "--equity", "10000", "--risk", "1", "--entry", "60000", "--stop", "59400"]
    assert cli.main(argv + ["--leverage", "5", "--side", "long"]) == 0
    out = capsys.readouterr().out
    assert "0.16666666666666666" in out and "10,000.00" in out and "2,000.00" in out
    assert "48,600" in out  # 60000 * (1 - 1/5 + 0.01)


def test_risk_calc_rejects_inconsistent_side_and_degenerate_input(tmp_path, capsys):
    config = write_config(tmp_path)
    base = ["--config", str(config), "risk-calc", "--equity", "10000", "--risk", "1", "--entry", "100"]
    assert cli.main(base + ["--stop", "98", "--side", "short"]) == 2
    assert cli.main(base + ["--stop", "100"]) == 2


class BrokenSymbolMarketData(StubMarketData):
    """Serves non-positive prices for ETH/USDT only."""

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 500):
        frame = super().get_ohlcv(symbol, timeframe, limit)
        return frame * -1 if symbol == "ETH/USDT" else frame


def test_scan_skips_a_symbol_with_bad_data(config_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "MarketData", BrokenSymbolMarketData)
    assert cli.main(["--config", str(config_path), "scan", "--symbols", "ETH/USDT,BTC/USDT"]) == 0
    out = capsys.readouterr().out
    assert "ETH/USDT 15m: veri reddedildi" in out
    assert "BTC/USDT 15m: 1 kurulum" in out


# --- adversarial review fixes ---------------------------------------------------------------

import sqlite3  # noqa: E402
from datetime import timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from tradeagent.brokers.base import Balance, Broker, OrderResult, OrderStateUnknown, Position  # noqa: E402
from tradeagent.models import Side  # noqa: E402


def as_tty(monkeypatch, is_tty: bool) -> None:
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: is_tty))


def live_setup(tmp_path, monkeypatch):
    """Live-mode config whose 'live' broker is a local PaperBroker (no venue is ever contacted)."""
    config = write_config(tmp_path, mode="live")
    ticket_id = scanned_ticket_id(config)
    broker = PaperBroker(tmp_path / "fake-live.json")
    monkeypatch.setattr(cli, "make_broker", lambda cfg, symbol, live_flag: broker)
    return config, ticket_id, broker


def test_live_confirmation_refuses_non_tty_stdin(config_path, tmp_path, monkeypatch, capsys):
    config, ticket_id, broker = live_setup(tmp_path, monkeypatch)
    as_tty(monkeypatch, False)
    prompts = answer(monkeypatch, "ONAY")
    assert cli.main(["--config", str(config), "execute", ticket_id, "--live"]) == 1
    assert prompts == [] and broker.brackets == []
    assert "terminal" in capsys.readouterr().out


def test_live_confirmation_works_on_a_tty(config_path, tmp_path, monkeypatch):
    config, ticket_id, broker = live_setup(tmp_path, monkeypatch)
    as_tty(monkeypatch, True)
    answer(monkeypatch, "ONAY")
    assert cli.main(["--config", str(config), "execute", ticket_id, "--live"]) == 0
    assert len(broker.brackets) == 1 and ticket_status(config, ticket_id) == "submitted"


@pytest.mark.parametrize("price,needle", [(100.65 * 0.99, "sapma"), (99.0, "stop"), (104.5, "hedef"), (None, "eski")])
def test_stale_tickets_are_refused_before_the_prompt(config_path, monkeypatch, capsys, price, needle):
    ticket_id = scanned_ticket_id(config_path)
    if price is None:
        journal = Journal(load_config(config_path).journal_path)
        old = (pd.Timestamp.now(tz="UTC") - timedelta(hours=3)).isoformat()
        journal._conn.execute("UPDATE tickets SET created_at = ? WHERE id = ?", (old, ticket_id))
        journal._conn.commit()
        journal.close()
    else:
        monkeypatch.setattr(StubMarketData, "price", price)
    prompts = answer(monkeypatch, "ONAY")
    assert cli.main(["--config", str(config_path), "execute", ticket_id]) == 2
    assert prompts == [] and needle in capsys.readouterr().out
    assert ticket_status(config_path, ticket_id) == "new"


def test_guard_runs_again_right_after_onay(config_path, monkeypatch, capsys):
    ticket_id = scanned_ticket_id(config_path)

    def move_then_confirm(prompt=""):
        StubMarketData.price = 100.65 * 0.99
        return "ONAY"

    monkeypatch.setattr(builtins, "input", move_then_confirm)
    assert cli.main(["--config", str(config_path), "execute", ticket_id]) == 2
    assert "onaydan sonra" in capsys.readouterr().out.lower()
    assert ticket_status(config_path, ticket_id) == "new"
    assert PaperBroker(load_config(config_path).paper_state_path).brackets == []


def test_concurrent_submission_is_refused_atomically(config_path, monkeypatch):
    ticket_id = scanned_ticket_id(config_path)

    def other_process_wins(prompt=""):
        other = Journal(load_config(config_path).journal_path)
        assert other.claim_for_submission(ticket_id, broker="paper", mode="paper")
        other.close()
        return "ONAY"

    monkeypatch.setattr(builtins, "input", other_process_wins)
    assert cli.main(["--config", str(config_path), "execute", ticket_id]) == 1
    assert PaperBroker(load_config(config_path).paper_state_path).brackets == []


class ScriptedBroker(PaperBroker):
    """PaperBroker whose place_bracket raises a scripted error."""

    error: BaseException | None = None

    def place_bracket(self, ticket):
        if self.error is not None:
            raise self.error
        return super().place_bracket(ticket)


@pytest.mark.parametrize(
    "error,status,code",
    [(OrderStateUnknown("zaman aşımı"), "unknown", 3), (BrokerError("reddedildi"), "new", 1)],
)
def test_broker_outcomes_set_the_right_ticket_status(config_path, tmp_path, monkeypatch, capsys, error, status, code):
    ticket_id = scanned_ticket_id(config_path)
    broker = ScriptedBroker(tmp_path / "scripted.json")
    broker.error = error
    monkeypatch.setattr(cli, "make_broker", lambda cfg, symbol, live_flag: broker)
    answer(monkeypatch, "ONAY")
    assert cli.main(["--config", str(config_path), "execute", ticket_id]) == code
    assert ticket_status(config_path, ticket_id) == status
    if status == "unknown":
        assert "DİKKAT" in capsys.readouterr().out


def test_journal_failure_after_sending_never_leaves_the_ticket_new(config_path, monkeypatch, capsys):
    ticket_id = scanned_ticket_id(config_path)

    def broken(self, *args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Journal, "mark_submitted", broken)
    answer(monkeypatch, "ONAY")
    assert cli.main(["--config", str(config_path), "execute", ticket_id]) == 3
    assert ticket_status(config_path, ticket_id) == "submitting"
    assert "journal" in capsys.readouterr().out.lower()


class KillBroker(Broker):
    """Records the order of kill operations; the close can be made to fail."""

    name = "fake-live"
    environment = "test"

    def __init__(self, close_fails: bool):
        self.close_fails = close_fails
        self.flat = False
        self.calls: list[str] = []

    def get_price(self, symbol):
        return 100.0

    def get_ohlcv(self, symbol, timeframe, limit=500):
        raise BrokerError("veri yok")

    def get_balance(self):
        return Balance(equity=1000.0, free=1000.0, currency="USDT")

    def get_positions(self):
        self.calls.append("get_positions")
        return [] if self.flat else [Position("BTC/USDT:USDT", Side.LONG, 1.0, 100.0)]

    def get_open_orders(self, symbol=None):
        if any(c.startswith("cancel") for c in self.calls):
            return []
        return [OrderResult("t-x-s", "BTC/USDT:USDT", "sell", "stop", "stop_market", 1.0, 95.0, "open")]

    def place_bracket(self, ticket):
        raise BrokerError("kullanılmaz")

    def cancel_all(self):
        self.calls.append("cancel_all")
        return 1

    def cancel_orders(self, symbol):
        self.calls.append(f"cancel_orders:{symbol}")
        return 1

    def close_all(self):
        self.calls.append("close_all")
        if self.close_fails:
            return [
                OrderResult("failed:BTC", "BTC/USDT:USDT", "sell", "close", "market", 1.0, None, "failed",
                            "kapatılamadı: rejected")
            ]
        self.flat = True
        return [OrderResult("c1", "BTC/USDT:USDT", "sell", "close", "market", 1.0, None, "closed")]


@pytest.mark.parametrize("close_fails", [False, True])
def test_kill_closes_first_and_cancels_only_flat_symbols(tmp_path, monkeypatch, capsys, close_fails):
    config = write_config(tmp_path, mode="live")
    broker = KillBroker(close_fails)
    monkeypatch.setattr(cli, "account_brokers", lambda cfg, live_flag: [broker])
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    as_tty(monkeypatch, True)
    answer(monkeypatch, "ONAY")
    code = cli.main(["--config", str(config), "kill", "--live"])
    out = capsys.readouterr().out
    cancels = [i for i, c in enumerate(broker.calls) if c.startswith("cancel")]
    assert all(i > broker.calls.index("close_all") for i in cancels)
    if close_fails:
        assert code == 1 and cancels == []
        assert "hâlâ açık" in out and "BTC/USDT:USDT" in out
    else:
        assert code == 0 and "cancel_orders:BTC/USDT:USDT" in broker.calls


def test_kill_reports_a_venue_exception_and_exits_non_zero(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path, mode="live")
    broker = KillBroker(False)

    def fail():
        raise BrokerError("bağlantı koptu")

    broker.close_all = fail
    monkeypatch.setattr(cli, "account_brokers", lambda cfg, live_flag: [broker])
    as_tty(monkeypatch, True)
    answer(monkeypatch, "ONAY")
    assert cli.main(["--config", str(config), "kill", "--live"]) == 1
    assert "bağlantı koptu" in capsys.readouterr().out


def test_scan_keeps_a_closed_last_candle(config_path, monkeypatch):
    old_ltf, old_daily = long_setup_frames()

    class OldData(StubMarketData):
        def __init__(self, *args, **kwargs):
            self.ltf, self.daily = old_ltf, old_daily

    monkeypatch.setattr(cli, "MarketData", OldData)
    assert cli.main(["--config", str(config_path), "scan", "--symbols", "BTC/USDT"]) == 0
    journal = Journal(load_config(config_path).journal_path)
    (row,) = journal.latest_tickets()
    setup = journal.get_setup(row["setup_id"])
    journal.close()
    assert setup.detected_at == old_ltf.index[-1]


def test_rescan_of_a_sent_ticket_says_so(config_path, monkeypatch, capsys):
    ticket_id = scanned_ticket_id(config_path)
    answer(monkeypatch, "ONAY")
    assert cli.main(["--config", str(config_path), "execute", ticket_id]) == 0
    capsys.readouterr()
    assert cli.main(["--config", str(config_path), "scan", "--symbols", "BTC/USDT"]) == 0
    assert "zaten" in capsys.readouterr().out


def test_risk_calc_rejects_bad_leverage(tmp_path, capsys):
    config = write_config(tmp_path)
    base = ["--config", str(config), "risk-calc", "--equity", "10000", "--risk", "1", "--entry", "100", "--stop", "99"]
    for leverage in ("0", "-2", "nan"):
        assert cli.main(base + ["--leverage", leverage]) == 2
    assert "kaldıraç" in capsys.readouterr().out.lower()


def test_unexpected_errors_are_turkish_without_traceback(config_path, monkeypatch, capsys):
    def boom(args, cfg):
        raise RuntimeError("boom")

    monkeypatch.setitem(cli.COMMANDS, "stats", boom)
    assert cli.main(["--config", str(config_path), "stats"]) == 1
    err = capsys.readouterr().err
    assert "Beklenmeyen hata" in err and "Traceback" not in err


def test_scan_limit_is_capped(config_path):
    assert cli.main(["--config", str(config_path), "scan", "--symbols", "BTC/USDT", "--limit", "5000"]) == 2


def test_numbers_are_shown_without_scientific_notation():
    assert cli.num(1.2345678e-05) == "0.000012345678"
    assert cli.num(123456789.0) == "123,456,789"
    assert cli.num(99.45025) == "99.45025"
