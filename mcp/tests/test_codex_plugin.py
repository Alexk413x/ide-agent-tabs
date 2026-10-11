"""What Codex reads from the plugin folder, and that Claude Code's files stay as they were."""

from __future__ import annotations

import json
import os
import unittest

from support import ROOT

PLUGIN = os.path.join(ROOT, "claude-plugin")


def read_json(*parts: str) -> dict[str, object]:
    with open(os.path.join(PLUGIN, *parts), encoding="utf-8") as f:
        return json.load(f)


class CodexManifestTest(unittest.TestCase):
    def test_it_tracks_the_claude_manifest(self) -> None:
        claude = read_json(".claude-plugin", "plugin.json")
        codex = read_json(".codex-plugin", "plugin.json")
        for key in ("name", "version", "description", "license", "repository"):
            self.assertEqual(codex[key], claude[key], key)

    def test_it_declares_no_mcp_server(self) -> None:
        # Codex follows mcpServers in the Claude manifest when it finds no .codex-plugin, loads the HTTP
        # entry, cannot expand ${user_config.server_port}, and fails to start every thread.
        self.assertNotIn("mcpServers", read_json(".codex-plugin", "plugin.json"))

    def test_it_names_a_hooks_file_with_no_hooks(self) -> None:
        path = str(read_json(".codex-plugin", "plugin.json")["hooks"])
        self.assertEqual(path, "./hooks/codex-hooks.json")
        self.assertEqual(read_json(*path.split("/")[1:])["hooks"], {})

    def test_its_skills_folder_exists(self) -> None:
        path = str(read_json(".codex-plugin", "plugin.json")["skills"])
        self.assertTrue(os.path.isfile(os.path.join(PLUGIN, path, "new-tab", "SKILL.md")))


class ClaudeMcpFileTest(unittest.TestCase):
    def test_the_manifest_names_the_file_and_the_plugin_has_no_root_mcp_json(self) -> None:
        path = read_json(".claude-plugin", "plugin.json")["mcpServers"]
        self.assertEqual(path, "./mcp/claude.mcp.json")
        self.assertTrue(os.path.isfile(os.path.join(PLUGIN, "mcp", "claude.mcp.json")))
        self.assertFalse(os.path.exists(os.path.join(PLUGIN, ".mcp.json")))


if __name__ == "__main__":
    unittest.main()
