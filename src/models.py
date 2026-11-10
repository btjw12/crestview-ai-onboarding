"""Pydantic models for client applications and validated AI output."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# LOW / MEDIUM / HIGH, used for both risk level and onboarding complexity.
class Level(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

    @property
    def monday_label(self) -> str:
        """Label used by the monday.com status column (Low / Medium / High)."""
        return self.value.capitalize()


# Accept "high", " High " etc. from the model and turn it into "HIGH".
def _normalise_level(value: Any) -> Any:
    return value.strip().upper() if isinstance(value, str) else value


# --------------------------------------------------------------------------
# Guardrails: claims the AI must never make. Checked during validation so that
# a violating response is rejected and retried, never written to monday.com.
#
# No screening is ever performed by this tool, so any statement that screening
# was done, passed, or produced results is fabricated. Statements that screening
# is required, pending, outstanding or not performed are legitimate and allowed.
#
# Matching is sentence-by-sentence and per output field. A rule only fires on an
# affirmative claim: negated subjects ("No sanctions screening has been
# performed") and instructions ("Ensure PEP screening is completed") are exempt.
# Exemptions for past-tense claims are deliberately narrow (negation only), so
# "Sanctions screening was completed" can never be excused by surrounding text.
# --------------------------------------------------------------------------
_SUBJECT = r"(?:sanctions?|pep|politically\s+exposed|watch\s?lists?|ofac|adverse[\s-]media)"
_SCREEN = r"(?:screenings?|checks?|search(?:es)?)"

# Sentence starts with an instruction to someone else (imperative), e.g. "Confirm that ...".
_IMPERATIVE_START = re.compile(
    r"^\W*(?:please\s+)?(?:ensure|confirm|verify|check|determine|establish|obtain|request|perform|"
    r"complete|conduct|run|carry\s+out|arrange|initiate)\b",
    re.IGNORECASE,
)
# Words that make a present-tense "is completed" conditional or required rather than a claim.
_CONDITIONAL_CONTEXT = re.compile(
    r"\b(?:until|before|once|when|whenever|if|unless|whether|after|pending|must|should|shall|"
    r"needs?|required?|requires|recommend\w*|so\s+that|ensure|confirm|verify)\b",
    re.IGNORECASE,
)
_NEGATED_SUBJECT = re.compile(r"\b(?:no|without|neither|nor|not)\s+(?:\w+\s+){0,2}$", re.IGNORECASE)
_NEGATED_VERB = re.compile(r"(?:\b(?:not|never|yet\s+to|failed\s+to)|n't)(?:\s+\w+){0,2}\s*$",
                           re.IGNORECASE)


# One guardrail: a regex, the reason shown on failure, and a function that decides exceptions.
class _Rule:
    def __init__(self, pattern: str, reason: str, exempt):
        self.pattern = re.compile(pattern, re.IGNORECASE)
        self.reason = reason
        self.exempt = exempt  # (sentence, match) -> bool


# "Screening was/is performed" is allowed only when negated or phrased as an instruction.
def _performed_exempt(sentence: str, m: re.Match) -> bool:
    prefix = sentence[: m.start()]
    if _NEGATED_SUBJECT.search(prefix):          # "No sanctions screening has been performed"
        return True
    if m.group("tense").lower() in ("is", "are"):  # "Ensure screening is completed before ..."
        return bool(_IMPERATIVE_START.search(sentence) or _CONDITIONAL_CONTEXT.search(sentence))
    return bool(_IMPERATIVE_START.search(sentence))  # "Confirm that screening was performed"


# "Passed/cleared screening" is allowed only when negated ("has not yet cleared") or an instruction.
def _passed_exempt(sentence: str, m: re.Match) -> bool:
    prefix = sentence[: m.start()]
    return bool(_NEGATED_VERB.search(prefix) or _IMPERATIVE_START.search(sentence))


# Claims about screening results are allowed only inside an instruction.
def _results_exempt(sentence: str, m: re.Match) -> bool:
    return bool(_IMPERATIVE_START.search(sentence))


# Approval/rejection statements are never allowed.
def _never_exempt(sentence: str, m: re.Match) -> bool:
    return False


_SCREENING_PERFORMED = "claims that sanctions/PEP/watchlist screening was performed"
_SCREENING_PASSED = "claims the client passed or was cleared by screening"
_SCREENING_RESULTS = "claims sanctions/PEP/watchlist screening produced results"

_RULES = [
    _Rule(
        rf"\b{_SUBJECT}\b[\w\s,/-]{{0,30}}?\b{_SCREEN}\b[\w\s]{{0,15}}?"
        r"\b(?P<tense>was|were|has\s+been|have\s+been|is|are)\s+(?:successfully\s+|already\s+|now\s+)?"
        r"(?:performed|completed|conducted|run|done|carried\s+out|undertaken|passed|clear(?:ed)?|"
        r"negative|clean|satisfactory|successful)\b",
        _SCREENING_PERFORMED, _performed_exempt,
    ),
    _Rule(
        rf"\b(?:passed|passes|cleared|clears|clear\s+of)\b[\w\s,/-]{{0,30}}?\b{_SUBJECT}",
        _SCREENING_PASSED, _passed_exempt,
    ),
    _Rule(
        rf"\bno\s+(?:\w+\s+){{0,2}}?{_SUBJECT}[\w\s/-]{{0,25}}?\b(?:matches|hits|alerts|findings)\b",
        _SCREENING_RESULTS, _results_exempt,
    ),
    _Rule(
        rf"\b{_SUBJECT}\b[\w\s,/-]{{0,30}}?\b{_SCREEN}\b[\w\s]{{0,15}}?"
        r"\b(?:returned|revealed|showed|produced|found|identified|yielded|confirmed)\b",
        _SCREENING_RESULTS, _results_exempt,
    ),
    _Rule(
        r"\b(?:client|application|applicant|onboarding)\s+(?:is|has\s+been)\s+(?:approved|rejected|cleared)\b",
        "makes a final compliance approval/rejection decision", _never_exempt,
    ),
]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")


# One guardrail hit: why it failed and the exact matched words.
class GuardrailMatch(BaseModel):
    reason: str
    fragment: str  # only the matched span, truncated; never the whole field
    field: str = ""


# Check text sentence by sentence and return every rule it breaks.
def find_guardrail_matches(text: str) -> list[GuardrailMatch]:
    found = []
    for sentence in _SENTENCE_SPLIT.split(text):
        for rule in _RULES:
            for m in rule.pattern.finditer(sentence):
                if not rule.exempt(sentence, m):
                    found.append(GuardrailMatch(reason=rule.reason, fragment=m.group(0)[:80]))
                    break
    return found


# Same as above, but returns only the reasons (used by tests).
def find_guardrail_violations(text: str) -> list[str]:
    return [m.reason for m in find_guardrail_matches(text)]


def _iter_fields(value: Any, path: str = ""):
    """Yield (field_path, text) for every string in a dumped model."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _iter_fields(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            yield from _iter_fields(v, f"{path}[{i}]")


class GuardedModel(BaseModel):
    """Base for AI output models: rejects text that breaks compliance guardrails."""

    @model_validator(mode="after")
    def _check_guardrails(self):
        problems = []
        for path, text in _iter_fields(self.model_dump(mode="json")):
            for m in find_guardrail_matches(text):
                problems.append(f"{path}: {m.reason} (matched: \"{m.fragment}\")")
        if problems:
            # Surfaces in the retry prompt and the warning log; contains only the field
            # name and the matched span, not the full text or any credentials.
            raise ValueError("Guardrail violation: " + "; ".join(problems))
        return self


# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------
class ClientApplication(BaseModel):
    """A client application read from monday.com."""

    item_id: str = Field(description="monday.com item ID (used for write-back)")
    client_name: str
    application_id: str | None = None
    client_type: str | None = None
    requested_services: str | None = None
    estimated_aum: str | None = None
    description: str | None = None

    def for_prompt(self) -> dict[str, str]:
        """Application fields as shown to the LLM; blanks are made explicit."""
        fields = self.model_dump(exclude={"item_id"})
        return {k: (v if v not in (None, "") else "NOT PROVIDED") for k, v in fields.items()}


# --------------------------------------------------------------------------
# Stage 1: risk assessment (LLM output)
# --------------------------------------------------------------------------
# One risk factor with its supporting evidence from the application.
class RiskFactor(BaseModel):
    factor: str = Field(min_length=1, description="Short name of the compliance risk factor")
    evidence: str = Field(min_length=1, description="Facts from the application supporting it")
    severity: Level

    @field_validator("severity", mode="before")
    @classmethod
    def _upper_level(cls, v: Any) -> Any:
        return _normalise_level(v)


# Stage 1 output; guardrails run automatically when it is created.

class RiskAssessment(GuardedModel):
    risk_level: Level
    risk_factors: list[RiskFactor] = Field(default_factory=list)
    risk_explanation: str = Field(min_length=1)
    missing_information: list[str] = Field(default_factory=list)
    recommended_due_diligence: list[str] = Field(default_factory=list)

    review_type: Literal[
        "AUTO_TRIAGE",
        "DOCUMENT_REVIEW",
        "COMPLIANCE_REVIEW",
        "ENHANCED_DUE_DILIGENCE",
    ]
    review_reason: str = Field(
    min_length=1,
    description=(
        "Explain in 1-3 sentences why the selected review_type was chosen. "
        "Reference evidence from the application. "
        "For AUTO_TRIAGE, explain why preliminary manual triage is unnecessary. "
        "For DOCUMENT_REVIEW, identify material missing documentation. "
        "For COMPLIANCE_REVIEW, identify specific compliance concerns. "
        "For ENHANCED_DUE_DILIGENCE, identify serious risk indicators. "
        "Do not imply that screening or KYC approval is complete."
    ),
)
    human_review_required: bool

    @field_validator("risk_level", mode="before")
    @classmethod
    def _upper_level(cls, v: Any) -> Any:
        return _normalise_level(v)


# --------------------------------------------------------------------------
# Stage 2: onboarding summary (LLM output)
# --------------------------------------------------------------------------
# Stage 2 output; guardrails run automatically when it is created.
class OnboardingSummary(GuardedModel):
    client_overview: str = Field(min_length=1)
    onboarding_complexity: Level
    complexity_rationale: str = Field(min_length=1)
    key_compliance_considerations: list[str] = Field(default_factory=list)
    missing_documentation: list[str] = Field(default_factory=list)
    recommended_next_steps: list[str] = Field(default_factory=list)
    recommended_action: str = Field(min_length=1)

    @field_validator("onboarding_complexity", mode="before")
    @classmethod
    def _upper_level(cls, v: Any) -> Any:
        return _normalise_level(v)


# --------------------------------------------------------------------------
# Final combined result (what we save to JSON and write to monday.com)
# --------------------------------------------------------------------------
# Final combined record, saved to JSON and written to monday.com.
class OnboardingResult(BaseModel):
    item_id: str
    application_id: str | None
    client_name: str
    risk_level: Level
    risk_factors: list[RiskFactor]
    risk_explanation: str
    missing_information: list[str]
    recommended_due_diligence: list[str]
    onboarding_complexity: Level
    onboarding_summary: str
    recommended_action: str
    human_review_required: bool
    review_type: str
    review_reason: str
    model: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
