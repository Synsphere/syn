from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote


LOCAL_KINDS = frozenset(
    {
        "type",
        "thing",
        "event",
        "ember",
        "cog",
        "credential",
        "enigma",
        "pondle",
        "snug",
        "om",
    }
)
_LOCAL_REF = re.compile(
    r"^syn://(?P<table>type|thing|event|ember|cog|credential|enigma|pondle|snug|om)/"
    r"(?P<slug>[a-z0-9]+(?:[-_][a-z0-9]+)*)$"
)
_SLUG_CHARS = re.compile(r"[^a-z0-9_-]+")
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class ObjectRef(str):
    """A URL reference understood by Syn.

    General ObjectRefs may be absolute or relative URL references. Local references
    use ``syn://{kind}/{slug}`` and are validated more strictly.
    """

    def __new__(cls, value: str) -> ObjectRef:
        if not isinstance(value, str):
            raise TypeError("ObjectRef must be created from a string")
        if not value or any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError("ObjectRef must be a non-empty URL reference without whitespace")
        if _BAD_PERCENT_ESCAPE.search(value):
            raise ValueError("ObjectRef contains an invalid percent escape")
        if value.startswith("syn://") and _LOCAL_REF.fullmatch(value) is None:
            raise ValueError(f"Invalid local ObjectRef: {value}")
        return str.__new__(cls, value)

    @classmethod
    def local(cls, objectTable: str, slug: str) -> ObjectRef:
        if objectTable not in LOCAL_KINDS:
            raise ValueError(f"Unsupported local object table: {objectTable}")
        normalized = normalizeSlug(slug)
        if not normalized:
            raise ValueError("A local ObjectRef requires a non-empty slug")
        return cls(f"syn://{objectTable}/{quote(normalized, safe='-_')}")

    @property
    def isLocal(self) -> bool:
        return self.startswith("syn://")

    @property
    def objectTable(self) -> str | None:
        match = _LOCAL_REF.fullmatch(self)
        return match.group("table") if match else None

    @property
    def slug(self) -> str | None:
        match = _LOCAL_REF.fullmatch(self)
        return match.group("slug") if match else None


def normalizeSlug(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    asciiValue = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    normalized = _SLUG_CHARS.sub("-", asciiValue).strip("-_")
    normalized = re.sub(r"[-_]{2,}", "-", normalized)
    return normalized[:255].rstrip("-_")
