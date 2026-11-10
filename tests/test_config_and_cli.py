import json
import logging
from unittest.mock import patch

import pytest

import main
from src.config import ConfigError, SecretRedactingFilter, load_settings
from tests.conftest import RISK_JSON, SUMMARY_JSON, FakeLLM


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("MONDAY_API_TOKEN", "ANTHROPIC_API_KEY", "MONDAY_BOARD_ID", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("src.config.load_dotenv", lambda **kw: None)  # ignore any real .env


def test_missing_credentials_raise_config_error():
    with pytest.raises(ConfigError, match="MONDAY_API_TOKEN, ANTHROPIC_API_KEY"):
        load_settings()


def test_placeholder_values_count_as_missing(monkeypatch):
    monkeypatch.setenv("MONDAY_API_TOKEN", "your-monday-api-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    with pytest.raises(ConfigError, match="MONDAY_API_TOKEN"):
        load_settings()


def test_settings_repr_hides_secrets(monkeypatch):
    monkeypatch.setenv("MONDAY_API_TOKEN", "secret-monday-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    settings = load_settings()
    assert settings.board_id == "5032323173"
    assert "secret" not in repr(settings)


def test_log_filter_redacts_secrets():
    record = logging.LogRecord("x", logging.INFO, "", 0, "token=%s", ("abcdef123456",), None)
    SecretRedactingFilter(["abcdef123456"]).filter(record)
    assert record.getMessage() == "token=***"


def test_main_exits_cleanly_without_credentials():
    assert main.main(["--dry-run"]) == 2


def test_sample_file_dry_run_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    sample = tmp_path / "apps.json"
    sample.write_text(json.dumps([
        {"item_id": "1", "client_name": "A", "description": "x"},
        {"item_id": "2", "client_name": "B", "description": "y"},
    ]))
    fake = FakeLLM([RISK_JSON, SUMMARY_JSON])
    with patch.object(main, "ClaudeCompleter", return_value=fake):
        code = main.main(["--sample-file", str(sample), "--limit", "1", "--output-dir", str(tmp_path)])
    assert code == 0
    [out] = list(tmp_path.glob("onboarding_results_*.json"))
    data = json.loads(out.read_text())
    assert data["dry_run"] is True and data["processed"] == 1
    assert data["results"][0]["result"]["risk_level"] == "HIGH"
