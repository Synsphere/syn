from __future__ import annotations

from unittest.mock import Mock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from syn.models import InvalidTypeParentsError, Type
from syn.refs import ObjectRef


def typeRecord(slug: str) -> Type:
    return Type(id=ObjectRef.local("type", slug), name=slug)


def scalarResult(*values: ObjectRef) -> Mock:
    result = Mock()
    result.all.return_value = list(values)
    return result


def executeResult(*rows: tuple[ObjectRef, ObjectRef]) -> Mock:
    result = Mock()
    result.all.return_value = list(rows)
    return result


def test_replace_parents_accepts_independent_branches() -> None:
    record = typeRecord("person")
    organism = ObjectRef.local("type", "human-organism")
    agent = ObjectRef.local("type", "agent")
    session = Mock(spec=Session)
    session.scalars.side_effect = [
        scalarResult(organism, agent),
        scalarResult(),
    ]
    session.execute.side_effect = [
        executeResult(),
        executeResult(),
        executeResult(),
    ]

    record.replaceParents(session, [organism, agent])

    ancestryStatement = session.execute.call_args_list[1].args[0]
    compiled = str(ancestryStatement.compile(dialect=postgresql.dialect()))
    assert 'WITH RECURSIVE "candidateAncestry"' in compiled
    assert 'FROM "typeParents"' in compiled
    deleteStatement = session.execute.call_args_list[2].args[0]
    assert 'DELETE FROM "typeParents"' in str(
        deleteStatement.compile(dialect=postgresql.dialect())
    )
    added = list(session.add_all.call_args.args[0])
    assert {(row.childRef, row.parentRef) for row in added} == {
        (record.id, organism),
        (record.id, agent),
    }


@pytest.mark.parametrize(
    ("descendant", "ancestor"),
    [
        ("human-organism", "organism"),
        ("human-organism", "material-thing"),
    ],
)
def test_replace_parents_rejects_direct_and_transitive_ancestors(
    descendant: str, ancestor: str
) -> None:
    record = typeRecord("person")
    descendantRef = ObjectRef.local("type", descendant)
    ancestorRef = ObjectRef.local("type", ancestor)
    session = Mock(spec=Session)
    session.scalars.return_value = scalarResult(descendantRef, ancestorRef)
    session.execute.side_effect = [
        executeResult(),
        executeResult((descendantRef, ancestorRef)),
    ]

    with pytest.raises(
        InvalidTypeParentsError,
        match=f"{ancestorRef} is an ancestor of {descendantRef}",
    ):
        record.replaceParents(session, [descendantRef, ancestorRef])

    session.add_all.assert_not_called()


def test_replace_parents_rejects_unknown_types() -> None:
    record = typeRecord("person")
    missing = ObjectRef.local("type", "missing")
    session = Mock(spec=Session)
    session.scalars.return_value = scalarResult()
    session.execute.return_value = executeResult()

    with pytest.raises(InvalidTypeParentsError, match=f"Unknown parent types: {missing}"):
        record.replaceParents(session, [missing])


@pytest.mark.parametrize(
    "parents",
    [
        ["https://example.com/type"],
        ["syn://thing/not-a-type"],
        ["syn://type/parent", "syn://type/parent"],
        ["syn://type/person"],
    ],
)
def test_replace_parents_rejects_invalid_reference_lists(parents: list[str]) -> None:
    record = typeRecord("person")
    session = Mock(spec=Session)

    with pytest.raises(InvalidTypeParentsError):
        record.replaceParents(session, parents)

    session.execute.assert_not_called()


def test_replace_parents_does_not_rewrite_an_unchanged_relationship() -> None:
    record = typeRecord("person")
    parent = ObjectRef.local("type", "agent")
    session = Mock(spec=Session)
    session.scalars.side_effect = [scalarResult(parent), scalarResult(parent)]
    session.execute.return_value = executeResult()

    record.replaceParents(session, [parent])

    session.add_all.assert_not_called()
    assert session.execute.call_count == 1
