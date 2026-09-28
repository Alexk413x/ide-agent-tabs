# Agent Tabs design

Agent Tabs opens AI coding-agent sessions (Claude Code, Codex, Gemini CLI, Copilot CLI and others) in
IDE editor tabs. A person opens them with one button. An agent opens, lists and closes them in any IDE
running on the same machine.

This document is the contract every part builds against. Change it before you change the protocol.

## Parts

| Part | Status | Location |
|---|---|---|
| JetBrains plugin | Built | `jetbrains/` |
| Protocol: registry and HTTP API | Built | This document |
| Claude Code plugin: `delegate` skill | Built | `claude-plugin/`, marketplace in `.claude-plugin/` |
| CI and release workflows | Built | `.github/workflows/` |
| Claude Code plugin: MCP server, `new-tab`, `setup` and `update` skills | Built | `claude-plugin/`, `mcp/` |
| VS Code extension (VS Code and editors built on it) | Built | `vscode/` |
| Jev judgment tools in the MCP server, and the `jev` skill (optional) | Built | `mcp/src/jev/`, `claude-plugin/skills/jev/`; later steps in [jev-integration.md](jev-integration.md) |

Messaging between agents is out of scope. Claude Code sessions message each other with Claude Code's
own `ListAgents` and `SendMessage`. For other CLIs, see [Messaging](#messaging).

## Registry

Each IDE process writes one file to `~/.ide-agent-tabs/endpoints/`. A VS Code extension writes one file
per window. Name the file `<ide>-<pid>.json`, or `vscode-<pid>-<8 hex characters>.json` for VS Code.
The file name without `.json` is the IDE's id in the MCP tools.

```json
{
  "protocol": 1,
  "ide": "jetbrains",
  "product": "IntelliJ IDEA",
  "version": "2026.2.2",
  "pid": 12345,
  "url": "http://127.0.0.1:63342/ide-agent-tabs",
  "token": "<64 hex characters>"
}
```

- `ide` is one of `jetbrains`, `vscode` or `visualstudio`.
- `url` is the API base. Routes are `<url>/<route>`.
- `token` is 32 random bytes, hex-encoded, made fresh each time the IDE starts.
- Write the file atomically: write a temporary file in the same folder, then rename it.
- On macOS and Linux, create the folder with mode `0700` and the file with mode `0600`. On Windows, the
  folder inherits the user profile's permissions, which already exclude other users.
- Delete the file when the IDE or window closes.
- Readers ignore a file whose `pid` isn't a running process, and may delete it.
- A reader that sees a `protocol` value it doesn't know skips that file.

To use a folder other than `~/.ide-agent-tabs`, set the environment variable `IDE_AGENT_TABS_HOME` (VS Code
extension and MCP server) or the Java system property `ide.agent.tabs.home` (JetBrains). Tests and the
sandbox IDE use this so they never mix with real IDEs.

## HTTP API

Every route takes a `POST` with `Content-Type: application/json` and an `Authorization: Bearer <token>`
header. Every reply is JSON with `"ok": true`, or `"ok": false` and an `"error"`.

The server refuses non-loopback addresses and any request with an `Origin` or `Referer` header.

| Route | Body | Reply |
|---|---|---|
| `info` | `{}` | `ide`, `product`, `version`, `pid`, and `projects`: `name`, `path`, `focused` for each open project or folder |
| `agents` | `{}` | `default`, and `agents`: `name`, `label`, `command`, `installed` for each profile |
| `open` | `path`, and optional `agent`, `prompt`, `args`, `env` | `id`, `agent`, `project`, `path` |
| `close` | `id` | `id` |
| `list` | `{}` | `tabs`: `id`, `agent`, `project`, `path` for each open tab this IDE opened |

`open` fields:

- `path` (required): an absolute path to an existing folder. The session starts there.
- `agent`: a profile name. The default is the configured default agent.
- `prompt`: the session's first message, up to 30,000 characters.
- `args`: up to 64 extra arguments for the agent. They go after the profile's own arguments and before
  the prompt. Flags that skip the agent's permission prompts are allowed.
- `env`: up to 64 environment variables for the session. Names that start with `IDE_AGENT_TABS_` or
  `JEDITERM_SOURCE` are refused.

The tab opens in the open project or folder that contains `path`, or in the last focused window if none
does.

| Status | Meaning |
|---|---|
| 200 | Done. |
| 400 | Bad body, relative path, missing folder, missing `id`, or unknown `agent`. |
| 401 | Missing or wrong token. |
| 403 | Non-loopback address, or an `Origin` or `Referer` header. |
| 404 | `close`: no open tab with that id. |
| 405 | Not a `POST`. |
| 409 | `open`: no project or folder is open. |
| 413 | The body is over 16 MB (VS Code extension). |
| 415 | `Content-Type` isn't `application/json`. |
| 503 | The IDE didn't act within 10 seconds, usually because a dialog is open. Nothing happens later. |

## Agent profiles

A profile says how to start one agent CLI. Every IDE uses the same built-in profiles:

| Name | Label | Command | First prompt |
|---|---|---|---|
| `claude` | Claude Code | `claude` | positional |
| `codex` | Codex | `codex` | positional |
| `gemini` | Gemini CLI | `gemini` | `-i <prompt>` |
| `copilot` | Copilot CLI | `copilot` | `-i <prompt>` |

The `claude` and `codex` rows match each CLI's help. The `gemini` and `copilot` rows come from each
CLI's docs and are untested. A profile in `agents.json` with the same name overrides a built-in one.

You add or override profiles in `~/.ide-agent-tabs/agents.json`:

```json
{
  "opencode": {
    "label": "OpenCode",
    "command": "opencode",
    "args": ["--model", "<provider>/<model>"],
    "promptFlag": "--prompt",
    "env": {}
  }
}
```

- `command`: the executable, found on the shell's `PATH`.
- `args`: arguments before the caller's `args`.
- `promptFlag`: the flag placed before the prompt. Leave it out when the prompt is positional. With no
  prompt, neither the flag nor a prompt is passed.
- `env`: environment variables for the session. The caller's `env` wins on a clash.
- `icon`: optional path to an SVG file for menus.

A profile is `installed` when its command is on the IDE's `PATH`. On Windows, the IDE also looks for
`.exe`, `.cmd`, `.bat` and `.ps1` files, because npm installs CLIs as `.cmd` and `.ps1` shims.

`~/.ide-agent-tabs/config.json` holds shared settings:

```json
{ "defaultAgent": "claude" }
```

### How a tab starts the agent

The IDE never builds a shell command from caller text. It passes everything in environment variables,
and a fixed launch script reads them:

| Variable | Holds |
|---|---|
| `IDE_AGENT_TABS_ID` | The tab id. It stays set, so the session can close its own tab. |
| `IDE_AGENT_TABS_AGENT` | The profile name. It stays set. |
| `IDE_AGENT_TABS_COMMAND` | The executable. Cleared before the agent starts. |
| `IDE_AGENT_TABS_ARGS` | PowerShell: all arguments as a JSON array. Cleared before the agent starts. |
| `IDE_AGENT_TABS_ARGC`, `IDE_AGENT_TABS_ARG_<n>` | bash, zsh, fish: the argument count, and each argument. Cleared before the agent starts. |
| `IDE_AGENT_TABS_PROMPT` | The first prompt. Cleared before the agent starts. |

The arguments are the profile's `args`, then the caller's `args`, then the `promptFlag` if there is a
prompt. The prompt comes last.

- Terminal tabs can't use environment variables, because a new Windows Terminal tab inherits the running
  terminal's environment, not the caller's. The MCP server writes a launch spec file instead (owner-only,
  under `~/.ide-agent-tabs/launch/`) holding the same values, and passes only its path to a fixed
  launcher, which reads and deletes it.
- Windows allows at most 32,767 characters in one environment variable, so a very large `args` list can
  fail to start on Windows.
- Parse `IDE_AGENT_TABS_ARGS` with a JSON parser that keeps strings as strings. PowerShell 7's
  `ConvertFrom-Json` turns a string such as `2024-01-01T00:00:00Z` into a date.
- Windows PowerShell 5.1 strips embedded double quotes from arguments it passes to native programs.
  Launchers that may run under 5.1 must escape them.

## Button

Every IDE shows a **New Agent Tab** button, and binds **Ctrl+Alt+A** (**⌘⌥A** on macOS) to it.

- Click it to open a tab running the default agent in the current project or folder.
- To open another agent, open the agent menu: right-click the button in a JetBrains IDE, or click the
  arrow next to it in VS Code. The menu lists each installed agent with its logo, and a **Settings**
  item. Choosing an agent opens a tab and leaves the default unchanged.
- To change the default, use **Settings > Tools > Agent Tabs** in a JetBrains IDE, or the
  `ideAgentTabs.defaultAgent` setting in VS Code.

The default lives in `~/.ide-agent-tabs/config.json`, so all IDEs and the MCP server share it.

## MCP server

A stdio MCP server, written in TypeScript and bundled into one file for Node 20 or later. It reads the
registry and calls the HTTP API.

| Tool | Does |
|---|---|
| `list_ides` | Lists running IDEs with their projects, from the registry and each IDE's `info`. |
| `list_agents` | Lists profiles and which are installed. |
| `list_tabs` | Lists tabs across all IDEs, or in one. |
| `open_tab` | Opens a tab. Takes `path`, and optional `agent`, `prompt`, `args`, `env`, `ide`. |
| `close_tab` | Closes a tab by `id`. With no `id`, closes the caller's own tab through `IDE_AGENT_TABS_ID`. |

`open_tab` routing, first match wins:

1. The IDE or terminal named by `ide`, using its id from `list_ides`.
2. The IDE with an open project that contains `path`, preferring the focused window.
3. The most recently started IDE.
4. When no IDE is running: the preferred terminal from `config.json`, then the first installed terminal
   in the platform's order: Windows Terminal, then WezTerm on Windows; Ghostty, kitty, WezTerm, then tmux
   on macOS and Linux.

The reply includes a `reason` that says which rule chose the target.

## Terminals

Standalone terminal apps need no extension. The MCP server drives them directly and shows each one in
`list_ides` next to the IDEs, with `ide` set to the terminal's name, such as `windows-terminal` or
`ghostty`. A terminal tab starts the agent with the same profiles and argument order as an IDE tab, but
through a launch spec file (see [How a tab starts the agent](#how-a-tab-starts-the-agent)).

What each terminal allows differs. `list_ides` reports each terminal's capabilities, and `list_tabs` and
`close_tab` answer only for terminals that can list or close tabs.

| Terminal | OS | Open | List | Close | How |
|---|---|---|---|---|---|
| Windows Terminal | Windows | Tab | Tracked | Best effort | `wt.exe -w 0 new-tab` with `pwsh` and `agent-launch.ps1`. No outside API to query tabs, so `list_tabs` shows only the tabs this server opened, while their launcher's shell runs. `close_tab` ends that shell, which may leave the tab open with an exit message. |
| Ghostty 1.3+ | macOS | Tab | Yes | Yes | AppleScript: `new tab` with a surface configuration (command and environment variables); query `terminals` by id; `close`. |
| Ghostty | Linux | Window | Tracked | Best effort | A new process per agent: `ghostty --gtk-single-instance=false --working-directory=<dir> --confirm-close-surface=false --wait-after-command=false -e <shell> -l -i -c …`. Ghostty can't open a tab in a running instance from outside ([ghostty#12136](https://github.com/ghostty-org/ghostty/issues/12136)). The launcher writes its shell's pid; `list_tabs` checks that the shell runs, and `close_tab` sends it `SIGHUP`. |
| WezTerm | Windows, macOS, Linux | Tab | Yes | Yes | `wezterm cli --no-auto-start spawn --cwd <dir> -- …` prints the pane id; `cli list --format json`; `cli kill-pane --pane-id`. `WEZTERM_UNIX_SOCKET` names the newest running GUI's `gui-sock-<pid>` ([wezterm#4456](https://github.com/wezterm/wezterm/issues/4456)). With no GUI running, `wezterm start` opens one. |
| kitty | macOS, Linux | Tab | Yes | Yes | With remote control on: `kitten @ --to <socket> launch --type=tab`, `ls` and `close-window --match id:<n>`. Without it: a new `kitty` process per agent, tracked like Ghostty on Linux (Window, Tracked, Best effort). |
| tmux 3.0+ | macOS, Linux | Tab | Yes | Yes | `tmux new-window -e … -- …` in the most recently attached session, else in the detached session `agents`; `list-windows -a`; `kill-window`. |

- The MCP server tracks the tabs it opens in terminals in `~/.ide-agent-tabs/terminal-tabs.json`, with
  the terminal's own tab, pane or window id where it has one, and the shell's pid file otherwise.
- `open_tab` uses a terminal when the caller names one, or when no IDE is running. The preferred one is
  `"terminal"` in `~/.ide-agent-tabs/config.json`, such as `"terminal": "ghostty"`.
- A new tab gets the launcher and spec paths in one of two ways. In env mode, the terminal sets
  `IDE_AGENT_TABS_LAUNCHER` and `IDE_AGENT_TABS_SPEC` in the tab (Ghostty, kitty, tmux). In argv mode, they
  are positional arguments of the login shell, whose fixed `-c` script sets `IDE_AGENT_TABS_SPEC` and
  sources the launcher (WezTerm, whose new panes get the GUI's environment, not the caller's). Argv mode
  with fish needs fish 3.2 or later.
- A command line holds only fixed flags, the server's own paths, the folder and a cleaned title. The
  server refuses a path that holds a control character, and a path with `;` for Windows Terminal and tmux,
  which split commands at `;`. tmux gets no `-c <dir>`, because it expands formats such as `#(…)` there;
  the launcher changes to the folder instead.
- A terminal the server starts gets the server's environment without the variables that identify the
  calling agent session, such as `CLAUDECODE` or `CODEX_SANDBOX`.
- The POSIX launchers write the shell's pid to `launch/<id>.pid` when the spec names a pid file. The
  shell then replaces itself with an interactive login shell, so the pid stays the same after the agent
  exits.
- kitty has no remote control by default and no default socket. To turn it on, add these lines to
  `kitty.conf` and restart kitty:
  - Linux: `allow_remote_control socket-only` and `listen_on unix:${XDG_RUNTIME_DIR}/kitty-agent-tabs`
  - macOS: `allow_remote_control socket-only` and `listen_on unix:${TMPDIR}/kitty-agent-tabs`

  kitty appends `-<pid>` to the socket path. The server uses `KITTY_LISTEN_ON` when it's set, and
  otherwise the newest `kitty-agent-tabs-*` socket that answers `kitten @ ls`. `list_ides` reports kitty's
  capabilities for the mode a new tab would use.
- WezTerm's most recent stable release is 20240203, and most users run nightly builds. The driver uses only
  `cli spawn`, `cli list`, `cli kill-pane` and `start`, which both have. It records the GUI socket with
  each pane id, so a restarted GUI doesn't match old ids. When the server starts the GUI itself, it
  records the pane id once the new GUI's socket answers, within 10 seconds. Otherwise `list_tabs` shows
  the tab for 60 seconds and `close_tab` can't close it.
- tmux: the server uses the default tmux server. When no client is attached, `open_tab` adds a `note` to
  its reply: run `tmux attach -t agents`. The server records the tmux socket and server pid with each
  window id, so a restarted tmux server doesn't match old ids.

## Messaging

- Claude Code to Claude Code: Claude Code's native `ListAgents` and `SendMessage`. They work across tabs
  and IDEs on the same machine, but not between WSL and native Windows.
- Other CLIs: no messaging. An agent hands one-shot work to another CLI with the `delegate` skill, and
  starts an interactive session in a tab with `open_tab`.

## Delegation

Tabs are for interactive sessions. Delegation is for one-shot work: an agent hands a task, review or
question to another agent CLI in headless mode and reads the answer back. The Claude Code plugin ships a
`delegate` skill for this (`/ide-agent-tabs:delegate`).

### What already exists

- **Codex:** OpenAI's [Codex plugin for Claude Code](https://github.com/openai/codex-plugin-cc)
  (Apache-2.0) adds `/codex:review`, `/codex:adversarial-review`, `/codex:rescue`, and background-job
  commands. It drives Codex's app server, not `codex exec`. The `delegate` skill hands Codex work to this
  plugin when it's installed, and falls back to `codex exec` when it isn't.
- **Several CLIs from one MCP server:** [pal-mcp-server](https://github.com/BeehiveInnovations/pal-mcp-server)
  (Apache-2.0) has a `clink` tool that runs Gemini CLI, Codex or Claude Code as child processes. It's an
  option if the `delegate` skill outgrows plain shell calls.
- Codex 0.154 has no MCP server mode (`codex mcp-server`), so the skill runs the CLI directly.

### Rules the skill follows

- **Prompt in a file, never on the command line.** Each run gets a folder under
  `$TMPDIR/ide-agent-tabs/delegate/` holding `prompt.md`, the final answer, the event log and
  `meta.json` (agent, mode, folder, session id).
- **Read-only by default.** Reviews, questions and second opinions can't change files, so they can run
  next to the calling session. Write tasks run in a separate git worktree (`codex exec --worktree`), or
  only after the user agrees to changes in the current tree. The skill never turns off an agent's
  sandbox or approvals unless the user asks.
- **Long runs go to the background.** Agents often take minutes. In one test, a trivial `codex exec` took
  106 seconds. The skill uses the Bash tool's `run_in_background` and waits for the exit
  notification. Claude Code's foreground Bash calls stop after 10 minutes.
- **Success needs an answer file.** A zero exit code isn't enough, because some CLIs exit 0 after doing
  nothing. The final-answer file must exist and be non-empty.
- **No hard-coded models.** Model ids change often and depend on the user's login and plan. The skill
  passes a model only when the user names one.
- **Check the installed CLI's help first.** Flags change between releases, so the skill confirms them
  with `--help` once per session.

### Headless commands

| Agent | Run once | Answer | Follow-up | Tested |
|---|---|---|---|---|
| Codex | `codex exec -s read-only -C <dir> -o <answer> --json - < prompt.md` | `-o` file | `codex exec resume <thread_id> - < followup.md`; the id is in the `thread.started` event | Yes, 0.154.0 |
| Claude | `claude -p --output-format json --permission-mode plan < prompt.md` | `.result` | `--resume <session_id>` | Flags checked in help |
| Gemini CLI | `gemini -p "<instruction>" --output-format json < prompt.md` | `.response` | Unreliable in headless mode | No |
| Copilot CLI | `copilot -p …` | Output | `--resume` has open Windows bugs | No |
| OpenCode | `opencode run … --format json` | JSON events | `opencode run -c` | No |

### Later

- A delegated run could open as a tab instead, so the user can watch it: `open_tab` with the same prompt,
  then read the result through the messaging layer.
- If shell calls prove fragile, move delegation into the MCP server as a `delegate` tool, or adopt
  pal-mcp-server's `clink`.

## Install

The repository is a Claude Code plugin marketplace. Two commands install the plugin:

```sh
claude plugin marketplace add Alexk413x/ide-agent-tabs
claude plugin install ide-agent-tabs@ide-agent-tabs
```

Installing the plugin at user scope makes its skills available in every session and every IDE.

Then, in a session, run `/ide-agent-tabs:setup`. The setup skill asks before each change, and:

- checks for Node.js 20 or later;
- finds VS Code and editors built on it with `sync-ides.mjs --status`, and installs the bundled `.vsix`
  into the editors the user picks with `sync-ides.mjs --install <cli>…`;
- finds JetBrains IDEs through their `product-info.json`, and skips any build older than 262.10315;
- creates the local JetBrains plugin repository with `sync-ides.mjs --install --jetbrains`, and gives the
  user two one-time steps for each JetBrains IDE: add the repository's `file:///` URL in **Settings >
  Plugins > ⚙ > Manage Plugin Repositories**, then install **Agent Tabs** from the **Marketplace** tab
  and restart the IDE;
- reports which agent CLIs are installed, and writes the default agent and the preferred terminal to
  `~/.ide-agent-tabs/config.json`;
- registers the MCP server with the other agent CLIs the user picks (Codex, Gemini CLI, Copilot CLI,
  OpenCode) with `sync-ides.mjs --register <agent>…`;
- offers to add OpenAI's Codex plugin (`claude plugin marketplace add openai/codex-plugin-cc`, then
  `claude plugin install codex@openai-codex`) when Codex is installed.

The setup skill never edits an IDE's settings files, and never installs a JetBrains plugin from the
command line.

The Codex plugin isn't declared as a plugin dependency. A dependency from another marketplace installs
only when this marketplace lists it in `allowCrossMarketplaceDependenciesOn` and the user has already
added OpenAI's marketplace. Otherwise the install is refused, which would break the one-step install.

## Distribution and updates

The Claude Code plugin carries the IDE extensions. The IDEs install them from files on the local disk,
so no IDE downloads anything.

### Bundled files

`claude-plugin/dist/` is generated. Commit it with the plugin, because the plugin runs it as is.

| File | Holds | Built by |
|---|---|---|
| `mcp-server.mjs`, `launch/`, `THIRD_PARTY_NOTICES.txt` | The MCP server, the terminal launch scripts and bundled licenses | `mcp/build.mjs` |
| `sync-ides.mjs` | The IDE sync script, from `mcp/src/sync.ts` | `mcp/build.mjs` |
| `ide/ide-agent-tabs.vsix` | The VS Code extension | `scripts/pack-ides.mjs` |
| `ide/ide-agent-tabs-jetbrains.zip` | The JetBrains plugin | `scripts/pack-ides.mjs` |
| `ide/versions.json` | The bundled version of each, such as `{"vscode": "0.1.17", "jetbrains": "0.4.1"}` | `scripts/pack-ides.mjs` |

`scripts/pack-ides.mjs` runs `gradlew buildPlugin` with a JDK 25 or later, and `npm run package` in
`vscode/`. It finds the JDK through `JAVA_HOME`, the JetBrains Runtime bundled with an installed IDE, or
common JDK folders. Run it after you raise `pluginVersion` in `jetbrains/gradle.properties` or `version`
in `vscode/package.json`.

### What updates from where

| Part | Update source | How it updates |
|---|---|---|
| Claude Code plugin | This repository, through the marketplace | `claude plugin update ide-agent-tabs@ide-agent-tabs`, or auto-update turned on for the marketplace in `/plugin` (off by default for a marketplace you add yourself) |
| VS Code extension | `dist/ide/ide-agent-tabs.vsix` in the installed plugin | The session start hook runs `<cli> --install-extension <vsix> --force` in each editor that has an older version. |
| JetBrains plugin | `~/.ide-agent-tabs/repository/updatePlugins.xml` | The session start hook puts the bundled zip there. The IDE offers the update from its custom plugin repository. |
| MCP server for other agents | `~/.ide-agent-tabs/mcp/`, a copy of `mcp-server.mjs`, `launch/` and `THIRD_PARTY_NOTICES.txt` | Codex, Gemini CLI, Copilot CLI and OpenCode run this copy, because the plugin's own path changes with each version. The session start hook refreshes it when the bundled server changes and the folder exists. |

### Session start hook

`claude-plugin/hooks/hooks.json` runs `node dist/sync-ides.mjs --hook` when a Claude Code session starts,
with a 60-second timeout. The hook:

1. Compares `dist/ide/versions.json` with `~/.ide-agent-tabs/synced.json`, and stops when the versions
   match the last sync and the server copy needs no refresh (step 5). After a failed IDE sync, it tries
   again at later sessions, up to three attempts for the same versions. Steps 3 and 4 run only when the
   versions changed or a retry is due.
2. Creates `~/.ide-agent-tabs/sync.lock`, so two sessions don't sync at once. It treats a lock older than
   five minutes as stale.
3. Finds each editor command-line tool: `code`, `code-insiders`, `cursor`, `windsurf`, `codium` and
   `antigravity-ide`, on `PATH` or in the usual install folders. For each, it lists the installed
   extensions, and installs the bundled `.vsix` only where an older version of Agent Tabs is installed.
   It never installs the extension into an editor that doesn't have it.
4. If `~/.ide-agent-tabs/repository/` exists, copies the zip there as `ide-agent-tabs-<version>.zip`,
   rewrites `updatePlugins.xml` with a `file:///` URL, and deletes older zips. It never replaces a newer
   version that is already in the folder.
5. If `~/.ide-agent-tabs/mcp/` exists and the bundled server's hash differs from the one in
   `synced.json`, copies the server there, one file at a time through a temporary file and a rename. A
   file in use fails the copy, and the next session tries again.
6. Writes `synced.json`, appends any errors to `~/.ide-agent-tabs/sync.log`, and prints a message for the
   session when it updated something.

The hook always exits with code 0, so a failed sync never blocks a session.

The JetBrains IDE offers the update at its next update check, or at once from **Settings > Plugins >
Installed > Check for Updates**. A JetBrains update needs an IDE restart. A VS Code update needs a window
reload.

The `publishLocal` Gradle task writes the same repository layout, so a development build and the bundled
plugin share one folder. After `publishLocal` writes a higher version, the hook leaves it in place until
the bundled version passes it.

### Update skill

`/ide-agent-tabs:update`:

1. Runs `claude plugin marketplace update ide-agent-tabs`, `claude plugin list` and
   `sync-ides.mjs --status`, and reports the installed and available versions.
2. If a newer plugin version exists, runs `claude plugin update ide-agent-tabs@ide-agent-tabs`, then asks
   the user to run `/reload-plugins` and the skill again. The loaded skill's paths point to the old
   version's files.
3. Runs `sync-ides.mjs --hook` to bring each set-up IDE to the bundled versions.
4. Reports each part's old and new version, and the reload or restart each IDE needs.

An IDE that was never set up needs the setup skill, not the update skill.

### GitHub Releases

The release workflow, `.github/workflows/release.yml`, publishes each IDE extension to GitHub Releases
for people who install by hand.

- **Trigger:** a tag per part and version, such as `jetbrains-v0.4.1` or `vscode-v0.1.17`. The version in
  the tag must match the part's own version (`pluginVersion` in `jetbrains/gradle.properties`, or
  `version` in `vscode/package.json`). The workflow fails otherwise.
- **JetBrains:** a Linux job sets up JDK 25, installs zsh and fish, and runs
  `./gradlew test buildPlugin verifyPlugin`. A Windows job runs the tests, including the PowerShell launch
  tests.
- **VS Code:** a Linux job runs `npm test` and `npm run build`, and packages the `.vsix`.
- **Publish:** the workflow attaches `ide-agent-tabs-<version>.zip` or `ide-agent-tabs-<version>.vsix`,
  and a `SHA256SUMS` file, to a GitHub Release named after the tag.

The build compiles against a local IDE when `studioPath` is set in `~/.gradle/gradle.properties`, and
downloads IntelliJ IDEA 2026.2.2 otherwise, as the release workflow does. The Plugin Verifier checks
IntelliJ IDEA 2026.2.2 and Android Studio 2026.2.2.2.

The plugin needs build 262.10315 or later (IntelliJ IDEA and Android Studio 2026.2.2). Earlier 2026.2
builds have a different `TerminalViewVirtualFile` constructor, so opening a tab would fail there.

## Jev judgments (optional)

[Jev](https://docs.typesafe.ai/) is TypeSafe's "System One" model. It reads text and returns a typed
judgment: one option out of up to 255 (Choice), a probability of yes (Noul), or a position on 2 to 10
described levels (Score). It writes no text. When Jev is turned on, the MCP server lists tools that let
any agent it serves ask Jev instead of spending a large-model turn on a pick, a yes or no, or a grade.
Codex, Gemini CLI, Copilot CLI and OpenCode get them through the same registration as the tab tools.

### Turning it on

`~/.ide-agent-tabs/config.json` holds the settings under `jev`:

```json
{
  "defaultAgent": "claude",
  "jev": {
    "enabled": true,
    "sure": 0.85,
    "tiers": {
      "claude:haiku": "Short lookups, renames and one-file edits",
      "claude:opus": "Design judgment and changes across many files",
      "codex": "A second opinion or an independent review"
    }
  }
}
```

- `enabled`: `false` by default. The server reads it when it starts. When it is not `true`, the server
  lists no Jev tools and sends no Jev instructions, so a session pays nothing for them.
- `sure`: the probability at or above which a Choice is `sure`. The default is 0.85.
- `tiers`: the options `jev_route` chooses from, written by the user. A name is `<profile>` or
  `<profile>:<model>`, and the text says what that tier is for. Nothing in the server names a model.
- `pricePerMillionInput`: optional. Overrides the price used for cost estimates, which is $0.042 per
  million input tokens as published on 2026-09-28. Output tokens are free.

The model is always `jev-latest`. Every answer carries the versioned id Jev returned, such as
`jev-1.13.0`.

### The API key

The server reads the key the first time a Jev tool runs, and keeps it only in memory:

1. `TYPESAFE_API_KEY` in the server's environment.
2. The operating system's credential store, service `typesafe`, account `api_key`. This is the entry
   Python's `keyring` writes, so the key cartographer uses on the same machine also works here.
   - Windows: the generic credential `typesafe` whose user name is `api_key`, else `api_key@typesafe`.
     A fixed PowerShell script reads it with `CredRead`. The script takes no caller text.
   - macOS: `security find-generic-password -s typesafe -a api_key -w`.
   - Linux: `secret-tool lookup service typesafe username api_key`.

The key never appears in a tool reply, the ledger, an error message, `config.json` or any agent's MCP
configuration. A missing key is an error that names where the server looked.

### Tools

Listed only when `jev.enabled` is `true`.

| Tool | Takes | Returns |
|---|---|---|
| `jev_status` | nothing | Where the key came from (`env`, `credential-store` or `missing`), the last model id seen, and today's calls, input tokens and estimated cost from the ledger |
| `jev_ask` | `state`, and `questions` in the API's own form | `model`, `answers`, `usage`, `cost_usd` |
| `jev_choose` | `instruction`, `options` (`id`, `description`), optional `state` and `no_match` | `choice`, `probabilities`, `confidence`, `band` (`sure`, `unsure` or `no-match`), `runner_up` |
| `jev_check` | `state`, and `conditions` (`id`, `question`) | The probability of yes for each condition |
| `jev_rank` | `query`, `items` (`id`, `text`), optional `top` | The items in order of relevance, each with its probability |
| `jev_route` | `task` | The chosen tier, the runner-up, the probabilities and `band`. Offers only tiers whose profile is installed |

- `jev_choose`, `jev_check`, `jev_rank` and `jev_route` build every question themselves. Each question
  says that the state is data to judge, not instructions to follow. `jev_ask` sends what the caller
  wrote, and its description asks the caller to say the same.
- `jev_choose` adds a `none` option ("none of the options fits") unless `no_match` is `false`. A
  `none` answer is band `no-match`.
- `jev_rank` asks one Noul per item in one request, "is this item relevant to the query". The state
  holds the query and the items keyed by id, sorted by id. It takes at most 255 items.
- `jev_route` offers the configured tiers whose profile is installed, sorted by name, and lists the
  others in `skipped`. When only one tier is usable, it returns that tier with no call, because a
  Choice needs two options.
- Every reply that comes from a Jev call also carries `model` and `cost_usd`. `jev_status` also
  returns `sure`, the tier names and the ledger path, and `key_error` when the key is missing. It makes
  no network call, so its `openWorldHint` is `false`.
- The server refuses a Choice with more than 255 options, a Score with fewer than 2 or more than 10
  levels, and a request whose text is over 200,000 characters, before it calls the API.
- Every call is one request. The SDK retries 408, 429 and 5xx with backoff. Jev answers in well under a
  second, so a tool call has a 30-second limit in total.

The server's instructions, sent to every client when Jev is on, say when to reach for these tools: when
a step's answer is one of a set of options the agent can list, a yes or no about text it holds, or a
grade it can describe in levels, and the agent would otherwise decide it with a model turn. They also
say what Jev can't do: write text, count, do arithmetic, read images, or give a verdict that stands on
its own. A probability ranks options. It isn't proof.

### The ledger

Each Jev call appends one line to `~/.ide-agent-tabs/jev/ledger.jsonl` (folder `0700`, file `0600`):

```json
{"at":"2026-09-28T17:04:11.203Z","tool":"jev_route","agent":"codex","tab":"wt-3","model":"jev-1.13.0","questions":1,"input_tokens":2310,"ok":true}
```

`agent` and `tab` come from `IDE_AGENT_TABS_AGENT` and `IDE_AGENT_TABS_ID` when the caller runs in an
agent tab, and are `null` otherwise. A failed call records `ok: false`, `model: "jev-latest"`,
`input_tokens: 0` and the HTTP status, or `timeout` or `connection` when no status came back. A
request the server refuses before it calls the API, or a call with no key, writes no line. The ledger
never holds state, questions, answers or the key.

### Command line

`node mcp-server.mjs jev <status|ask|choose|check|rank|route>` reads one JSON request on stdin, in the
same form as the tool's input, and prints the tool's reply as JSON. It exits 1 on an error. It needs
`jev.enabled`, like the tools. An agent with no MCP support can use Jev this way, through the same copy
in `~/.ide-agent-tabs/mcp/` that other agents register.

### Skills

- `jev` (new): tells Claude Code when to use the Jev tools on its own, and how to write a question that
  Jev answers well. See [jev-integration.md](jev-integration.md) for the rules and the tests behind
  them.
- `delegate`: with the Jev tools listed and no agent named, the skill asks `jev_route` which tier takes
  the task. On `sure`, it uses the answer and says so. Otherwise it shows the top two and asks.
- `setup`: asks whether to turn Jev on, and checks that a key is found with `jev status`.

## Security

- The token limits the API to processes that can read your registry files, which means your own user
  account.
- Any such process can start any agent with any flags, including flags that skip permission prompts.
  This is by design: it is the same power as running the agent yourself.
- The server never passes caller text through a shell parser.
- The server makes one kind of network call: a Jev request to `https://api.typesafe.ai`, and only
  when Jev is turned on and a tool asks. Everything a caller puts in a Jev request leaves the machine.
  The tool descriptions say so.

## Tested on

| Part | Tested | Untested |
|---|---|---|
| VS Code extension | VS Code 1.118 and Antigravity 1.107 on Windows; the bash launcher through WSL | zsh, fish, macOS, remote workspaces |
| JetBrains plugin | Plugin Verifier against IntelliJ IDEA 2026.2.2 and Android Studio 2026.2.2.2 | — |
| WezTerm | Nightly 20260917 on Windows 11 and nightly 20260802 in WSL Ubuntu, with and without a GUI running | macOS |
| kitty | 0.49.1 in WSL Ubuntu, with and without remote control | macOS |
| tmux | 3.6 with bash in WSL Ubuntu, with and without an attached client | macOS |
| Ghostty on Linux | 1.3.1 in WSL Ubuntu accepts the flags. No window opens, because Ghostty needs OpenGL 4.3 and WSLg offers 4.1. | A working window |
| Ghostty on macOS | Unit tests of the AppleScript and command generation | A live run |
| Agent profiles | `claude` and `codex` flags checked against each CLI's help | `gemini` and `copilot` |

Open, list and close through the MCP server pass for tmux, kitty and WezTerm. No part is tested on a real
Mac. For the headless delegation commands, see the **Tested** column in
[Headless commands](#headless-commands).

## Possible future work

None of these is scheduled.

- **Visual Studio extension:** the **New Agent Tab** button and editor tabs in Visual Studio on Windows.
  Until then, the MCP server opens tabs for Visual Studio users in Windows Terminal.
- **Terminal and iTerm2 on macOS:** terminal drivers through AppleScript.
- **Messaging between other agent CLIs:** for example, a shared MCP mailbox such as
  [mcp_agent_mail](https://github.com/Dicklesworthstone/mcp_agent_mail).
- **Jev steps J3 to J5:** a routing bench, a guard hook and a cost report. See
  [jev-integration.md](jev-integration.md#phases).
