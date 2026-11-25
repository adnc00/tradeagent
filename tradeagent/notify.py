"""Alerts for the watch loop: what the trade is, what it risks and what it can earn.

Channels: the console (always), Telegram (when ``TELEGRAM_BOT_TOKEN`` and
``TELEGRAM_CHAT_ID`` are set) and a Windows desktop toast. Channels only
*send*; nothing received through them can approve or send an order.
"""

import json
import math
import os
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from tradeagent.brokers.data import asset_class
from tradeagent.journal import Stats
from tradeagent.models import OrderTicket
from tradeagent.risk import ticket_metrics

DISCLAIMER = "Kural tabanlı sezgisel kurulum; yatırım tavsiyesi değildir. Karar ve sorumluluk kullanıcınındır."
ISTANBUL = ZoneInfo("Europe/Istanbul")
MIN_TRADES_FOR_EXPECTANCY = 30
TELEGRAM_LIMIT = 4096
TELEGRAM_TIMEOUT = 15
TOAST_BODY_LIMIT = 240

# Windows PowerShell 5.1 toast. The text arrives through environment variables,
# never through the script, so alert content cannot inject commands.
_TOAST_SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$nodes = $xml.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($xml.CreateTextNode($env:TOAST_TITLE)) > $null
$nodes.Item(1).AppendChild($xml.CreateTextNode($env:TOAST_BODY)) > $null
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


class NotifyError(RuntimeError):
    """A channel could not deliver an alert. The message never contains secrets."""


@dataclass(frozen=True, slots=True)
class Alert:
    title: str
    body: str


# --- formatting ---------------------------------------------------------------------------------------


def num(value: float | None) -> str:
    """Exact decimal text with thousands separators (never scientific notation)."""
    if value is None:
        return "-"
    number = float(value)
    if not math.isfinite(number):
        return str(number)
    text = format(Decimal(repr(number)), ",f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def money(value: float) -> str:
    return f"{value:,.2f}"


def quote_currency(symbol: str) -> str:
    kind = asset_class(symbol)
    if kind == "crypto":
        return symbol.split("/", 1)[1].split(":", 1)[0]
    return "TRY" if kind == "bist" else "USD"


def _expectation_line(stats: Stats, risk_amount: float, currency: str) -> str:
    if stats.trades < MIN_TRADES_FOR_EXPECTANCY:
        return (
            f"Beklenen kazanç: hesaplanamaz - kapanmış işlem {stats.trades}/{MIN_TRADES_FOR_EXPECTANCY}; "
            "kazanma olasılığı henüz bilinmiyor."
        )
    value = stats.expectancy * risk_amount
    return (
        f"Beklenen kazanç (geçmiş {stats.trades} işlem): {stats.expectancy:+.2f}R ≈ {money(value)} {currency} / işlem "
        f"(kazanma oranı %{stats.win_rate * 100:.1f}; gelecek garantisi değildir)"
    )


def ticket_alert(
    ticket: OrderTicket,
    *,
    equity: float,
    mmr: float,
    stats: Stats,
    narrative: str,
    killzone: str | None,
    valid_minutes: int,
    action: str,
    headline: str = "yeni kurulum",
) -> Alert:
    """Plain-text alert with levels, size, risk, gain at target and the next step."""
    metrics = ticket_metrics(ticket, equity, mmr)
    currency = quote_currency(ticket.symbol)
    side = "LONG" if ticket.side.value == "long" else "SHORT"
    lines = [
        f"Yön: {side} ({'alış' if side == 'LONG' else 'açığa satış'})  [{ticket.market}, {ticket.timeframe or '-'}]",
        f"Giriş (limit): {num(ticket.entry)}",
        f"Stop: {num(ticket.stop)}",
        f"Hedef (take-profit): {num(ticket.take_profit)}"
        + (f"  | sonraki: {', '.join(num(t) for t in ticket.targets[1:])}" if len(ticket.targets) > 1 else ""),
        f"Miktar: {num(ticket.qty)}  | pozisyon değeri {money(metrics.notional)} {currency}",
        f"Kaldıraç: {num(ticket.leverage)}x  | marjin {money(metrics.margin)} {currency}",
    ]
    if metrics.liquidation is not None:
        lines.append(f"Likidasyon (tahmini): {num(metrics.liquidation)}")
    risk_pct = metrics.risk_pct if metrics.risk_pct is not None else 0.0
    lines.append(
        f"Risk (stop olursa): {money(metrics.risk_amount)} {currency} = özsermayenin %{risk_pct:.2f} (komisyon dahil)"
    )
    if metrics.rr is not None:
        gain = metrics.rr * metrics.risk_amount
        gain_pct = gain / equity * 100.0 if equity > 0 else 0.0
        break_even = 1.0 / (1.0 + metrics.rr) * 100.0 if metrics.rr > 0 else 100.0
        lines.append(
            f"Hedefte kazanç: {money(gain)} {currency} = özsermayenin %{gain_pct:.2f}  ({metrics.rr:.2f}R, komisyon dahil)"
        )
        lines.append(f"Başabaş kazanma oranı: %{break_even:.1f} (bu R:R ile en az bu kadar işlem kazanmalı)")
    lines.append(_expectation_line(stats, metrics.risk_amount, currency))
    lines.append(f"Özsermaye: {money(equity)} {currency}")
    if narrative:
        lines.append(f"Neden: {narrative}")
    lines.append(f"Killzone: {killzone or '-'}")
    if ticket.created_at is not None:
        until = (ticket.created_at + timedelta(minutes=valid_minutes)).astimezone(ISTANBUL)
        lines.append(f"Geçerlilik: {until:%d.%m %H:%M} (Türkiye saati) - sonra bilet bayat sayılır")
    lines.append(f"Bilet: {ticket.id}")
    if action:
        lines.append("")
        lines.append(action)
    lines.append("")
    lines.append(DISCLAIMER)
    return Alert(f"{side} {ticket.symbol} - {headline}", "\n".join(lines))


# --- channels ---------------------------------------------------------------------------------------------


class FileNotifier:
    """Appends alerts to a JSON-lines file for the panel; keeps roughly the last ``keep`` entries."""

    name = "kayıt"

    def __init__(self, path, *, keep: int = 500) -> None:
        from pathlib import Path

        self._path = Path(path)
        self._keep = keep

    def send(self, alert: Alert) -> None:
        from datetime import UTC, datetime

        self._path.parent.mkdir(parents=True, exist_ok=True)
        row = json.dumps({"at": datetime.now(UTC).isoformat(), "title": alert.title, "body": alert.body}, ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(row + "\n")
        lines = self._path.read_text(encoding="utf-8").splitlines()
        if len(lines) > 2 * self._keep:
            self._path.write_text("\n".join(lines[-self._keep:]) + "\n", encoding="utf-8")


class ConsoleNotifier:
    name = "konsol"

    def send(self, alert: Alert) -> None:
        line = "=" * 72
        print(f"{line}\n {alert.title}\n{line}\n{alert.body}\n{line}", flush=True)


def _api_description(exc: Exception) -> str:
    """Telegram's own reason from an HTTP error body ("Forbidden: bot was blocked by the user")."""
    reader = getattr(exc, "read", None)
    if reader is None:
        return ""
    try:
        return str(json.loads(reader() or b"{}").get("description", ""))
    except Exception:
        return ""


class TelegramNotifier:
    """Send-only Telegram bot client (Bot API ``sendMessage``)."""

    name = "Telegram"

    def __init__(self, token: str, chat_id: str, *, opener: Callable[..., object] = urllib.request.urlopen) -> None:
        self._token = token
        self._chat_id = chat_id
        self._opener = opener

    def send(self, alert: Alert) -> None:
        text = f"{alert.title}\n\n{alert.body}"[:TELEGRAM_LIMIT]
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/sendMessage",
            data=json.dumps({"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=TELEGRAM_TIMEOUT) as response:
                payload = json.loads(response.read() or b"{}")
        except Exception as exc:  # network, HTTP and JSON errors alike
            detail = _api_description(exc)
            message = f"gönderilemedi ({type(exc).__name__}: {exc})" + (f" - {detail}" if detail else "")
            raise NotifyError(self._redact(message)) from None
        if not payload.get("ok"):
            raise NotifyError(self._redact(f"API reddetti: {payload.get('description', 'bilinmeyen hata')}"))

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<gizli>") if self._token else text


def telegram_chats(token: str, *, opener: Callable[..., object] = urllib.request.urlopen) -> list[tuple[str, str]]:
    """(chat id, label) of everyone who wrote to the bot recently (Bot API ``getUpdates``)."""
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        with opener(urllib.request.Request(url, method="GET"), timeout=TELEGRAM_TIMEOUT) as response:
            payload = json.loads(response.read() or b"{}")
    except Exception as exc:
        raise NotifyError(f"getUpdates başarısız ({type(exc).__name__}: {exc})".replace(token, "<gizli>")) from None
    if not payload.get("ok"):
        raise NotifyError(f"API reddetti: {payload.get('description', 'bilinmeyen hata')}".replace(token, "<gizli>"))
    chats: dict[str, str] = {}
    for update in payload.get("result", []):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat")
            if chat and "id" in chat:
                name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username") or "?"
                chats.setdefault(str(chat["id"]), f"{name} ({chat.get('type', '?')})")
    return list(chats.items())


class DesktopNotifier:
    """Windows toast through Windows PowerShell; the text travels in environment variables."""

    name = "Windows bildirimi"

    def __init__(self, *, runner: Callable[..., object] = subprocess.run) -> None:
        self._runner = runner

    def send(self, alert: Alert) -> None:
        body = alert.body if len(alert.body) <= TOAST_BODY_LIMIT else alert.body[: TOAST_BODY_LIMIT - 1] + "…"
        env = dict(os.environ, TOAST_TITLE=alert.title, TOAST_BODY=body)
        try:
            done = self._runner(
                ["powershell.exe", "-NoProfile", "-Command", _TOAST_SCRIPT],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception as exc:
            raise NotifyError(f"gösterilemedi ({type(exc).__name__})") from None
        if getattr(done, "returncode", 1) != 0:
            detail = (getattr(done, "stderr", "") or "").strip().splitlines()[-1:] or ["bilinmeyen hata"]
            raise NotifyError(f"gösterilemedi: {detail[0][:200]}")


def telegram_settings(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """(bot token, chat id) from the environment / ``.env``; empty strings when unset."""
    source = os.environ if env is None else env
    return source.get("TELEGRAM_BOT_TOKEN", "").strip(), source.get("TELEGRAM_CHAT_ID", "").strip()


def notifiers_from_env(
    env: Mapping[str, str] | None = None, *, platform: str = sys.platform, desktop: bool = True
) -> list:
    """Console always; Telegram when both variables are set; desktop toast on Windows unless ``desktop`` is off."""
    channels: list = [ConsoleNotifier()]
    token, chat_id = telegram_settings(env)
    if token and chat_id:
        channels.append(TelegramNotifier(token, chat_id))
    if platform == "win32" and desktop:
        channels.append(DesktopNotifier())
    return channels


def broadcast(channels: list, alert: Alert) -> tuple[list[str], list[str]]:
    """Send to every channel; one failing channel never stops the others. Returns (sent, errors)."""
    sent: list[str] = []
    errors: list[str] = []
    for channel in channels:
        try:
            channel.send(alert)
        except Exception as exc:
            errors.append(f"{channel.name}: {exc}")
        else:
            sent.append(channel.name)
    return sent, errors
