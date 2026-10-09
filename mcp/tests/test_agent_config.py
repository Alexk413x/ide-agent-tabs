from __future__ import annotations

import os
import unittest

from ide_agent_tabs.agent_config import (
    AGY_ALLOW_RULES,
    HookTarget,
    agy_hook_command,
    copilot_hooks,
    has_any_hermes_hooks,
    has_any_settings_hooks,
    has_our_hooks,
    hermes_hook_items,
    hook_command,
    hook_config_file,
    mentions_hook,
    merge_hook_settings,
    posix_hook_command,
    posix_quote,
    with_agy_allow_rule,
    with_hermes_approvals,
)

HOOK = "/home/a/.ide-agent-tabs/mcp/py/launch/agent_hook.py"
WIN_HOOK = "C:/Users/a/.ide-agent-tabs/mcp/py/launch/agent_hook.py"
POSIX = HookTarget("/usr/bin/python3", HOOK, "linux")
WINDOWS = HookTarget("C:/Python313/python.exe", WIN_HOOK, "win32")
OLD_NODE = "node /home/a/.ide-agent-tabs/mcp/agent-hook.mjs"


class Commands(unittest.TestCase):
    def test_shell_hooks_put_a_bare_interpreter_first(self) -> None:
        self.assertEqual(hook_command(POSIX, "gemini", "Stop"), f'/usr/bin/python3 -I -S "{HOOK}" gemini Stop')
        self.assertEqual(hook_command(WINDOWS, "qwen", "Stop"), f'C:/Python313/python.exe -I -S "{WIN_HOOK}" qwen Stop')

    def test_an_interpreter_path_a_shell_would_split_falls_back(self) -> None:
        spaced = HookTarget("C:/Users/a b/python.exe", WIN_HOOK, "win32")
        self.assertEqual(hook_command(spaced, "grok", "Stop"), f'py -3 -I -S "{WIN_HOOK}" grok Stop')
        posix = HookTarget("/opt/my python/bin/python3", HOOK, "darwin")
        self.assertEqual(hook_command(posix, "grok", "Stop"), f'"/opt/my python/bin/python3" -I -S "{HOOK}" grok Stop')

    def test_shell_hooks_refuse_characters_shells_expand(self) -> None:
        for bad in (
            HookTarget("/usr/bin/python3", "/home/100%/agent_hook.py", "linux"),
            HookTarget('C:/a"b/python.exe', WIN_HOOK, "win32"),
        ):
            with self.assertRaisesRegex(ValueError, "shell command"):
                hook_command(bad, "gemini", "Stop")

    def test_antigravity_runs_py_3_on_windows_with_the_path_bare(self) -> None:
        self.assertEqual(agy_hook_command(WINDOWS, "Stop"), f"py -3 -I -S {WIN_HOOK} agy Stop")
        self.assertEqual(agy_hook_command(POSIX, "Stop"), f"/usr/bin/python3 -I -S {HOOK} agy Stop")
        self.assertEqual(agy_hook_command(HookTarget("/my py/python3", HOOK, "linux"), "Stop"), f"python3 -I -S {HOOK} agy Stop")
        for bad in ("C:/Users/John Smith/h.py", "C:/a&b/h.py", "C:/%X%/h.py", "C:/Program Files (x86)/h.py"):
            with self.assertRaisesRegex(ValueError, "Antigravity CLI"):
                agy_hook_command(HookTarget("py", bad, "win32"), "Stop")

    def test_posix_hooks_quote_only_words_that_need_it(self) -> None:
        self.assertEqual(posix_hook_command(POSIX, "goose", "Stop"), f"/usr/bin/python3 -I -S {HOOK} goose Stop")
        spaced = HookTarget("C:/Users/a b/python.exe", "C:\\Users\\o'neil\\agent_hook.py", "win32")
        self.assertEqual(
            posix_hook_command(spaced, "hermes", "x"), "'C:/Users/a b/python.exe' -I -S 'C:\\Users\\o'\\''neil\\agent_hook.py' hermes x"
        )
        self.assertEqual(posix_quote("/home/o'neil/x"), "'/home/o'\\''neil/x'")
        with self.assertRaisesRegex(ValueError, "hook command"):
            posix_hook_command(HookTarget("/usr/bin/python3", "/a\nb", "linux"), "goose", "Stop")

    def test_copilot_runs_the_interpreter_with_arguments(self) -> None:
        hooks = copilot_hooks(WINDOWS)
        self.assertEqual(hooks["version"], 1)
        self.assertEqual(
            hooks["hooks"]["agentStop"],
            [
                {
                    "type": "command",
                    "exec": "C:/Python313/python.exe",
                    "args": ["-I", "-S", WIN_HOOK, "copilot", "agentStop"],
                    "timeoutSec": 5,
                }
            ],
        )


class Migration(unittest.TestCase):
    def test_old_node_and_new_python_hooks_both_count_as_ours(self) -> None:
        for text in (f"{OLD_NODE} gemini Stop", 'node "C:/x/agent-hook.mjs" qwen Stop', f"/usr/bin/python3 -I -S {HOOK} goose Stop", HOOK):
            self.assertTrue(mentions_hook(text), text)
        for text in ("python /home/a/agent_hook.py", "node /x/my-agent-hook.mjs.bak", None):
            self.assertFalse(mentions_hook(text), text)

    def test_settings_hooks_from_node_are_replaced(self) -> None:
        theirs = {"matcher": "Bash", "hooks": [{"type": "command", "command": "python check.py"}]}
        root = {
            "hooks": {"PreToolUse": [theirs], "Stop": [{"hooks": [{"type": "command", "command": f"{OLD_NODE} qwen Stop", "timeout": 5}]}]}
        }
        self.assertTrue(has_any_settings_hooks(root))
        self.assertFalse(has_our_hooks(root, "qwen", POSIX))
        merged = merge_hook_settings(root, "f", "qwen", POSIX)
        self.assertTrue(has_our_hooks(merged, "qwen", POSIX))
        self.assertEqual(merged["hooks"]["PreToolUse"][0], theirs)
        self.assertEqual(
            merged["hooks"]["Stop"], [{"hooks": [{"type": "command", "command": hook_command(POSIX, "qwen", "Stop"), "timeout": 5}]}]
        )
        self.assertFalse(has_any_settings_hooks(merge_hook_settings(merged, "f", "qwen", None)))

    def test_hermes_approvals_move_to_the_python_commands(self) -> None:
        mine = {"event": "pre_llm_call", "command": "echo hi"}
        root = {
            "approvals": [
                mine,
                {
                    "event": "pre_llm_call",
                    "command": f"node '{HOOK.replace('py/launch/agent_hook.py', 'agent-hook.mjs')}' hermes pre_llm_call",
                },
            ]
        }
        nxt = with_hermes_approvals(root, "f", POSIX)
        self.assertEqual(nxt["approvals"], [mine, *({"event": i["event"], "command": i["command"]} for i in hermes_hook_items(POSIX))])
        self.assertEqual(with_hermes_approvals(nxt, "f", None), {"approvals": [mine]})
        self.assertTrue(has_any_hermes_hooks({"pre_llm_call": [{"command": f"{OLD_NODE} hermes pre_llm_call"}]}))

    def test_antigravity_allow_rules_drop_the_old_wildcard(self) -> None:
        old = {"permissions": {"allow": ["command(git)", "mcp(ide-agent-tabs/*)"]}}
        self.assertEqual(with_agy_allow_rule(old, "f", True)["permissions"]["allow"], ["command(git)", *AGY_ALLOW_RULES])
        self.assertEqual(with_agy_allow_rule(old, "f", False)["permissions"]["allow"], ["command(git)"])


class Paths(unittest.TestCase):
    def test_hook_files_honor_the_relocation_variables(self) -> None:
        h = "h"
        self.assertEqual(hook_config_file("grok", {"GROK_HOME": "g"}, h, "linux"), os.path.join("g", "hooks", "ide-agent-tabs.json"))
        self.assertEqual(hook_config_file("copilot", {}, h, "linux"), os.path.join(h, ".copilot", "hooks", "ide-agent-tabs.json"))
        self.assertEqual(hook_config_file("hermes", {"LOCALAPPDATA": "L"}, h, "win32"), os.path.join("L", "hermes", "config.yaml"))
        self.assertEqual(hook_config_file("hermes", {}, h, "linux"), os.path.join(h, ".hermes", "config.yaml"))
        root = os.path.abspath("groot")
        self.assertEqual(
            hook_config_file("goose", {"GOOSE_PATH_ROOT": root}, h, "linux"),
            os.path.join(root, ".agents", "plugins", "ide-agent-tabs", "hooks", "hooks.json"),
        )
        self.assertEqual(
            hook_config_file("goose", {"GOOSE_PATH_ROOT": "relative"}, h, "linux"),
            os.path.join(h, ".agents", "plugins", "ide-agent-tabs", "hooks", "hooks.json"),
        )
        self.assertEqual(
            hook_config_file("qwen", {"QWEN_HOME": "~/q"}, h, "linux"), os.path.join(os.path.abspath(os.path.join(h, "q")), "settings.json")
        )


if __name__ == "__main__":
    unittest.main()
