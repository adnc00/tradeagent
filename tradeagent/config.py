"""Configuration (YAML) and ``.env`` loading.

Unknown keys are rejected, so a typo can never silently disable a rule and
there are no hidden switches. API keys are never part of the config file:
they come from environment variables, optionally loaded from ``.env``.
"""

import math
import os
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from tradeagent.models import RiskLimits
from tradeagent.risk import MAX_FEE_RATE
from tradeagent.scanner import ScannerParams

PROJECT_DIR = Path.home() / "trading-agent"
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.yaml"
DEFAULT_ENV_PATH = PROJECT_DIR / ".env"
DEFAULT_DATA_DIR = PROJECT_DIR / "data"
MODES = ("paper", "live")


class ConfigError(ValueError):
    """Invalid configuration. The message is user-facing (Turkish)."""


@dataclass(frozen=True, slots=True)
class CryptoConfig:
    """ccxt venue used for crypto data and, in live mode, for orders."""

    exchange: str = "binance"
    testnet: bool = True


@dataclass(frozen=True, slots=True)
class AlpacaConfig:
    """Alpaca account for US stocks; ``paper`` selects Alpaca's paper endpoint."""

    paper: bool = True


@dataclass(frozen=True, slots=True)
class PaperConfig:
    """Local simulator: starting equity and whether fills replay public market data."""

    equity: float = 10_000.0
    sync: bool = True
    sync_timeframe: str = "5m"


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """Stale-ticket guard used by ``execute`` before and right after the confirmation."""

    ticket_max_deviation_pct: float = 0.5
    ticket_max_age_minutes: int = 60
    entry_expiry_minutes: int = 120

    def __post_init__(self) -> None:
        if not 5 <= self.entry_expiry_minutes <= 10080:
            raise ValueError(f"entry_expiry_minutes 5 ile 10080 arasında olmalı (verilen {self.entry_expiry_minutes!r})")
        deviation = self.ticket_max_deviation_pct
        if not (math.isfinite(deviation) and 0 < deviation <= 20):
            raise ValueError(f"ticket_max_deviation_pct 0 ile 20 arasında olmalı (verilen {deviation!r})")
        if not 1 <= self.ticket_max_age_minutes <= 1440:
            raise ValueError(f"ticket_max_age_minutes 1 ile 1440 arasında olmalı (verilen {self.ticket_max_age_minutes!r})")


@dataclass(frozen=True, slots=True)
class UniverseConfig:
    """``--universe``: which pairs of the crypto exchange are scanned (24h quote volume ranking)."""

    quote: str = "USDT"
    top: int = 100
    min_quote_volume: float = 10_000_000.0
    exclude: tuple[str, ...] = ()
    coins_only: bool = True

    def __post_init__(self) -> None:
        if not 0 <= self.top <= 1000:
            raise ValueError(f"top 0 (sınırsız) ile 1000 arasında olmalı (verilen {self.top!r})")
        if not (math.isfinite(self.min_quote_volume) and self.min_quote_volume >= 0):
            raise ValueError(f"min_quote_volume 0 veya pozitif olmalı (verilen {self.min_quote_volume!r})")
        if not self.quote.strip():
            raise ValueError("quote boş olamaz")


@dataclass(frozen=True, slots=True)
class GoalConfig:
    """Small-account rehearsal and goal tracking.

    With ``start_capital`` > 0 a remote (demo/live) account is sized and risk-limited
    as if it held ``start_capital`` plus the net P&L of the tickets closed since
    ``since`` (a date, Istanbul time), never more than its real balance.
    """

    start_capital: float = 0.0
    target: float = 1000.0
    since: str = ""

    def __post_init__(self) -> None:
        if not (math.isfinite(self.start_capital) and self.start_capital >= 0):
            raise ValueError(f"start_capital 0 (kapalı) veya pozitif olmalı (verilen {self.start_capital!r})")
        if self.start_capital > 0 and not (math.isfinite(self.target) and self.target > self.start_capital):
            raise ValueError(f"target başlangıç sermayesinden büyük olmalı (verilen {self.target!r})")
        if self.since:
            try:
                date.fromisoformat(self.since)
            except ValueError:
                raise ValueError(f"since YYYY-AA-GG biçiminde bir tarih olmalı (verilen {self.since!r})") from None

    def since_moment(self) -> datetime | None:
        """``since`` as the UTC moment of midnight in Istanbul; ``None`` when not set."""
        if not self.since:
            return None
        day = date.fromisoformat(self.since)
        return datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("Europe/Istanbul")).astimezone(UTC)

    @property
    def enabled(self) -> bool:
        return self.start_capital > 0


@dataclass(frozen=True, slots=True)
class Config:
    mode: str = "paper"
    data_dir: Path = DEFAULT_DATA_DIR
    fee_rate: float = 0.001
    risk: RiskLimits = field(default_factory=RiskLimits)
    scanner: ScannerParams = field(default_factory=ScannerParams)
    crypto: CryptoConfig = field(default_factory=CryptoConfig)
    alpaca: AlpacaConfig = field(default_factory=AlpacaConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    goal: GoalConfig = field(default_factory=GoalConfig)
    source: Path | None = None

    @property
    def journal_path(self) -> Path:
        return self.data_dir / "journal.sqlite"

    @property
    def paper_state_path(self) -> Path:
        return self.data_dir / "paper_state.json"


_SECTIONS: dict[str, type] = {
    "risk": RiskLimits,
    "scanner": ScannerParams,
    "crypto": CryptoConfig,
    "alpaca": AlpacaConfig,
    "paper": PaperConfig,
    "execution": ExecutionConfig,
    "universe": UniverseConfig,
    "goal": GoalConfig,
}


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys instead of silently keeping the last one."""

    def construct_mapping(self, node, deep=False):  # type: ignore[no-untyped-def]
        seen: set[object] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise ConfigError(f"Ayar dosyasında yinelenen anahtar: {key} (satır {key_node.start_mark.line + 1})")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)
_TOP_LEVEL_SCALARS = ("mode", "data_dir", "fee_rate")


def config_keys() -> list[str]:
    """Every key the YAML file may contain, as dotted paths."""
    keys = list(_TOP_LEVEL_SCALARS)
    for name, cls in _SECTIONS.items():
        keys.extend(f"{name}.{f.name}" for f in fields(cls))
    return keys


def _default_of(cls: type, name: str) -> Any:
    for f in fields(cls):
        if f.name == name:
            if f.default is not MISSING:
                return f.default
            if f.default_factory is not MISSING:
                return f.default_factory()
    raise KeyError(name)


def _coerce(key: str, value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"Geçersiz değer: {key}={value!r} (true/false olmalı).")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"Geçersiz değer: {key}={value!r} (tam sayı olmalı).")
        return value
    if isinstance(default, float):
        if isinstance(value, str):
            try:  # YAML 1.1 reads 1e-3 as text
                value = float(value)
            except ValueError:
                raise ConfigError(f"Geçersiz değer: {key}={value!r} (sayı olmalı).") from None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"Geçersiz değer: {key}={value!r} (sayı olmalı).")
        if not math.isfinite(value):
            raise ConfigError(f"Geçersiz değer: {key}={value!r} (sonlu bir sayı olmalı).")
        return float(value)
    if isinstance(default, tuple):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"Geçersiz değer: {key} bir metin listesi olmalı, ör. [BTC/USDT, AAPL].")
        return tuple(value)
    if not isinstance(value, str):
        raise ConfigError(f"Geçersiz değer: {key}={value!r} (metin olmalı).")
    return value


def _section(name: str, cls: type, data: Any) -> Any:
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ConfigError(f"'{name}' bölümü bir sözlük (anahtar: değer) olmalı.")
    allowed = {f.name for f in fields(cls)}
    values: dict[str, Any] = {}
    for key, value in data.items():
        if key not in allowed:
            raise ConfigError(f"Bilinmeyen ayar: {name}.{key}")
        values[key] = _coerce(f"{name}.{key}", value, _default_of(cls, key))
    try:
        return cls(**values)
    except ValueError as exc:
        raise ConfigError(f"'{name}' bölümü geçersiz: {exc}") from exc


def load_config(path: str | Path | None = None, *, default_path: Path = DEFAULT_CONFIG_PATH) -> Config:
    """Read the YAML config. Without ``path`` the default file is used if it exists, else defaults."""
    if path is None:
        if not default_path.exists():
            return Config()
        path = default_path
    config_path = Path(path).expanduser()
    if not config_path.is_file():
        raise ConfigError(f"Ayar dosyası bulunamadı: {config_path}")
    try:
        data = yaml.load(config_path.read_text(encoding="utf-8-sig"), Loader=_UniqueKeyLoader)  # noqa: S506
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML okunamadı ({config_path}): {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("Ayar dosyasının kökü anahtar: değer çiftlerinden oluşmalı.")

    unknown = [key for key in data if key not in _TOP_LEVEL_SCALARS and key not in _SECTIONS]
    if unknown:
        raise ConfigError(f"Bilinmeyen ayar: {', '.join(map(str, unknown))}")

    mode = data.get("mode", "paper")
    if mode not in MODES:
        raise ConfigError(f"Geçersiz değer: mode={mode!r} (paper veya live olmalı).")
    fee_rate = _coerce("fee_rate", data.get("fee_rate", 0.001), 0.001)
    if not 0 <= fee_rate <= MAX_FEE_RATE:
        raise ConfigError(f"Geçersiz değer: fee_rate={fee_rate!r} (0 ile {MAX_FEE_RATE} arasında olmalı).")
    data_dir = Path(_coerce("data_dir", data.get("data_dir", str(DEFAULT_DATA_DIR)), "")).expanduser()
    if not data_dir.is_absolute():
        # Relative paths belong to the config file, not to the current working directory.
        data_dir = config_path.resolve().parent / data_dir
    data_dir = data_dir.resolve()

    sections = {name: _section(name, cls, data.get(name)) for name, cls in _SECTIONS.items()}
    if not sections["paper"].equity > 0:
        raise ConfigError("paper.equity sıfırdan büyük olmalı.")
    return Config(mode=mode, data_dir=data_dir, fee_rate=fee_rate, source=config_path, **sections)


def env_is_set(name: str) -> bool:
    """Whether an environment variable has a non-empty value (the value itself is never returned)."""
    return bool(os.environ.get(name, "").strip())


def _dotenv_value(value: str) -> str | None:
    """Value of one ``KEY=...`` line; ``None`` for a malformed quoted value."""
    if value[:1] in ("'", '"'):
        closing = value.find(value[0], 1)
        if closing == -1:
            return None
        rest = value[closing + 1 :].strip()
        return value[1:closing] if not rest or rest.startswith("#") else None
    for index, char in enumerate(value):
        if char == "#" and (index == 0 or value[index - 1] in " \t"):
            return value[:index].rstrip()
    return value


def load_dotenv(path: str | Path) -> list[str]:
    """Load ``KEY=VALUE`` lines into ``os.environ`` without overriding existing variables.

    Supports comments (also ``KEY=value  # comment``), blank lines, an optional
    ``export`` prefix, quoted values, a UTF-8 BOM and UTF-16 files (PowerShell).
    Returns the names that were set, never the values.
    """
    env_path = Path(path).expanduser()
    if not env_path.is_file():
        return []
    raw_bytes = env_path.read_bytes()
    text = raw_bytes.decode("utf-16") if raw_bytes[:2] in (b"\xff\xfe", b"\xfe\xff") else raw_bytes.decode("utf-8-sig")
    loaded: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not name.isidentifier():
            continue
        parsed = _dotenv_value(value.strip())
        if parsed is None:
            continue
        value = parsed
        if name in os.environ:
            continue
        os.environ[name] = value
        loaded.append(name)
    return loaded
