from __future__ import annotations

from unittest.mock import Mock

from fastapi.testclient import TestClient

from syn.api import createApp


def editTypesPage() -> tuple[TestClient, str]:
    http = TestClient(createApp(Mock(), {}))
    response = http.get("/editTypes")
    assert response.status_code == 200
    return http, response.text


def test_edit_types_view_renders_as_html() -> None:
    _, html = editTypesPage()

    assert "<title>Edit types · Syn</title>" in html
    assert "<h1>Edit types</h1>" in html


def test_edit_types_view_has_accessible_autocomplete_controls() -> None:
    _, html = editTypesPage()

    assert 'id="typeSearch"' in html
    assert 'role="combobox"' in html
    assert 'aria-autocomplete="list"' in html
    assert 'aria-controls="typeResults"' in html
    assert 'id="typeResults" role="listbox"' in html


def test_edit_types_view_uses_type_read_and_update_api_calls() -> None:
    _, html = editTypesPage()

    assert "`/api/type?query=${encodeURIComponent(query)}`" in html
    assert "`/api/type/${encodeURIComponent(slug)}`" in html
    assert 'method: "PUT"' in html
    assert '"Content-Type": "application/json"' in html
    assert "body: JSON.stringify(body)" in html
    assert "await response.json()" in html


def test_edit_types_view_contains_editable_type_fields() -> None:
    _, html = editTypesPage()

    expectedFields = {
        "id",
        "name",
        "wikipediaRef",
        "locality",
        "confidence",
        "stats",
        "knowledge",
        "statsUpdated",
        "knowledgeUpdated",
        "confidenceUpdated",
    }
    for field in expectedFields:
        assert f'name="{field}"' in html
    assert 'name="id" readonly' in html
    assert 'name="name" required' in html
    assert 'id="typeEditor"' in html
    assert 'id="typeEditor" class="panel" aria-labelledby="editorHeading" hidden' in html


def test_edit_types_view_has_save_controls_and_client_validation() -> None:
    _, html = editTypesPage()

    assert '<button id="saveType" type="submit">Save changes</button>' in html
    assert 'id="saveStatus"' in html
    assert "JSON.parse(value)" in html
    assert "Number.isFinite(Number(confidence))" in html
    assert "Locality must be a whole number." in html
    assert "Confidence must be between 0 and 1." in html
    assert 'saveStatus.textContent = "Changes saved."' in html


def test_edit_types_view_uses_safe_text_rendering_for_search_results() -> None:
    _, html = editTypesPage()

    assert ".textContent = record.name" in html
    assert ".textContent = record.id" in html
    assert ".innerHTML" not in html


def test_edit_types_view_is_not_an_api_operation() -> None:
    http, _ = editTypesPage()

    assert "/editTypes" not in http.get("/openapi.json").json()["paths"]
