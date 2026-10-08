# Agent Tabs Claude Code plugin

This folder is the Agent Tabs plugin for Claude Code. It lets an agent open, list and close agent
sessions in IDE tabs and terminal tabs, and delegate work to other agent CLIs. It also carries the IDE
extensions and keeps them up to date.

For an overview of the whole project, see the [root README](https://github.com/Alexk413x/ide-agent-tabs#readme).

## Install

Add the marketplace and install the plugin:

```sh
claude plugin marketplace add Alexk413x/marketplace
claude plugin install ide-agent-tabs@alexk413x
```

Then, in a Claude Code session, run `/ide-agent-tabs:setup` to install the IDE extensions.

## Contents

| Path | What it is |
|---|---|
| `.claude-plugin/plugin.json` | The plugin manifest. |
| `.mcp.json` | Registers the `ide-agent-tabs` MCP server: the shared HTTP server `mcp/launch/shared_server.py` on `127.0.0.1` at the `server_port` option (47828 by default), with the headers helper `mcp/launch/headers.py`. Claude Code runs the helper as `py -3 -I -S … \|\| python3 -I -S … \|\| python -I -S …`, through `cmd.exe` on Windows. Its tools are `list_agents`, `list_tabs`, `open_tab` and `close_tab`, and for messaging between agent sessions, `list_sessions`, `send_message`, `read_messages` and `wait_for_message`. With Jev turned on, it also lists `jev_ask`, `jev_choose`, `jev_check`, `jev_rank` and `jev_route`. `mcp/launch/agent-tabs list-ides` and `agent-tabs jev status` run on the command line. |
| `skills/new-tab/` | `/ide-agent-tabs:new-tab` opens, lists and closes agent tabs. |
| `skills/delegate/` | `/ide-agent-tabs:delegate` hands a task, review or question to another agent CLI in headless mode. With Jev on and no agent named, it asks `jev_route` which tier takes the task. |
| `skills/setup/` | `/ide-agent-tabs:setup` installs the IDE extensions, picks a default agent and terminal, and can turn on Jev. Only you start it; Claude doesn't run it on its own. `agent-profiles.md` holds the custom profile format. |
| `skills/jev/` | When the `jev_` tools are listed, tells Claude Code when to ask Jev for a pick, a yes or no, or a ranking instead of spending a model turn, and how to write the question. |
| `skills/handoff/` | `/ide-agent-tabs:handoff` hands this session's work to a new agent tab with a written brief, then stops. |
| `skills/message/` | `/ide-agent-tabs:message` splits work with another live agent session, of any CLI, by messaging it, and says how to answer a message. |
| `skills/update/` | `/ide-agent-tabs:update` updates the plugin and the IDE extensions. Only you start it. |
| `evals/` | Trigger and behavior cases for `claude plugin eval`. |
| `hooks/hooks.json` | A `SessionStart` hook that runs `mcp/launch/server_hook.py` to start the shared MCP server, and a `SessionEnd` hook that tells the server the session ended. A `SessionStart` hook that runs `mcp/launch/agent-tabs sync-ides --hook`. When the bundled IDE versions change, it updates the extension in each editor that has it, and the local JetBrains plugin repository. When the bundled MCP server changes, it refreshes the copy in `~/.ide-agent-tabs/mcp/py/` that other agent CLIs run. The other hooks run `mcp/launch/agent_hook.py`, which keeps the session's messaging state and reminds the agent of unread messages; a shell guard skips Python in a session that no Agent Tabs tab or a mod-driven tab runs. The server and agent hooks are bash-style commands that source `mcp/launch/server-hook.ps1` or `agent-hook.ps1`, so Windows needs Git Bash. `register.tsx` is the Claude Code mod. |
| `mcp/launch/` | Entry points, all for Python 3.9 or later: `agent-tabs` (a POSIX `sh` launcher that picks `py -3`, `python3` or `python`, skipping the Microsoft Store stubs, and runs `agent_tabs.py`), `mcp_server.py` (the stdio server), `shared_server.py`, `headers.py`, `server_hook.py`, `agent_hook.py` and the two hook scripts. |
| `mcp/src/ide_agent_tabs/` | The MCP server's Python package. It imports only the standard library. `catalog.json` holds the tool names, descriptions and input schemas. |
| `dist/launch/` | The terminal launch scripts `agent-launch.sh`, `agent-launch.fish` and `agent-launch.ps1`, which start an agent in a terminal tab. |
| `dist/ide/` | The bundled IDE extensions: `ide-agent-tabs.vsix`, `ide-agent-tabs-jetbrains.zip` and `versions.json`. |

## Generated files

Don't edit `dist/ide/` by hand. `scripts/pack-ides.mjs` writes it: it builds the JetBrains plugin and
the VS Code extension from `jetbrains/` and `vscode/`. Commit the result. Everything else in this folder
is source, edited in place.

[docs/design.md](https://github.com/Alexk413x/ide-agent-tabs/blob/main/docs/design.md#distribution-and-updates) describes how the plugin
distributes and updates the IDE extensions.
