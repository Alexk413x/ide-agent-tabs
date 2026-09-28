---
name: setup
description: Set up Agent Tabs on this machine - check Node.js, install the IDE extensions that ship with this plugin into JetBrains IDEs, VS Code and editors built on it, pick a default agent and terminal, and optionally add OpenAI's Codex plugin. Use after installing the ide-agent-tabs plugin, or when the user asks to set up, repair or check Agent Tabs.
argument-hint: "[--check]"
---

Set up Agent Tabs, one step at a time. With `--check`, only report what's installed and what's
missing; change nothing.

Arguments: `$ARGUMENTS`

Ask before each change. Show the exact command you will run. At the end, give a short table of what's
done, what's skipped, and anything the user must do by hand, such as restarting an IDE.

This plugin carries the IDE extensions in `${CLAUDE_PLUGIN_ROOT}/dist/ide/`. You don't download
anything. `${CLAUDE_PLUGIN_ROOT}/dist/sync-ides.mjs` installs them and reports on them.

## 1. Prerequisite

Run `node --version`. The MCP server and `sync-ides.mjs` need Node.js 20 or later. If Node.js is
missing or older, ask the user to install the current Node.js LTS, and stop. Nothing else is required.

## 2. Find the IDEs

1. Find VS Code and editors built on it, such as Cursor, Windsurf, VSCodium and Antigravity:

   ```sh
   node "${CLAUDE_PLUGIN_ROOT}/dist/sync-ides.mjs" --status
   ```

   The JSON lists each editor's command-line tool (`cli`, `path`) and the extension version installed
   there (`installed`, or `null`). It also shows the bundled versions and the JetBrains repository.
   The script looks for `code`, `code-insiders`, `cursor`, `windsurf`, `codium` and `antigravity-ide`
   on `PATH` and in the usual install folders. If the user has an editor it misses, ask for the path to
   its command-line tool.

2. Find JetBrains IDEs, including Android Studio. Look for folders that contain `product-info.json`
   (on macOS, `Contents/Resources/product-info.json`):

   - Windows: `C:\Program Files\Android\Android Studio*`, `C:\Program Files\JetBrains\*`,
     `%LOCALAPPDATA%\Programs\*` (Toolbox 2.x installs IDEs here), and
     `%LOCALAPPDATA%\JetBrains\Toolbox\apps\*`.
   - macOS: `/Applications/*.app` and `~/Applications/*.app`.
   - Linux: `~/.local/share/JetBrains/Toolbox/apps/*`, `/opt/*`, `/usr/share/*`, `/usr/local/*`,
     `/snap/*/current`, and Flatpak apps in `/var/lib/flatpak/app/*/current/active/files` and
     `~/.local/share/flatpak/app/*/current/active/files`.

   Read each IDE's `product-info.json` for its `name` and `buildNumber`. The plugin needs build
   262.10315 or later (2026.2.2). List an older IDE as too old; don't set it up.

3. Show one list of every IDE found, with the installed Agent Tabs version where there is one. Ask
   once which ones to set up. The default is all of them.

Stop here with `--check`.

## 3. VS Code and editors built on it

Install the extension into the editors the user chose. Pass each `cli` name, or the `path` for a tool
that isn't on `PATH`:

```sh
node "${CLAUDE_PLUGIN_ROOT}/dist/sync-ides.mjs" --install <cli> [<cli>...]
```

The JSON report lists each editor with `ok`, or an `error`. Tell the user to reload open windows
(**Developer: Reload Window**).

## 4. JetBrains IDEs

1. Create the local plugin repository:

   ```sh
   node "${CLAUDE_PLUGIN_ROOT}/dist/sync-ides.mjs" --install --jetbrains
   ```

   The report's `jetbrains.url` is the `file:///` URL of `updatePlugins.xml` in
   `~/.ide-agent-tabs/repository/`.

2. Give the user these one-time steps for each JetBrains IDE they chose:
   1. Open **Settings > Plugins**, click **⚙**, and choose **Manage Plugin Repositories**.
   2. Add the `jetbrains.url` from the report, and click **OK**.
   3. On the **Marketplace** tab, find **Agent Tabs** and click **Install**.
   4. Restart the IDE.

3. Tell the user that later versions arrive as normal JetBrains plugin updates. The IDE offers each one
   at its next update check, or at once from **Settings > Plugins > Installed > Check for Updates**.

## 5. Default agent

1. Call the MCP tool `list_agents` and show which agent CLIs are installed.
2. Ask which agent the **New Agent Tab** button opens by default. The default is `claude`, or the only
   installed agent. Write it to `~/.ide-agent-tabs/config.json` as `"defaultAgent"`. Keep any other
   keys in that file.
3. To add a custom agent, such as another agent CLI or a CLI set to a specific model, add a profile to
   `~/.ide-agent-tabs/agents.json`. See
   [Agent profiles](https://github.com/Alexk413x/ide-agent-tabs/blob/main/docs/design.md#agent-profiles)
   for the format.

## 6. Terminal

Ask whether the user wants agent tabs outside IDEs, in a terminal app. If not, skip this step.

`list_ides` shows the terminal apps installed here. Ask which one `open_tab` uses when no IDE fits, and
write the choice to `~/.ide-agent-tabs/config.json` as `"terminal"`. Without one, the server uses the
first installed terminal in this order:

- Windows: `windows-terminal`, `wezterm`.
- macOS and Linux: `ghostty`, `kitty`, `wezterm`, `tmux`.

If the user picks `kitty`, tell them to add these two lines to `kitty.conf` (usually
`~/.config/kitty/kitty.conf`) and restart kitty, so it can open tabs. Without them, each agent opens
in a new kitty window that `close_tab` can only close on a best-effort basis.

- Linux: `allow_remote_control socket-only` and `listen_on unix:${XDG_RUNTIME_DIR}/kitty-agent-tabs`
- macOS: `allow_remote_control socket-only` and `listen_on unix:${TMPDIR}/kitty-agent-tabs`

If the user picks `tmux`, tell them that tabs open in their most recently attached session, or in a
detached session named `agents` that they open with `tmux attach -t agents`.

## 7. Codex plugin (optional)

If `codex` is installed, offer OpenAI's Codex plugin for Claude Code. It gives `/codex:review` and
`/codex:rescue`, and the `delegate` skill uses it when present:

```sh
claude plugin marketplace add openai/codex-plugin-cc
claude plugin install codex@openai-codex
```

Then run `/reload-plugins`.

## 8. Check it works

1. Call `list_ides`. Every IDE with the extension installed and a window open appears.
2. Offer to open a test tab with `open_tab` in the current folder, then close it with `close_tab`.

Tell the user that the plugin keeps the IDE extensions up to date. When a Claude Code session starts
after a plugin update, it updates the extension in each editor that has it, and puts the new JetBrains
plugin in the local repository.
