from __future__ import annotations

from syn.schema import schemaSql, schemaStatements


def test_packaged_schema_matches_canonical_schema() -> None:
    canonical = open("database/objectReferenceSchema.sql", encoding="utf-8-sig").read()
    assert schemaSql() == canonical


def test_real_schema_splits_into_single_statements() -> None:
    statements = schemaStatements()
    assert len(statements) >= 90
    assert all(statement.strip().upper() not in {"BEGIN", "COMMIT"} for statement in statements)
    assert any('CREATE FUNCTION "ensureLocalObject"' in statement for statement in statements)
    ensure = next(
        statement for statement in statements if 'CREATE FUNCTION "ensureLocalObject"' in statement
    )
    assert ensure.count("ON CONFLICT (id) DO NOTHING") == 9
    assert "END CASE;" in ensure


def test_splitter_preserves_quotes_comments_dollar_blocks_and_nested_comments() -> None:
    sql = """
    BEGIN;
    -- a comment containing ;
    SELECT 'single;quote''escaped', "double;quote""escaped";
    /* outer ; /* nested ; */ still outer */ SELECT 2;
    CREATE FUNCTION example() RETURNS void AS $body$
    BEGIN
      PERFORM 'inside;body';
    END;
    $body$ LANGUAGE plpgsql;
    COMMIT;
    """
    statements = schemaStatements(sql)
    assert len(statements) == 3
    assert "single;quote''escaped" in statements[0]
    assert '"double;quote"' in statements[0]
    assert "nested ;" in statements[1]
    assert "PERFORM 'inside;body';" in statements[2]


def test_splitter_supports_empty_dollar_tag_and_trailing_statement() -> None:
    statements = schemaStatements("SELECT $$a;b$$; SELECT 2")
    assert statements == ("SELECT $$a;b$$", "SELECT 2")


def test_splitter_ignores_trailing_comment_only_remainder() -> None:
    assert schemaStatements("SELECT 1; -- trailing only") == ("SELECT 1",)


def test_splitter_treats_invalid_or_unclosed_dollar_tags_as_plain_text() -> None:
    assert schemaStatements("SELECT $9$; SELECT $unclosed") == (
        "SELECT $9$",
        "SELECT $unclosed",
    )
