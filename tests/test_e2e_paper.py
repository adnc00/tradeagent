"""End-to-end paper flow through a real stdin pipe (offline: no network, no keys)."""

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from tradeagent.brokers.paper import PaperBroker
from tradeagent.config import load_config
from tradeagent.journal import Journal
from tradeagent.models import AccountState
from tradeagent.scanner import scan_symbol
from tests.synthetic import follow_through_candles, long_setup_frames

REPO = Path(__file__).resolve().parents[1]


def run_cli(config: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tradeagent.cli", "--config", str(config), *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        cwd=REPO,
        timeout=120,
    )


def test_scan_ticket_piped_onay_paper_fill_and_stats(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        f"data_dir: {(tmp_path / 'data').as_posix()}\nrisk:\n  allowlist: [BTC/USDT]\npaper:\n  sync: false\n",
        encoding="utf-8",
    )
    cfg = load_config(config)
    ltf, daily = long_setup_frames()
    (setup,) = scan_symbol(
        "BTC/USDT",
        ltf,
        daily,
        timeframe="15m",
        account=AccountState(equity=cfg.paper.equity),
        limits=cfg.risk,
        params=cfg.scanner,
        fee_rate=cfg.fee_rate,
        market="spot",
        now=datetime.now(UTC),
    )
    assert setup.ticket is not None
    journal = Journal(cfg.journal_path)
    journal.save_setup(setup)
    journal.close()
    # the paper market's current price: the last close, 0.4% above the entry
    PaperBroker(cfg.paper_state_path).feed_price("BTC/USDT", float(ltf["close"].iloc[-1]))

    refused = run_cli(config, "execute", setup.ticket.id, stdin="onay\n")
    assert refused.returncode == 1, refused.stderr
    assert "GÖNDERİLMEDİ" in refused.stdout

    sent = run_cli(config, "execute", setup.ticket.id, stdin="ONAY\n")
    assert sent.returncode == 0, sent.stderr
    assert "Emir gönderildi" in sent.stdout and "EMİR ONAYI" in sent.stdout

    broker = PaperBroker(cfg.paper_state_path)
    fills = [fill for candle in follow_through_candles() for fill in broker.feed_candle("BTC/USDT", candle)]
    assert [f.kind for f in fills] == ["entry", "take_profit"]
    assert fills[-1].r > 1.5

    stats = run_cli(config, "stats")
    assert stats.returncode == 0, stats.stderr
    assert "İşlem sayısı      : 1" in stats.stdout
