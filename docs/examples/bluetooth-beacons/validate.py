#!/usr/bin/env python3
"""Validate the specification fixtures or supplied knowledge payloads, offline.

This is a JSON Schema validator, not the advertising semantic compiler. It
does not query adapters, invoke codecs, resolve credentials or transmit data.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

try:
    from jsonschema import Draft202012Validator, FormatChecker
    from jsonschema.exceptions import best_match
    from referencing import Registry, Resource
    from referencing.exceptions import NoSuchResource
except ImportError:
    raise SystemExit(
        "This offline documentation validator requires jsonschema in the "
        "validation environment; Syn's runtime dependencies are unchanged."
    ) from None


HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE.parents[1] / "schemas" / "bluetooth-beacons.schema.json"
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_DOCUMENT_DEPTH = 16


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON property: {key}")
        value[key] = item
    return value


def reject_constant(value):
    raise ValueError(f"Non-finite JSON number: {value}")


def load_document(path, *, depth_limit=MAX_DOCUMENT_DEPTH):
    with path.open("rb") as stream:
        raw = stream.read(MAX_DOCUMENT_BYTES + 1)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("Document exceeds 256 KiB")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                       parse_constant=reject_constant)
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > depth_limit:
            raise ValueError(f"Document exceeds nesting depth {depth_limit}")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return value


def no_remote_resources(uri):
    raise NoSuchResource(ref=uri)


def make_validator():
    schema = load_document(SCHEMA_PATH, depth_limit=64)
    Draft202012Validator.check_schema(schema)
    registry = Registry(retrieve=no_remote_resources).with_resource(
        schema["$id"], Resource.from_contents(schema)
    )
    return Draft202012Validator(
        schema, registry=registry, format_checker=FormatChecker()
    )


def failure(validator, path):
    try:
        instance = load_document(path)
        error = best_match(validator.iter_errors(instance))
        if error is not None:
            location = "/" + "/".join(str(part) for part in error.absolute_path)
            return f"{location}: {error.message}"
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        return f"{type(error).__name__}: {error}"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths", nargs="*", type=Path,
        help="Knowledge payload JSON files to validate; omit to check fixtures.",
    )
    args = parser.parse_args()
    validator = make_validator()
    failures = []
    if args.paths:
        for path in args.paths:
            message = failure(validator, path)
            if message is not None:
                failures.append(f"{path}: {message}")
        print(f"Validated {len(args.paths)} supplied document(s)")
    else:
        valid = sorted((HERE / "valid").glob("*.json"))
        invalid = sorted((HERE / "invalid").glob("*.json"))
        if not valid or not invalid:
            raise SystemExit("Both valid and invalid fixture sets are required")
        for path in valid:
            message = failure(validator, path)
            if message is not None:
                failures.append(f"Expected valid {path.name}: {message}")
        for path in invalid:
            if failure(validator, path) is None:
                failures.append(f"Expected rejection: {path.name}")
        print("Draft 2020-12 meta-schema: passed")
        print(f"Valid fixtures: {len(valid)}; expected-invalid fixtures: {len(invalid)}")
    if failures:
        for message in failures:
            print(message, file=sys.stderr)
        print(f"FAILED: {len(failures)} unexpected result(s)", file=sys.stderr)
        return 1
    print("Passed. Structural validation only; semantic compilation remains required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
