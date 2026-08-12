"""JSON Schema validation for every boundary object (P1-6).

The README claims "schema-validated" for route decisions, jobs, verdicts,
artifact manifests and route events — this module makes that claim real.
Every boundary writes through validate(); a schema violation raises
SchemaValidationError instead of silently shipping a malformed record.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
from referencing import Registry, Resource

_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
_CACHE: dict[str, dict] = {}


class SchemaValidationError(ValueError):
    """Raised when a boundary object fails its declared JSON Schema."""


def _load(name: str) -> dict:
    if name not in _CACHE:
        _CACHE[name] = json.loads((_SCHEMA_DIR / f"{name}.schema.json").read_text())
    return _CACHE[name]


def _registry() -> Registry:
    """Registry keyed by each schema's $id so $refs resolve across schemas."""
    reg: Registry = Registry()
    for f in sorted(_SCHEMA_DIR.glob("*.schema.json")):
        doc = json.loads(f.read_text())
        rid = doc.get("$id")
        if rid:
            reg = reg.with_resource(rid, Resource.from_contents(doc))
    return reg


_REGISTRY = _registry()


def validate(name: str, instance: dict) -> None:
    """Raise SchemaValidationError if instance violates the named schema."""
    schema = _load(name)
    try:
        jsonschema.Draft202012Validator(schema, registry=_REGISTRY).validate(instance)
    except jsonschema.ValidationError as exc:
        raise SchemaValidationError(
            f"{name} schema violation: {exc.message} (path: {list(exc.path)})"
        ) from exc


def is_valid(name: str, instance: dict) -> bool:
    try:
        validate(name, instance)
        return True
    except SchemaValidationError:
        return False
