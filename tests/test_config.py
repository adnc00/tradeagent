import os
from pathlib import Path

import pytest

from tradeagent.config import Config, ConfigError, config_keys, load_config, load_dotenv

REPO = Path(__file__).resolve().parents[1]


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_match_the_spec():
    cfg = Config()
    assert cfg.mode == "paper"
    risk = cfg.risk
    assert (risk.risk_pct, risk.max_risk_pct, risk.daily_loss_pct, risk.weekly_loss_pct) == (0.5, 1.0, 2.0, 5.0)
    assert (risk.max_open_risk_pct, risk.max_leverage, risk.min_rr) == (3.0, 5.0, 2.0)
    assert risk.allowlist == ()
    assert cfg.crypto.testnet is True
    assert cfg.alpaca.paper is True
    assert cfg.journal_path == cfg.data_dir / "journal.sqlite"


def test_missing_default_file_gives_defaults(tmp_path):
    assert load_config(None, default_path=tmp_path / "absent.yaml") == Config()


def test_explicit_missing_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "absent.yaml")


def test_example_config_is_valid_and_safe():
    cfg = load_config(REPO / "config.example.yaml")
    assert cfg.mode == "paper"
    assert cfg.crypto.testnet is True
    assert cfg.alpaca.paper is True
    assert set(cfg.risk.allowlist) >= {"BTC/USDT", "ETH/USDT", "THYAO.IS", "AAPL"}
    assert cfg.risk.risk_pct == 0.5


def test_values_are_read(tmp_path):
    path = write(
        tmp_path,
        """
mode: live
data_dir: ~/custom-data
fee_rate: 0.0004
risk:
  risk_pct: 0.25
  allowlist: [BTC/USDT:USDT]
scanner:
  entry: top
  killzones: [london, ny_am]
  leverage: 3
crypto:
  exchange: binanceusdm
  testnet: false
paper:
  equity: 2500
  sync: false
""",
    )
    cfg = load_config(path)
    assert cfg.mode == "live"
    assert cfg.data_dir == Path.home() / "custom-data"
    assert cfg.fee_rate == 0.0004
    assert cfg.risk.risk_pct == 0.25 and cfg.risk.max_risk_pct == 1.0
    assert cfg.risk.allowlist == ("BTC/USDT:USDT",)
    assert cfg.scanner.entry == "top" and cfg.scanner.killzones == ("london", "ny_am") and cfg.scanner.leverage == 3
    assert (cfg.crypto.exchange, cfg.crypto.testnet) == ("binanceusdm", False)
    assert (cfg.paper.equity, cfg.paper.sync) == (2500.0, False)
    assert cfg.source == path


@pytest.mark.parametrize(
    "text",
    [
        "mode: yolo\n",
        "unknown_key: 1\n",
        "risk:\n  typo_pct: 1\n",
        "risk:\n  risk_pct: 2\n  max_risk_pct: 1\n",
        "risk:\n  risk_pct: -1\n",
        "risk:\n  allowlist: BTC/USDT\n",
        "scanner:\n  entry: mid\n",
        "crypto:\n  testnet: maybe\n",
        "fee_rate: -0.1\n",
        "- just\n- a list\n",
    ],
)
def test_invalid_configs_are_rejected(tmp_path, text):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_config_schema_has_no_confirmation_switch():
    keys = config_keys()
    assert "risk.allowlist" in keys and "mode" in keys
    assert not [k for k in keys if "confirm" in k.lower() or "onay" in k.lower() or "yes" in k.lower()]


def test_dotenv_sets_missing_variables_only(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n\nBINANCE_API_KEY=abc\nexport BINANCE_API_SECRET='s3cr3t'\nALPACA_API_KEY=\"q\"\nKEEP=new\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KEEP", "old")
    for name in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "ALPACA_API_KEY"):
        # registered with monkeypatch first so the values set by load_dotenv are removed afterwards
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    loaded = load_dotenv(env_file)
    assert loaded == ["BINANCE_API_KEY", "BINANCE_API_SECRET", "ALPACA_API_KEY"]
    assert os.environ["BINANCE_API_SECRET"] == "s3cr3t"
    assert os.environ["ALPACA_API_KEY"] == "q"
    assert os.environ["KEEP"] == "old"


def test_dotenv_missing_file_is_fine(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == []


# --- adversarial review fixes ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "risk:\n  risk_pct: 0.5\n  risk_pct: 0.25\n",
        "risk:\n  risk_pct: 0.5\nrisk:\n  risk_pct: 0.25\n",
        "risk:\n  max_leverage: .inf\n",
        "scanner:\n  displacement_k: .nan\n",
        "execution:\n  ticket_max_deviation_pct: 0\n",
        "execution:\n  ticket_max_age_minutes: 0\n",
        "fee_rate: 0.05\n",
    ],
)
def test_duplicate_keys_and_out_of_range_values_are_rejected(tmp_path, text):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_execution_defaults_and_values(tmp_path):
    assert (Config().execution.ticket_max_deviation_pct, Config().execution.ticket_max_age_minutes) == (0.5, 60)
    cfg = load_config(write(tmp_path, "execution:\n  ticket_max_deviation_pct: 1.5\n  ticket_max_age_minutes: 30\n"))
    assert (cfg.execution.ticket_max_deviation_pct, cfg.execution.ticket_max_age_minutes) == (1.5, 30)


def test_relative_data_dir_is_anchored_at_the_config_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path.parent)
    cfg = load_config(write(tmp_path, "data_dir: ./data\n"))
    assert cfg.data_dir == (tmp_path / "data").resolve()
    assert cfg.data_dir.is_absolute()


def test_yaml_exponent_floats_are_accepted(tmp_path):
    assert load_config(write(tmp_path, "fee_rate: 1e-3\n")).fee_rate == 0.001


@pytest.mark.parametrize("encoding,bom", [("utf-8", b"\xef\xbb\xbf"), ("utf-16", b"")])
def test_dotenv_handles_bom_utf16_and_inline_comments(tmp_path, monkeypatch, encoding, bom):
    env_file = tmp_path / ".env"
    text = "FIRST_KEY=abc # yorum\nSECOND_KEY='x # y' # yorum\nHASH_KEY=a#b\n"
    env_file.write_bytes(bom + text.encode(encoding))
    for name in ("FIRST_KEY", "SECOND_KEY", "HASH_KEY"):
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    assert load_dotenv(env_file) == ["FIRST_KEY", "SECOND_KEY", "HASH_KEY"]
    assert (os.environ["FIRST_KEY"], os.environ["SECOND_KEY"], os.environ["HASH_KEY"]) == ("abc", "x # y", "a#b")
