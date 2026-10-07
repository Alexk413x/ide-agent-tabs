---
name: setup
description: Set up Agent Tabs on this machine - check Node.js, install the IDE extensions that ship with this plugin into JetBrains IDEs, VS Code and editors built on it, pick a default agent and where new tabs open, let other agent CLIs use Agent Tabs, and optionally add OpenAI's Codex plugin and turn on Jev judgments. Use after installing the ide-agent-tabs plugin, or when the user asks to set up, repair or check Agent Tabs.
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

Codex, Antigravity CLI, Copilot CLI, Gemini CLI, Grok Build, Pi, Hermes, OpenCode, Qwen Code and Goose can use
Agent Tabs too. With it, they can list IDEs, open,
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
   is `true` when the messaging hooks are in place (`null` for Codex, Pi and OpenCode, which get none). Claude Code
   isn't listed, because it gets the server and the hooks from this plugin.

2. Show the installed agents and whether each can already use Agent Tabs. Treat an agent with
   `registered` but not `stable`, or with `hooks: false`, as one that needs registering again. If no
   agent is installed, skip this step.

3. Ask once which agents to register. The default is every installed agent that isn't registered with
   the stable copy, except Codex on Windows. Then run:

   ```sh
   node "${CLAUDE_PLUGIN_ROOT}/dist/sync-ides.mjs" --register <agent> [<agent>...]
   ```

   Registering also adds the messaging hooks: as the `ide-agent-tabs` group in
   `~/.gemini/config/hooks.json` for Antigravity CLI, as `~/.copilot/hooks/ide-agent-tabs.json` for
   Copilot CLI, to `~/.gemini/settings.json` for Gemini CLI, to `$GROK_HOME/hooks/ide-agent-tabs.json`
   for Grok Build, to `hooks` in `config.yaml` (and the allowlist `shell-hooks-allowlist.json`) under
   `HERMES_HOME` for Hermes, to `~/.qwen/settings.json` for Qwen Code, and as a plugin in
   `~/.agents/plugins/ide-agent-tabs/` for Goose. For Antigravity CLI, it also adds allow rules to
   `permissions.allow` in `~/.gemini/antigravity-cli/settings.json` for the tools that read or message, so
   the agent doesn't ask before each of those calls. `open_tab`, `close_tab`, `handoff` and the `jev_`
   tools still ask. For Codex, it sets `env_vars` and
   `tool_timeout_sec` in the server's table in `~/.codex/config.toml`, and removes Agent Tabs hooks that
   earlier versions added to `~/.codex/hooks.json`. The script keeps every other entry in those files.

   The JSON report lists each agent with `ok`, or an `error`. For a config file the script can't edit
   safely, such as a JSON file with comments, the error says so; show the user the entry to add by hand
   from the "Other agents" section of the MCP server README.

4. Tell the user to restart open sessions of those agents, so they load the server and the hooks. Tell
   them that Codex sessions get messaging hooks only in Codex agent tabs.

   Tell them also that Grok Build, Pi, Hermes, OpenCode, Qwen Code, Goose and Codex (local) are untested:
   they come from each CLI's documentation. Pi and OpenCode get no hooks, so a session of either reads a
   message only when it calls `read_messages` or `wait_for_message`. Goose runs hooks with `sh -c`, so
   Windows needs Git Bash. Codex (local) needs Ollama 0.13.4 or later and needs no registration.
   Registering Hermes adds an allowlist entry for each of Agent Tabs' own hook commands and nothing else;
   never set `hooks_auto_accept` or `HERMES_ACCEPT_HOOKS`.

## 7. Tab settings and terminal

Settings in `~/.ide-agent-tabs/config.json` decide where a new tab opens when a request names no IDE or
terminal, whether it comes to the front, how an agent starts, and what happens to the old tab after a
handoff. An explicit request always wins: an `open_tab` call that names `ide`, an agent, `via` or
`focus`, or a user who names an IDE, a terminal or a launch, overrides these settings. A missing key means the default.

**IDE tabs**

| Setting | Key | Values | Default |
|---|---|---|---|
| Open new tabs in | `tabRouting` | `project` (IDE that has the project open), `caller` (IDE the request came from) | `project` |
| Bring new agent tabs to the front | `focusNewTabs` | `auto` (when you asked for the tab), `always`, `never` | `auto` |

- Open new tabs in: Where a new agent tab opens when no IDE or terminal is named.
- Bring new agent tabs to the front: Whether a tab that an agent opens with `open_tab` or `handoff`
  takes focus. With `auto`, it opens behind the current tab unless the agent passes `focus: true`, which
  the skills do when the user asked for the tab. `always` brings it to the front unless the call passes
  `focus: false`. `never` opens it behind unless the call passes `focus: true`. The **New Agent Tab**
  button always brings its tab to the front. Windows Terminal, WezTerm, Ghostty on Linux and kitty
  without remote control always bring a new tab or window to the front.

**Terminal tabs**

| Setting | Key | Values | Default |
|---|---|---|---|
| Preferred terminal | `terminal` | `auto`, or a terminal id such as `windows-terminal`, `wezterm`, `kitty`, `tmux`, `ghostty` or `iterm2` | `auto` |
| Shell (Windows) | `shell` | `auto`, or the absolute path to a PowerShell executable | `auto` |
| Terminal window | `terminalWindow` | `last` (use my last window), `dedicated` (a dedicated Agent Tabs window) | `last` |

- Preferred terminal: Terminal for agent tabs when no IDE is running or a terminal is asked for.
- Shell (Windows): PowerShell that runs agent tabs in a terminal. `auto` picks the newest PowerShell 7
  of any install (Store, MSI or winget), else Windows PowerShell 5.1.
- Terminal window: Whether terminal tabs join your last window or a window kept for Agent Tabs.

**Agents**

| Setting | Key | Values | Default |
|---|---|---|---|
| Launch through OpenRouter (Ori) | `launchVia` | `direct`, `ori` | `direct` |
| Close the old tab after a handoff | `closeAfterHandoff` | `true`, `false` | `true` |
| Allow resuming closed sessions | `allowResume` | `true`, `false` | `true` |
| Use the Claude Code mod (in-process messaging) | `claudeMod` | `on`, `off` | `on` |

- Launch through OpenRouter (Ori): Start supported agents with `ori <agent>`, which bills model usage
  through OpenRouter. Offer it only when `~/.ide-agent-tabs/detected.json` has a non-null `ori`, or
  `list_agents` shows `ori: true` for an agent. Ori launches `claude`, `codex`, `grok`, `hermes`,
  `opencode`, `pi` and `prime-agent`, only those it lists as installed. When Ori can't launch an agent,
  the tab starts directly. Tell the user that usage through Ori is billed through OpenRouter, and keep
  the setting `direct` unless they ask for `ori`. An `open_tab` call with `via` overrides it. A
  Codex tab doesn't launch through Ori with an npm Codex on Windows, and `ori claude` may fail with
  `401 Missing Authentication header` in Ori 0.14.3; both are Ori limits.
- Close the old tab after a handoff: After a handoff, the new session closes the old tab once both
  sides confirm. `false` leaves the old tab open, marked as handed off.
- Allow resuming closed sessions: With `true`, agents can reopen a Claude Code, Codex or Antigravity CLI
  session that ended in the last 7 days with `resume_tab`. A resume past the prompt cache, or of a large
  session, re-reads the whole history at full input price, so the agent asks the user first. `false`
  refuses every resume. The record of each ended session, in `~/.ide-agent-tabs/history/`, keeps no
  transcript text beyond a 120-character preview of the last answer.
- Use the Claude Code mod (in-process messaging): With `on`, Claude Code sessions message other agents
  with SendMessage and ListAgents, get their messages in-process, and have the `/agent-messages` pane.
  `off` turns the mod off; Claude Code then uses the hooks, wake lines and messaging tools. Claude Code
  sessions that start after the change pick it up. Suggest `off` only when the user reports a problem
  with the mod.

To change a setting, use the IDE settings or edit `config.json` and keep every other key:

- VS Code and editors built on it: **Settings > Extensions > Agent Tabs**, in the sections **Agent Tabs:
  IDE tabs** and **Agent Tabs: Terminal tabs**. The terminal and shell settings have a **Choose…** link
  that lists the detected options. The launch, handoff and resume settings are in the **Agent Tabs** section,
  and the launch setting shows only when Ori is detected.
- JetBrains IDEs: **Settings > Tools > Agent Tabs**, in the groups **IDE tabs** and **Terminal tabs**.
  The Shell row shows on Windows only. The launch setting sits next to **Default agent** and shows only
  when Ori is detected.

The lists of terminals and PowerShell installs come from `~/.ide-agent-tabs/detected.json`, which the
MCP server writes. `mcp__plugin_ide-agent-tabs_ide-agent-tabs__list_ides` shows the same `terminals`, and
on Windows the `shells`.

Ask whether the user wants to change any of them. Write only the keys they change. Ask whether they want agent tabs outside IDEs, in a
terminal app. If not, skip the rest of this step. Otherwise ask which terminal `open_tab` uses when no
IDE fits, and write the choice as `"terminal"`. Without one, the server uses the first installed
terminal in this order:

- Windows: `windows-terminal`, `wezterm`.
- macOS: `ghostty`, `iterm2`, `kitty`, `wezterm`, `tmux`.
- Linux: `ghostty`, `kitty`, `wezterm`, `tmux`.

If the user picks `kitty`, tell them to add these two lines to `kitty.conf` (usually
`~/.config/kitty/kitty.conf`) and restart kitty, so it can open tabs. Without them, each agent opens
in a new kitty window that `close_tab` can only close on a best-effort basis.

- Linux: `allow_remote_control socket-only` and `listen_on unix:${XDG_RUNTIME_DIR}/kitty-agent-tabs`
- macOS: `allow_remote_control socket-only` and `listen_on unix:${TMPDIR}/kitty-agent-tabs`

If the user picks `iterm2`, tell them that the first tab makes macOS ask whether the app that runs the
agent may control iTerm. They must allow it. They can change the answer later in **System Settings >
Privacy & Security > Automation**.

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
     `keyring` writes this entry, so a key stored with it works here too.
5. Tell the user to restart open agent sessions, so the server lists the Jev tools.

## 10. Check it works

1. Call `mcp__plugin_ide-agent-tabs_ide-agent-tabs__list_ides`. Every IDE with the extension installed and a window open appears.
2. Offer to open a test tab with `mcp__plugin_ide-agent-tabs_ide-agent-tabs__open_tab` in the current folder, then close it with
   `mcp__plugin_ide-agent-tabs_ide-agent-tabs__close_tab`.

Tell the user that the plugin keeps the IDE extensions up to date. When a Claude Code session starts
after a plugin update, it updates the extension in each editor that has it, puts the new JetBrains
plugin in the local repository, and refreshes the server copy that other agents use.
