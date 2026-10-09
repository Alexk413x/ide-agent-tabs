from __future__ import annotations

import json
import os
import unittest
from typing import Any, Callable

from ide_agent_tabs.agent_config import with_agy_allow_rule, with_codex_settings
from ide_agent_tabs.register import edit_json, entry_server_path, parse_codex_get, same_path, strip_json_comments, with_server_entry
from ide_agent_tabs.sync import file_url, hook_message, needs_sync, parse_extension_list, repository_version, update_plugins_xml
from ide_agent_tabs.toml_table import read_toml_table, toml_table, with_toml_table
from ide_agent_tabs.yaml_block import YamlDoc, edit_yaml
from support import FIXTURES

_OUR_HOOK = ("agent-hook.mjs", "agent_hook.py")


def _hermes_hooks(text: str | None, items: list[dict[str, Any]] | None) -> str | None:
    def change(doc: YamlDoc) -> None:
        doc.filter_lists("hooks", lambda item: isinstance(item, dict) and any(h in str(item.get("command")) for h in _OUR_HOOK))
        for item in items or []:
            doc.add_list_item("hooks", item["event"], {"command": item["command"], "timeout": item["timeout"]})

    return edit_yaml(text, "f", change)


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "withTomlTable": lambda t, table: with_toml_table(t, "f", "mcp_servers", "ide-agent-tabs", table),
    "readTomlTable": lambda t: read_toml_table(t, "mcp_servers", "ide-agent-tabs"),
    "tomlTable": lambda python, server: toml_table("mcp_servers", "ide-agent-tabs", {"command": python, "args": ["-I", "-S", server]}),
    "withServerEntry": lambda t, section, entry, skeleton: with_server_entry(t, "f", section, entry, skeleton),
    "editJsonAllow": lambda t, allow: edit_json(t, "f", lambda root: with_agy_allow_rule(root, "f", allow)),
    "withCodexSettings": lambda t: with_codex_settings(t, "f"),
    "stripJsonComments": strip_json_comments,
    "entryServerPath": entry_server_path,
    "parseCodexGet": parse_codex_get,
    "samePath": same_path,
    "parseExtensionList": parse_extension_list,
    "fileUrl": file_url,
    "updatePluginsXml": update_plugins_xml,
    "repositoryVersion": repository_version,
    "hookMessage": hook_message,
    "needsSync": needs_sync,
    "setYamlEntry": lambda t, section, entry: edit_yaml(t, "f", lambda doc: doc.set_entry(section, "ide-agent-tabs", entry)),
    "hermesHooks": _hermes_hooks,
    "yamlScalar": lambda value: edit_yaml("k: 1\n", "f", lambda doc: doc.set_entry("m", value, {value: value})),
}

# The JSON and YAML parsers word their errors differently; the other errors are Agent Tabs' own and match.
_PARSER_ERRORS = ("isn't plain JSON", "isn't valid YAML", "doesn't hold a JSON object")


def load_cases() -> list[dict[str, Any]]:
    with open(os.path.join(FIXTURES, "config.json"), encoding="utf-8") as f:
        return json.load(f)["cases"]


class ConfigParity(unittest.TestCase):
    def test_every_case_matches_the_node_implementation(self) -> None:
        cases = load_cases()
        self.assertEqual(sorted({c["fn"] for c in cases}), sorted(FUNCTIONS))
        for case in cases:
            with self.subTest(fn=case["fn"], args=json.dumps(case["args"])[:200]):
                run = FUNCTIONS[case["fn"]]
                if "error" in case:
                    with self.assertRaises(ValueError) as caught:
                        run(*case["args"])
                    if not any(p in case["error"] for p in _PARSER_ERRORS):
                        self.assertEqual(str(caught.exception), case["error"])
                    continue
                self.assertEqual(run(*case["args"]), case["result"])


if __name__ == "__main__":
    unittest.main()
