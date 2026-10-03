---
name: new-tab
description: Open a new agent session (Claude Code, Codex, Antigravity CLI, Copilot CLI, Gemini CLI, Grok Build, Pi, Hermes, OpenCode, Qwen Code, Goose, Codex (local), or a custom profile) in an IDE editor tab or a terminal tab, optionally in another folder and with a first message. Also lists and closes those tabs. Use when the user asks to open a new tab, session or agent somewhere, to see which agent tabs are open, or to close one. Not for opening a file or a web page in a tab.
argument-hint: "[agent] [folder] [-- first message]"
---

Open, list or close agent tabs through the `ide-agent-tabs` MCP server. The tab can be in any running IDE
(JetBrains IDEs, VS Code and VS Code-based editors) or in a terminal app. The tools are
`mcp__plugin_ide-agent-tabs_ide-agent-tabs__list_ides`, `mcp__plugin_ide-agent-tabs_ide-agent-tabs__list_agents`,
`mcp__plugin_ide-agent-tabs_ide-agent-tabs__open_tab`, `mcp__plugin_ide-agent-tabs_ide-agent-tabs__list_tabs` and
`mcp__plugin_ide-agent-tabs_ide-agent-tabs__close_tab`; below they go by their short names.

Arguments: `$ARGUMENTS`

## Parse the arguments

- Everything after a standalone `--` is the first message. It is optional.
- Before `--`: an agent name (a profile such as `claude`, `codex`, `agy`, `copilot`, `gemini`, `grok`, `pi`, `hermes`,
  `opencode`, `qwen`, `goose`, `codex-local`, or a custom one)
  and a folder. Either can be missing. Call `list_agents` when you need to tell a profile name from a
  folder name.
- No folder means the current working directory. Resolve a relative folder to an absolute path and check
  that it exists. If it doesn't, tell the user and stop.
- No agent means the default agent. Don't pass `agent`.

## Open

Call `open_tab` with `path`, and `agent` and `prompt` when given. Pass `ide` only when the user names an
IDE or terminal; get its id from `list_ides`. Pass `args` or `env` only when the user asks for extra
agent flags or variables.

Pass `model` only when the user names a model. The server passes it with the agent's model flag. An
agent whose `list_agents` entry has `model: false` has no model option, and `open_tab` returns an error
for it instead of ignoring the model.

Pass `via` only when the user asks to launch through Ori (OpenRouter, billed there) or to launch
directly. `list_agents` marks the agents Ori can launch with `ori: true`, and `launchVia` shows the
setting that applies when you pass no `via`. A result with `via: "ori"` started through Ori.

Report the folder, where the tab opened (IDE and project, or terminal), the agent, the model when you
passed one, `via` when it is `ori`, and the tab id.

## When a subagent fits better

When you start a session for your own task, and it would work in the same folder or project as this
session and you only need its result, run a subagent in this session instead. A subagent reports back
directly and ends by itself. Open a tab for another repository, for a session the user will work in, or
for a handoff that needs a fresh session, such as one that loads an updated plugin.

To move this session's own work to a new tab and stop here, use the `handoff` skill instead.

## List and close

- "Which tabs are open": call `list_tabs` and show agent, folder and where each tab is.
- "Close that tab": call `close_tab` with its id. With no id, `close_tab` closes this session's own tab,
  so only omit it when the user means this tab.

## Errors

An IDE error from the server ends with the next step. Follow it, and retry at most once. After an
unknown-agent error, show the user the installed agents from `list_agents`.

## Without the MCP server

If the `ide-agent-tabs` tools aren't available, call the IDE's HTTP API directly. Read the newest file in
`~/.ide-agent-tabs/endpoints/`, then `POST` JSON to `<url>/open` with the header
`Authorization: Bearer <token>`. In PowerShell, pass `-UserAgent 'ide-agent-tabs'`, because the IDE
refuses PowerShell's default browser-like User-Agent. Tell the user the MCP server isn't running and
suggest `/ide-agent-tabs:setup`.
