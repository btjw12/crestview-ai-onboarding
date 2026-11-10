import json

import pytest
import requests

from src.config import OUTPUT_COLUMNS
from src.models import Level, OnboardingResult
from src.monday_client import (
    MondayAPIError,
    MondayAuthError,
    MondayClient,
    MondayGraphQLError,
)
from tests.conftest import FakeResponse

COLUMNS = [
    {"id": "name", "title": "Name", "type": "name"},
    {"id": "text_app", "title": "Application ID", "type": "text"},
    {"id": "status_type", "title": "Client Type", "type": "status"},
    {"id": "dropdown_svc", "title": "Requested Services", "type": "dropdown"},
    {"id": "numbers_aum", "title": "Estimated AUM", "type": "numbers"},
    {"id": "long_text_desc", "title": "Description", "type": "long_text"},
    {"id": "color_mm8vjgpn", "title": "AI Risk Level", "type": "status"},
    {"id": "long_text_mm8vqq3n", "title": "Risk Explanation", "type": "long_text"},
]


def client(session, **kw):
    return MondayClient("test-token", session=session, sleep=lambda s: None, **kw)


def item(i, name="Client"):
    return {"id": str(i), "name": f"{name} {i}", "column_values": [
        {"id": "text_app", "text": f"APP-{i}", "value": None, "type": "text"},
        {"id": "long_text_desc", "text": "Some description", "value": None, "type": "long_text"},
        {"id": "numbers_aum", "text": "", "value": None, "type": "numbers"},
    ]}


def test_missing_token_raises():
    with pytest.raises(MondayAuthError):
        MondayClient("")


def test_graphql_error_with_http_200_is_raised(make_session):
    session = make_session(FakeResponse(200, {"errors": [{"message": "Field 'foo' doesn't exist"}]}))
    with pytest.raises(MondayGraphQLError, match="foo"):
        client(session).execute("query { foo }")


def test_legacy_error_envelope_is_raised(make_session):
    session = make_session(FakeResponse(200, {"error_code": "InvalidBoardIdException",
                                              "error_message": "Board not found"}))
    with pytest.raises(MondayGraphQLError, match="Board not found"):
        client(session).execute("query { boards { id } }")


def test_retries_on_429_then_succeeds(make_session):
    session = make_session(FakeResponse(429, {}, {"Retry-After": "1"}),
                           FakeResponse(200, {"data": {"ok": True}}))
    assert client(session).execute("query { ok }") == {"ok": True}
    assert session.post.call_count == 2


def test_retries_on_complexity_error_and_network_error(make_session):
    session = make_session(
        requests.ConnectionError("boom"),
        FakeResponse(200, {"errors": [{"message": "Complexity budget exhausted",
                                       "extensions": {"code": "ComplexityException",
                                                      "retry_in_seconds": 2}}]}),
        FakeResponse(200, {"data": {"ok": 1}}),
    )
    assert client(session).execute("q") == {"ok": 1}


def test_gives_up_after_max_retries(make_session):
    session = make_session(*[FakeResponse(503, {}) for _ in range(3)])
    with pytest.raises(MondayAPIError, match="after 3 attempts"):
        client(session, max_retries=3).execute("q")


def test_auth_failure_is_not_retried(make_session):
    session = make_session(FakeResponse(401, {"errors": [{"message": "Not Authenticated"}]}))
    with pytest.raises(MondayAuthError):
        client(session).execute("q")
    assert session.post.call_count == 1


def test_token_sent_in_header_not_body(make_session):
    session = make_session(FakeResponse(200, {"data": {}}))
    client(session).execute("q", {"a": 1})
    kwargs = session.post.call_args.kwargs
    assert kwargs["headers"]["Authorization"] == "test-token"
    assert "test-token" not in json.dumps(kwargs["json"])


def test_column_mapping_by_title_excludes_output_columns():
    mapping = MondayClient.map_input_columns(COLUMNS)
    assert mapping == {
        "application_id": "text_app",
        "client_type": "status_type",
        "requested_services": "dropdown_svc",
        "estimated_aum": "numbers_aum",
        "description": "long_text_desc",
    }


def test_column_mapping_reports_missing_fields():
    mapping = MondayClient.map_input_columns([{"id": "x", "title": "Unrelated", "type": "text"}])
    assert all(v is None for v in mapping.values())


def test_pagination_follows_cursor(make_session):
    session = make_session(
        FakeResponse(200, {"data": {"boards": [{"items_page": {"cursor": "c1", "items": [item(1), item(2)]}}]}}),
        FakeResponse(200, {"data": {"next_items_page": {"cursor": None, "items": [item(3)]}}}),
    )
    items = list(client(session).iter_items("123"))
    assert [i["id"] for i in items] == ["1", "2", "3"]
    second_call = session.post.call_args_list[1].kwargs["json"]
    assert "next_items_page" in second_call["query"]
    assert second_call["variables"]["cursor"] == "c1"


def test_pagination_respects_limit(make_session):
    session = make_session(
        FakeResponse(200, {"data": {"boards": [{"items_page": {"cursor": "c1", "items": [item(1), item(2)]}}]}}),
    )
    items = list(client(session).iter_items("123", limit=2))
    assert len(items) == 2
    assert session.post.call_count == 1


def test_fetch_applications_maps_values_and_preserves_item_id(make_session):
    session = make_session(
        FakeResponse(200, {"data": {"boards": [{"name": "Board", "columns": COLUMNS}]}}),
        FakeResponse(200, {"data": {"boards": [{"items_page": {"cursor": None, "items": [item(42, "Acme")]}}]}}),
    )
    apps = client(session).fetch_applications("123")
    assert len(apps) == 1
    app = apps[0]
    assert app.item_id == "42"
    assert app.client_name == "Acme 42"
    assert app.application_id == "APP-42"
    assert app.description == "Some description"
    assert app.estimated_aum is None  # blank -> None


def _result(**overrides):
    data = dict(
        item_id="42", application_id="APP-42", client_name="Acme", risk_level=Level.HIGH,
        risk_factors=[], risk_explanation="Because.", missing_information=["UBO"],
        recommended_due_diligence=["Get UBO register"], onboarding_complexity=Level.MEDIUM,
        onboarding_summary="Summary text", recommended_action="Route to compliance.",
        human_review_required=True, review_type="ENHANCED_DUE_DILIGENCE",
        review_reason="Opaque ownership.", model="m",
    )
    data.update(overrides)
    return OnboardingResult(**data)


def test_build_column_values_only_touches_output_columns():
    values = MondayClient.build_column_values(_result())
    assert set(values) == set(OUTPUT_COLUMNS.values())
    assert values["color_mm8vjgpn"] == {"label": "High"}
    assert values["color_mm8v1s9e"] == {"label": "Yes"}
    assert values["long_text_mm8vtgjg"] == {"text": "Summary text"}
    assert "Get UBO register" in values["long_text_mm8v99qe"]["text"]


def test_long_text_is_truncated():
    values = MondayClient.build_column_values(_result(onboarding_summary="x" * 5000))
    assert len(values["long_text_mm8vtgjg"]["text"]) <= 2000


def _current_item(screening_text):
    """Response for the read-before-write query: the item's current Screening Status."""
    return FakeResponse(200, {"data": {"items": [{"column_values": [
        {"id": "color_mm8wva1n", "text": screening_text}]}]}})


WRITE_OK = FakeResponse(200, {"data": {"change_multiple_column_values": {"id": "42"}}})


def test_update_item_uses_change_multiple_column_values(make_session):
    session = make_session(_current_item(""), WRITE_OK)
    client(session).update_item("123", _result(risk_level=Level.LOW, human_review_required=False))
    payload = session.post.call_args.kwargs["json"]
    assert "change_multiple_column_values" in payload["query"]
    assert "create_item" not in payload["query"]
    assert payload["variables"]["itemId"] == "42"
    assert payload["variables"]["boardId"] == "123"
    sent = json.loads(payload["variables"]["columnValues"])
    assert sent["color_mm8vjgpn"] == {"label": "Low"}
    assert sent["color_mm8v1s9e"] == {"label": "No"}


def test_update_item_initialises_empty_screening_status(make_session):
    session = make_session(_current_item(""), WRITE_OK)
    client(session).update_item("123", _result())
    sent = json.loads(session.post.call_args.kwargs["json"]["variables"]["columnValues"])
    assert sent["color_mm8wva1n"] == {"label": "Not Started"}


@pytest.mark.parametrize("existing", ["In Progress", "Clear", "Escalated", "Potential Match"])
def test_update_item_never_overwrites_screening_progress(make_session, existing):
    session = make_session(_current_item(existing), WRITE_OK)
    client(session).update_item("123", _result())
    sent = json.loads(session.post.call_args.kwargs["json"]["variables"]["columnValues"])
    assert "color_mm8wva1n" not in sent


def test_update_item_fails_if_item_missing(make_session):
    session = make_session(FakeResponse(200, {"data": {"items": []}}))
    with pytest.raises(MondayAPIError, match="not found"):
        client(session).update_item("123", _result())
    assert session.post.call_count == 1  # nothing was written
