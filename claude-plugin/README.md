# Agent Tabs Claude Code plugin

This folder is the Agent Tabs plugin for Claude Code. It lets an agent open, list and close agent
sessions in IDE tabs and terminal tabs, and delegate work to other agent CLIs. It also carries the IDE
extensions and keeps them up to date.

For an overview of the whole project, see the [root README](../README.md).

## Install

Add the marketplace and install the plugin:

```sh
claude plugin marketplace add Alexk413x/ide-agent-tabs
claude plugin install ide-agent-tabs@ide-agent-tabs
```

Then, in a Claude Code session, run `/ide-agent-tabs:setup` to install the IDE extensions.

## Contents

| Path | What it is |
|---|---|
| `.claude-plugin/plugin.json` | The plugin manifest. |
| `.mcp.json` | Registers the `ide-agent-tabs` MCP server, which runs `dist/mcp-server.mjs`. Its tools are `list_ides`, `list_agents`, `list_tabs`, `open_tab` and `close_tab`. With Jev turned on, it also lists `jev_status`, `jev_ask`, `jev_choose`, `jev_check`, `jev_rank` and `jev_route`. |
| `skills/new-tab/` | `/ide-agent-tabs:new-tab` opens, lists and closes agent tabs. |
| `skills/delegate/` | `/ide-agent-tabs:delegate` hands a task, review or question to another agent CLI in headless mode. With Jev on and no agent named, it asks `jev_route` which tier takes the task. |
| `skills/setup/` | `/ide-agent-tabs:setup` installs the IDE extensions, picks a default agent and terminal, and can turn on Jev. |
| `skills/jev/` | Tells Claude Code when to ask Jev for a pick, a yes or no, or a ranking instead of spending a model turn, and how to write the question. |
| `skills/update/` | `/ide-agent-tabs:update` updates the plugin and the IDE extensions. |
| `hooks/hooks.json` | A `SessionStart` hook that runs `dist/sync-ides.mjs --hook`. When the bundled IDE versions change, it updates the extension in each editor that has it, and the local JetBrains plugin repository. When the bundled MCP server changes, it refreshes the copy in `~/.ide-agent-tabs/mcp/` that other agent CLIs run. |
| `dist/ide/` | The bundled IDE extensions: `ide-agent-tabs.vsix`, `ide-agent-tabs-jetbrains.zip` and `versions.json`. |

## Generated files

Don't edit `dist/` by hand. The build writes it, and the plugin runs it without `node_modules`, so
commit it:

- `mcp/build.mjs` (`npm run build` in `mcp/`) writes `mcp-server.mjs`, `sync-ides.mjs`, `launch/` and
  `THIRD_PARTY_NOTICES.txt`.
- `scripts/pack-ides.mjs` writes `dist/ide/`. It builds the JetBrains plugin and the VS Code extension
  from `jetbrains/` and `vscode/`.

[docs/design.md](../docs/design.md#distribution-and-updates-phase-1) describes how the plugin
distributes and updates the IDE extensions.
