---
name: setup
description: Set up Agent Tabs on this machine - check Node.js, install the IDE extensions that ship with this plugin into JetBrains IDEs, VS Code and editors built on it, pick a default agent and terminal, let other agent CLIs use Agent Tabs, and optionally add OpenAI's Codex plugin and turn on Jev judgments. Use after installing the ide-agent-tabs plugin, or when the user asks to set up, repair or check Agent Tabs.
argument-hint: "[--check]"
disable-model-invocation: true
---

Set up Agent Tabs, one step at a time. With `--check`, only report what's installed and what's
missing; change nothing.

Arguments: `$ARGUMENTS`

Ask before each change. Show the exact command you will run. At the end, give a short table of what's
done, what's skipped, and anything the user must do by hand, such as restarting an IDE.

This plugin carries the IDE extensions in `${CLAUDE_PLUGIN_ROOT}/dist/ide/`. You don't download
anything. `${CLAUDE_PLUGIN_ROOT}/dist/sync-ides.mjs` installs them and reports on them. Every step runs
on this machine and needs a local shell, so this skill can't run in claude.ai chat or a cloud session.

## 1. Prerequisite

Run `node --version`. The MCP server and `sync-ides.mjs` need Node.js 20 or later. If Node.js is
missing or older, ask the user to install the current Node.js LTS, and stop. Nothing else is required.

## 2. Find the IDEs

1. Find VS Code and editors built on it, such as Cursor, Windsurf, VSCodium, Antigravity, Kiro,
   Positron and Trae:

   ```sh
   node "${CLAUDE_PLUGIN_ROOT}/dist/sync-ides.mjs" --status
   ```

   The JSON lists each editor's command-line tool (`cli`, `path`) and the extension version installed
   there (`installed`, or `null`). It also shows the bundled versions and the JetBrains repository.
   The script looks for `code`, `code-insiders`, `cursor`, `windsurf`, `codium`, `antigravity-ide`,
   `kiro`, `positron` and `trae` on `PATH` and in the usual install folders. If the user has an editor it misses, ask for the path to
   its command-line tool.

2. Find JetBrains IDEs, including Android Studio. Look for folders that contain `product-info.json`
   (on macOS, `Contents/Resources/product-info.json`):

   - Windows: `C:\Program Files\Android\Android Studio*`, `C:\Program Files\JetBrains\*`,
     `%LOCALAPPDATA%\Programs\*` (Toolbox 2.x installs IDEs here), and
     `%LOCALAPPDATA%\JetBrains\Toolbox\apps\*`.
   - macOS: `/Applications/*.app`, `~/Applications/*.app` (Toolbox 2.x), and
     `~/Library/Application Support/JetBrains/Toolbox/apps/*` (Toolbox 1.x).
   - Linux: `~/.local/share/JetBrains/Toolbox/apps/*`, `/opt/*`, `/usr/share/*`, `/usr/local/*`,
     `/snap/*/current`, and Flatpak apps in `/var/lib/flatpak/app/*/current/active/files` and
     `~/.local/share/flatpak/app/*/current/active/files`.

   Toolbox 1.x nests each IDE in `<ide>/ch-<n>/<build>/` under its `apps` folder, so search the
   Toolbox `apps` folders three levels down. Toolbox 2.1 and later can install to a folder the user
   chose; ask the user for it if a Toolbox IDE is missing.

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

1. Call `mcp__plugin_ide-agent-tabs_ide-agent-tabs__list_agents` and show which agent CLIs are installed.
2. Ask which agent the **New Agent Tab** button opens by default. The default is `claude`, or the only
   installed agent. Write it to `~/.ide-agent-tabs/config.json` as `"defaultAgent"`. Keep any other
   keys in that file.
3. To add a custom agent, such as another agent CLI or a CLI set to a specific model, add a profile to
   `~/.ide-agent-tabs/agents.json`. Read `${CLAUDE_SKILL_DIR}/agent-profiles.md` for the format.

## 6. Other agents

Codex, Gemini CLI, Copilot CLI, Antigravity CLI and OpenCode can use Agent Tabs too. With it, they can list IDEs, open,
list and close agent tabs, and message any other agent session on this machine, including this one.

A Codex agent tab needs no registration: it starts Codex with its own Agent Tabs server and messaging
hooks. It needs Codex 0.158 or later. Register Codex only for Codex sessions outside tabs, and only on
macOS and Linux. On Windows, `--register codex` refuses, because the Codex desktop app reads the same
config and would open a console window each time it starts the server.

1. Run:

   ```sh
   node "${CLAUDE_PLUGIN_ROOT}/dist/sync-ides.mjs" --agents
   ```

   The JSON lists each agent with `installed`, `registered`, the server `path` it's registered with,
   `stable`, which is `true` when that path is the copy in `~/.ide-agent-tabs/mcp/`, and `hooks`, which
   is `true` when the messaging hooks are in place (`null` for Codex and OpenCode, which get none). Claude Code
   isn't listed, because it gets the server and the hooks from this plugin.

2. Show the installed agents and whether each can already use Agent Tabs. Treat an agent with
   `registered` but not `stable`, or with `hooks: false`, as one that needs registering again. If no
   agent is installed, skip this step.

3. Ask once which agents to register. The default is every installed agent that isn't registered with
   the stable copy, except Codex on Windows. Then run:

   ```sh
   node "${CLAUDE_PLUGIN_ROOT}/dist/sync-ides.mjs" --register <agent> [<agent>...]
   ```

   Registering also adds the messaging hooks: to `~/.gemini/settings.json` for Gemini CLI, as
   `~/.copilot/hooks/ide-agent-tabs.json` for Copilot CLI, and as the `ide-agent-tabs` group in
   `~/.gemini/config/hooks.json` for Antigravity CLI. For Antigravity CLI, it also adds the allow rule
   `mcp(ide-agent-tabs/*)` to `permissions.allow` in `~/.gemini/antigravity-cli/settings.json`, so the
   agent doesn't ask before each Agent Tabs tool call. For Codex, it sets `env_vars` and
   `tool_timeout_sec` in the server's table in `~/.codex/config.toml`, and removes Agent Tabs hooks that
   earlier versions added to `~/.codex/hooks.json`. The script keeps every other entry in those files.

   The JSON report lists each agent with `ok`, or an `error`. For a config file the script can't edit
   safely, such as a JSON file with comments, the error says so; show the user the entry to add by hand
   from the "Other agents" section of the MCP server README.

4. Tell the user to restart open sessions of those agents, so they load the server and the hooks. Tell
   them that Codex sessions get messaging hooks only in Codex agent tabs.

## 7. Terminal

Ask whether the user wants agent tabs outside IDEs, in a terminal app. If not, skip this step.

`mcp__plugin_ide-agent-tabs_ide-agent-tabs__list_ides` shows the terminal apps installed here. Ask which one `open_tab` uses when no IDE fits, and
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

## 8. Codex plugin (optional)

If `codex` is installed, offer OpenAI's Codex plugin for Claude Code. It gives `/codex:review` and
`/codex:rescue`, and the `delegate` skill uses it when present:

```sh
claude plugin marketplace add openai/codex-plugin-cc
claude plugin install codex@openai-codex
```

Then run `/reload-plugins`.

## 9. Jev (optional)

Jev is TypeSafe's "System One" model. With Jev on, the MCP server adds `jev_` tools that let every
agent it serves ask Jev for a pick, a yes or no, or a ranking instead of spending a model turn. Each
request goes to TypeSafe's API and needs a TypeSafe API key. Ask whether the user wants it. If not,
skip this step.

1. Ask before you change `~/.ide-agent-tabs/config.json`. Then set `"jev": {"enabled": true}` in it,
   and keep every other key.
2. Offer to add tiers for `jev_route`, which the `delegate` skill uses to pick an agent. Each key is
   `<profile>` or `<profile>:<model>`, and each value says what that tier is for:

   ```json
   "jev": {
     "enabled": true,
     "tiers": {
       "claude:haiku": "Short lookups, renames and one-file edits",
       "claude:sonnet": "Well-specified implementation and reviews",
       "claude:opus": "Design judgment, changes across many files and long-horizon work",
       "codex": "A second opinion or an independent review"
     }
   }
   ```

   Write only the tiers the user agrees to, for agents that `list_agents` shows as installed. The
   `sonnet` alias resolves to Sonnet 5.5 only on the Anthropic API with Claude Code 2.1.284 or later;
   Bedrock, Google Cloud and Microsoft Foundry resolve it to an older Sonnet.
3. Check that the server finds a key:

   ```sh
   echo '{}' | node "${CLAUDE_PLUGIN_ROOT}/dist/mcp-server.mjs" jev status
   ```

   The reply's `key` is `env`, `credential-store` or `missing`. This check sends nothing to TypeSafe.
4. If `key` is `missing`, show the `key_error` text. Never ask the user to paste the key into this
   conversation or into a command. Tell them to do one of these themselves:
   - Set `TYPESAFE_API_KEY` in the environment that agent CLIs start from.
   - Store the key in the operating system's credential store under service `typesafe`, account
     `api_key`. On Windows, that's the generic credential `typesafe` with user name `api_key`. Python's
     `keyring` writes this entry, so a key that cartographer already uses works here too.
5. Tell the user to restart open agent sessions, so the server lists the Jev tools.

## 10. Check it works

1. Call `mcp__plugin_ide-agent-tabs_ide-agent-tabs__list_ides`. Every IDE with the extension installed and a window open appears.
2. Offer to open a test tab with `mcp__plugin_ide-agent-tabs_ide-agent-tabs__open_tab` in the current folder, then close it with
   `mcp__plugin_ide-agent-tabs_ide-agent-tabs__close_tab`.

Tell the user that the plugin keeps the IDE extensions up to date. When a Claude Code session starts
after a plugin update, it updates the extension in each editor that has it, puts the new JetBrains
plugin in the local repository, and refreshes the server copy that other agents use.
