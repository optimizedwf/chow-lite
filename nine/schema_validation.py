"""JSON Schema validation for every boundary object (P1-6).

The README claims "schema-validated" for route decisions, jobs, verdicts,
artifact manifests and route events — this module makes that claim real.
Every boundary writes through validate(); a schema violation raises
SchemaValidationError instead of silently shipping a malformed record.
"""

from __future__ import annotations

import datetime as _dt
import json
import re as _re
from pathlib import Path

import jsonschema
from jsonschema import FormatChecker
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


def _format_checker() -> FormatChecker:
    """FormatChecker with `date-time` explicitly registered.

    torture-16 F2: bare `FormatChecker()` does NOT include date-time in
    jsonschema 4.x (default checkers are date/time/email/ipv4/ipv6/regex/
    uuid only) — passing it in made `format: date-time` a dead constraint
    exactly like passing nothing. Register date-time (RFC 3339 subset via
    datetime.fromisoformat, plus a trailing-Z normalization) so garbage
    timestamps fail validate() at every boundary.
    """
    fc = FormatChecker()

    def _check_date_time(value: str) -> bool:
        if not isinstance(value, str):
            return True  # type mismatch handled by the schema itself
        # torture-17 F6: RFC 3339 requires a time component AND a UTC
        # offset — fromisoformat alone accepts naive/date-only/partial
        # strings ("2026-08-13", "2026-08", "2026", "…T12:00:00") which
        # then compare badly with aware datetimes and silently misorder
        # analytics. Require a T separator (RFC 3339 uppercase; lowercase t
        # also accepted by fromisoformat) and a non-None tzinfo (RFC 3339
        # Z is uppercase; fromisoformat rejects lowercase z).
        if "T" not in value.upper():
            return False
        try:
            dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        if dt.tzinfo is None:
            return False
        # T19-F7 (slice 37): fromisoformat accepts hour-only offsets (+00,
        # -01) and colon-less offsets (+0530) — the docstring claims an
        # "RFC 3339 subset", and RFC 3339 time-offset is Z or +/-HH:MM only.
        # Reject the non-canonical shapes so hand-edited records or plugin
        # verdicts with a non-RFC offset fail the boundary loudly instead of
        # entering durable stores in a shape comparisons/analytics don't
        # expect.
        m = _re.search(r"[+-]\d\d(?::?\d\d)?$", value)
        if m:
            off = m.group(0)
            if len(off) != 6:
                return False  # +00 / -01 (len 3) or +0530 (len 5): not
                              # RFC 3339 (Z or +/-HH:MM only)
        return True

    fc.checkers["date-time"] = (  # type: ignore[assignment]
        _check_date_time,
        ("date-time",),
    )
    return fc


_FORMAT_CHECKER = _format_checker()


def validate(name: str, instance: dict) -> None:
    """Raise SchemaValidationError if instance violates the named schema."""
    schema = _load(name)
    try:
        # torture-16 F2: Draft202012Validator without a format checker made
        # `format: date-time` a DEAD constraint — garbage timestamps passed
        # validate() for every schema. _FORMAT_CHECKER enforces the declared
        # formats (date-time, etc.) at every boundary.
        jsonschema.Draft202012Validator(
            schema, registry=_REGISTRY, format_checker=_FORMAT_CHECKER
        ).validate(instance)
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
