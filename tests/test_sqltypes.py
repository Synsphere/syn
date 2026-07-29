from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from syn import ObjectRef
from syn.sqltypes import (
    ObjectRefType,
    SlugType,
    cogRefType,
    emberRefType,
    enigmaRefType,
    eventRefType,
    objectRefType,
    omRefType,
    pondleRefType,
    slugType,
    snugRefType,
    thingRefType,
    typeRefType,
)


TYPED_REFS = {
    "type": typeRefType,
    "thing": thingRefType,
    "event": eventRefType,
    "ember": emberRefType,
    "cog": cogRefType,
    "enigma": enigmaRefType,
    "pondle": pondleRefType,
    "snug": snugRefType,
    "om": omRefType,
}


@pytest.mark.parametrize(("kind", "sqlType"), TYPED_REFS.items())
def test_typed_ref_bind_processor_accepts_matching_local_ref(kind: str, sqlType: ObjectRefType) -> None:
    processor = sqlType.bind_processor(postgresql.dialect())
    assert processor(f"syn://{kind}/example") == f"syn://{kind}/example"


@pytest.mark.parametrize(("kind", "sqlType"), TYPED_REFS.items())
def test_typed_ref_bind_processor_rejects_external_ref(kind: str, sqlType: ObjectRefType) -> None:
    processor = sqlType.bind_processor(postgresql.dialect())
    with pytest.raises(ValueError, match="local syn://"):
        processor("https://example.com/x")


@pytest.mark.parametrize(("kind", "sqlType"), TYPED_REFS.items())
def test_typed_ref_bind_processor_rejects_wrong_local_kind(
    kind: str, sqlType: ObjectRefType
) -> None:
    wrongKind = "thing" if kind != "thing" else "event"
    processor = sqlType.bind_processor(postgresql.dialect())
    with pytest.raises(ValueError, match=f"syn://{kind}/"):
        processor(f"syn://{wrongKind}/example")


def test_general_ref_processor_accepts_external_and_local_values() -> None:
    processor = objectRefType.bind_processor(postgresql.dialect())
    assert processor("https://example.com") == "https://example.com"
    assert processor(ObjectRef("syn://thing/example")) == "syn://thing/example"
    assert processor(None) is None


def test_result_processor_returns_object_ref_and_preserves_none() -> None:
    processor = objectRefType.result_processor(postgresql.dialect(), None)
    result = processor("syn://thing/example")
    assert isinstance(result, ObjectRef)
    assert result.objectTable == "thing"
    assert processor(None) is None


@pytest.mark.parametrize(("kind", "sqlType"), TYPED_REFS.items())
def test_typed_ref_sql_spec_uses_quoted_camel_case_domain(
    kind: str, sqlType: ObjectRefType
) -> None:
    assert sqlType.get_col_spec() == f'"{kind}Ref"'


def test_general_ref_and_slug_specs() -> None:
    assert objectRefType.get_col_spec() == '"objectRef"'
    assert slugType.get_col_spec() == "slug"
    assert isinstance(slugType, SlugType)


def test_custom_type_instances_are_cacheable() -> None:
    assert ObjectRefType.cache_ok is True
    assert SlugType.cache_ok is True
