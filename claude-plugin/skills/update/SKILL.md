---
name: update
description: Update Agent Tabs everywhere on this machine - the Claude Code plugin, then the JetBrains plugin and the VS Code-family extensions that ship with it. Use when the user asks to update, upgrade or check the version of Agent Tabs.
argument-hint: "[--check]"
---

Update Agent Tabs. The Claude Code plugin carries the IDE extensions, so updating the plugin brings the
new IDE versions with it. With `--check`, only report installed and available versions; change
nothing.

Arguments: `$ARGUMENTS`

## 1. Check versions

1. Run `claude plugin marketplace update ide-agent-tabs`, then `claude plugin list`. Report the
   installed version of `ide-agent-tabs@ide-agent-tabs` and whether a newer one is available.
2. Run:

   ```sh
   node "${CLAUDE_PLUGIN_ROOT}/dist/sync-ides.mjs" --status
   ```

   Report the bundled versions (`bundled`), the extension version in each editor (`editors`), and the
   version in the JetBrains repository (`jetbrains.version`).

Stop here with `--check`.

## 2. Update the Claude Code plugin

If a newer version is available, run:

```sh
claude plugin update ide-agent-tabs@ide-agent-tabs
```

Then stop, and tell the user to run `/reload-plugins` and then `/ide-agent-tabs:update` again. The
paths in this skill point to the version that was installed when it loaded, so only a fresh run uses the
new version's files. A new Claude Code session also updates the IDEs by itself.

## 3. Update the IDEs

When the Claude Code plugin is current, bring the IDEs up to its bundled versions:

```sh
node "${CLAUDE_PLUGIN_ROOT}/dist/sync-ides.mjs" --hook
```

It updates the extension only in editors that already have an older version, and puts the new
JetBrains plugin in `~/.ide-agent-tabs/repository/` if that folder exists. It also refreshes the MCP
server copy in `~/.ide-agent-tabs/mcp/`, if that folder exists. Codex, Gemini CLI, Copilot CLI and
OpenCode run the server from that copy, so their registrations keep working after an update. It prints nothing when
there's nothing to update. Errors go to `~/.ide-agent-tabs/sync.log`.

If an IDE was never set up, run the `setup` skill instead.

## 4. Report

Run `--status` again. List each part with its old and new version, and each step the user must take:

- VS Code and editors built on it: reload open windows (**Developer: Reload Window**).
- JetBrains IDEs: install the update when the IDE offers it, or at once from **Settings > Plugins >
  Installed > Check for Updates**, then restart the IDE.
- Other agents registered with Agent Tabs: restart their open sessions to load the updated server.
