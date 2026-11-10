import pytest

from src.models import OnboardingSummary, RiskAssessment, find_guardrail_matches, find_guardrail_violations
from tests.conftest import RISK_JSON, SUMMARY_JSON

SHOULD_PASS = [
    # Required by the spec
    "Sanctions screening is required.",
    "PEP screening has not been completed.",
    "No sanctions screening results were provided.",
    "The client requires additional watchlist screening.",
    "Sanctions screening status remains unverified.",
    # Phrasings the model produces that the old regex rejected
    "No sanctions, PEP or watchlist screening has been performed.",
    "Ensure sanctions screening is performed on all directors.",
    "Confirm that PEP screening is completed before account opening.",
    "Do not open the account until sanctions screening is completed.",
    "Sanctions and PEP screening must be completed before onboarding.",
    "Sanctions screening is pending.",
    "The client has not yet cleared sanctions screening.",
    "Perform sanctions screening before account opening.",
    "Sanctions screening has not been performed.",
    "No red flags were identified in the stated source of funds.",
]

SHOULD_FAIL = [
    # Required by the spec
    "The clientsanct passed ions screening.",
    "PEP screening was completed successfully.",
    "The client was cleared against all watchlists.",
    "No sanctions matches were found.",
    # Fabricated results must not slip through via surrounding wording
    "Sanctions screening was performed and returned no matches.",
    "The client passed OFAC checks.",
    "Sanctions screening returned no matches.",
    "PEP checks have been completed.",
    "Although no UBO register was provided, sanctions screening was completed.",
    "Sanctions screening is completed; ensure the UBO register is obtained.",
    "The director is clear of sanctions.",
    "No PEP hits were identified.",
    "The client has been approved for onboarding.",
]


@pytest.mark.parametrize("text", SHOULD_PASS)
def test_legitimate_screening_statements_pass(text):
    assert find_guardrail_violations(text) == []


@pytest.mark.parametrize("text", SHOULD_FAIL)
def test_fabricated_screening_claims_fail(text):
    assert find_guardrail_violations(text)


def test_each_sentence_is_judged_on_its_own():
    # A negation in one sentence must not excuse a claim in the next one.
    text = "No sanctions screening results were provided. PEP screening was completed."
    [match] = find_guardrail_matches(text)
    assert match.fragment == "PEP screening was completed"


def test_error_names_field_and_fragment_only():
    bad = {**SUMMARY_JSON, "recommended_next_steps": ["Request UBO register",
                                                      "Note: the client passed sanctions screening."]}
    with pytest.raises(ValueError) as exc:
        OnboardingSummary(**bad)
    msg = str(exc.value)
    assert "recommended_next_steps[1]" in msg
    assert '"passed sanctions"' in msg
    assert "Request UBO register" not in msg  # unrelated text is not echoed


def test_matches_are_not_joined_across_fields():
    # Old code concatenated all fields, so text could match across field boundaries.
    ok = {**RISK_JSON, "missing_information": ["Results of sanctions"],
          "recommended_due_diligence": ["screening was completed by the introducing bank (unverified claim)"]}
    RiskAssessment(**ok)
