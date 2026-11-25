"""Read-only live panel: a tiny local web server over the journal and the watch heartbeat.

* Binds to 127.0.0.1 only and refuses requests whose Host header is not this
  server (DNS-rebinding guard).
* Only GET ``/`` (the page) and GET ``/api/state`` (JSON) exist; every other
  method is 405. Nothing here can place, change or cancel an order.
* The journal is opened with SQLite's read-only URI mode, so the panel never
  writes to it.
"""

import json
import sqlite3
import webbrowser
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

from tradeagent.config import Config
from tradeagent.ict import NEW_YORK
from tradeagent.journal import OPEN_STATUSES, compute_stats

DEFAULT_PORT = 8765
STATUS_FILE = "watch_status.json"
ALERTS_FILE = "alerts.jsonl"
MAX_ALERTS = 60
MAX_RECENT_TICKETS = 40
MAX_EQUITY_POINTS = 500
TICKET_COLUMNS = (
    "id, symbol, side, market, timeframe, status, entry, stop, targets, qty, leverage, risk_amount, rr, "
    "broker, mode, created_at, submitted_at, closed_at, result_r, pnl"
)


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_alerts(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    alerts = []
    for line in reversed(lines[-MAX_ALERTS:]):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            alerts.append({"at": row.get("at"), "title": row.get("title", ""), "body": row.get("body", "")})
    return alerts


def _ticket(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        item["targets"] = json.loads(item.get("targets") or "[]")
    except ValueError:
        item["targets"] = []
    return item


def _journal_state(path: Path) -> dict[str, Any]:
    empty = {"open_tickets": [], "recent_tickets": [], "closed": [], "equity": []}
    if not path.is_file():
        return empty
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ", ".join("?" * len(OPEN_STATUSES))
        open_rows = conn.execute(
            f"SELECT {TICKET_COLUMNS} FROM tickets WHERE status IN ({placeholders}) ORDER BY submitted_at DESC",
            OPEN_STATUSES,
        ).fetchall()
        recent_rows = conn.execute(
            f"SELECT {TICKET_COLUMNS} FROM tickets ORDER BY created_at DESC LIMIT ?", (MAX_RECENT_TICKETS,)
        ).fetchall()
        closed_rows = conn.execute(
            "SELECT id, symbol, closed_at, result_r FROM tickets WHERE status = 'closed' AND result_r IS NOT NULL "
            "ORDER BY closed_at, rowid"
        ).fetchall()
        latest = conn.execute("SELECT broker FROM equity_snapshots ORDER BY ts DESC LIMIT 1").fetchone()
        equity_rows = []
        if latest is not None:
            equity_rows = conn.execute(
                "SELECT ts, equity FROM (SELECT ts, equity FROM equity_snapshots WHERE broker = ? ORDER BY ts DESC LIMIT ?) "
                "ORDER BY ts",
                (latest["broker"], MAX_EQUITY_POINTS),
            ).fetchall()
    except sqlite3.DatabaseError:
        return empty
    finally:
        conn.close()
    return {
        "open_tickets": [_ticket(r) for r in open_rows],
        "recent_tickets": [_ticket(r) for r in recent_rows],
        "closed": [dict(r) for r in closed_rows],
        "equity": [{"at": r["ts"], "equity": r["equity"], "broker": latest["broker"]} for r in equity_rows],
    }


def build_state(cfg: Config, now: datetime | None = None) -> dict[str, Any]:
    """Everything the page shows, from files on disk only (no exchange calls)."""
    moment = now if now is not None else datetime.now(UTC)
    journal = _journal_state(cfg.journal_path)
    results = [float(r["result_r"]) for r in journal["closed"]]
    stats = compute_stats(results)
    curve, total = [], 0.0
    for row in journal["closed"]:
        total += float(row["result_r"])
        curve.append({"at": row["closed_at"], "r": round(total, 6), "symbol": row["symbol"]})
    watch = _read_json(cfg.data_dir / STATUS_FILE)
    last = _parse_time(watch.get("last_cycle_at"))
    interval = watch.get("interval_minutes") or 15
    alive = last is not None and moment - last <= timedelta(minutes=2 * float(interval) + 3)
    profit_factor = stats.profit_factor if stats.profit_factor != float("inf") else None
    equity = journal["equity"][-1]["equity"] if journal["equity"] else None
    open_risk = sum(float(t["risk_amount"] or 0.0) for t in journal["open_tickets"])
    day_start = moment.astimezone(NEW_YORK).replace(hour=0, minute=0, second=0, microsecond=0)
    today = [p["equity"] for p in journal["equity"] if (_parse_time(p["at"]) or moment) >= day_start]
    day_loss = max(0.0, (today[0] - equity) / today[0] * 100.0) if today and equity is not None and today[0] > 0 else 0.0
    risk = {
        "equity": equity,
        "open_risk": open_risk,
        "open_risk_pct": open_risk / equity * 100.0 if equity else None,
        "max_open_risk_pct": cfg.risk.max_open_risk_pct,
        "group_cap_pct": cfg.risk.max_risk_pct,
        "day_loss_pct": day_loss,
        "daily_loss_limit_pct": cfg.risk.daily_loss_pct,
    }
    return {
        "generated_at": moment.isoformat(),
        "watch": {**watch, "alive": alive},
        "config": {
            "mode": cfg.mode,
            "exchange": cfg.crypto.exchange,
            "testnet": cfg.crypto.testnet,
            "risk_pct": cfg.risk.risk_pct,
            "daily_loss_pct": cfg.risk.daily_loss_pct,
            "max_open_risk_pct": cfg.risk.max_open_risk_pct,
            "max_leverage": cfg.risk.max_leverage,
            "min_rr": cfg.risk.min_rr,
        },
        "stats": {
            "trades": stats.trades,
            "wins": stats.wins,
            "losses": stats.losses,
            "win_rate": stats.win_rate,
            "expectancy": stats.expectancy,
            "profit_factor": profit_factor,
            "max_drawdown_r": stats.max_drawdown_r,
            "total_r": stats.total_r,
        },
        "risk": risk,
        "open_tickets": journal["open_tickets"],
        "recent_tickets": journal["recent_tickets"],
        "r_curve": curve,
        "equity": journal["equity"],
        "alerts": _read_alerts(cfg.data_dir / ALERTS_FILE),
    }


def _page() -> bytes:
    return resources.files("tradeagent").joinpath("panel.html").read_bytes()


_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


def make_server(cfg: Config, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """A read-only HTTP server on 127.0.0.1 (``port`` 0 picks a free port)."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "tradeagent-panel"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - quiet console
            return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in _SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _host_ok(self) -> bool:
            actual = self.server.server_address[1]
            return self.headers.get("Host", "") in {f"127.0.0.1:{actual}", f"localhost:{actual}"}

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if not self._host_ok():
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, _page(), "text/html; charset=utf-8")
            elif path == "/api/state":
                body = json.dumps(build_state(cfg), ensure_ascii=False, default=str).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        do_HEAD = do_GET

        def _not_allowed(self) -> None:
            self._send(405, b"read-only panel", "text/plain; charset=utf-8")

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(cfg: Config, port: int = DEFAULT_PORT, *, open_browser: bool = True) -> None:
    httpd = make_server(cfg, port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"Panel: {url}  (yalnızca bu bilgisayardan erişilir, salt okunur). Durdurmak için Ctrl+C.")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
