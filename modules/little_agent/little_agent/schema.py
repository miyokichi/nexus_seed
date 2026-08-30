"""A tiny JSON Schema validator for structured agent output.

Only the keywords an ``output_schema`` realistically uses are implemented, so
Little Agent can validate a model's final JSON itself instead of trusting a
particular LLM backend's structured-output feature. Unknown keywords are ignored
rather than rejected, which keeps a richer schema usable as a prompt hint.

Supported: ``type`` (single or list), ``enum``, ``const``, ``properties``,
``required``, ``additionalProperties``, ``items``, ``minItems``/``maxItems``,
``minLength``/``maxLength``, ``minimum``/``maximum``, and the ``anyOf`` /
``oneOf`` / ``allOf`` combinators.
"""

from __future__ import annotations

from typing import Any

_TYPE_NAMES = {
    "object": "object",
    "array": "array",
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "null": "null",
}


def json_type(value: Any) -> str:
    """The JSON Schema type name of a Python value (bools are not integers)."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _type_matches(value: Any, expected: str) -> bool:
    actual = json_type(value)
    if expected == "number":
        return actual in {"number", "integer"}
    return actual == expected


def validate(instance: Any, schema: Any, path: str = "$") -> list[str]:
    """Return a list of human-readable validation errors (empty when valid)."""

    if not isinstance(schema, dict):
        return []  # nothing to check against

    errors: list[str] = []

    expected = schema.get("type")
    if isinstance(expected, str):
        expected = [expected]
    if isinstance(expected, list):
        known = [item for item in expected if item in _TYPE_NAMES]
        if known and not any(_type_matches(instance, item) for item in known):
            errors.append(f"{path}: expected {' or '.join(known)}, got {json_type(instance)}")
            return errors  # further keyword checks would only add noise

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: must be {schema['const']!r}")
    enum = schema.get("enum")
    if isinstance(enum, list) and instance not in enum:
        errors.append(f"{path}: must be one of {enum!r}")

    if isinstance(instance, dict):
        errors.extend(_validate_object(instance, schema, path))
    elif isinstance(instance, list):
        errors.extend(_validate_array(instance, schema, path))
    elif isinstance(instance, str):
        errors.extend(_validate_string(instance, schema, path))
    elif isinstance(instance, (int, float)) and not isinstance(instance, bool):
        errors.extend(_validate_number(instance, schema, path))

    errors.extend(_validate_combinators(instance, schema, path))
    return errors


def _validate_object(instance: dict[str, Any], schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}

    required = schema.get("required")
    if isinstance(required, list):
        for key in required:
            if isinstance(key, str) and key not in instance:
                errors.append(f"{path}: missing required property '{key}'")

    for key, value in instance.items():
        if key in properties:
            errors.extend(validate(value, properties[key], f"{path}.{key}"))

    extra = schema.get("additionalProperties")
    if extra is False:
        for key in instance:
            if key not in properties:
                errors.append(f"{path}: unexpected property '{key}'")
    elif isinstance(extra, dict):
        for key, value in instance.items():
            if key not in properties:
                errors.extend(validate(value, extra, f"{path}.{key}"))
    return errors


def _validate_array(instance: list[Any], schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    items = schema.get("items")
    if isinstance(items, dict):
        for index, value in enumerate(instance):
            errors.extend(validate(value, items, f"{path}[{index}]"))
    minimum = schema.get("minItems")
    if isinstance(minimum, int) and len(instance) < minimum:
        errors.append(f"{path}: needs at least {minimum} item(s), got {len(instance)}")
    maximum = schema.get("maxItems")
    if isinstance(maximum, int) and len(instance) > maximum:
        errors.append(f"{path}: allows at most {maximum} item(s), got {len(instance)}")
    return errors


def _validate_string(instance: str, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    minimum = schema.get("minLength")
    if isinstance(minimum, int) and len(instance) < minimum:
        errors.append(f"{path}: shorter than minLength {minimum}")
    maximum = schema.get("maxLength")
    if isinstance(maximum, int) and len(instance) > maximum:
        errors.append(f"{path}: longer than maxLength {maximum}")
    return errors


def _validate_number(instance: float, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and instance < minimum:
        errors.append(f"{path}: must be >= {minimum}")
    maximum = schema.get("maximum")
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and instance > maximum:
        errors.append(f"{path}: must be <= {maximum}")
    return errors


def _validate_combinators(instance: Any, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for subschema in all_of:
            errors.extend(validate(instance, subschema, path))
    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and any_of:
        if all(validate(instance, subschema, path) for subschema in any_of):
            errors.append(f"{path}: does not match any of the allowed schemas")
    one_of = schema.get("oneOf")
    if isinstance(one_of, list) and one_of:
        matched = sum(1 for subschema in one_of if not validate(instance, subschema, path))
        if matched != 1:
            errors.append(f"{path}: must match exactly one schema, matched {matched}")
    return errors
