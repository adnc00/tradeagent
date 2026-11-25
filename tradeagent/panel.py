"""Live panel: a small local web server over the journal, the watch heartbeat and the panel actions.

* Binds to 127.0.0.1 only and refuses requests whose Host header is not this
  server (DNS-rebinding guard).
* GET ``/`` (the page) and GET ``/api/state`` (JSON) only read: the journal is
  opened with SQLite's read-only URI mode for them.
* Actions (``tradeagent.actions``) are POST-only and need all of: a JSON body,
  an Origin of this server, and the secret token that is embedded in the page
  when it is served (a fresh one per panel process). Another web page can
  therefore neither read the token nor trigger an action. Sending an order
  and the kill also need the typed word ``ONAY``; actions run one at a time.
"""

import hmac
import json
import secrets
import sqlite3
import threading
import webbrowser
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

from tradeagent import __version__
from tradeagent.config import Config
from tradeagent.ict import NEW_YORK
from tradeagent.journal import OPEN_STATUSES, compute_stats
from tradeagent.risk import rr_after_fees

DEFAULT_PORT = 8765
STATUS_FILE = "watch_status.json"
ALERTS_FILE = "alerts.jsonl"
MAX_ALERTS = 60
MAX_RECENT_TICKETS = 40
MAX_EQUITY_POINTS = 500
MAX_BODY_BYTES = 16_384
TOKEN_PLACEHOLDER = b"{{PANEL_TOKEN}}"
VERSION_PLACEHOLDER = b"{{PANEL_VERSION}}"
TICKET_COLUMNS = (
    "id, symbol, side, market, timeframe, status, entry, stop, targets, qty, leverage, fee_rate, risk_amount, rr, "
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


def _journal_state(path: Path, *, live: bool = False) -> dict[str, Any]:
    empty = {"open_tickets": [], "recent_tickets": [], "new_tickets": [], "closed": [], "equity": []}
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
        new_rows = conn.execute(
            f"SELECT {TICKET_COLUMNS} FROM tickets WHERE status = 'new' ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        closed_rows = conn.execute(
            "SELECT id, symbol, closed_at, result_r FROM tickets WHERE status = 'closed' AND result_r IS NOT NULL "
            "ORDER BY closed_at, rowid"
        ).fetchall()
        # The equity shown is the account the panel acts on: a remote account when live, the simulator otherwise.
        preferred = "broker != 'paper'" if live else "broker = 'paper'"
        latest = conn.execute(f"SELECT broker FROM equity_snapshots WHERE {preferred} ORDER BY ts DESC LIMIT 1").fetchone()
        if latest is None:
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
        "new_tickets": [_ticket(r) for r in new_rows],
        "closed": [dict(r) for r in closed_rows],
        "equity": [{"at": r["ts"], "equity": r["equity"], "broker": latest["broker"]} for r in equity_rows],
    }


def account_info(cfg: Config, live: bool) -> dict[str, Any]:
    """Which account panel actions reach: live/demo only with ``mode: live`` AND ``panel --live``."""
    enabled = cfg.mode == "live" and live
    real_money = enabled and (not cfg.crypto.testnet or not cfg.alpaca.paper)
    if not enabled:
        label = "yerel paper (simülasyon)"
    else:
        crypto = f"{cfg.crypto.exchange} {'demo' if cfg.crypto.testnet else 'GERÇEK PARA'}"
        label = f"{crypto} · Alpaca {'paper' if cfg.alpaca.paper else 'GERÇEK PARA'}"
    return {"live": enabled, "real_money": real_money, "label": label, "actions": True}


def _mark_expiry(ticket: dict[str, Any], cfg: Config, moment: datetime) -> dict[str, Any]:
    created = _parse_time(ticket.get("created_at"))
    limit = timedelta(minutes=cfg.execution.ticket_max_age_minutes)
    ticket["expired"] = ticket.get("status") == "new" and (created is None or moment - created > limit)
    try:
        ticket["rr_net"] = rr_after_fees(ticket["entry"], ticket["stop"], ticket["targets"][0], ticket.get("fee_rate") or 0.0)
    except (TypeError, ValueError, IndexError, KeyError):
        ticket["rr_net"] = None
    ticket["expires_at"] = (created + limit).isoformat() if created is not None else None
    return ticket


def build_state(cfg: Config, now: datetime | None = None, *, live: bool = False) -> dict[str, Any]:
    """Everything the page shows, from files on disk only (no exchange calls)."""
    moment = now if now is not None else datetime.now(UTC)
    journal = _journal_state(cfg.journal_path, live=cfg.mode == "live" and live)
    for key in ("recent_tickets", "new_tickets"):
        journal[key] = [_mark_expiry(t, cfg, moment) for t in journal[key]]
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
        "version": __version__,
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
        "account": account_info(cfg, live),
        "awaiting": [t for t in journal["new_tickets"] if not t["expired"]],
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


class _PanelServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # type: ignore[no-untyped-def]
        import sys

        if isinstance(sys.exc_info()[1], ConnectionError):
            return  # the browser closed the connection (tab closed, reload): not an error
        super().handle_error(request, client_address)


def make_server(cfg: Config, port: int = DEFAULT_PORT, *, live: bool = False) -> ThreadingHTTPServer:
    """The panel server on 127.0.0.1 (``port`` 0 picks a free port)."""
    token = secrets.token_urlsafe(32)
    action_lock = threading.Lock()

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

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def _origins(self) -> set[str]:
            actual = self.server.server_address[1]
            return {f"http://127.0.0.1:{actual}", f"http://localhost:{actual}"}

        def _host_ok(self) -> bool:
            actual = self.server.server_address[1]
            return self.headers.get("Host", "") in {f"127.0.0.1:{actual}", f"localhost:{actual}"}

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if not self._host_ok():
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return
            path = self.path.split("?", 1)[0]
            if path == "/":
                page = _page().replace(TOKEN_PLACEHOLDER, token.encode("ascii"))
                page = page.replace(VERSION_PLACEHOLDER, __version__.encode("ascii"))
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(200, build_state(cfg, live=live))
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        do_HEAD = do_GET

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            from tradeagent import actions

            path = self.path.split("?", 1)[0]
            if path in ("/", "/api/state"):
                self._send(405, b"read-only", "text/plain; charset=utf-8")
                return
            action = actions.ACTIONS.get(path)
            if action is None:
                self._json(404, {"error": "bilinmeyen işlem"})
                return
            if not self._host_ok() or self.headers.get("Origin", "") not in self._origins():
                self._json(403, {"error": "yalnızca bu bilgisayardaki panel sayfasından"})
                return
            if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                self._json(415, {"error": "JSON bekleniyor"})
                return
            if not hmac.compare_digest(self.headers.get("X-Panel-Token", ""), token):
                self._json(403, {"error": "panel anahtarı geçersiz; sayfayı yenileyin"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY_BYTES:
                self._json(413, {"error": "istek çok büyük"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = None
            if not isinstance(body, dict):
                self._json(400, {"error": "geçersiz istek"})
                return
            with action_lock:  # one action at a time: no double sends from double clicks
                try:
                    self._json(200, action(cfg, live, body))
                except actions.ActionError as exc:
                    self._json(exc.code, exc.payload())
                except Exception as exc:  # report, never crash the panel
                    self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def _not_allowed(self) -> None:
            self._send(405, b"not allowed", "text/plain; charset=utf-8")

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    return _PanelServer(("127.0.0.1", port), Handler)


def serve(cfg: Config, port: int = DEFAULT_PORT, *, open_browser: bool = True, live: bool = False) -> None:
    httpd = make_server(cfg, port, live=live)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    account = account_info(cfg, live)
    print(f"Panel: {url}  (yalnızca bu bilgisayardan). İşlemler: {account['label']}. Durdurmak için Ctrl+C.")
    if account["real_money"]:
        print("DİKKAT: panelden onaylanan emirler GERÇEK PARA hesabına gider (her emirde ONAY yazmanız gerekir).")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
