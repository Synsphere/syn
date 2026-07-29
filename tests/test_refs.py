from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syn import LOCAL_KINDS, ObjectRef, normalizeSlug


@pytest.mark.parametrize("kind", sorted(LOCAL_KINDS))
def test_local_reference_round_trip_for_every_kind(kind: str) -> None:
    reference = ObjectRef.local(kind, "Hello, World!")
    assert reference == f"syn://{kind}/hello-world"
    assert reference.isLocal is True
    assert reference.objectTable == kind
    assert reference.slug == "hello-world"


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/a?b=c#fragment",
        "http://example.com",
        "mailto:user@example.com",
        "urn:isbn:9780141036144",
        "/relative/path",
        "../parent",
        "#fragment",
        "?query=value",
        "percent%20encoded",
    ],
)
def test_general_external_and_relative_references_are_accepted(value: str) -> None:
    reference = ObjectRef(value)
    assert reference == value
    assert reference.isLocal is False
    assert reference.objectTable is None
    assert reference.slug is None


@pytest.mark.parametrize("value", [None, 1, b"syn://thing/x", object()])
def test_object_ref_requires_string_input(value: object) -> None:
    with pytest.raises(TypeError, match="string"):
        ObjectRef(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    ["", " ", "\t", "\n", "https://example.com/a b", "abc\x00def", "abc\x1fdef"],
)
def test_empty_whitespace_and_control_references_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        ObjectRef(value)


@pytest.mark.parametrize(
    "value",
    ["%", "%0", "%GG", "path/%2", "https://example.com/%xz", "syn://thing/a%20b"],
)
def test_invalid_percent_escapes_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        ObjectRef(value)


@pytest.mark.parametrize(
    "value",
    [
        "syn://",
        "syn://thing",
        "syn://unknown/x",
        "syn://Thing/x",
        "syn://thing/Uppercase",
        "syn://thing/-leading",
        "syn://thing/trailing-",
        "syn://thing/a--b",
        "syn://thing/a__b",
        "syn://thing/a/b",
        "syn://thing/a.b",
        "syn://thing/a%20b",
    ],
)
def test_malformed_local_references_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="Invalid local ObjectRef"):
        ObjectRef(value)


def test_local_factory_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        ObjectRef.local("unknown", "example")


@pytest.mark.parametrize("title", ["", "---", "___", "你好", "🔥"])
def test_local_factory_rejects_titles_without_ascii_slug(title: str) -> None:
    with pytest.raises(ValueError, match="non-empty slug"):
        ObjectRef.local("thing", title)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Crème brûlée", "creme-brulee"),
        ("Hello, World!", "hello-world"),
        ("  padded  ", "padded"),
        ("UPPER lower", "upper-lower"),
        ("already-valid", "already-valid"),
        ("under_score", "under_score"),
        ("double__underscore", "double-underscore"),
        ("mixed-_separator", "mixed-separator"),
        ("many     spaces", "many-spaces"),
        ("rock & roll", "rock-roll"),
    ],
)
def test_slug_normalization_examples(value: str, expected: str) -> None:
    assert normalizeSlug(value) == expected


def test_slug_normalization_enforces_maximum_length() -> None:
    result = normalizeSlug("a" * 300)
    assert len(result) == 255
    assert result == "a" * 255


@given(st.text(alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")), min_size=1))
def test_normalized_slugs_are_lowercase_and_bounded(value: str) -> None:
    result = normalizeSlug(value)
    assert len(result) <= 255
    assert result == result.lower()
    assert not result.startswith("-")
    assert not result.endswith("-")
    assert not result.startswith("_")
    assert not result.endswith("_")


@given(
    st.sampled_from(sorted(LOCAL_KINDS)),
    st.from_regex(re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*"), fullmatch=True).filter(
        lambda value: len(value) <= 255
    ),
)
def test_valid_local_reference_strings_round_trip(kind: str, slug: str) -> None:
    reference = ObjectRef(f"syn://{kind}/{slug}")
    assert reference.objectTable == kind
    assert reference.slug == slug
