"""
Crestview Capital Group - AI-powered client onboarding.

COMMAND REFERENCE
=================

All commands use the project's virtual environment:
.venv/bin/python


1. MONDAY.COM CONNECTION
------------------------
# Verify monday.com API connection and column mapping
.venv/bin/python main.py --check-connection

# Check connection and retrieve up to 5 applications
.venv/bin/python main.py --check-connection --limit 5


2. STANDARD PROCESSING (WITH MONDAY.COM WRITE-BACK)
---------------------------------------------------
# Full run: process all retrieved applications
.venv/bin/python main.py

# Process a maximum of 3 applications
.venv/bin/python main.py --limit 3

# Process one specific application
.venv/bin/python main.py --application-id APP-2026-0303


3. DRY-RUN MODE (NO MONDAY.COM WRITE-BACK)
------------------------------------------
# Process all applications without updating monday.com
.venv/bin/python main.py --dry-run

# Process up to 3 applications without updating monday.com
.venv/bin/python main.py --dry-run --limit 3

# Process only 1 application in dry-run mode
.venv/bin/python main.py --dry-run --limit 1

# Dry-run a specific application
.venv/bin/python main.py --application-id APP-2026-0303 --dry-run


4. GOOGLE DRIVE JSON ARCHIVING
------------------------------
# Full processing with monday.com write-back and Drive archiving
.venv/bin/python main.py --archive-drive

# Process up to 3 applications and archive results
.venv/bin/python main.py --limit 3 --archive-drive

# Dry-run 1 application and archive results
.venv/bin/python main.py --dry-run --limit 1 --archive-drive

# Dry-run 3 applications and archive results
.venv/bin/python main.py --dry-run --limit 3 --archive-drive

# Process a specific application and archive results
.venv/bin/python main.py --application-id APP-2026-0303 --archive-drive

# Dry-run a specific application and archive results
.venv/bin/python main.py --application-id APP-2026-0303 --dry-run --archive-drive


5. OFFLINE TESTING (SAMPLE DATA)
--------------------------------
# Process fictional applications without accessing monday.com
.venv/bin/python main.py --sample-file sample_data/applications.json

# Process up to 3 fictional applications
.venv/bin/python main.py --sample-file sample_data/applications.json --limit 3

# Process fictional applications and archive results to Drive
.venv/bin/python main.py --sample-file sample_data/applications.json --archive-drive

# Process a specific fictional application
.venv/bin/python main.py --sample-file sample_data/applications.json --application-id APP-2026-0303


6. OUTPUT CONFIGURATION & DEBUGGING
-----------------------------------
# Save JSON results to a custom local directory
.venv/bin/python main.py --output-dir custom_output

# Enable detailed debug logging
.venv/bin/python main.py --verbose

# Dry-run with detailed logging
.venv/bin/python main.py --dry-run --limit 1 --verbose

# Dry-run with Google Drive archiving and debug logging
.venv/bin/python main.py --dry-run --limit 1 --archive-drive --verbose


7. GOOGLE DRIVE CONNECTION & UPLOAD TESTS
-----------------------------------------
# Verify Google Drive OAuth authentication
.venv/bin/python google_drive.py

# Upload a sample JSON file to Google Drive
.venv/bin/python test_drive_upload.py

# Verify Google OAuth dependency
.venv/bin/python -c "from google_auth_oauthlib.flow import InstalledAppFlow; print('Google OAuth OK')"

# Verify Google Drive folder configuration
.venv/bin/python -c "from dotenv import load_dotenv; import os; load_dotenv('.env'); print('Folder configured:', bool(os.getenv('GOOGLE_DRIVE_FOLDER_ID')))"


8. AUTOMATED TESTING (PYTEST)
-----------------------------
# Run the complete automated test suite
.venv/bin/python -m pytest

# Run all tests with detailed output
.venv/bin/python -m pytest -v

# Run AI pipeline tests
.venv/bin/python -m pytest src/tests/test_ai_pipeline.py -v

# Run monday.com client tests
.venv/bin/python -m pytest src/tests/test_monday_client.py -v

# Run guardrail tests
.venv/bin/python -m pytest src/tests/test_guardrails.py -v

# Run configuration tests
.venv/bin/python -m pytest src/tests/test_config_and_models.py -v


9. PYTHON ENVIRONMENT
---------------------
# Check the Python interpreter
.venv/bin/python --version

# Check installed packages
.venv/bin/python -m pip list

# Install Google Drive dependencies
.venv/bin/python -m pip install google-api-python-client google-auth-oauthlib google-auth-httplib2

# Install python-dotenv
.venv/bin/python -m pip install python-dotenv

# Verify main.py syntax without running the pipeline
.venv/bin/python -m py_compile main.py


IMPORTANT NOTES
---------------
- Run all commands from the crestview-ai-onboarding project root.
- Use .venv/bin/python consistently on macOS.
- --dry-run prevents monday.com write-back, not Google Drive uploads.
- --archive-drive enables Google Drive JSON uploads.
- Without --archive-drive, results are saved locally only.
- --sample-file automatically enables dry-run mode.
- --application-id processes the specified application ID.
- Replace example application IDs with actual application IDs.
- GOOGLE_DRIVE_FOLDER_ID must be configured in .env.
- Google Drive OAuth credentials are stored locally.
- JSON results are always saved locally after pipeline execution.
- Google Drive archiving is optional and must be explicitly enabled.
- The standalone Drive upload test creates a real file in Google Drive.
- Keep real API upload tests out of automatic pytest collection.
"""



from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.config import ConfigError, load_settings, setup_logging
from src.llm_client import ClaudeCompleter
from src.models import ClientApplication
from src.monday_client import MondayAPIError, MondayClient
from src.pipeline import OnboardingPipeline, Outcome
from src.risk_assessment import RiskAssessor
from src.summary_generator import SummaryGenerator

import os
from google_drive import upload_json

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("crestview")


# Read command-line flags (--dry-run, --limit, --sample-file, ...).
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI-powered client onboarding for monday.com")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate results without writing to monday.com")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N applications")
    parser.add_argument("--application-id",type=str,default=None,
                        help="Process only the specified application ID (e.g. APP-2026-0303)",)
    parser.add_argument("--item-id",type=str,default=None,
                        help="Process only the specified Monday.com item ID",
)
    parser.add_argument("--output-dir", default="output", help="Where to save JSON results")
    parser.add_argument("--check-connection", action="store_true",
                        help="Only test monday.com connectivity and column mapping")
    parser.add_argument("--sample-file", type=Path, default=None,
                        help="Read applications from a local JSON file instead of monday.com "
                             "(implies --dry-run)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    parser.add_argument("--archive-drive", action="store_true", help="Archive the generated JSON results to Google Drive",)
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer")
    if args.sample_file:  # local sample data is never written back to monday.com
        args.dry_run = True
    return args


# Load fictional applications from a local JSON file (offline demo).
def load_sample_applications(path: Path, limit: int | None) -> list[ClientApplication]:
    records = json.loads(path.read_text())
    apps = [ClientApplication(**r) for r in records]
    return apps[:limit] if limit else apps


# Write every outcome (success or failure) to a timestamped JSON file in output/.
def save_results(outcomes: list[Outcome], args: argparse.Namespace, board_id: str, model: str) -> Path:
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    path = out_dir / f"onboarding_results_{now:%Y%m%d_%H%M%S}.json"
    payload = {
        "run_at": now.isoformat(),
        "board_id": board_id,
        "source": str(args.sample_file) if args.sample_file else "monday.com",
        "dry_run": args.dry_run,
        "model": model,
        "processed": len(outcomes),
        "succeeded": sum(o.status == "success" for o in outcomes),
        "failed": sum(o.status == "failed" for o in outcomes),
        "results": [o.to_dict() for o in outcomes],
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


# Archive local JSON results to Google Drive.
def archive_results_to_drive(path: Path) -> bool:
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

    if not folder_id:
        logger.error("GOOGLE_DRIVE_FOLDER_ID is not configured")
        return False

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))

        result = upload_json(
            data=payload,
            filename=path.name,
            folder_id=folder_id,
        )

        logger.info(
            "Google Drive archive successful | file=%s | id=%s",
            result["name"],
            result["id"],
        )
        return True

    except Exception:
        logger.exception("Google Drive archive failed")
        return False

# Print a one-line-per-client results table to the terminal.
def print_summary(outcomes: list[Outcome]) -> None:
    print("\n" + "=" * 92)
    print(f"{'Client':<34} {'Risk':<8} {'Complexity':<11} {'Review':<7} {'Status':<9} Written")
    print("-" * 92)
    for o in outcomes:
        r = o.result
        print(f"{o.client_name[:33]:<34} "
              f"{(r.risk_level.value if r else '-'):<8} "
              f"{(r.onboarding_complexity.value if r else '-'):<11} "
              f"{(('Yes' if r.human_review_required else 'No') if r else '-'):<7} "
              f"{o.status:<9} {'yes' if o.written_back else 'no'}")
    print("=" * 92)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Load API keys/settings from .env; exit code 2 means configuration is missing.
    use_monday = args.sample_file is None
    try:
        settings = load_settings(require_monday=use_monday, require_llm=not args.check_connection)
    except ConfigError as exc:
        setup_logging()
        logger.error("%s", exc)
        return 2

    # Logging masks API keys if they ever appear in a log message.
    setup_logging("DEBUG" if args.verbose else settings.log_level, settings.secrets)
    logger.info("Crestview AI Onboarding | board=%s | model=%s | dry_run=%s | limit=%s",
                settings.board_id, settings.model, args.dry_run, args.limit)

    # Step 1: get the applications, from monday.com or from the sample file.
    monday = None
    try:
        if use_monday:
            monday = MondayClient(settings.monday_api_token, api_url=settings.monday_api_url,
                                  api_version=settings.monday_api_version)
            if args.check_connection:  # just show a few items and stop, no AI calls
                apps = monday.fetch_applications(settings.board_id, limit=args.limit or 5)
                for app in apps:
                    logger.info("  item %s: %s", app.item_id, app.model_dump(exclude={"item_id"}))
                logger.info("Connection check OK")
                return 0
            applications = monday.fetch_applications(settings.board_id,
                                                     limit=None if (args.application_id or args.item_id) else args.limit,)

        else:
            applications = load_sample_applications(
                args.sample_file,
                None if args.application_id else args.limit,
)
            logger.info("Loaded %d application(s) from %s", len(applications), args.sample_file)
    except MondayAPIError as exc:
        logger.error("Could not read from monday.com: %s", exc)
        return 1
    
    if args.item_id:
        applications = [
            app for app in applications
            if str(app.item_id) == str(args.item_id)
    ]

    if not applications:
        logger.error(
            "Monday.com item %s not found",
            args.item_id,
        )
        return 1
    
    if args.application_id:
        applications = [
            app for app in applications
            if app.application_id == args.application_id
        ]

        if not applications:
            logger.error(
                "Application %s not found",
                args.application_id,
            )
            return 1
    
    if not applications:
        logger.warning("No applications to process")
        return 0

    # Step 2: run each application through the AI pipeline (risk assessment, then summary).
    llm = ClaudeCompleter(settings.anthropic_api_key, settings.model)
    pipeline = OnboardingPipeline(
        assessor=RiskAssessor(llm),
        summarizer=SummaryGenerator(llm),
        model_name=settings.model,
        monday=monday,
        board_id=settings.board_id,
        dry_run=args.dry_run,
    )
    outcomes = pipeline.run(applications)

    
    # Step 3: Save results locally.
    path = save_results(
        outcomes, args, settings.board_id, settings.model
    )

    print_summary(outcomes)

    failed = sum(o.status == "failed" for o in outcomes)

    logger.info(
        "Saved results to %s | %d succeeded, %d failed",
        path,
        len(outcomes) - failed,
        failed,
    )

    # Step 4: Optionally archive JSON results to Google Drive.
    archive_failed = False

    if args.archive_drive:
        logger.info("Starting Google Drive JSON archiving")

        archive_success = archive_results_to_drive(path)
        archive_failed = not archive_success

    return 1 if failed or archive_failed else 0



if __name__ == "__main__":
    sys.exit(main())
