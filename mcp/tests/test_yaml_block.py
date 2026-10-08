from __future__ import annotations

import unittest

from ide_agent_tabs.yaml_block import YamlDoc, YamlError, edit_yaml, yaml_value

ENTRY = {"command": "/usr/bin/python3", "args": ["-I", "-S", "/h/mcp_server.py"], "env": {"A": "${A}"}, "timeout": 660}


def set_entry(text: str | None, section: str = "mcp_servers", entry: object = ENTRY) -> str | None:
    return edit_yaml(text, "config.yaml", lambda doc: doc.set_entry(section, "ide-agent-tabs", entry))


class RoundTrips(unittest.TestCase):
    def test_an_entry_goes_in_and_comes_out_leaving_the_rest_byte_for_byte(self) -> None:
        original = (
            "# my config\n"
            "model:   x   # spacing kept\n"
            "system_prompt: |\n"
            "  You are\n"
            "  helpful: yes\n"
            "toolsets: [web, 'files']\n"
            "mcp_servers:\n"
            "  # a comment inside\n"
            "  other:\n"
            "    command: foo\n"
            "\n"
            "# trailing comment\n"
        )
        added = set_entry(original)
        assert added is not None
        self.assertIn("  other:\n    command: foo\n  ide-agent-tabs:\n    command: /usr/bin/python3\n    args:\n      - -I\n", added)
        self.assertTrue(added.startswith(original[: original.index("  other:")]))
        self.assertIsNone(set_entry(added), "the same entry writes nothing")
        self.assertEqual(set_entry(added, entry=None), original)
        self.assertEqual(yaml_value(added, "f", "mcp_servers")["ide-agent-tabs"], ENTRY)

    def test_crlf_and_a_missing_final_newline_are_handled(self) -> None:
        added = set_entry("a: 1\r\nmcp_servers:\r\n  other:\r\n    command: x")
        assert added is not None
        self.assertNotIn("\n", added.replace("\r\n", ""))
        self.assertTrue(added.endswith("\r\n"))

    def test_a_section_is_created_at_the_end_or_filled_when_empty(self) -> None:
        self.assertEqual(set_entry(None, entry={"a": 1}), "mcp_servers:\n  ide-agent-tabs:\n    a: 1\n")
        for empty in ("mcp_servers:\n", "mcp_servers: ~\n", "mcp_servers: {}\n", "mcp_servers: null # none\n"):
            self.assertEqual(set_entry(empty, entry={"a": 1}), "mcp_servers:\n  ide-agent-tabs:\n    a: 1\n", empty)
        self.assertEqual(set_entry("x: 1\n", entry={"a": 1}), "x: 1\nmcp_servers:\n  ide-agent-tabs:\n    a: 1\n")
        self.assertEqual(set_entry("mcp_servers:\n  ide-agent-tabs:\n    a: 1\n", entry=None), "mcp_servers: {}\n")
        self.assertIsNone(set_entry("x: 1\n", entry=None))

    def test_an_existing_entry_is_replaced_in_place_at_its_indent(self) -> None:
        text = "mcp_servers:\n    ide-agent-tabs:\n        command: node\n    other:\n        command: x\n"
        out = set_entry(text, entry={"command": "py"})
        self.assertEqual(out, "mcp_servers:\n    ide-agent-tabs:\n      command: py\n    other:\n        command: x\n")
        self.assertEqual(yaml_value(out, "f", "mcp_servers"), {"ide-agent-tabs": {"command": "py"}, "other": {"command": "x"}})

    def test_lists_at_the_key_indent_and_inline_maps_in_lists_parse(self) -> None:
        text = "hooks:\n  pre:\n  - command: a\n    timeout: 5\n  -   command: b\n  post:\n    -\n      command: c\n"
        self.assertEqual(
            yaml_value(text, "f", "hooks"), {"pre": [{"command": "a", "timeout": 5}, {"command": "b"}], "post": [{"command": "c"}]}
        )
        out = edit_yaml(text, "f", lambda doc: doc.add_list_item("hooks", "pre", {"command": "d"}))
        self.assertEqual(out, text.replace("  -   command: b\n", "  -   command: b\n  - command: d\n"))

    def test_scalars_resolve_like_the_yaml_core_schema(self) -> None:
        text = "s:\n  a: 1\n  b: -2.5\n  c: true\n  d: ~\n  e: 'it''s'\n  f: \"q\\\"\\u00e9\"\n  g: 0x1f\n  h: plain text # note\n  i: []\n  j: {k: v}\n"
        self.assertEqual(
            yaml_value(text, "f", "s"),
            {"a": 1, "b": -2.5, "c": True, "d": None, "e": "it's", "f": 'q"\u00e9', "g": 31, "h": "plain text", "i": [], "j": {"k": "v"}},
        )

    def test_hook_lists_are_filtered_and_emptied_sections_removed(self) -> None:
        text = "a: 1\nhooks:\n  x:\n    - command: ours\n  y:\n    - command: mine\n    - command: ours\n"

        def drop(doc: YamlDoc) -> None:
            doc.filter_lists("hooks", lambda item: isinstance(item, dict) and item.get("command") == "ours")

        self.assertEqual(edit_yaml(text, "f", drop), "a: 1\nhooks:\n  y:\n    - command: mine\n")
        self.assertEqual(edit_yaml("a: 1\nhooks:\n  x:\n    - command: ours\n", "f", drop), "a: 1\n")
        self.assertIsNone(edit_yaml("a: 1\n", "f", drop))


class Refusals(unittest.TestCase):
    def assert_refused(self, text: str, pattern: str, section: str = "mcp_servers") -> None:
        with self.assertRaisesRegex(YamlError, pattern):
            set_entry(text, section)

    def test_a_document_that_is_not_a_mapping(self) -> None:
        self.assert_refused("- 1\n", "doesn't hold a YAML mapping")
        self.assert_refused("  a: 1\nb: 2\n", "unexpected indentation on line 1")

    def test_constructs_outside_the_block_subset_in_the_section_it_edits(self) -> None:
        cases = {
            "mcp_servers:\n  x: |\n    text\n": "a block scalar on line 2",
            "mcp_servers:\n  x: &a 1\n": "an anchor, alias or tag",
            "mcp_servers:\n  x: !!str 1\n": "an anchor, alias or tag",
            "mcp_servers:\n  x: [a, [b]]\n": "a nested flow collection",
            "mcp_servers:\n  x: [a,\n    b]\n": "continues on the next line",
            "mcp_servers:\n  x: plain\n    more\n": "continues on the next line",
            "mcp_servers:\n\tx: 1\n": "a tab in the indentation",
            "mcp_servers:\n  ? x\n  : 1\n": "a complex key",
            "mcp_servers:\n  x: 1\n  x: 2\n": "the key x twice",
            "mcp_servers: {a: 1}\n": '"mcp_servers" in flow style',
            "a: 1\n---\nb: 2\n": "more than one document",
            "a: [\n]\n": "a line that is not a key: value pair on line 2",
            'mcp_servers:\n  x: "open\n': "continues on the next line",
        }
        for text, pattern in cases.items():
            with self.subTest(text=text):
                self.assert_refused(text, pattern)
                self.assert_refused(text, "edit it by hand")

    def test_a_section_of_the_wrong_kind(self) -> None:
        self.assert_refused("mcp_servers: [1]\n", '"mcp_servers" isn\'t a mapping')
        self.assert_refused("mcp_servers: text\n", '"mcp_servers" isn\'t a mapping')
        with self.assertRaisesRegex(YamlError, "hooks.x isn't a list"):
            edit_yaml("hooks:\n  x: 1\n", "f", lambda doc: doc.add_list_item("hooks", "x", {"a": 1}))
        with self.assertRaisesRegex(YamlError, "in flow style"):
            edit_yaml("hooks:\n  x: [1]\n", "f", lambda doc: doc.add_list_item("hooks", "x", {"a": 1}))

    def test_other_sections_may_use_any_yaml(self) -> None:
        text = "prompt: >\n  folded\n  text\nanchors: &x\n  a: 1\nmore: *x\n"
        self.assertEqual(set_entry(text, entry={"a": 1}), text + "mcp_servers:\n  ide-agent-tabs:\n    a: 1\n")

    def test_values_it_cannot_write(self) -> None:
        with self.assertRaisesRegex(YamlError, "line break"):
            set_entry("", entry={"a": "x\ny"})


if __name__ == "__main__":
    unittest.main()
