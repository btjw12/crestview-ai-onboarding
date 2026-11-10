"""Stage 2: onboarding summary, built from the application AND the risk assessment."""

from __future__ import annotations

import json

from .llm_client import JSONCompleter, generate_structured, schema_instructions
from .models import ClientApplication, OnboardingSummary, RiskAssessment

# Instructions for stage 2: an onboarding brief that must not contradict the risk assessment.
SUMMARY_SYSTEM_PROMPT = """\
You are an onboarding coordinator assistant at Crestview Capital Group, a wealth management firm.
You write a concise onboarding brief for the onboarding team, based on a client application and
a completed preliminary compliance risk assessment.

Rules you must follow:
1. Use ONLY facts from the application and the risk assessment. Do not invent details.
2. The risk assessment is authoritative: do not change or contradict its risk level. Your
   key_compliance_considerations must reflect its risk factors and recommended due diligence.
3. Screening status: sanctions, PEP, adverse-media and watchlist screening is OUTSTANDING.
   Screening results have not been provided or verified for this application.
   Never state or imply that screening was performed, completed, passed, or returned no matches.
   Never claim that "no results exist", as the absence of provided results does not prove
   that screening has never been performed.
   Describe screening only as outstanding or as a required next step.
   Screening must be completed and verified before account opening.
4. Never approve or reject the client. The recommended_action is the next operational step for
   the onboarding team (e.g. "Route to compliance for enhanced due diligence before account
   opening"), not a compliance decision.
5. onboarding_complexity reflects operational effort (number of services, entity structure,
   documentation burden, AUM size), which is separate from compliance risk.
6. client_overview: 2-3 sentences. recommended_next_steps: ordered, concrete actions.
7. Missing_documentation: documents to request, consistent with the assessment's missing information.
8. Missing documentation:
   List only documents or information that must be obtained
   from the client or retrieved from existing internal records.
   Do not list outstanding sanctions, PEP, adverse-media or
   watchlist screening results as missing client documentation.
   Include screening requirements under key_compliance_considerations
   and recommended_next_steps instead.
"""


# Stage 2: sends the application plus the risk assessment to the LLM.
class SummaryGenerator:
    def __init__(self, llm: JSONCompleter):
        self._llm = llm

    def generate(self, application: ClientApplication, assessment: RiskAssessment) -> OnboardingSummary:
        user_prompt = (
            "Write the onboarding brief.\n\n"
            f"APPLICATION:\n{json.dumps(application.for_prompt(), indent=2)}\n\n"
            f"COMPLETED RISK ASSESSMENT:\n{assessment.model_dump_json(indent=2)}\n\n"
            f"{schema_instructions(OnboardingSummary)}"
        )
        return generate_structured(self._llm, SUMMARY_SYSTEM_PROMPT, user_prompt, OnboardingSummary)


def format_summary_text(summary: OnboardingSummary, assessment: RiskAssessment) -> str:
    """Render the summary as plain text for the monday.com long-text column."""
    lines = [
        summary.client_overview,
        "",
        f"Compliance risk: {assessment.risk_level.monday_label}",
        f"Onboarding complexity: {summary.onboarding_complexity.monday_label} - "
        f"{summary.complexity_rationale}",
    ]

    def section(title: str, items: list[str], numbered: bool = False) -> None:
        if items:
            lines.extend(["", f"{title}:"])
            lines.extend(f"{i}. {x}" if numbered else f"- {x}" for i, x in enumerate(items, 1))

    section("Key compliance considerations", summary.key_compliance_considerations)
    section("Missing documentation", summary.missing_documentation)
    section("Recommended next steps", summary.recommended_next_steps, numbered=True)
    return "\n".join(lines)
