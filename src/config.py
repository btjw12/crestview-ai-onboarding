"""Configuration loading and logging setup.

All secrets come from environment variables (optionally loaded from a local
.env file). Nothing sensitive is hardcoded, and a logging filter redacts any
configured secret that accidentally makes it into a log message.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

DEFAULT_BOARD_ID = "5032323173"
DEFAULT_MODEL = "claude-opus-5-5"
MONDAY_API_URL = "https://api.monday.com/v2"

# Output columns on the board (known IDs, provided by the board owner).
OUTPUT_COLUMNS = {
    "risk_level": "color_mm8vjgpn",           # AI Risk Level (status)
    "risk_explanation": "long_text_mm8vqq3n",  # Risk Explanation
    "onboarding_summary": "long_text_mm8vtgjg",  # Onboarding Summary
    "recommended_action": "long_text_mm8v99qe",  # Recommendation Action
    "human_review_required": "color_mm8v1s9e",  # Manual Triage Required (status)
    "review_type": "color_mm8whrrp", #Review Type (status)
    "review_reason": "long_text_mm8w9cjk", #Review Reason
}

# Screening Status column (status). Owned by compliance: the app only sets the initial
# label when the column is empty, and never overwrites progress.
SCREENING_STATUS_COLUMN = "color_mm8wva1n"
SCREENING_STATUS_INITIAL_LABEL = "Not Started"


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


# All runtime settings; repr=False keeps secrets out of printed/logged objects.
@dataclass(frozen=True)
class Settings:
    monday_api_token: str | None = field(repr=False)
    anthropic_api_key: str | None = field(repr=False)
    board_id: str = DEFAULT_BOARD_ID
    model: str = DEFAULT_MODEL
    monday_api_url: str = MONDAY_API_URL
    monday_api_version: str | None = None
    log_level: str = "INFO"

    # Values the logging filter must mask.
    @property
    def secrets(self) -> list[str]:
        return [s for s in (self.monday_api_token, self.anthropic_api_key) if s]


# Trim a value and treat blanks or .env.example placeholders as unset.
def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    # Treat the placeholder values from .env.example as "not set".
    if not value or value.startswith("your-"):
        return None
    return value


def load_settings(*, require_monday: bool = True, require_llm: bool = True) -> Settings:
    """Load settings from the environment / .env and validate required values."""
    load_dotenv(override=False)

    settings = Settings(
        monday_api_token=_clean(os.getenv("MONDAY_API_TOKEN")),
        anthropic_api_key=_clean(os.getenv("ANTHROPIC_API_KEY")),
        board_id=_clean(os.getenv("MONDAY_BOARD_ID")) or DEFAULT_BOARD_ID,
        model=_clean(os.getenv("ANTHROPIC_MODEL")) or DEFAULT_MODEL,
        monday_api_version=_clean(os.getenv("MONDAY_API_VERSION")),
        log_level=(_clean(os.getenv("LOG_LEVEL")) or "INFO").upper(),
    )

    # Fail early with a clear message if a required key is missing.
    missing = []
    if require_monday and not settings.monday_api_token:
        missing.append("MONDAY_API_TOKEN")
    if require_llm and not settings.anthropic_api_key:
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        raise ConfigError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Copy .env.example to .env and fill in your credentials."
        )
    return settings


class SecretRedactingFilter(logging.Filter):
    """Replaces any known secret value in log records with '***'."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, "***")
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


# Configure console logging with secret redaction and quiet HTTP libraries.
def setup_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%H:%M:%S")
    )
    handler.addFilter(SecretRedactingFilter(secrets or []))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Keep third-party HTTP libraries quiet; they can log request details.
    for noisy in ("httpx", "httpcore", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
