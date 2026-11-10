import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.models import ClientApplication  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeLLM:
    """Returns queued JSON responses and records the prompts it received."""

    model = "fake-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete_json(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, str) else json.dumps(item)


@pytest.fixture
def make_session():
    def _make(*responses):
        session = MagicMock()
        session.post.side_effect = list(responses)
        return session
    return _make


@pytest.fixture
def application():
    return ClientApplication(
        item_id="111",
        client_name="Fictional Holdings Ltd",
        application_id="APP-1",
        client_type="Corporate",
        requested_services="Portfolio management",
        estimated_aum="$10,000,000",
        description="Holding company with nominee shareholders; UBOs not disclosed.",
    )


RISK_JSON = {
    "risk_level": "high",
    "risk_factors": [{"factor": "Opaque ownership", "evidence": "UBOs not disclosed", "severity": "HIGH"}],
    "risk_explanation": "Beneficial owners are not disclosed and nominees are used.",
    "missing_information": ["Ultimate beneficial owner details"],
    "recommended_due_diligence": ["Obtain certified UBO register", "Perform sanctions screening"],
    "review_type": "COMPLIANCE_REVIEW",
    "review_reason": "Undisclosed beneficial owners need specialist review.",
    "human_review_required": False,
}

SUMMARY_JSON = {
    "client_overview": "Corporate holding company seeking portfolio management.",
    "onboarding_complexity": "MEDIUM",
    "complexity_rationale": "Corporate structure with nominee shareholders.",
    "key_compliance_considerations": ["Undisclosed UBOs"],
    "missing_documentation": ["UBO register"],
    "recommended_next_steps": ["Request UBO register", "Route to compliance for EDD"],
    "recommended_action": "Route to compliance for enhanced due diligence before account opening.",
}
