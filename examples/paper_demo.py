"""Offline end-to-end paper demo.

1. Builds a synthetic BTC/USDT 15m frame (prices ~60,000) that contains one
   ICT long setup, plus matching daily candles.
2. Runs the scanner -> setup + order ticket, stores both in a demo journal.
3. Calls the real CLI: ``plan``, then ``execute`` twice through a stdin pipe:
   first with a wrong answer (aborts), then with ``ONAY`` (paper order).
4. Replays the next candles through the PaperBroker (entry fills, target hit).
5. Calls ``positions`` and ``stats``.

No network, no API keys, no live orders. Everything is written to a
temporary directory that is deleted at the end.

    python examples/paper_demo.py
"""

import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.synthetic import follow_through_candles, long_setup_frames  # noqa: E402
from tradeagent.brokers.paper import PaperBroker  # noqa: E402
from tradeagent.config import load_config  # noqa: E402
from tradeagent.journal import Journal  # noqa: E402
from tradeagent.models import AccountState  # noqa: E402
from tradeagent.scanner import scan_symbol  # noqa: E402

SCALE = 600.0
SYMBOL = "BTC/USDT"
DEMO_CONFIG = """\
mode: paper
data_dir: {data_dir}
fee_rate: 0.001
risk:
  allowlist: [BTC/USDT]
paper:
  equity: 10000
  sync: false
"""


def run_cli(config: Path, *args: str, stdin: str | None = None) -> int:
    shown = " ".join(("tradeagent", *args))
    print(f"\n$ {'echo ' + stdin.strip() + ' | ' if stdin else ''}{shown}")
    result = subprocess.run(
        [sys.executable, "-m", "tradeagent.cli", "--config", str(config), *args],
        input=stdin or "",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        cwd=ROOT,
        timeout=120,
    )
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    print(f"[çıkış kodu {result.returncode}]")
    return result.returncode


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        if stream.isatty():
            reconfigure(errors="replace")
        else:
            reconfigure(encoding="utf-8", errors="replace")
    with tempfile.TemporaryDirectory(prefix="tradeagent-demo-") as tmp:
        workdir = Path(tmp)
        config = workdir / "config.yaml"
        config.write_text(DEMO_CONFIG.format(data_dir=(workdir / "data").as_posix()), encoding="utf-8")
        cfg = load_config(config)

        end = pd.Timestamp.now(tz="UTC").floor("15min")
        ltf, daily = long_setup_frames(end=end, scale=SCALE)
        print(f"Sentetik veri: {SYMBOL} 15m, {len(ltf)} mum, {ltf.index[0]:%Y-%m-%d %H:%M} - {ltf.index[-1]:%H:%M} UTC")
        setups = scan_symbol(
            SYMBOL,
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
        journal = Journal(cfg.journal_path)
        for setup in setups:
            journal.save_setup(setup)
        journal.close()
        print(f"Tarayıcı: {len(setups)} kurulum -> " + ", ".join(f"{s.id} ({s.side.value})" for s in setups))
        (setup,) = setups
        if setup.ticket is None:
            print("Risk motoru bileti reddetti: " + "; ".join(setup.reasons))
            return 1

        # The simulated market's current price is the last close (0.4% above the entry).
        PaperBroker(cfg.paper_state_path).feed_price(SYMBOL, float(ltf["close"].iloc[-1]))
        run_cli(config, "plan", setup.id)
        run_cli(config, "execute", setup.ticket.id, stdin="evet\n")
        if run_cli(config, "execute", setup.ticket.id, stdin="ONAY\n") != 0:
            return 1

        print("\nSonraki mumlar PaperBroker'a besleniyor:")
        broker = PaperBroker(cfg.paper_state_path)
        for candle in follow_through_candles(after=end, scale=SCALE):
            fills = broker.feed_candle(SYMBOL, candle)
            events = ", ".join(
                f"{f.kind} @ {f.price:,.2f}" + (f" (K/Z {f.pnl:,.2f}, R {f.r:.2f})" if f.r is not None else "")
                for f in fills
            )
            print(
                f"  {candle.ts:%H:%M} O {candle.open:,.0f} Y {candle.high:,.0f} D {candle.low:,.0f} "
                f"K {candle.close:,.0f}  {events or '-'}"
            )

        run_cli(config, "positions")
        run_cli(config, "stats")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
