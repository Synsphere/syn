from __future__ import annotations

from importlib.resources import files


def schemaSql() -> str:
    return files("syn").joinpath("schema.sql").read_text(encoding="utf-8-sig")


def schemaStatements(sql: str | None = None) -> tuple[str, ...]:
    """Split the packaged PostgreSQL schema without breaking function bodies."""

    source = schemaSql() if sql is None else sql
    statements: list[str] = []
    current: list[str] = []
    index = 0
    singleQuoted = False
    doubleQuoted = False
    lineComment = False
    blockCommentDepth = 0
    dollarTag: str | None = None

    while index < len(source):
        character = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""

        if lineComment:
            current.append(character)
            if character == "\n":
                lineComment = False
            index += 1
            continue

        if blockCommentDepth:
            current.append(character)
            if character == "/" and following == "*":
                current.append(following)
                blockCommentDepth += 1
                index += 2
            elif character == "*" and following == "/":
                current.append(following)
                blockCommentDepth -= 1
                index += 2
            else:
                index += 1
            continue

        if dollarTag is not None:
            if source.startswith(dollarTag, index):
                current.append(dollarTag)
                index += len(dollarTag)
                dollarTag = None
            else:
                current.append(character)
                index += 1
            continue

        if singleQuoted:
            current.append(character)
            if character == "'":
                if following == "'":
                    current.append(following)
                    index += 2
                    continue
                singleQuoted = False
            index += 1
            continue

        if doubleQuoted:
            current.append(character)
            if character == '"':
                if following == '"':
                    current.append(following)
                    index += 2
                    continue
                doubleQuoted = False
            index += 1
            continue

        if character == "-" and following == "-":
            current.extend((character, following))
            lineComment = True
            index += 2
            continue

        if character == "/" and following == "*":
            current.extend((character, following))
            blockCommentDepth = 1
            index += 2
            continue

        if character == "'":
            current.append(character)
            singleQuoted = True
            index += 1
            continue

        if character == '"':
            current.append(character)
            doubleQuoted = True
            index += 1
            continue

        if character == "$":
            tagEnd = source.find("$", index + 1)
            if tagEnd != -1:
                candidate = source[index : tagEnd + 1]
                tagBody = candidate[1:-1]
                if not tagBody or (
                    (tagBody[0].isalpha() or tagBody[0] == "_")
                    and all(char.isalnum() or char == "_" for char in tagBody)
                ):
                    dollarTag = candidate
                    current.append(candidate)
                    index = tagEnd + 1
                    continue

        if character == ";":
            statement = "".join(current).strip()
            current.clear()
            if statement and statement.upper() not in {"BEGIN", "COMMIT"}:
                statements.append(statement)
            index += 1
            continue

        current.append(character)
        index += 1

    remainder = "".join(current).strip()
    if remainder and not remainder.startswith("--"):
        statements.append(remainder)
    return tuple(statements)
