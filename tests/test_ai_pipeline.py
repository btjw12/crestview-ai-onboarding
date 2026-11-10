import json
from unittest.mock import MagicMock

import pytest

from src.llm_client import AIResponseError
from src.models import Level, RiskAssessment, find_guardrail_violations
from src.pipeline import OnboardingPipeline
from src.risk_assessment import RiskAssessor
from src.summary_generator import SummaryGenerator
from tests.conftest import RISK_JSON, SUMMARY_JSON, FakeLLM


def make_pipeline(llm, monday=None, dry_run=True):
    return OnboardingPipeline(RiskAssessor(llm), SummaryGenerator(llm), "fake-model",
                              monday=monday, board_id="123", dry_run=dry_run)


def test_risk_assessment_is_validated_and_normalised(application):
    assessment = RiskAssessor(FakeLLM([RISK_JSON])).assess(application)
    assert assessment.risk_level is Level.HIGH  # "high" -> HIGH
    assert assessment.risk_factors[0].evidence == "UBOs not disclosed"


def test_policy_forces_human_review_for_high_risk(application):
    assessment = RiskAssessor(FakeLLM([RISK_JSON])).assess(application)
    assert RISK_JSON["human_review_required"] is False
    assert assessment.human_review_required is True


def test_low_risk_without_gaps_does_not_force_review(application):
    low = {**RISK_JSON, "risk_level": "LOW", "risk_factors": [], "missing_information": [],
           "review_type": "AUTO_TRIAGE"}
    assessment = RiskAssessor(FakeLLM([low])).assess(application)
    assert assessment.human_review_required is False


def test_low_risk_missing_information_alone_does_not_escalate(application):
    low = {**RISK_JSON, "risk_level": "LOW", "risk_factors": [], "review_type": "AUTO_TRIAGE",
           "missing_information": ["Sanctions and PEP screening outstanding"]}
    assessment = RiskAssessor(FakeLLM([low])).assess(application)
    assert assessment.review_type == "AUTO_TRIAGE"
    assert assessment.human_review_required is False


def test_low_risk_document_review_from_model_is_kept(application):
    low = {**RISK_JSON, "risk_level": "LOW", "risk_factors": [], "review_type": "DOCUMENT_REVIEW",
           "missing_information": ["Source of wealth"]}
    assessment = RiskAssessor(FakeLLM([low])).assess(application)
    assert assessment.review_type == "DOCUMENT_REVIEW"
    assert assessment.review_reason == RISK_JSON["review_reason"]  # no override, no prefix
    assert assessment.human_review_required is True


def test_medium_risk_is_never_auto_triaged(application):
    medium = {**RISK_JSON, "risk_level": "MEDIUM", "review_type": "AUTO_TRIAGE"}
    assessment = RiskAssessor(FakeLLM([medium])).assess(application)
    assert assessment.review_type == "COMPLIANCE_REVIEW"
    assert assessment.review_reason.startswith("Manual compliance review is required")
    assert assessment.human_review_required is True


def test_low_risk_enhanced_due_diligence_is_downgraded(application):
    low = {**RISK_JSON, "risk_level": "LOW", "review_type": "ENHANCED_DUE_DILIGENCE"}
    assessment = RiskAssessor(FakeLLM([low])).assess(application)
    assert assessment.review_type == "COMPLIANCE_REVIEW"
    assert assessment.human_review_required is True


def test_invalid_ai_output_is_retried_with_feedback(application):
    llm = FakeLLM(["not json", {"risk_level": "EXTREME"}, RISK_JSON])
    with pytest.raises(AIResponseError):
        RiskAssessor(llm).assess(application)  # 2 attempts both invalid
    llm = FakeLLM([{"risk_level": "EXTREME"}, RISK_JSON])
    assert RiskAssessor(llm).assess(application).risk_level is Level.HIGH
    assert "rejected because" in llm.calls[1][1]


@pytest.mark.parametrize("text", [
    "Sanctions screening was performed and returned no matches.",
    "The client passed OFAC checks.",
    "The client has been approved for onboarding.",
])
def test_guardrails_reject_forbidden_claims(text):
    assert find_guardrail_violations(text)
    with pytest.raises(ValueError, match="Guardrail"):
        RiskAssessment(**{**RISK_JSON, "risk_explanation": text})


def test_guardrails_allow_recommending_screening():
    assert not find_guardrail_violations("Perform sanctions screening before account opening.")
    assert not find_guardrail_violations("Sanctions screening has not been performed.")


def test_summary_prompt_includes_risk_assessment(application):
    llm = FakeLLM([RISK_JSON, SUMMARY_JSON])
    outcome = make_pipeline(llm).process(application)
    summary_prompt = llm.calls[1][1]
    assert "COMPLETED RISK ASSESSMENT" in summary_prompt
    assert "Opaque ownership" in summary_prompt
    assert outcome.status == "success"
    assert "Compliance risk: High" in outcome.result.onboarding_summary


def test_missing_fields_are_explicit_in_prompt(application):
    app = application.model_copy(update={"estimated_aum": None})
    llm = FakeLLM([RISK_JSON, SUMMARY_JSON])
    make_pipeline(llm).process(app)
    sent = json.loads(llm.calls[0][1].split("APPLICATION:\n")[1].split("\n\n")[0])
    assert sent["estimated_aum"] == "NOT PROVIDED"
    assert "item_id" not in sent


def test_dry_run_never_writes(application):
    monday = MagicMock()
    outcome = make_pipeline(FakeLLM([RISK_JSON, SUMMARY_JSON]), monday, dry_run=True).process(application)
    monday.update_item.assert_not_called()
    assert outcome.written_back is False


def test_write_back_when_not_dry_run(application):
    monday = MagicMock()
    outcome = make_pipeline(FakeLLM([RISK_JSON, SUMMARY_JSON]), monday, dry_run=False).process(application)
    monday.update_item.assert_called_once()
    board_id, result = monday.update_item.call_args.args
    assert board_id == "123" and result.item_id == "111"
    assert outcome.written_back is True


def test_one_failure_does_not_stop_the_batch(application):
    second = application.model_copy(update={"item_id": "222", "client_name": "Second"})
    llm = FakeLLM([RuntimeError("LLM API down"), RISK_JSON, SUMMARY_JSON])
    outcomes = make_pipeline(llm).run([application, second])
    assert [o.status for o in outcomes] == ["failed", "success"]
    assert outcomes[0].stage == "risk_assessment"
    assert "LLM API down" in outcomes[0].error


def test_write_back_failure_is_recorded(application):
    monday = MagicMock()
    monday.update_item.side_effect = RuntimeError("monday down")
    outcome = make_pipeline(FakeLLM([RISK_JSON, SUMMARY_JSON]), monday, dry_run=False).process(application)
    assert outcome.status == "failed" and outcome.stage == "write_back"
    assert outcome.result is not None  # AI result still saved to JSON
