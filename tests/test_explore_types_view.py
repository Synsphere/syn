from __future__ import annotations

from unittest.mock import Mock

from fastapi.testclient import TestClient

from syn.api import createApp


def exploreTypesPage() -> tuple[TestClient, str]:
    http = TestClient(createApp(Mock(), {}))
    response = http.get("/exploreTypes")
    assert response.status_code == 200
    return http, response.text


def test_explore_types_view_renders_as_html() -> None:
    _, html = exploreTypesPage()

    assert "<title>Explore types · Syn</title>" in html
    assert "<h1>Explore types</h1>" in html
    assert 'id="typeTree"' in html


def test_explore_types_view_uses_d3_collapsible_tree_primitives() -> None:
    _, html = exploreTypesPage()

    assert "d3@7.9.0/dist/d3.min.js" in html
    assert "d3.tree().nodeSize" in html
    assert "d3.linkHorizontal()" in html
    assert "d3.hierarchy" in html
    assert "node._children = node.children" in html
    assert '.on("click", toggleNode)' in html


def test_explore_types_view_loads_all_primitive_roots_through_ajax() -> None:
    _, html = exploreTypesPage()

    assert '["cog", "credential", "ember", "enigma", "event", "om", "pondle", "snug", "thing"]' in html
    assert "primitiveTypes.map(slug => fetchTree(slug, depth, request.signal))" in html
    assert "`/api/type/${encodeURIComponent(slug)}/tree?depth=${depth}`" in html
    assert 'headers: { Accept: "application/json" }' in html


def test_explore_types_view_exposes_bounded_depth_and_tree_controls() -> None:
    _, html = exploreTypesPage()

    assert 'id="treeDepth" type="number" min="0" max="100" step="1" value="10"' in html
    assert 'id="reloadTree"' in html
    assert 'id="expandAll"' in html
    assert 'id="collapseAll"' in html
    assert 'id="resetView"' in html
    assert "Depth must be a whole number between 0 and 100." in html


def test_explore_types_view_has_accessible_status_and_keyboard_nodes() -> None:
    _, html = exploreTypesPage()

    assert 'role="status" aria-live="polite"' in html
    assert 'role="img" aria-labelledby="treeTitle treeDescription"' in html
    assert '.attr("role", "button")' in html
    assert '.attr("tabindex", 0)' in html
    assert 'event.key === "Enter" || event.key === " "' in html


def test_explore_types_view_is_not_an_api_operation() -> None:
    http, _ = exploreTypesPage()

    assert "/exploreTypes" not in http.get("/openapi.json").json()["paths"]
