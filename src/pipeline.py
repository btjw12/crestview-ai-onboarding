"""Orchestrates: risk assessment -> summary -> (optional) monday.com write-back."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .models import ClientApplication, OnboardingResult
from .monday_client import MondayClient
from .risk_assessment import RiskAssessor
from .summary_generator import SummaryGenerator, format_summary_text

logger = logging.getLogger(__name__)


# What happened to one application: status, where it failed, and the result.
@dataclass
class Outcome:
    item_id: str
    client_name: str
    status: str = "pending"  # success | failed
    stage: str | None = None  # stage at which a failure happened
    error: str | None = None
    written_back: bool = False
    result: OnboardingResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "client_name": self.client_name,
            "status": self.status,
            "failed_stage": self.stage,
            "error": self.error,
            "written_back": self.written_back,
            "result": self.result.model_dump(mode="json") if self.result else None,
        }


# Runs every application through both AI stages and optionally writes back to monday.com.
@dataclass
class OnboardingPipeline:
    assessor: RiskAssessor
    summarizer: SummaryGenerator
    model_name: str
    monday: MondayClient | None = None
    board_id: str | None = None
    dry_run: bool = True
    outcomes: list[Outcome] = field(default_factory=list)

    # Process applications one by one; a failure is recorded and the batch continues.
    def run(self, applications: list[ClientApplication]) -> list[Outcome]:
        total = len(applications)
        for index, app in enumerate(applications, 1):
            logger.info("[%d/%d] Processing '%s' (item %s)", index, total, app.client_name, app.item_id)
            self.outcomes.append(self.process(app))
        return self.outcomes

    # Process a single application; `outcome.stage` tracks where a failure happened.
    def process(self, app: ClientApplication) -> Outcome:
        outcome = Outcome(item_id=app.item_id, client_name=app.client_name)
        try:
            # Stage 1: AI compliance risk triage.
            outcome.stage = "risk_assessment"
            assessment = self.assessor.assess(app)
            logger.info("   risk: %s (%d factor(s))", assessment.risk_level.value, len(assessment.risk_factors))

            # Stage 2: AI onboarding brief, built from the application plus the stage 1 result.
            outcome.stage = "summary_generation"
            summary = self.summarizer.generate(app, assessment)
            logger.info("   complexity: %s", summary.onboarding_complexity.value)

            # Combine both stages into the record that is saved and written to monday.com.
            outcome.stage = "result_validation"
            outcome.result = OnboardingResult(
                item_id=app.item_id,
                application_id=app.application_id,
                client_name=app.client_name,
                risk_level=assessment.risk_level,
                risk_factors=assessment.risk_factors,
                risk_explanation=assessment.risk_explanation,
                missing_information=assessment.missing_information,
                recommended_due_diligence=assessment.recommended_due_diligence,
                onboarding_complexity=summary.onboarding_complexity,
                onboarding_summary=format_summary_text(summary, assessment),
                recommended_action=summary.recommended_action,
                human_review_required=assessment.human_review_required,
                review_type=assessment.review_type,
                review_reason=assessment.review_reason,
                model=self.model_name,
            )

            # Stage 3: write back to monday.com, unless this is a dry run.
            if self.dry_run:
                logger.info("   dry-run: skipping monday.com write-back")
            else:
                outcome.stage = "write_back"
                if not (self.monday and self.board_id):
                    raise RuntimeError("monday.com client and board_id are required unless dry-run")
                self.monday.update_item(self.board_id, outcome.result)
                outcome.written_back = True
                logger.info("   wrote results to monday.com item %s", app.item_id)

            outcome.status, outcome.stage = "success", None
        except Exception as exc:  # keep going with the next application
            outcome.status = "failed"
            outcome.error = f"{type(exc).__name__}: {exc}"
            logger.error("   FAILED at %s: %s", outcome.stage, outcome.error)
        return outcome
