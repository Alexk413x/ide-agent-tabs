from __future__ import annotations

import os
import unittest

from ide_agent_tabs.messaging.messaging import MAX_WAIT_S
from ide_agent_tabs.register import (
    OPENCODE_TIMEOUT_MS,
    TOOL_TIMEOUT_S,
    config_file,
    goose_entry,
    hermes_entry,
    opencode_entry,
    pi_entry,
    qwen_entry,
    takes_hooks,
)


def none(_file: str) -> bool:
    return False


class ConfigFileTest(unittest.TestCase):
    def test_finds_each_agent_config_file_honoring_the_relocation_variables(self) -> None:
        h = "h"
        j = os.path.join
        self.assertEqual(config_file("codex", {}, h, none), j("h", ".codex", "config.toml"))
        self.assertEqual(config_file("codex", {"CODEX_HOME": "c"}, h, none), j("c", "config.toml"))
        self.assertEqual(config_file("gemini", {}, h, none), j("h", ".gemini", "settings.json"))
        self.assertEqual(config_file("gemini", {"GEMINI_CLI_HOME": "g"}, h, none), j("g", ".gemini", "settings.json"))
        self.assertEqual(config_file("copilot", {}, h, none), j("h", ".copilot", "mcp-config.json"))
        self.assertEqual(config_file("copilot", {"COPILOT_HOME": "p"}, h, none), j("p", "mcp-config.json"))
        self.assertEqual(config_file("agy", {"GEMINI_CLI_HOME": "g"}, h, none), j("h", ".gemini", "config", "mcp_config.json"))
        self.assertEqual(config_file("opencode", {}, h, none), j("h", ".config", "opencode", "opencode.json"))
        self.assertEqual(config_file("opencode", {"XDG_CONFIG_HOME": "x"}, h, none), j("x", "opencode", "opencode.json"))
        jsonc = j("x", "opencode", "opencode.jsonc")
        self.assertEqual(config_file("opencode", {"XDG_CONFIG_HOME": "x"}, h, lambda f: f == jsonc), jsonc)

    def test_finds_the_config_of_each_newer_agent_honoring_the_relocation_variables(self) -> None:
        h = "h"
        j = os.path.join
        self.assertEqual(config_file("grok", {}, h, none), j(h, ".grok", "config.toml"))
        self.assertEqual(config_file("grok", {"GROK_HOME": "g"}, h, none), j("g", "config.toml"))
        self.assertEqual(config_file("pi", {}, h, none), j(h, ".pi", "agent", "mcp.json"))
        self.assertEqual(config_file("pi", {"PI_CODING_AGENT_DIR": "p"}, h, none), j("p", "mcp.json"))
        self.assertEqual(config_file("hermes", {}, h, none, "linux"), j(h, ".hermes", "config.yaml"))
        self.assertEqual(config_file("hermes", {"LOCALAPPDATA": "L"}, h, none, "win32"), j("L", "hermes", "config.yaml"))
        self.assertEqual(config_file("hermes", {"HERMES_HOME": "m", "LOCALAPPDATA": "L"}, h, none, "win32"), j("m", "config.yaml"))
        self.assertEqual(config_file("qwen", {}, h, none), j(h, ".qwen", "settings.json"))
        self.assertEqual(config_file("qwen", {"QWEN_HOME": os.path.abspath("q")}, h, none), j(os.path.abspath("q"), "settings.json"))
        self.assertEqual(config_file("goose", {}, h, none, "darwin"), j(h, ".config", "goose", "config.yaml"))
        self.assertEqual(config_file("goose", {"APPDATA": "R"}, h, none, "win32"), j("R", "Block", "goose", "config", "config.yaml"))
        root = os.path.abspath("groot")
        self.assertEqual(config_file("goose", {"GOOSE_PATH_ROOT": root}, h, none, "linux"), j(root, "config", "config.yaml"))

    def test_every_agent_but_pi_and_codex_gets_global_hooks(self) -> None:
        self.assertEqual([takes_hooks(a) for a in ("grok", "pi", "hermes", "qwen", "goose")], [True, False, True, True, True])
        self.assertEqual([takes_hooks(a) for a in ("codex", "gemini", "copilot", "agy")], [False, True, True, True])

    def test_every_mcp_entry_outlasts_the_longest_wait_for_message(self) -> None:
        longest = MAX_WAIT_S + 60
        self.assertGreaterEqual(TOOL_TIMEOUT_S, longest)
        self.assertGreaterEqual(OPENCODE_TIMEOUT_MS, longest * 1000)
        self.assertGreaterEqual(opencode_entry(["s"])["timeout"], longest * 1000)
        self.assertGreaterEqual(pi_entry(["s"])["timeout"], longest)
        self.assertGreaterEqual(hermes_entry(["s"])["timeout"], longest)
        self.assertGreaterEqual(goose_entry(["s"])["timeout"], longest)
        self.assertGreaterEqual(qwen_entry(["s"])["timeout"], longest * 1000)


if __name__ == "__main__":
    unittest.main()
