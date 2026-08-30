"""Tests for the built-in JSON Schema subset validator."""

from __future__ import annotations

import unittest

from little_agent.schema import json_type, validate


class TypeTests(unittest.TestCase):
    def test_json_type_names(self) -> None:
        self.assertEqual(json_type(None), "null")
        self.assertEqual(json_type(True), "boolean")
        self.assertEqual(json_type(3), "integer")
        self.assertEqual(json_type(3.5), "number")
        self.assertEqual(json_type("s"), "string")
        self.assertEqual(json_type([]), "array")
        self.assertEqual(json_type({}), "object")

    def test_integer_satisfies_number_but_bool_does_not(self) -> None:
        self.assertEqual(validate(3, {"type": "number"}), [])
        self.assertTrue(validate(True, {"type": "integer"}))

    def test_type_may_be_a_list(self) -> None:
        schema = {"type": ["string", "null"]}
        self.assertEqual(validate("x", schema), [])
        self.assertEqual(validate(None, schema), [])
        self.assertTrue(validate(1, schema))

    def test_unknown_keywords_are_ignored(self) -> None:
        self.assertEqual(validate({"a": 1}, {"type": "object", "$id": "x", "format": "custom"}), [])


class ObjectTests(unittest.TestCase):
    SCHEMA = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "score": {"type": "integer", "minimum": 0}},
        "required": ["name"],
        "additionalProperties": False,
    }

    def test_valid(self) -> None:
        self.assertEqual(validate({"name": "a", "score": 2}, self.SCHEMA), [])

    def test_missing_required(self) -> None:
        errors = validate({"score": 2}, self.SCHEMA)
        self.assertIn("missing required property 'name'", errors[0])

    def test_additional_property_rejected(self) -> None:
        errors = validate({"name": "a", "extra": 1}, self.SCHEMA)
        self.assertIn("unexpected property 'extra'", errors[0])

    def test_nested_error_reports_its_path(self) -> None:
        schema = {"type": "object", "properties": {"inner": self.SCHEMA}}
        errors = validate({"inner": {"name": 5}}, schema)
        self.assertIn("$.inner.name", errors[0])

    def test_additional_properties_schema_is_applied(self) -> None:
        schema = {"type": "object", "additionalProperties": {"type": "string"}}
        self.assertEqual(validate({"a": "x"}, schema), [])
        self.assertTrue(validate({"a": 1}, schema))


class ArrayAndScalarTests(unittest.TestCase):
    def test_items_and_bounds(self) -> None:
        schema = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 2}
        self.assertEqual(validate(["a"], schema), [])
        self.assertTrue(validate([], schema))
        self.assertTrue(validate(["a", "b", "c"], schema))
        self.assertIn("$[0]", validate([1], schema)[0])

    def test_numeric_bounds(self) -> None:
        schema = {"type": "number", "minimum": 0, "maximum": 1}
        self.assertEqual(validate(0.5, schema), [])
        self.assertTrue(validate(1.5, schema))
        self.assertTrue(validate(-0.5, schema))

    def test_string_length(self) -> None:
        schema = {"type": "string", "minLength": 2, "maxLength": 3}
        self.assertEqual(validate("ab", schema), [])
        self.assertTrue(validate("a", schema))
        self.assertTrue(validate("abcd", schema))

    def test_enum_and_const(self) -> None:
        self.assertEqual(validate("a", {"enum": ["a", "b"]}), [])
        self.assertTrue(validate("c", {"enum": ["a", "b"]}))
        self.assertEqual(validate(5, {"const": 5}), [])
        self.assertTrue(validate(6, {"const": 5}))


class CombinatorTests(unittest.TestCase):
    def test_any_of(self) -> None:
        schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        self.assertEqual(validate("a", schema), [])
        self.assertEqual(validate(1, schema), [])
        self.assertTrue(validate([], schema))

    def test_one_of_requires_exactly_one_match(self) -> None:
        schema = {"oneOf": [{"type": "integer"}, {"type": "number"}]}
        self.assertTrue(validate(1, schema))  # matches both
        self.assertEqual(validate(1.5, schema), [])

    def test_all_of(self) -> None:
        schema = {"allOf": [{"type": "object"}, {"required": ["a"]}]}
        self.assertEqual(validate({"a": 1}, schema), [])
        self.assertTrue(validate({"b": 1}, schema))


if __name__ == "__main__":
    unittest.main()
