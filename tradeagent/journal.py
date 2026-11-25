"""SQLite trade journal: setups, tickets, orders, fills, equity snapshots and R statistics.

Every table carries ``reason`` (why the row exists: setup narrative, exit
reason, order role) and ``notes`` (an append-only, timestamped event log).
The ``equity_snapshots`` table backs the daily/weekly loss limits.
"""

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tradeagent.brokers.base import Fill, OrderResult
from tradeagent.ict import NEW_YORK
from tradeagent.models import OrderTicket, Setup
from tradeagent.risk import rr, ticket_risk_amount

DEFAULT_JOURNAL_PATH = Path.home() / "trading-agent" / "data" / "journal.sqlite"
# Tickets in these states count as open risk. "submitting" is the write-ahead
# claim taken before anything is sent; "unknown" means the venue state could
# not be verified after an attempt.
OPEN_STATUSES = ("submitting", "submitted", "open", "unknown")


class JournalError(RuntimeError):
    """The journal database cannot be used. The message is user-facing (Turkish)."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS setups (
    id TEXT PRIMARY KEY,
    detected_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    side TEXT NOT NULL,
    bias TEXT NOT NULL,
    killzone TEXT,
    sweep_kind TEXT NOT NULL,
    sweep_level REAL NOT NULL,
    sweep_extreme REAL NOT NULL,
    mss_at TEXT NOT NULL,
    mss_level REAL NOT NULL,
    fvg_top REAL NOT NULL,
    fvg_bottom REAL NOT NULL,
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    targets TEXT NOT NULL,
    target_kinds TEXT NOT NULL,
    range_high REAL NOT NULL,
    range_low REAL NOT NULL,
    status TEXT NOT NULL,
    ticket_id TEXT,
    rejections TEXT NOT NULL,
    reason TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    setup_id TEXT,
    created_at TEXT,
    symbol TEXT NOT NULL,
    timeframe TEXT,
    side TEXT NOT NULL,
    market TEXT NOT NULL,
    entry REAL NOT NULL,
    stop REAL,
    targets TEXT NOT NULL,
    qty REAL NOT NULL,
    leverage REAL NOT NULL,
    risk_pct REAL NOT NULL,
    fee_rate REAL NOT NULL,
    risk_amount REAL,
    rr REAL,
    broker TEXT,
    mode TEXT,
    status TEXT NOT NULL,
    submitted_at TEXT,
    closed_at TEXT,
    result_r REAL,
    pnl REAL,
    reason TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT NOT NULL,
    broker TEXT NOT NULL,
    ticket_id TEXT,
    created_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    role TEXT NOT NULL,
    type TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL,
    status TEXT NOT NULL,
    reason TEXT,
    notes TEXT,
    PRIMARY KEY (broker, id)
);
CREATE TABLE IF NOT EXISTS fills (
    id TEXT PRIMARY KEY,
    ticket_id TEXT,
    order_id TEXT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    kind TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    pnl REAL,
    r REAL,
    reason TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts TEXT NOT NULL,
    broker TEXT NOT NULL,
    equity REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS equity_snapshots_broker_ts ON equity_snapshots (broker, ts);
CREATE TABLE IF NOT EXISTS notifications (
    ticket_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (ticket_id, kind)
);
"""

_EXIT_REASON_TR = {"stop": "stop", "take_profit": "take-profit", "close": "piyasa kapanışı", "entry": "giriş"}


def _iso(moment: datetime) -> str:
    """Fixed-width UTC timestamp so that text order equals time order."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _parse(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def _parse_required(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _stamp(note: str) -> str:
    return f"{datetime.now(UTC):%Y-%m-%d %H:%M:%S}Z {note}"


@dataclass(frozen=True, slots=True)
class Stats:
    """R-based performance of closed tickets.

    ``win_rate`` is a fraction; ``expectancy`` = win% x avg win - loss% x avg
    loss (R); ``profit_factor`` = gross win R / gross loss R (``inf`` without
    losses); ``max_drawdown_r`` is the largest peak-to-trough drop of the
    cumulative R curve.
    """

    trades: int
    wins: int
    losses: int
    win_rate: float
    avg_r: float
    expectancy: float
    profit_factor: float
    max_drawdown_r: float
    total_r: float


def compute_stats(results: list[float]) -> Stats:
    """Statistics for R multiples given in chronological order."""
    trades = len(results)
    if trades == 0:
        return Stats(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    wins = [r for r in results if r > 0]
    losses = [r for r in results if r < 0]
    win_rate = len(wins) / trades
    loss_rate = len(losses) / trades
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = -sum(losses) / len(losses) if losses else 0.0
    gross_win, gross_loss = sum(wins), -sum(losses)
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = math.inf if gross_win > 0 else 0.0
    peak = equity = drawdown = 0.0
    for r in results:
        equity += r
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return Stats(
        trades=trades,
        wins=len(wins),
        losses=len(losses),
        win_rate=win_rate,
        avg_r=sum(results) / trades,
        expectancy=win_rate * avg_win - loss_rate * avg_loss,
        profit_factor=profit_factor,
        max_drawdown_r=drawdown,
        total_r=sum(results),
    )


class Journal:
    """Thin repository over one SQLite file."""

    def __init__(self, path: str | Path = DEFAULT_JOURNAL_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(self.path, timeout=10)
            self._conn.row_factory = sqlite3.Row
            with self._conn:
                self._conn.executescript(_SCHEMA)
        except sqlite3.DatabaseError as exc:
            raise JournalError(
                f"Journal okunamadı: {self.path} ({exc}). Dosyayı yedekten geri yükleyin; "
                "silerseniz geçmiş kayıtlar ve açık bilet takibi kaybolur."
            ) from exc

    def close(self) -> None:
        self._conn.close()

    # --- setups / tickets ------------------------------------------------------------------

    def save_setup(self, setup: Setup) -> None:
        """Insert or refresh a scanned setup; its ticket is only rewritten while still ``new``."""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO setups (id, detected_at, symbol, timeframe, side, bias, killzone, sweep_kind,
                    sweep_level, sweep_extreme, mss_at, mss_level, fvg_top, fvg_bottom, entry, stop, targets,
                    target_kinds, range_high, range_low, status, ticket_id, rejections, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    detected_at = excluded.detected_at, bias = excluded.bias, killzone = excluded.killzone,
                    sweep_extreme = excluded.sweep_extreme, fvg_top = excluded.fvg_top,
                    fvg_bottom = excluded.fvg_bottom, entry = excluded.entry, stop = excluded.stop,
                    targets = excluded.targets, target_kinds = excluded.target_kinds,
                    range_high = excluded.range_high, range_low = excluded.range_low,
                    status = excluded.status, ticket_id = excluded.ticket_id,
                    rejections = excluded.rejections, reason = excluded.reason
                WHERE NOT EXISTS (
                    SELECT 1 FROM tickets t WHERE t.id = setups.ticket_id AND t.status <> 'new'
                )
                """,
                (
                    setup.id,
                    _iso(setup.detected_at),
                    setup.symbol,
                    setup.timeframe,
                    setup.side.value,
                    setup.bias,
                    setup.killzone,
                    setup.sweep_kind,
                    setup.sweep_level,
                    setup.sweep_extreme,
                    _iso(setup.mss_at),
                    setup.mss_level,
                    setup.fvg_top,
                    setup.fvg_bottom,
                    setup.entry,
                    setup.stop,
                    json.dumps(setup.targets),
                    json.dumps(setup.target_kinds),
                    setup.range_high,
                    setup.range_low,
                    "ticket" if setup.ticket is not None else "rejected",
                    setup.ticket.id if setup.ticket is not None else None,
                    json.dumps(setup.reasons, ensure_ascii=False),
                    setup.narrative,
                ),
            )
            if setup.ticket is not None:
                self._write_ticket(setup.ticket, only_if_new=True)

    def _write_ticket(self, ticket: OrderTicket, *, only_if_new: bool) -> None:
        try:
            risk_amount: float | None = ticket_risk_amount(ticket)
        except ValueError:
            risk_amount = None
        take_profit = ticket.take_profit
        try:
            ratio = rr(ticket.entry, ticket.stop, take_profit) if ticket.stop and take_profit else None
        except ValueError:
            ratio = None
        guard = "WHERE tickets.status = 'new'" if only_if_new else ""
        self._conn.execute(
            f"""
            INSERT INTO tickets (id, setup_id, created_at, symbol, timeframe, side, market, entry, stop, targets,
                qty, leverage, risk_pct, fee_rate, risk_amount, rr, status, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                entry = excluded.entry, stop = excluded.stop, targets = excluded.targets, qty = excluded.qty,
                leverage = excluded.leverage, risk_pct = excluded.risk_pct, fee_rate = excluded.fee_rate,
                market = excluded.market, risk_amount = excluded.risk_amount, rr = excluded.rr,
                reason = excluded.reason, created_at = excluded.created_at
            {guard}
            """,
            (
                ticket.id,
                ticket.setup_id,
                _iso(ticket.created_at) if ticket.created_at is not None else None,
                ticket.symbol,
                ticket.timeframe,
                ticket.side.value,
                ticket.market,
                ticket.entry,
                ticket.stop,
                json.dumps(ticket.targets),
                ticket.qty,
                ticket.leverage,
                ticket.risk_pct,
                ticket.fee_rate,
                risk_amount,
                ratio,
                ticket.status,
                ticket.reason,
            ),
        )

    def get_setup(self, setup_id: str) -> Setup | None:
        row = self._conn.execute("SELECT * FROM setups WHERE id = ?", (setup_id,)).fetchone()
        if row is None:
            return None
        ticket = self.get_ticket(row["ticket_id"]) if row["ticket_id"] else None
        return Setup(
            id=row["id"],
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            side=row["side"],
            detected_at=_parse_required(row["detected_at"]),
            bias=row["bias"],
            sweep_kind=row["sweep_kind"],
            sweep_level=row["sweep_level"],
            sweep_extreme=row["sweep_extreme"],
            mss_at=_parse_required(row["mss_at"]),
            mss_level=row["mss_level"],
            fvg_top=row["fvg_top"],
            fvg_bottom=row["fvg_bottom"],
            entry=row["entry"],
            stop=row["stop"],
            targets=json.loads(row["targets"]),
            target_kinds=json.loads(row["target_kinds"]),
            range_high=row["range_high"],
            range_low=row["range_low"],
            killzone=row["killzone"],
            narrative=row["reason"] or "",
            reasons=json.loads(row["rejections"]),
            ticket=ticket,
        )

    def get_ticket(self, ticket_id: str) -> OrderTicket | None:
        row = self._conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return self._ticket_from_row(row) if row is not None else None

    @staticmethod
    def _ticket_from_row(row: sqlite3.Row) -> OrderTicket:
        return OrderTicket(
            id=row["id"],
            setup_id=row["setup_id"],
            symbol=row["symbol"],
            side=row["side"],
            entry=row["entry"],
            stop=row["stop"],
            targets=json.loads(row["targets"]),
            qty=row["qty"],
            leverage=row["leverage"],
            risk_pct=row["risk_pct"],
            fee_rate=row["fee_rate"],
            market=row["market"],
            timeframe=row["timeframe"] or "",
            reason=row["reason"] or "",
            created_at=_parse(row["created_at"]),
            status=row["status"],
        )

    def ticket_row(self, ticket_id: str) -> dict[str, Any] | None:
        """All stored columns of a ticket (broker, mode, result_r, notes...)."""
        row = self._conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return dict(row) if row is not None else None

    def get_ticket_notes(self, ticket_id: str) -> str:
        row = self._conn.execute("SELECT notes FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return (row["notes"] or "") if row is not None else ""

    def add_ticket_note(self, ticket_id: str, note: str) -> None:
        with self._conn:
            self._append_note("tickets", ticket_id, note)

    def _append_note(self, table: str, row_id: str, note: str) -> None:
        stamped = _stamp(note)
        self._conn.execute(
            f"UPDATE {table} SET notes = CASE WHEN notes IS NULL OR notes = '' THEN ? "
            "ELSE notes || char(10) || ? END WHERE id = ?",
            (stamped, stamped, row_id),
        )

    def claim_for_submission(self, ticket_id: str, *, broker: str, mode: str) -> bool:
        """Atomically move a ``new`` ticket to ``submitting``; ``False`` if someone else got it first."""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE tickets SET status = 'submitting', broker = ?, mode = ? WHERE id = ? AND status = 'new'",
                (broker, mode, ticket_id),
            )
            if cursor.rowcount == 1:
                self._append_note("tickets", ticket_id, f"gönderim için ayrıldı: {broker} ({mode})")
        return cursor.rowcount == 1

    def release_claim(self, ticket_id: str, note: str) -> None:
        """Return a claimed ticket to ``new`` when nothing was sent."""
        with self._conn:
            self._conn.execute(
                "UPDATE tickets SET status = 'new' WHERE id = ? AND status = 'submitting'", (ticket_id,)
            )
            self._append_note("tickets", ticket_id, note)

    def mark_submitted(self, ticket: OrderTicket, *, broker: str, mode: str, orders: list[OrderResult]) -> None:
        """Store the final (re-sized) ticket as sent, plus the broker's order acknowledgements."""
        with self._conn:
            self._write_ticket(ticket, only_if_new=False)
            self._conn.execute(
                "UPDATE tickets SET status = 'submitted', broker = ?, mode = ?, submitted_at = ? WHERE id = ?",
                (broker, mode, _iso(datetime.now(UTC)), ticket.id),
            )
            self._append_note("tickets", ticket.id, f"gönderildi: {broker} ({mode}), {len(orders)} emir")
            self._insert_orders(ticket.id, broker, mode, orders)

    def record_orders(self, ticket_id: str | None, *, broker: str, mode: str, orders: list[OrderResult]) -> None:
        with self._conn:
            self._insert_orders(ticket_id, broker, mode, orders)

    def _insert_orders(self, ticket_id: str | None, broker: str, mode: str, orders: list[OrderResult]) -> None:
        now = _iso(datetime.now(UTC))
        for order in orders:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO orders (id, broker, ticket_id, created_at, mode, symbol, side, role, type,
                    qty, price, status, reason, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.id,
                    broker,
                    ticket_id,
                    now,
                    mode,
                    order.symbol,
                    order.side,
                    order.role,
                    order.type,
                    order.qty,
                    order.price,
                    order.status,
                    order.role,
                    order.note or None,
                ),
            )

    def orders_for(self, ticket_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM orders WHERE ticket_id = ? ORDER BY rowid", (ticket_id,))
        return [dict(r) for r in rows]

    def set_ticket_status(
        self,
        ticket_id: str,
        status: str,
        *,
        result_r: float | None = None,
        pnl: float | None = None,
        closed_at: datetime | None = None,
        note: str | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                UPDATE tickets SET status = ?, result_r = COALESCE(?, result_r), pnl = COALESCE(?, pnl),
                    closed_at = COALESCE(?, closed_at)
                WHERE id = ?
                """,
                (status, result_r, pnl, _iso(closed_at) if closed_at is not None else None, ticket_id),
            )
            if note:
                self._append_note("tickets", ticket_id, note)

    def tickets_without_result(self, broker: str) -> list[OrderTicket]:
        """Sent tickets on ``broker`` that closed but have no R recorded yet."""
        rows = self._conn.execute(
            "SELECT * FROM tickets WHERE broker = ? AND status = 'closed' AND result_r IS NULL AND submitted_at IS NOT NULL "
            "ORDER BY submitted_at",
            (broker,),
        )
        return [self._ticket_from_row(r) for r in rows]

    def open_tickets(self, broker: str) -> list[OrderTicket]:
        """Tickets sent to ``broker`` that are not closed or cancelled (they count as open risk)."""
        rows = self._conn.execute(
            f"SELECT * FROM tickets WHERE broker = ? AND status IN ({', '.join('?' * len(OPEN_STATUSES))}) "
            "ORDER BY submitted_at",
            (broker, *OPEN_STATUSES),
        )
        return [self._ticket_from_row(r) for r in rows]

    # --- notifications (watch loop) -------------------------------------------------------------

    def claim_notification(self, ticket_id: str, kind: str) -> bool:
        """Record that ``kind`` was announced for a ticket; ``False`` if it already was."""
        with self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO notifications (ticket_id, kind, sent_at) VALUES (?, ?, ?)",
                (ticket_id, kind, _iso(datetime.now(UTC))),
            )
        return cursor.rowcount == 1

    def tickets_to_announce(self, kind: str, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        """Announced tickets (any earlier kind) now in ``statuses`` whose ``kind`` was not announced yet."""
        rows = self._conn.execute(
            f"SELECT * FROM tickets t WHERE t.status IN ({', '.join('?' * len(statuses))}) "
            "AND EXISTS (SELECT 1 FROM notifications n WHERE n.ticket_id = t.id) "
            "AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.ticket_id = t.id AND n.kind = ?) "
            "ORDER BY t.closed_at, t.rowid",
            (*statuses, kind),
        )
        return [dict(r) for r in rows]

    def latest_tickets(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM tickets ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # --- fills ---------------------------------------------------------------------------------

    def record_fill(self, fill: Fill) -> bool:
        """Insert a fill once; returns ``False`` when it was already journaled."""
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO fills (id, ticket_id, order_id, ts, symbol, side, kind, qty, price, fee,
                    pnl, r, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.id,
                    fill.ticket_id,
                    fill.order_id,
                    _iso(fill.ts),
                    fill.symbol,
                    fill.side,
                    fill.kind,
                    fill.qty,
                    fill.price,
                    fill.fee,
                    fill.pnl,
                    fill.r,
                    _EXIT_REASON_TR.get(fill.kind, fill.kind),
                ),
            )
        return cursor.rowcount == 1

    def fills_for(self, ticket_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM fills WHERE ticket_id = ? ORDER BY ts, rowid", (ticket_id,))
        return [dict(r) for r in rows]

    # --- account / statistics --------------------------------------------------------------------

    def equity_baselines(self, broker: str, equity: float, now: datetime | None = None) -> tuple[float, float]:
        """Record an equity snapshot and return (day start, week start) equity.

        Days and weeks start at midnight New York time (weeks on Monday). The
        first snapshot of a period is its baseline, so losses that happened
        before the tool was first run in a period are not seen.
        """
        moment = now if now is not None else datetime.now(UTC)
        local = moment.astimezone(NEW_YORK)
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=local.weekday())
        with self._conn:
            self._conn.execute(
                "INSERT INTO equity_snapshots (ts, broker, equity) VALUES (?, ?, ?)", (_iso(moment), broker, equity)
            )

        def first_since(start: datetime) -> float:
            row = self._conn.execute(
                "SELECT equity FROM equity_snapshots WHERE broker = ? AND ts >= ? ORDER BY ts LIMIT 1",
                (broker, _iso(start)),
            ).fetchone()
            return float(row["equity"]) if row is not None else equity

        return first_since(day_start), first_since(week_start)

    def stats(self) -> Stats:
        rows = self._conn.execute(
            "SELECT result_r FROM tickets WHERE status = 'closed' AND result_r IS NOT NULL ORDER BY closed_at, rowid"
        )
        return compute_stats([float(r["result_r"]) for r in rows])
