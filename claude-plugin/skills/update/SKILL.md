---
name: update
description: Update Agent Tabs everywhere on this machine - the Claude Code plugin, then the JetBrains plugin and the VS Code-family extensions that ship with it. Use when the user asks to update, upgrade or check the version of Agent Tabs.
argument-hint: "[--check]"
disable-model-invocation: true
---

Update Agent Tabs. The Claude Code plugin carries the IDE extensions, so updating the plugin brings the
new IDE versions with it. With `--check`, only report installed and available versions; change
nothing. Every step runs on this machine and needs a local shell.

Arguments: `$ARGUMENTS`

## 1. Check versions

1. Run `claude plugin list --json`. If it has an `ide-agent-tabs@ide-agent-tabs` entry, the plugin came
   from the old `ide-agent-tabs` marketplace, which no longer gets updates. Stop and give the user
   these commands to move to the `alexk413x` marketplace, then to run `/reload-plugins`:

   ```sh
   claude plugin marketplace add Alexk413x/marketplace
   claude plugin install ide-agent-tabs@alexk413x
   claude plugin uninstall ide-agent-tabs@ide-agent-tabs
   claude plugin marketplace remove ide-agent-tabs
   ```

   Otherwise run `claude plugin marketplace update alexk413x`. Read the newest version from `version`
   in `https://raw.githubusercontent.com/Alexk413x/ide-agent-tabs/release/.claude-plugin/plugin.json`,
   and the installed version from the `ide-agent-tabs@alexk413x` entry. `claude plugin list` shows
   only the installed version. Report both versions.
2. Run:

   ```sh
   "${CLAUDE_PLUGIN_ROOT}/mcp/launch/agent-tabs" sync-ides --status
   ```

   Report the bundled versions (`bundled`), the extension version in each editor (`editors`), and the
   version in the JetBrains repository (`jetbrains.version`).

Stop here with `--check`.

## 2. Update the Claude Code plugin

If a newer version is available, run:

```sh
claude plugin update ide-agent-tabs@alexk413x --json
```

Check the result line for the new version. Then stop, and tell the user to run `/reload-plugins` and then `/ide-agent-tabs:update` again. The
paths in this skill point to the version that was installed when it loaded, so only a fresh run uses the
new version's files. A new Claude Code session also updates the IDEs by itself.

## 3. Update the IDEs

When the Claude Code plugin is current, bring the IDEs up to its bundled versions:

```sh
"${CLAUDE_PLUGIN_ROOT}/mcp/launch/agent-tabs" sync-ides --hook
```

It updates the extension only in editors that already have an older version, and puts the new
JetBrains plugin in `~/.ide-agent-tabs/repository/` if that folder exists. It also refreshes the MCP
server copy in `~/.ide-agent-tabs/mcp/py/`, if `~/.ide-agent-tabs/mcp/` exists. Codex, Antigravity CLI, Copilot CLI, Gemini CLI, Grok Build, Pi,
Hermes, OpenCode, Qwen Code and Goose run the server from that copy, so their registrations keep working after an update.
When a registration or a hook entry runs an older command, such as `node …/mcp-server.mjs` from a
plugin before 0.9.0, the hook rewrites it to run the server copy on the Python interpreter that
`~/.ide-agent-tabs/mcp/python.json` records. Antigravity CLI gets `py -3` on Windows instead. Once no
registration it found runs the Node copy, it deletes that copy's files from `~/.ide-agent-tabs/mcp/`.
It prints nothing when there's nothing to update. Errors go to `~/.ide-agent-tabs/sync.log`.

If an IDE was never set up, run the `setup` skill instead.

## 4. Report

Run `--status` again. List each part with its old and new version, and each step the user must take:

- VS Code and editors built on it: reload open windows (**Developer: Reload Window**).
- JetBrains IDEs: install the update when the IDE offers it, or at once from **Settings > Plugins >
  Installed > Check for Updates**, then restart the IDE.
- Claude Code: restart open sessions. A session keeps the server and hooks it started with, and 0.9.0
  replaces the Node server with a Python one.
- Other agents registered with Agent Tabs: restart their open sessions to load the updated server and
  the rewritten registrations.

Tell the user that auto-update is off by default for a third-party marketplace such as this one. To
get updates at session start, they turn it on in `/plugin` > **Marketplaces**.
