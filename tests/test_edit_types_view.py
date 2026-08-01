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
    assert 'id="parentSearch"' in html
    assert 'aria-controls="parentResults"' in html
    assert 'id="parentResults" role="listbox"' in html


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


def test_edit_types_view_lists_direct_parent_ancestry_paths() -> None:
    _, html = editTypesPage()

    assert 'id="parentList"' in html
    assert 'aria-label="Current direct parents"' in html
    assert 'id="parentEmpty"' in html
    assert "directParents = record.parents" in html
    assert 'summary.textContent = labels.join(" → ")' in html
    assert "parentPaths(ancestor, nextVisited)" in html


def test_edit_types_view_can_add_and_remove_direct_parents() -> None:
    _, html = editTypesPage()

    assert "setTimeout(() => searchParentTypes(query), 220)" in html
    assert "addParent(parentResults[parentSelectedIndex])" in html
    assert "directParents.push(parent)" in html
    assert 'remove.className = "remove-parent"' in html
    assert 'remove.textContent = "Remove"' in html
    assert "directParents = directParents.filter" in html
    assert "parents: directParents.map(parent => parent.id)" in html


def test_edit_types_view_prevents_redundant_parent_selections() -> None:
    _, html = editTypesPage()

    assert "record.id === fields.id.value" in html
    assert "directParents.some(parent => parent.id === record.id)" in html
    assert "hasAncestor(parent, fields.id.value)" in html
    assert "is a descendant of this type" in html
    assert "hasAncestor(parent, existing.id) || hasAncestor(existing, parent.id)" in html
    assert "choose only the more specific parent" in html


def test_edit_types_view_uses_safe_text_rendering_for_search_results() -> None:
    _, html = editTypesPage()

    assert ".textContent = record.name" in html
    assert ".textContent = record.id" in html
    assert ".innerHTML" not in html


def test_edit_types_view_is_not_an_api_operation() -> None:
    http, _ = editTypesPage()

    assert "/editTypes" not in http.get("/openapi.json").json()["paths"]
