import asyncio
import logging
import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from src.monday_client import MondayClient
from webhook_store import (
    initialize_db,
    claim_item,
    update_status,
    get_item_status,
)


# --------------------------------------------------
# Configuration
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

WEBHOOK_SECRET = os.getenv("MONDAY_WEBHOOK_SECRET", "")
MONDAY_API_TOKEN = os.getenv("MONDAY_API_TOKEN", "")

BOARD_ID = os.getenv("MONDAY_BOARD_ID", "5032323173")
STATUS_COLUMN_ID = "color_mm8y4j3s"
TRIGGER_STATUS = "Ready for AI Review"

if not WEBHOOK_SECRET:
    raise RuntimeError("MONDAY_WEBHOOK_SECRET is missing")

if not MONDAY_API_TOKEN:
    raise RuntimeError("MONDAY_API_TOKEN is missing")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("crestview.webhook")

app = FastAPI(title="Crestview AI Onboarding Webhook")
initialize_db()


# --------------------------------------------------
# Health check
# --------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "healthy"}


# --------------------------------------------------
# Extract monday.com status
# --------------------------------------------------

def extract_status(event: dict) -> str | None:
    value = event.get("value") or {}

    if not isinstance(value, dict):
        return None

    label = value.get("label")

    if isinstance(label, dict):
        return label.get("text")

    if isinstance(label, str):
        return label

    return None


# --------------------------------------------------
# Update monday.com processing status
# --------------------------------------------------

async def set_monday_status(item_id: str, status: str):
    def update():
        client = MondayClient(api_token=MONDAY_API_TOKEN)

        client.update_processing_status(
            board_id=BOARD_ID,
            item_id=item_id,
            status=status,
        )

    await asyncio.to_thread(update)

    logger.info(
        "monday.com status updated | item=%s | status=%s",
        item_id,
        status,
    )


# --------------------------------------------------
# AI processing pipeline
# --------------------------------------------------

async def process_application(item_id: str):
    logger.info("Starting AI processing | item=%s", item_id)

    try:
        # 1. Set monday.com status to Processing
        await set_monday_status(item_id, "Processing")

        # 2. Execute AI pipeline in LIVE mode
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(BASE_DIR / "main.py"),
            "--item-id",
            item_id,
            "--archive-drive",
            cwd=str(BASE_DIR),
        )

        return_code = await process.wait()

        if return_code != 0:
            raise RuntimeError(
                f"AI pipeline exited with code {return_code}"
            )

        # 3. Update monday.com status to Completed
        await set_monday_status(item_id, "Completed")

        # 4. Mark SQLite record as completed
        update_status(item_id, "completed")

        logger.info(
            "AI processing completed | item=%s",
            item_id,
        )

    except Exception:
        logger.exception(
            "AI processing failed | item=%s",
            item_id,
        )

        try:
            await set_monday_status(item_id, "Failed")
        except Exception:
            logger.exception(
                "Could not update failure status | item=%s",
                item_id,
            )

        update_status(item_id, "failed")


# --------------------------------------------------
# monday.com GraphQL webhook
# --------------------------------------------------

@app.post("/webhook/monday/{webhook_secret}")
async def monday_webhook(
    webhook_secret: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    # 1. Validate webhook secret
    if not secrets.compare_digest(
        webhook_secret,
        WEBHOOK_SECRET,
    ):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
        )

    # 2. Parse request
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON",
        )

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="Invalid payload",
        )

    # 3. Handle monday.com verification challenge
    if "challenge" in payload:
        return JSONResponse(
            content={"challenge": payload["challenge"]}
        )

    event = payload.get("event")

    if not isinstance(event, dict):
        raise HTTPException(
            status_code=400,
            detail="Invalid event",
        )

    # 4. Validate event type
    if event.get("type") not in (
        "update_column_value",
        "change_status_column_value",
    ):
        return {
            "status": "ignored",
            "reason": "wrong_event_type",
        }

    # 5. Validate board
    if str(event.get("boardId")) != BOARD_ID:
        return {
            "status": "ignored",
            "reason": "wrong_board",
        }

    # 6. Validate status column
    if event.get("columnId") != STATUS_COLUMN_ID:
        return {
            "status": "ignored",
            "reason": "wrong_column",
        }

    # 7. Validate trigger status
    status = extract_status(event)

    if status != TRIGGER_STATUS:
        return {
            "status": "ignored",
            "reason": "wrong_status",
        }

    # 8. Validate item ID
    item_id = str(event.get("pulseId", ""))

    if not item_id.isdigit():
        raise HTTPException(
            status_code=400,
            detail="Invalid item ID",
        )

    # 9. Duplicate protection
    if not claim_item(item_id):
        existing_status = get_item_status(item_id)

        logger.info(
            "Duplicate webhook | item=%s | status=%s",
            item_id,
            existing_status,
        )

        # Restore Completed status for previously completed items
        if existing_status == "completed":
            try:
                await set_monday_status(item_id, "Completed")

            except Exception:
                logger.exception(
                    "Failed to restore Completed | item=%s",
                    item_id,
                )

                raise HTTPException(
                    status_code=502,
                    detail="Could not restore Completed status",
                )

            return {
                "status": "duplicate",
                "item_id": item_id,
                "existing_status": "completed",
                "board_status": "Completed",
            }

        # Do not rerun processing or failed jobs
        return {
            "status": "duplicate",
            "item_id": item_id,
            "existing_status": existing_status,
        }

    # 10. Schedule processing for a NEW item
    background_tasks.add_task(
        process_application,
        item_id,
    )

    logger.info(
        "Webhook accepted | item=%s | status=%s",
        item_id,
        status,
    )

    return {
        "status": "accepted",
        "item_id": item_id,
        "mode": "live",
    }
