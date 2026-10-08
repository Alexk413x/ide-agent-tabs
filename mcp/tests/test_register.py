from __future__ import annotations

import json
import os
import sys
import unittest

from config_support import calls, context, fake_cli, make_source, read_json, read_text, system_env, write
from ide_agent_tabs.agent_config import (
    AGY_ALLOW_RULES,
    agy_hook_command,
    hermes_hook_items,
    hook_command,
    hook_config_file,
    posix_hook_command,
)
from ide_agent_tabs.register import (
    AGENTS,
    CODEX_WINDOWS_REFUSAL,
    RegisterContext,
    agents_report,
    config_file,
    hook_target,
    migrate_registrations,
    refresh_copy,
    register_agents,
    same_path,
    server_argv,
    takes_hooks,
    unregister_agents,
)
from ide_agent_tabs.server_copy import hook_copy_path, old_server_path, read_python, server_copy_path
from support import temp_home

WINDOWS = sys.platform == "win32"
TAB_ENV = {"IDE_AGENT_TABS_ID": "${IDE_AGENT_TABS_ID}", "IDE_AGENT_TABS_AGENT": "${IDE_AGENT_TABS_AGENT}"}
HERMES_EVENTS = ["pre_llm_call", "post_tool_call", "pre_approval_request", "post_approval_response", "pre_verify", "on_session_end"]


class Setup:
    def __init__(self, test: unittest.TestCase, installed: tuple[str, ...] = AGENTS) -> None:
        self.root = temp_home(test, "iat-register-")
        self.bin = os.path.join(self.root, "bin")
        self.user = os.path.join(self.root, "user")
        os.makedirs(self.bin)
        for agent in installed:
            fake_cli(self.bin, agent, "codex" if agent == "codex" else None)
        self.env = system_env(
            {
                "PATH": self.bin,
                "CODEX_HOME": os.path.join(self.user, ".codex"),
                "GEMINI_CLI_HOME": self.user,
                "COPILOT_HOME": os.path.join(self.user, ".copilot"),
                "XDG_CONFIG_HOME": os.path.join(self.user, ".config"),
                "HERMES_HOME": os.path.join(self.user, ".hermes"),
            }
        )
        self.ctx = context(make_source(self.root), self.user, self.env)

    def file(self, agent: str) -> str:
        return config_file(agent, self.env, self.user, os.path.exists, self.ctx.platform)

    def hooks(self, agent: str) -> str:
        return hook_config_file(agent, self.env, self.user, self.ctx.platform)


class Register(unittest.TestCase):
    def test_registers_and_unregisters_every_agent_keeping_the_user_entries(self) -> None:
        s = Setup(self)
        ctx = s.ctx
        server = server_copy_path(ctx.home, ctx.platform)
        hook = hook_copy_path(ctx.home, ctx.platform)
        target = hook_target(ctx)
        argv = server_argv(ctx)
        self.assertEqual(argv[1:], ["-I", "-S", server])
        codex_registers = not WINDOWS

        originals = {
            "copilot": '{\n  "mcpServers": {\n    "github": {\n      "type": "http",\n      "url": "https://example.test/mcp"\n    }\n  }\n}\n',
            "codex": 'model = "x"\n\n',
            "grok": '# grok\nmodel = "grok-4"\n\n[mcp_servers.other]\ncommand = "x"\n',
            "pi": json.dumps({"mcpServers": {"other": {"command": "x"}}, "autoEnableCodemode": False}, indent=2),
            "hermes": "# hermes\nmodel:\n  default: x # mine\nmcp_servers:\n  other:\n    command: x\nhooks:\n  pre_llm_call:\n    - command: echo hi\n",
            "qwen": json.dumps(
                {
                    "mcpServers": {"other": {"command": "x"}},
                    "hooks": {"PreToolUse": [{"matcher": "x", "hooks": [{"type": "command", "command": "c.sh"}]}]},
                },
                indent=2,
            ),
            "goose": "extensions:\n  developer:\n    enabled: true\n    type: builtin\n    name: developer\nGOOSE_PROVIDER: ollama\n",
            "agy": "",
        }
        for agent, text in originals.items():
            write(s.file(agent), text)
        agy_user_hooks = {
            "lint": {"PostToolUse": [{"matcher": "run_command", "hooks": [{"type": "command", "command": "lint.cmd", "timeout": 10}]}]}
        }
        write(s.hooks("agy"), json.dumps(agy_user_hooks, indent=2))
        agy_settings = os.path.join(s.user, ".gemini", "antigravity-cli", "settings.json")
        write(agy_settings, json.dumps({"permissions": {"allow": ["command(adb devices)"]}}, indent=2))
        allowlist = os.path.join(s.env["HERMES_HOME"], "shell-hooks-allowlist.json")
        user_approval = {"event": "pre_llm_call", "command": "echo hi", "approved_at": "2026-01-01T00:00:00Z"}
        write(allowlist, json.dumps({"approvals": [user_approval]}, indent=2))

        before = agents_report(ctx)
        self.assertEqual(before["server"], {"path": server, "exists": False, "current": False})
        self.assertEqual(
            [(a["agent"], a["installed"], a["registered"], a.get("error")) for a in before["agents"]],
            [(a, True, False, None) for a in AGENTS],
        )

        report = register_agents(ctx, [*AGENTS, "claude", "nope"])
        expected_errors = [
            "claude: Claude Code gets the server from the plugin; nothing to register",
            f"nope: unknown agent; use {', '.join(AGENTS)}",
            *([f"codex: {CODEX_WINDOWS_REFUSAL}"] if not codex_registers else []),
        ]
        self.assertEqual(report["errors"], expected_errors)
        self.assertEqual(report["server"], {"path": server, "exists": True, "current": True})
        self.assertEqual(read_python(ctx.home), sys.executable)
        for a in report["agents"]:
            if a["agent"] == "codex" and not codex_registers:
                continue
            self.assertTrue(a["ok"] and a["registered"] and a["stable"] and a["path"] == server, a)
            self.assertEqual(a["hooks"], True if takes_hooks(a["agent"]) else None, a)

        self.assertEqual(read_json(s.file("gemini"))["mcpServers"]["ide-agent-tabs"], {"command": argv[0], "args": argv[1:]})
        self.assertEqual(
            read_json(s.file("gemini"))["hooks"]["AfterAgent"],
            [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "name": "ide-agent-tabs",
                            "command": hook_command(target, "gemini", "AfterAgent"),
                            "timeout": 5000,
                        }
                    ]
                }
            ],
        )
        copilot = read_json(s.file("copilot"))
        self.assertEqual(copilot["mcpServers"]["github"], {"type": "http", "url": "https://example.test/mcp"})
        self.assertEqual(
            copilot["mcpServers"]["ide-agent-tabs"], {"type": "local", "command": argv[0], "args": argv[1:], "env": TAB_ENV, "tools": ["*"]}
        )
        self.assertEqual(read_json(s.hooks("copilot"))["hooks"]["agentStop"][0]["args"], ["-I", "-S", hook, "copilot", "agentStop"])
        agy_command = ["py", "-3"] if WINDOWS else [argv[0]]
        self.assertEqual(
            read_json(s.file("agy")), {"mcpServers": {"ide-agent-tabs": {"command": agy_command[0], "args": [*agy_command[1:], *argv[1:]]}}}
        )
        agy_hooks = read_json(s.hooks("agy"))
        self.assertEqual(agy_hooks["lint"], agy_user_hooks["lint"])
        self.assertEqual(
            agy_hooks["ide-agent-tabs"]["Stop"], [{"type": "command", "command": agy_hook_command(target, "Stop"), "timeout": 5}]
        )
        self.assertEqual(read_json(agy_settings)["permissions"]["allow"], ["command(adb devices)", *AGY_ALLOW_RULES])
        self.assertEqual(
            read_json(s.file("opencode")),
            {
                "$schema": "https://opencode.ai/config.json",
                "mcp": {"ide-agent-tabs": {"type": "local", "command": argv, "enabled": True, "timeout": 660000}},
            },
        )
        grok_table = "\n".join(
            [
                "[mcp_servers.ide-agent-tabs]",
                f"command = {json.dumps(argv[0])}",
                "args = [" + ", ".join(json.dumps(a) for a in argv[1:]) + "]",
            ]
        )
        self.assertEqual(read_text(s.file("grok")), originals["grok"] + "\n" + grok_table + "\n")
        self.assertEqual(
            read_json(s.hooks("grok"))["hooks"]["Notification"],
            [
                {
                    "matcher": "permission_prompt|idle_prompt",
                    "hooks": [{"type": "command", "command": hook_command(target, "grok", "Notification"), "timeout": 5}],
                }
            ],
        )
        self.assertEqual(
            read_json(s.file("pi"))["mcpServers"]["ide-agent-tabs"],
            {"command": argv[0], "args": argv[1:], "env": TAB_ENV, "timeout": 660, "exposure": "direct"},
        )
        hermes = read_text(s.file("hermes"))
        self.assertTrue(hermes.startswith("# hermes\nmodel:\n  default: x # mine\n"))
        self.assertIn("pre_llm_call:\n    - command: echo hi\n    - command: ", hermes)
        for item in hermes_hook_items(target):
            self.assertIn(item["command"], hermes)
        self.assertEqual(
            read_json(allowlist)["approvals"],
            [user_approval, *({"event": e, "command": posix_hook_command(target, "hermes", e)} for e in HERMES_EVENTS)],
        )
        qwen = read_json(s.file("qwen"))
        self.assertEqual(qwen["mcpServers"]["ide-agent-tabs"], {"command": argv[0], "args": argv[1:], "env": TAB_ENV, "timeout": 700000})
        self.assertEqual(
            qwen["hooks"]["PreToolUse"][1],
            {"hooks": [{"type": "command", "command": hook_command(target, "qwen", "PreToolUse"), "timeout": 5}]},
        )
        goose = read_text(s.file("goose"))
        self.assertIn("  ide-agent-tabs:\n    name: ide-agent-tabs\n    type: stdio\n    cmd: ", goose)
        self.assertIn("    envs: {}\n    env_keys: []\n    description: Agent Tabs\n", goose)
        self.assertEqual(read_json(os.path.join(s.user, ".agents", "plugins", "ide-agent-tabs", "plugin.json"))["name"], "ide-agent-tabs")
        if codex_registers:
            toml = read_text(s.file("codex"))
            self.assertIn(
                '[mcp_servers.ide-agent-tabs]\nenv_vars = ["IDE_AGENT_TABS_ID", "IDE_AGENT_TABS_AGENT", "IDE_AGENT_TABS_HOME"]\ntool_timeout_sec = 660\n',
                toml,
            )
            writes = [[c["agent"], *c["args"]] for c in calls(s.bin) if c["args"][1] != "get"]
            self.assertEqual(writes, [["codex", "mcp", "add", "ide-agent-tabs", "--", *argv]])
        self.assertTrue(all(same_path(str(c["cwd"]), ctx.home, ctx.platform) for c in calls(s.bin)))

        snapshot = {a: read_text(s.file(a)) for a in AGENTS if a != "codex"}
        again = register_agents(ctx, list(AGENTS))
        self.assertEqual(again["errors"], [] if codex_registers else [f"codex: {CODEX_WINDOWS_REFUSAL}"])
        self.assertEqual({a: read_text(s.file(a)) for a in snapshot}, snapshot, "registering again changes nothing")

        removed = unregister_agents(ctx, list(AGENTS))
        self.assertEqual(removed["errors"], [])
        self.assertTrue(all(a["ok"] and not a["registered"] and not a["hooks"] for a in removed["agents"]))
        for agent in ("grok", "hermes", "goose"):
            self.assertEqual(read_text(s.file(agent)), originals[agent], agent)
        self.assertEqual(read_json(s.file("pi")), json.loads(originals["pi"]))
        self.assertEqual(read_json(s.file("qwen")), json.loads(originals["qwen"]))
        self.assertEqual(read_json(s.file("copilot")), json.loads(originals["copilot"]))
        self.assertNotIn("hooks", read_json(s.file("gemini")))
        self.assertEqual(read_json(s.hooks("agy")), agy_user_hooks)
        self.assertEqual(read_json(agy_settings), {"permissions": {"allow": ["command(adb devices)"]}})
        self.assertEqual(read_json(allowlist), {"approvals": [user_approval]})
        self.assertFalse(os.path.exists(s.hooks("copilot")) or os.path.exists(s.hooks("grok")))
        self.assertFalse(os.path.exists(os.path.join(s.user, ".agents", "plugins", "ide-agent-tabs")))
        if codex_registers:
            self.assertNotIn("ide-agent-tabs", read_text(s.file("codex")))

    def test_refuses_configs_it_cannot_edit_and_agents_that_are_missing(self) -> None:
        s = Setup(self, installed=("copilot", "opencode", "hermes"))
        write(s.file("copilot"), '{ "mcpServers": ')
        jsonc = os.path.join(s.env["XDG_CONFIG_HOME"], "opencode", "opencode.jsonc")
        write(jsonc, '{\n  // my settings\n  "model": "x"\n}\n')
        write(s.file("hermes"), "hooks: [1]\n")
        report = register_agents(s.ctx, ["copilot", "opencode", "codex", "hermes"])
        by_agent = {a["agent"]: a for a in report["agents"]}
        self.assertRegex(by_agent["copilot"]["error"], "isn't plain JSON")
        self.assertRegex(by_agent["opencode"]["error"], r"opencode\.jsonc isn't plain JSON")
        self.assertEqual(by_agent["codex"]["error"], "not installed")
        self.assertRegex(by_agent["hermes"]["error"], '"hooks" isn\'t a mapping')
        self.assertEqual(read_text(s.file("copilot")), '{ "mcpServers": ')
        self.assertFalse(os.path.exists(os.path.join(s.env["HERMES_HOME"], "shell-hooks-allowlist.json")))

    def test_a_hermes_config_outside_the_yaml_subset_is_left_for_the_user(self) -> None:
        s = Setup(self, installed=("hermes",))
        text = "mcp_servers: {other: {command: x}}\n"
        write(s.file("hermes"), text)
        report = register_agents(s.ctx, ["hermes"])
        self.assertRegex(report["errors"][0], "edit it by hand")
        self.assertEqual(read_text(s.file("hermes")), text)


def node_era(s: Setup) -> dict[str, str]:
    home = s.ctx.home
    platform = s.ctx.platform
    server = old_server_path(home, platform)
    hook = old_server_path(home, platform).replace("mcp-server.mjs", "agent-hook.mjs")
    write(os.path.join(home, "mcp", "mcp-server.mjs"), "// 0.8.0\n")
    node = {"command": "node", "args": [server]}
    write(
        s.file("gemini"),
        json.dumps(
            {
                "mcpServers": {"ide-agent-tabs": node},
                "hooks": {
                    "AfterAgent": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "name": "ide-agent-tabs",
                                    "command": f'node "{hook}" gemini AfterAgent',
                                    "timeout": 5000,
                                }
                            ]
                        }
                    ]
                },
            },
            indent=2,
        ),
    )
    write(
        s.file("copilot"),
        json.dumps({"mcpServers": {"ide-agent-tabs": {"type": "local", **node, "env": TAB_ENV, "tools": ["*"]}}}, indent=2),
    )
    write(
        s.hooks("copilot"),
        json.dumps(
            {
                "version": 1,
                "hooks": {"agentStop": [{"type": "command", "exec": "node", "args": [hook, "copilot", "agentStop"], "timeoutSec": 5}]},
            },
            indent=2,
        ),
    )
    write(s.file("agy"), json.dumps({"mcpServers": {"ide-agent-tabs": node}}, indent=2))
    write(
        s.hooks("agy"),
        json.dumps(
            {"ide-agent-tabs": {"Stop": [{"type": "command", "command": f"node {hook} agy Stop", "timeout": 5}]}, "mine": {}}, indent=2
        ),
    )
    write(
        s.file("opencode"),
        json.dumps(
            {"mcp": {"ide-agent-tabs": {"type": "local", "command": ["node", server], "enabled": True, "timeout": 660000}}}, indent=2
        ),
    )
    write(
        s.file("pi"),
        json.dumps({"mcpServers": {"ide-agent-tabs": {**node, "env": TAB_ENV, "timeout": 660, "exposure": "direct"}}}, indent=2),
    )
    write(
        s.file("qwen"),
        json.dumps(
            {
                "mcpServers": {"ide-agent-tabs": {**node, "env": TAB_ENV, "timeout": 700000}},
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": f'node "{hook}" qwen Stop', "timeout": 5}]}]},
            },
            indent=2,
        ),
    )
    write(s.file("grok"), f'model = "x"\n\n[mcp_servers.ide-agent-tabs]\ncommand = "node"\nargs = [{json.dumps(server)}]\n')
    write(
        s.hooks("grok"),
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": f'node "{hook}" grok Stop', "timeout": 5}]}]}}),
    )
    hermes_cmd = f"node '{hook}' hermes pre_llm_call"
    write(
        s.file("hermes"),
        f'mcp_servers:\n  ide-agent-tabs:\n    command: node\n    args:\n      - {server}\n    timeout: 660\nhooks:\n  pre_llm_call:\n    - command: echo hi\n    - command: "{hermes_cmd}"\n      timeout: 5\n',
    )
    write(
        os.path.join(s.env["HERMES_HOME"], "shell-hooks-allowlist.json"),
        json.dumps({"approvals": [{"event": "pre_llm_call", "command": "echo hi"}, {"event": "pre_llm_call", "command": hermes_cmd}]}),
    )
    write(
        s.file("goose"),
        f"extensions:\n  ide-agent-tabs:\n    name: ide-agent-tabs\n    type: stdio\n    cmd: node\n    args:\n      - {server}\n    enabled: true\n",
    )
    write(
        s.hooks("goose"),
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": f"node '{hook}' goose Stop", "timeout": 5}]}]}}),
    )
    write(
        os.path.join(s.user, ".gemini", "antigravity-cli", "settings.json"),
        json.dumps({"permissions": {"allow": ["mcp(ide-agent-tabs/*)"]}}),
    )
    return {"server": server, "hook": hook}


class Migration(unittest.TestCase):
    def test_node_registrations_and_hooks_move_to_python(self) -> None:
        s = Setup(self, installed=tuple(a for a in AGENTS if a != "codex"))
        old = node_era(s)
        refresh_copy(s.ctx)
        migrated, errors = migrate_registrations(s.ctx)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(migrated), sorted(a for a in AGENTS if a != "codex"))
        report = agents_report(s.ctx)
        for a in report["agents"]:
            if a["agent"] == "codex":
                continue
            self.assertTrue(a["registered"] and a["stable"], a)
            self.assertIn(a["hooks"], (True, None), a)
        for agent in [a for a in AGENTS if a != "codex"]:
            self.assertNotIn("mcp-server.mjs", read_text(s.file(agent)), agent)
            if takes_hooks(agent):
                self.assertNotIn("agent-hook.mjs", read_text(s.hooks(agent)), agent)
        self.assertIn("pre_llm_call:\n    - command: echo hi\n", read_text(s.file("hermes")))
        approvals = read_json(os.path.join(s.env["HERMES_HOME"], "shell-hooks-allowlist.json"))["approvals"]
        self.assertEqual(approvals[0], {"event": "pre_llm_call", "command": "echo hi"})
        self.assertFalse(any(old["hook"] in a["command"] for a in approvals))
        self.assertEqual(len(approvals), 1 + len(HERMES_EVENTS))
        self.assertIn("mine", read_json(s.hooks("agy")))
        self.assertEqual(
            read_json(os.path.join(s.user, ".gemini", "antigravity-cli", "settings.json"))["permissions"]["allow"], list(AGY_ALLOW_RULES)
        )
        self.assertEqual(migrate_registrations(s.ctx), ([], []), "a second run finds nothing to move")

        moved = RegisterContext(*s.ctx[:5], python=os.path.join(s.root, "new python", "python3"))
        self.assertTrue(refresh_copy(moved)[1])
        migrated, errors = migrate_registrations(moved)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(migrated), sorted(a for a in AGENTS if a != "codex" and not (WINDOWS and a == "agy")))
        self.assertIn("new python", read_json(s.file("pi"))["mcpServers"]["ide-agent-tabs"]["command"])

    def test_an_agent_registered_with_another_server_stays_as_it_is(self) -> None:
        s = Setup(self, installed=("pi",))
        text = json.dumps({"mcpServers": {"ide-agent-tabs": {"command": "uvx", "args": ["someone-else"]}}}, indent=2)
        write(s.file("pi"), text)
        refresh_copy(s.ctx)
        self.assertEqual(migrate_registrations(s.ctx), ([], []))
        self.assertEqual(read_text(s.file("pi")), text)

    @unittest.skipIf(WINDOWS, "Codex is not registered on Windows")
    def test_a_node_codex_registration_moves_through_the_codex_cli(self) -> None:
        s = Setup(self, installed=("codex",))
        server = old_server_path(s.ctx.home, s.ctx.platform)
        write(os.path.join(s.env["CODEX_HOME"], "fake-mcp.json"), json.dumps({"ide-agent-tabs": {"command": "node", "args": [server]}}))
        write(
            s.file("codex"),
            f'[mcp_servers.ide-agent-tabs]\nenv_vars = ["IDE_AGENT_TABS_ID", "IDE_AGENT_TABS_AGENT", "IDE_AGENT_TABS_HOME"]\ntool_timeout_sec = 660\ncommand = "node"\nargs = ["{server}"]\n',
        )
        refresh_copy(s.ctx)
        self.assertEqual(migrate_registrations(s.ctx), (["codex"], []))
        status = agents_report(s.ctx)["agents"][0]
        self.assertTrue(status["stable"], status)


if __name__ == "__main__":
    unittest.main()
