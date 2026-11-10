"""Stage 1: AI compliance risk assessment of a client application."""

from __future__ import annotations

import json
import logging

from .llm_client import JSONCompleter, generate_structured, schema_instructions
from .models import ClientApplication, Level, RiskAssessment

logger = logging.getLogger(__name__)

# Instructions for stage 1: evidence-based risk triage, no screening claims, no approvals.
RISK_SYSTEM_PROMPT = """\
You are a compliance analyst assistant at Crestview Capital Group, a wealth management firm.
You perform a PRELIMINARY, AI-assisted KYC/AML risk triage of a client onboarding application.
Your output supports a human compliance officer; it never replaces their judgement.

Rules you must follow:
1. Use ONLY facts stated in the application. Do not invent names, jurisdictions, amounts,
   ownership details, or history. If something needed for the assessment is unknown, list it
   under missing_information instead of guessing.
2. Screening status: sanctions, PEP, adverse-media and watchlist screening is OUTSTANDING. You
   have no screening results. Never state or imply that screening was performed, completed or
   passed, that the client was cleared, or that there were no matches/hits. When screening is
   relevant, describe it as outstanding or required (e.g. "Sanctions and PEP screening is
   outstanding", "Perform sanctions screening on all directors before account opening").
3. You do not approve or reject clients. Never state that a client is approved, cleared or rejected.
4. Separate compliance risk from operational complexity. Large AUM, many requested services or
   many accounts make onboarding more complex but are NOT compliance risk on their own.
   Compliance risk indicators include, for example: high-risk or secrecy jurisdictions; opaque or
   layered ownership / unclear beneficial owners; unclear or unverifiable source of funds or
   wealth; politically exposed persons named in the application; cash-intensive businesses;
   requests for anonymity, unusual secrecy or to bypass standard checks; inconsistencies within
   the application.
5. Risk levels:
   - LOW: no compliance risk indicators in the stated facts; standard due diligence is sufficient.
   - MEDIUM: one or more indicators needing enhanced checks, OR material information gaps that
     prevent a reliable assessment.
   - HIGH: multiple or serious indicators that warrant enhanced due diligence before proceeding.
6. Every risk factor must cite evidence from the application (quote or close paraphrase).
7. risk_explanation: 2-4 sentences explaining why this level was chosen, grounded in the evidence.
8. recommended_due_diligence: concrete, proportionate checks (e.g. "Obtain certified UBO register").
9. Review routing:
   - AUTO_TRIAGE: LOW risk, no material compliance red flags,
     inconsistencies or critical documentation gaps.
     Routine outstanding screening and standard onboarding
     documentation do not automatically require manual triage.

   - DOCUMENT_REVIEW: Material missing documentation or unresolved
     information that prevents reliable verification of the client's
     identity, legal status, ownership or source of funds.

   - COMPLIANCE_REVIEW: Specific compliance concerns requiring
     specialist review.

   - ENHANCED_DUE_DILIGENCE: Serious or multiple compliance risk
     indicators requiring enhanced due diligence.

   Distinguish routine onboarding tasks from material deficiencies.
   Missing information alone does not automatically require manual
   preliminary triage.

   human_review_required = false only for AUTO_TRIAGE.
   Otherwise, human_review_required = true.

   AUTO_TRIAGE does not mean screening is complete, KYC is approved,
   or account opening is authorized.
10. review_reason:
    Explain why the selected review_type was chosen using specific
    application evidence. Keep the explanation to 1-3 sentences.
    Do not imply that screening or KYC approval is complete.
11. Source-of-funds accuracy:
    Never assume or assert a specific source of funds unless
    explicitly established by the application.
    When unverified, recommend obtaining and verifying
    supporting evidence rather than confirming an assumed source.
"""


# Stage 1: sends the application to the LLM and returns a validated RiskAssessment.
class RiskAssessor:
    def __init__(self, llm: JSONCompleter):
        self._llm = llm

    def assess(self, application: ClientApplication) -> RiskAssessment:
        user_prompt = (
            "Assess this client application.\n\n"
            f"APPLICATION:\n{json.dumps(application.for_prompt(), indent=2)}\n\n"
            f"{schema_instructions(RiskAssessment)}"
        )
        assessment = generate_structured(self._llm, RISK_SYSTEM_PROMPT, user_prompt, RiskAssessment)
        return apply_review_policy(assessment)


def apply_review_policy(assessment: RiskAssessment) -> RiskAssessment:
    """
    Enforce preliminary review routing without automatically
    escalating routine outstanding onboarding requirements.
    """

    risk = assessment.risk_level
    review_type = assessment.review_type
    original_review_type = review_type
    review_reason = assessment.review_reason

    if risk == Level.HIGH:
        review_type = "ENHANCED_DUE_DILIGENCE"

    elif risk == Level.MEDIUM:
        if review_type == "AUTO_TRIAGE":
            review_type = "COMPLIANCE_REVIEW"

    elif risk == Level.LOW:
        if review_type == "ENHANCED_DUE_DILIGENCE":
            review_type = "COMPLIANCE_REVIEW"

        # Do not escalate solely because missing_information
        # contains routine screening or onboarding tasks.
        # Claude's DOCUMENT_REVIEW or COMPLIANCE_REVIEW
        # recommendation is retained.

    human_review = review_type != "AUTO_TRIAGE"

    if review_type != original_review_type:
        logger.info(
            "Policy override: %s -> %s (risk=%s)",
            original_review_type,
            review_type,
            risk.value,
        )

        if review_type == "ENHANCED_DUE_DILIGENCE":
            review_reason = (
                "Enhanced due diligence is required because the "
                "preliminary assessment identified HIGH compliance risk. "
                + assessment.review_reason
            )

        elif review_type == "COMPLIANCE_REVIEW":
            review_reason = (
                "Manual compliance review is required under the "
                "risk-based routing policy. "
                + assessment.review_reason
            )

    return assessment.model_copy(
        update={
            "review_type": review_type,
            "review_reason": review_reason,
            "human_review_required": human_review,
        }
    )