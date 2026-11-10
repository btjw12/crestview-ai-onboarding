
"""monday.com GraphQL API client for Crestview AI onboarding.

Responsibilities:
- Execute GraphQL requests with retry handling.
- Detect GraphQL errors, including HTTP 200 responses.
- Discover board columns and map application fields.
- Fetch applications using cursor-based pagination.
- Write AI assessment results back to monday.com.
- Preserve existing Screening Status values.
- Update AI Processing Status to Processing, Completed or Failed.

This module does not run FastAPI or register webhooks.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable

import requests

from .config import (
    MONDAY_API_URL,
    OUTPUT_COLUMNS,
    SCREENING_STATUS_COLUMN,
    SCREENING_STATUS_INITIAL_LABEL,
)
from .models import ClientApplication, OnboardingResult


logger = logging.getLogger(__name__)

PAGE_SIZE = 100
LONG_TEXT_MAX_CHARS = 2000

AI_PROCESSING_STATUS_COLUMN = "color_mm8y4j3s"

ALLOWED_PROCESSING_STATUSES = {
    "Processing",
    "Completed",
    "Failed",
}

RETRYABLE_ERROR_CODES = {
    "ComplexityException",
    "COMPLEXITY_BUDGET_EXHAUSTED",
    "RATE_LIMIT_EXCEEDED",
    "IP_RATE_LIMIT_EXCEEDED",
    "maxConcurrencyExceeded",
    "MAX_CONCURRENCY_EXCEEDED",
    "INTERNAL_SERVER_ERROR",
}

INPUT_FIELD_ALIASES: dict[str, list[str]] = {
    "application_id": [
        "application id",
        "application_id",
        "app id",
        "application number",
        "application no",
        "application ref",
        "reference",
    ],
    "client_type": [
        "client type",
        "client_type",
        "entity type",
        "type",
    ],
    "requested_services": [
        "requested services",
        "requested_services",
        "services requested",
        "services",
    ],
    "estimated_aum": [
        "estimated aum",
        "estimated_aum",
        "aum",
        "estimated assets under management",
        "assets under management",
    ],
    "description": [
        "description",
        "application description",
        "client description",
        "notes",
        "details",
        "background",
    ],
}


# --------------------------------------------------
# Exceptions
# --------------------------------------------------

class MondayAPIError(Exception):
    """Non-recoverable monday.com API error."""


class MondayAuthError(MondayAPIError):
    """Authentication or authorization failure."""


class MondayGraphQLError(MondayAPIError):
    """GraphQL operation returned errors."""

    def __init__(self, errors: list[dict[str, Any]]):
        self.errors = errors

        messages = "; ".join(
            _describe_error(error)
            for error in errors
        )

        super().__init__(
            f"GraphQL error(s): {messages}"
        )


# --------------------------------------------------
# General helpers
# --------------------------------------------------

def _describe_error(error: dict[str, Any]) -> str:
    code = (
        (error.get("extensions") or {}).get("code")
        or error.get("error_code")
    )

    message = (
        error.get("message")
        or error.get("error_message")
        or "Unknown error"
    )

    return f"[{code}] {message}" if code else message


def _normalise_title(title: str) -> str:
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        title.lower(),
    ).strip()


def _extract_errors(
    body: dict[str, Any],
) -> list[dict[str, Any]]:
    if body.get("errors"):
        return body["errors"]

    if body.get("error_message") or body.get("error_code"):
        return [
            {
                "message": body.get("error_message"),
                "error_code": body.get("error_code"),
            }
        ]

    return []


def _error_code(error: dict[str, Any]) -> str | None:
    return (
        (error.get("extensions") or {}).get("code")
        or error.get("error_code")
    )


def _retry_in_seconds(
    error: dict[str, Any],
) -> float | None:
    extensions = error.get("extensions") or {}

    for key in ("retry_in_seconds", "retryInSeconds"):
        value = extensions.get(key)

        if value is not None:
            return float(value)

    match = re.search(
        r"reset in (\d+) seconds?",
        error.get("message") or "",
    )

    return float(match.group(1)) if match else None


def _retry_after(
    response: requests.Response,
) -> float | None:
    value = response.headers.get("Retry-After")

    try:
        return float(value) if value else None
    except ValueError:
        return None


def _truncate(
    text: str,
    limit: int = LONG_TEXT_MAX_CHARS,
) -> str:
    text = text or ""

    if len(text) <= limit:
        return text

    suffix = "\n...[truncated]"

    return (
        text[: limit - len(suffix)].rstrip()
        + suffix
    )


# --------------------------------------------------
# Formatting AI results
# --------------------------------------------------

def format_risk_explanation(
    result: OnboardingResult,
) -> str:
    lines = [result.risk_explanation]

    if result.risk_factors:
        lines.append("\nRisk factors:")

        for factor in result.risk_factors:
            lines.append(
                f"- {factor.factor} "
                f"({factor.severity.monday_label}): "
                f"{factor.evidence}"
            )

    if result.missing_information:
        lines.append("\nMissing information:")

        for missing in result.missing_information:
            lines.append(f"- {missing}")

    lines.append(
        "\nAI-generated preliminary triage. "
        "No sanctions/PEP screening was performed."
    )

    return "\n".join(lines)


def format_recommended_action(
    result: OnboardingResult,
) -> str:
    lines = [result.recommended_action]

    if result.recommended_due_diligence:
        lines.append("\nRecommended due diligence:")

        for action in result.recommended_due_diligence:
            lines.append(f"- {action}")

    return "\n".join(lines)


# --------------------------------------------------
# Monday.com GraphQL client
# --------------------------------------------------

class MondayClient:
    """Read and update monday.com applications."""

    _ITEM_FIELDS = (
        "id name column_values { id text value type }"
    )

    def __init__(
        self,
        api_token: str,
        *,
        api_url: str = MONDAY_API_URL,
        api_version: str | None = None,
        max_retries: int = 5,
        timeout: float = 30.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_token:
            raise MondayAuthError(
                "MONDAY_API_TOKEN is not set"
            )

        self._api_url = api_url
        self._max_retries = max_retries
        self._timeout = timeout
        self._sleep = sleep
        self._session = session or requests.Session()

        self._headers = {
            "Authorization": api_token,
            "Content-Type": "application/json",
        }

        if api_version:
            self._headers["API-Version"] = api_version

    # --------------------------------------------------
    # Core GraphQL execution
    # --------------------------------------------------

    def execute(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute GraphQL with retry handling."""

        payload = {
            "query": query,
            "variables": variables or {},
        }

        last_problem = "unknown"

        for attempt in range(
            1,
            self._max_retries + 1,
        ):
            try:
                response = self._session.post(
                    self._api_url,
                    json=payload,
                    headers=self._headers,
                    timeout=self._timeout,
                )

            except (
                requests.ConnectionError,
                requests.Timeout,
            ) as exc:
                last_problem = (
                    f"Network error: {type(exc).__name__}"
                )

                self._backoff(
                    attempt,
                    last_problem,
                )
                continue

            status = response.status_code

            if status in (401, 403):
                raise MondayAuthError(
                    "monday.com rejected the credentials "
                    f"(HTTP {status})."
                )

            if status == 429 or status >= 500:
                last_problem = f"HTTP {status}"

                self._backoff(
                    attempt,
                    last_problem,
                    _retry_after(response),
                )
                continue

            try:
                body = response.json()
            except ValueError as exc:
                raise MondayAPIError(
                    "Non-JSON response from monday.com "
                    f"(HTTP {status})"
                ) from exc

            errors = _extract_errors(body)

            if errors:
                retryable = [
                    error
                    for error in errors
                    if _error_code(error)
                    in RETRYABLE_ERROR_CODES
                ]

                if (
                    retryable
                    and len(retryable) == len(errors)
                ):
                    last_problem = _describe_error(
                        retryable[0]
                    )

                    self._backoff(
                        attempt,
                        last_problem,
                        _retry_in_seconds(
                            retryable[0]
                        ),
                    )
                    continue

                raise MondayGraphQLError(errors)

            if status >= 400:
                raise MondayAPIError(
                    f"HTTP {status} from monday.com"
                )

            if (
                not isinstance(body, dict)
                or body.get("data") is None
            ):
                raise MondayAPIError(
                    "monday.com response contained no data"
                )

            return body["data"]

        raise MondayAPIError(
            "monday.com request failed after "
            f"{self._max_retries} attempts "
            f"({last_problem})"
        )

    def _backoff(
        self,
        attempt: int,
        reason: str,
        suggested: float | None = None,
    ) -> None:
        if attempt >= self._max_retries:
            return

        delay = (
            suggested
            if suggested is not None
            else min(2 ** (attempt - 1), 30)
        )

        logger.warning(
            "monday.com request failed (%s); "
            "retry %d/%d in %.1fs",
            reason,
            attempt,
            self._max_retries - 1,
            delay,
        )

        self._sleep(delay)

    # --------------------------------------------------
    # Board columns
    # --------------------------------------------------

    def get_board_columns(
        self,
        board_id: str,
    ) -> list[dict[str, str]]:
        query = """
        query ($boardId: [ID!]) {
            boards(ids: $boardId) {
                id
                name
                columns {
                    id
                    title
                    type
                }
            }
        }
        """

        data = self.execute(
            query,
            {"boardId": [str(board_id)]},
        )

        boards = data.get("boards") or []

        if not boards:
            raise MondayAPIError(
                f"Board {board_id} not found or inaccessible"
            )

        board = boards[0]
        columns = board.get("columns") or []

        logger.info(
            "Connected to board '%s' (%s) "
            "with %d columns",
            board.get("name"),
            board_id,
            len(columns),
        )

        return columns

    @staticmethod
    def map_input_columns(
        columns: list[dict[str, str]],
    ) -> dict[str, str | None]:
        """Map application fields to monday.com columns."""

        output_ids = (
            set(OUTPUT_COLUMNS.values())
            | {
                SCREENING_STATUS_COLUMN,
                AI_PROCESSING_STATUS_COLUMN,
            }
        )

        candidates = [
            column
            for column in columns
            if column["id"] not in output_ids
            and column["id"] != "name"
        ]

        by_title = {
            _normalise_title(column["title"]):
            column["id"]
            for column in candidates
        }

        mapping: dict[str, str | None] = {}
        used: set[str] = set()

        for field, aliases in INPUT_FIELD_ALIASES.items():
            normalised_aliases = [
                _normalise_title(alias)
                for alias in aliases
            ]

            match = next(
                (
                    by_title[alias]
                    for alias in normalised_aliases
                    if alias in by_title
                    and by_title[alias] not in used
                ),
                None,
            )

            if match is None:
                match = next(
                    (
                        column_id
                        for alias in normalised_aliases
                        for title, column_id
                        in by_title.items()
                        if column_id not in used
                        and re.search(
                            rf"\b{re.escape(alias)}\b",
                            title,
                        )
                    ),
                    None,
                )

            mapping[field] = match

            if match:
                used.add(match)

        return mapping

    # --------------------------------------------------
    # Fetch items with pagination
    # --------------------------------------------------

    def iter_items(
        self,
        board_id: str,
        limit: int | None = None,
    ):
        """Yield monday.com board items."""

        first_page = f"""
        query ($boardId: [ID!], $limit: Int!) {{
            boards(ids: $boardId) {{
                items_page(limit: $limit) {{
                    cursor
                    items {{
                        {self._ITEM_FIELDS}
                    }}
                }}
            }}
        }}
        """

        next_page = f"""
        query ($cursor: String!, $limit: Int!) {{
            next_items_page(
                cursor: $cursor,
                limit: $limit
            ) {{
                cursor
                items {{
                    {self._ITEM_FIELDS}
                }}
            }}
        }}
        """

        page_size = (
            min(PAGE_SIZE, limit)
            if limit
            else PAGE_SIZE
        )

        data = self.execute(
            first_page,
            {
                "boardId": [str(board_id)],
                "limit": page_size,
            },
        )

        boards = data.get("boards") or []

        if not boards:
            raise MondayAPIError(
                f"Board {board_id} not found"
            )

        page = boards[0]["items_page"]

        yielded = 0

        while True:
            for item in page["items"]:
                yield item
                yielded += 1

                if limit and yielded >= limit:
                    return

            cursor = page.get("cursor")

            if not cursor:
                return

            data = self.execute(
                next_page,
                {
                    "cursor": cursor,
                    "limit": page_size,
                },
            )

            page = data["next_items_page"]

    # --------------------------------------------------
    # Application conversion
    # --------------------------------------------------

    @staticmethod
    def item_to_application(
        item: dict[str, Any],
        mapping: dict[str, str | None],
    ) -> ClientApplication:
        values = {
            column["id"]: (
                (column.get("text") or "").strip()
                or None
            )
            for column in item.get(
                "column_values", []
            )
        }

        fields = {
            field: values.get(column_id)
            if column_id
            else None
            for field, column_id in mapping.items()
        }

        return ClientApplication(
            item_id=str(item["id"]),
            client_name=item["name"],
            **fields,
        )

    def fetch_applications(
        self,
        board_id: str,
        limit: int | None = None,
    ) -> list[ClientApplication]:
        columns = self.get_board_columns(board_id)
        mapping = self.map_input_columns(columns)

        titles = {
            column["id"]: column["title"]
            for column in columns
        }

        for field, column_id in mapping.items():
            if column_id:
                logger.info(
                    "%s -> %s (%s)",
                    field,
                    column_id,
                    titles[column_id],
                )
            else:
                logger.warning(
                    "%s -> NOT FOUND",
                    field,
                )

        applications = [
            self.item_to_application(
                item,
                mapping,
            )
            for item in self.iter_items(
                board_id,
                limit,
            )
        ]

        logger.info(
            "Retrieved %d applications",
            len(applications),
        )

        return applications

    # --------------------------------------------------
    # Build AI result column values
    # --------------------------------------------------

    @staticmethod
    def build_column_values(
        result: OnboardingResult,
    ) -> dict[str, Any]:
        return {
            OUTPUT_COLUMNS["risk_level"]: {
                "label": result.risk_level.monday_label,
            },
            OUTPUT_COLUMNS["risk_explanation"]: {
                "text": _truncate(
                    format_risk_explanation(result)
                ),
            },
            OUTPUT_COLUMNS["onboarding_summary"]: {
                "text": _truncate(
                    result.onboarding_summary
                ),
            },
            OUTPUT_COLUMNS["recommended_action"]: {
                "text": _truncate(
                    format_recommended_action(result)
                ),
            },
            OUTPUT_COLUMNS["human_review_required"]: {
                "label": (
                    "Yes"
                    if result.human_review_required
                    else "No"
                ),
            },
            OUTPUT_COLUMNS["review_type"]: {
                "label": (
                    result.review_type
                    .replace("_", " ")
                    .title()
                ),
            },
            OUTPUT_COLUMNS["review_reason"]: {
                "text": _truncate(
                    result.review_reason
                ),
            },
        }

    # --------------------------------------------------
    # Write AI results to monday.com
    # --------------------------------------------------

    def update_item(
        self,
        board_id: str,
        result: OnboardingResult,
    ) -> None:
        """
        Write AI assessment fields while preserving
        an existing Screening Status.
        """

        column_values = self.build_column_values(
            result
        )

        query = """
        query ($itemIds: [ID!]) {
            items(ids: $itemIds) {
                id
                column_values {
                    id
                    text
                }
            }
        }
        """

        current_data = self.execute(
            query,
            {
                "itemIds": [str(result.item_id)],
            },
        )

        items = current_data.get("items") or []

        if not items:
            raise MondayAPIError(
                f"Item {result.item_id} not found"
            )

        screening_column = next(
            (
                column
                for column in items[0]["column_values"]
                if column["id"]
                == SCREENING_STATUS_COLUMN
            ),
            None,
        )

        if screening_column is None:
            raise MondayAPIError(
                "Screening Status column not found"
            )

        if not screening_column.get("text"):
            column_values[
                SCREENING_STATUS_COLUMN
            ] = {
                "label": SCREENING_STATUS_INITIAL_LABEL
            }

        mutation = """
        mutation (
            $boardId: ID!,
            $itemId: ID!,
            $columnValues: JSON!
        ) {
            change_multiple_column_values(
                board_id: $boardId,
                item_id: $itemId,
                column_values: $columnValues
            ) {
                id
            }
        }
        """

        data = self.execute(
            mutation,
            {
                "boardId": str(board_id),
                "itemId": str(result.item_id),
                "columnValues": json.dumps(
                    column_values
                ),
            },
        )

        updated_id = (
            data.get(
                "change_multiple_column_values"
            ) or {}
        ).get("id")

        if str(updated_id) != str(result.item_id):
            raise MondayAPIError(
                "Unexpected AI write-back response "
                f"for item {result.item_id}"
            )

        logger.info(
            "AI assessment written to monday.com "
            "| item=%s",
            result.item_id,
        )

    # --------------------------------------------------
    # Update AI Processing Status
    # --------------------------------------------------

    def update_processing_status(
        self,
        board_id: str,
        item_id: str,
        status: str,
    ) -> None:
        """
        Update AI Processing Status.

        Allowed:
        - Processing
        - Completed
        - Failed
        """

        if status not in ALLOWED_PROCESSING_STATUSES:
            raise ValueError(
                f"Invalid processing status: {status}"
            )

        mutation = """
        mutation (
            $boardId: ID!,
            $itemId: ID!,
            $columnId: String!,
            $value: String!
        ) {
            change_simple_column_value(
                board_id: $boardId,
                item_id: $itemId,
                column_id: $columnId,
                value: $value
            ) {
                id
            }
        }
        """

        data = self.execute(
            mutation,
            {
                "boardId": str(board_id),
                "itemId": str(item_id),
                "columnId": AI_PROCESSING_STATUS_COLUMN,
                "value": status,
            },
        )

        updated_id = (
            data.get(
                "change_simple_column_value"
            ) or {}
        ).get("id")

        if str(updated_id) != str(item_id):
            raise MondayAPIError(
                "Unexpected status mutation response "
                f"for item {item_id}"
            )

        logger.info(
            "AI Processing Status updated "
            "| item=%s | status=%s",
            item_id,
            status,
        )
