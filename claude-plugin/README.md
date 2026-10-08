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
| `.mcp.json` | Registers the `ide-agent-tabs` MCP server: the shared HTTP server `dist/shared-server.mjs` on `127.0.0.1` at the `server_port` option (47828 by default), with the headers helper `mcp/launch/headers.mjs`. Its tools are `list_agents`, `list_tabs`, `open_tab` and `close_tab`, and for messaging between agent sessions, `list_sessions`, `send_message`, `read_messages` and `wait_for_message`. With Jev turned on, it also lists `jev_ask`, `jev_choose`, `jev_check`, `jev_rank` and `jev_route`. `dist/mcp-server.mjs list-ides` and `jev status` run on the command line. |
| `skills/new-tab/` | `/ide-agent-tabs:new-tab` opens, lists and closes agent tabs. |
| `skills/delegate/` | `/ide-agent-tabs:delegate` hands a task, review or question to another agent CLI in headless mode. With Jev on and no agent named, it asks `jev_route` which tier takes the task. |
| `skills/setup/` | `/ide-agent-tabs:setup` installs the IDE extensions, picks a default agent and terminal, and can turn on Jev. Only you start it; Claude doesn't run it on its own. `agent-profiles.md` holds the custom profile format. |
| `skills/jev/` | When the `jev_` tools are listed, tells Claude Code when to ask Jev for a pick, a yes or no, or a ranking instead of spending a model turn, and how to write the question. |
| `skills/message/` | `/ide-agent-tabs:message` splits work with another live agent session, of any CLI, by messaging it, and says how to answer a message. |
| `skills/update/` | `/ide-agent-tabs:update` updates the plugin and the IDE extensions. Only you start it. |
| `evals/` | Trigger and behavior cases for `claude plugin eval`. |
| `hooks/hooks.json` | A `SessionStart` hook that runs `dist/server-start.mjs` to start the shared MCP server, and a `SessionEnd` hook that tells the server the session ended. A `SessionStart` hook that runs `dist/sync-ides.mjs --hook`. When the bundled IDE versions change, it updates the extension in each editor that has it, and the local JetBrains plugin repository. When the bundled MCP server changes, it refreshes the copy in `~/.ide-agent-tabs/mcp/` that other agent CLIs run. The other hooks run `dist/agent-hook.mjs`, which keeps the session's messaging state and reminds the agent of unread messages. |
| `dist/ide/` | The bundled IDE extensions: `ide-agent-tabs.vsix`, `ide-agent-tabs-jetbrains.zip` and `versions.json`. |

## Generated files

Don't edit `dist/` by hand. The build writes it, and the plugin runs it without `node_modules`, so
commit it:

- `mcp/build.mjs` (`npm run build` in `mcp/`) writes `mcp-server.mjs`, `sync-ides.mjs`,
  `agent-hook.mjs`, `launch/` and `THIRD_PARTY_NOTICES.txt`.
- `scripts/pack-ides.mjs` writes `dist/ide/`. It builds the JetBrains plugin and the VS Code extension
  from `jetbrains/` and `vscode/`.

[docs/design.md](https://github.com/Alexk413x/ide-agent-tabs/blob/main/docs/design.md#distribution-and-updates) describes how the plugin
distributes and updates the IDE extensions.
