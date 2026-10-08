from __future__ import annotations

import json
import os
import unittest
from typing import Any

from support import PACKAGE

KNOWN_KEYWORDS = {
    "type",
    "properties",
    "required",
    "items",
    "enum",
    "description",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "pattern",
    "format",
    "default",
    "additionalProperties",
    "anyOf",
    "oneOf",
    "propertyNames",
    "const",
    "$schema",
}


def keywords(schema: Any, found: set[str]) -> None:
    if isinstance(schema, dict):
        for key, value in schema.items():
            found.add(key)
            if key == "properties":
                for sub in value.values():
                    keywords(sub, found)
            elif key in ("items", "additionalProperties", "propertyNames"):
                keywords(value, found)
            elif key in ("anyOf", "oneOf"):
                for sub in value:
                    keywords(sub, found)


class CatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        with open(os.path.join(PACKAGE, "catalog.json"), encoding="utf-8") as f:
            self.catalog = json.load(f)

    def test_every_tool_has_a_name_description_and_object_schema(self) -> None:
        names = [t["name"] for t in self.catalog["tools"]]
        self.assertEqual(len(names), len(set(names)))
        for tool in self.catalog["tools"]:
            with self.subTest(tool=tool["name"]):
                self.assertTrue(tool["description"])
                self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_client_only_tools_name_real_tools(self) -> None:
        names = {t["name"] for t in self.catalog["tools"]}
        self.assertEqual(self.catalog["only"], {"agent_tabs_hook": ["codex"], "agent_tabs_mod": ["claude"]})
        self.assertLessEqual(set(self.catalog["only"]), names)

    def test_schemas_use_only_the_keywords_the_validator_will_know(self) -> None:
        found: set[str] = set()
        for tool in self.catalog["tools"]:
            keywords(tool["inputSchema"], found)
        self.assertLessEqual(found, KNOWN_KEYWORDS, "a new JSON Schema keyword needs support in the Python validator")

    def test_instructions_cover_both_jev_states(self) -> None:
        self.assertIn("jev_", self.catalog["instructions"]["jev"])
        self.assertNotIn("jev_", self.catalog["instructions"]["plain"])


if __name__ == "__main__":
    unittest.main()
