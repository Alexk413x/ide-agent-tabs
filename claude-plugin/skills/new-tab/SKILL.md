---
name: new-tab
description: Open a new agent session (Claude Code, Codex, Gemini CLI, Copilot CLI, or a custom profile) in an IDE editor tab or a terminal tab, optionally in another folder and with a first message. Also lists and closes those tabs. Use when the user asks to open a new tab, session or agent somewhere, to see which agent tabs are open, or to close one.
argument-hint: "[agent] [folder] [-- first message]"
---

Open, list or close agent tabs through the `ide-agent-tabs` MCP server. The tab can be in any running IDE
(JetBrains IDEs, VS Code and VS Code-based editors) or in a terminal app.

Arguments: `$ARGUMENTS`

## Parse the arguments

- Everything after a standalone `--` is the first message. It is optional.
- Before `--`: an agent name (a profile such as `claude`, `codex`, `gemini`, `copilot`, or a custom one)
  and a folder. Either can be missing. Call `list_agents` when you need to tell a profile name from a
  folder name.
- No folder means the current working directory. Resolve a relative folder to an absolute path and check
  that it exists. If it doesn't, tell the user and stop.
- No agent means the default agent. Don't pass `agent`.

## Open

Call `open_tab` with `path`, and `agent` and `prompt` when given. Pass `ide` only when the user names an
IDE or terminal; get its id from `list_ides`. Pass `args` or `env` only when the user asks for extra
agent flags or variables.

Report the folder, where the tab opened (IDE and project, or terminal), the agent, and the tab id.

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
