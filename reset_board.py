"""Reset AI-generated onboarding results in monday.com.

Usage:
    python reset_board.py            # Preview only: list items, change nothing
    python reset_board.py --execute  # Clear the AI output columns on every item
"""

import argparse
import logging
import json

from src.config import load_settings, OUTPUT_COLUMNS
from src.monday_client import MondayClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reset")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually clear the AI output columns",
    )
    args = parser.parse_args()

    settings = load_settings(
        require_monday=True,
        require_llm=False,
    )

    monday = MondayClient(
        settings.monday_api_token,
        api_url=settings.monday_api_url,
        api_version=settings.monday_api_version,
    )

    applications = monday.fetch_applications(
        settings.board_id,
        limit=None,
    )

    # Clear only the AI-generated output columns.
    column_values = dict.fromkeys(OUTPUT_COLUMNS.values())

    logger.info(
        "Found %d applications; %d output columns per item",
        len(applications),
        len(column_values),
    )

    for app in applications:
        logger.info(
            "%s | item_id=%s",
            app.client_name,
            app.item_id,
        )

        if not args.execute:
            continue

        mutation = """
        mutation (
            $boardId: ID!,
            $itemId: ID!,
            $values: JSON!
        ) {
            change_multiple_column_values(
                board_id: $boardId,
                item_id: $itemId,
                column_values: $values
            ) {
                id
            }
        }
        """

        monday.execute(
            mutation,
            {
                "boardId": str(settings.board_id),
                "itemId": str(app.item_id),
                "values": json.dumps(column_values),
            },
        )

        logger.info("Cleared AI results for %s", app.item_id)

    if not args.execute:
        logger.info(
            "PREVIEW ONLY: no changes made. "
            "Use --execute to clear the output columns."
        )


if __name__ == "__main__":
    main()