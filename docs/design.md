# Agent Tabs design

Agent Tabs opens AI coding-agent sessions (Claude Code, Codex, Gemini CLI, Copilot CLI and others) in
IDE editor tabs. A person opens them with one button. An agent opens, lists and closes them in any IDE
running on the same machine.

This document is the contract every part builds against. Change it before you change the protocol.

## Parts

| Part | Status | Location |
|---|---|---|
| JetBrains plugin | Built | `jetbrains/` |
| Protocol: registry and HTTP API | Built (Phase 0) | This document |
| Claude Code plugin: `delegate` skill | Built | `claude-plugin/`, marketplace in `.claude-plugin/` |
| CI and release workflows | Built | `.github/workflows/` |
| Claude Code plugin: MCP server, `new-tab`, `setup` and `update` skills | Built | `claude-plugin/`, `mcp/` |
| VS Code extension (also Antigravity and other VS Code-based editors) | Built | `vscode/` |
| Visual Studio extension (Windows Terminal tabs first) | Phase 3 | `visualstudio/` |

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
  "product": "Android Studio",
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

The server also keeps the loopback rules from version 0.2: it refuses non-loopback addresses and any
request with an `Origin` or `Referer` header.

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

The `claude` and `codex` rows are checked against each CLI's help. The `gemini` and `copilot` rows come
from their docs and are untested, because neither CLI is installed on the build machine. A profile in
`agents.json` with the same name overrides a built-in one.

You add or override profiles in `~/.ide-agent-tabs/agents.json`:

```json
{
  "opencode-local": {
    "label": "OpenCode (LM Studio)",
    "command": "opencode",
    "args": ["--model", "lmstudio/qwen3-coder"],
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

Every IDE shows a **New Agent Tab** button.

- Click it to open a tab running the default agent in the current project or folder.
- Right-click it to list every profile whose command is installed, each with its logo. Choose one to
  open a tab running that agent and to make it the default.

The default lives in `~/.ide-agent-tabs/config.json`, so all IDEs share it.

## MCP server (Phase 1)

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

## Terminals (Phase 1)

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
| Terminal, iTerm2 | macOS | Tab | Partly | Partly | AppleScript. Later. |

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
  calling agent session, such as `CLAUDECODE`.
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
- WezTerm's latest stable release is 20240203, and most users run nightly builds. The driver uses only
  `cli spawn`, `cli list`, `cli kill-pane` and `start`, which both have. It records the GUI socket with
  each pane id, so a restarted GUI doesn't match old ids. When the server starts the GUI itself, it
  records the pane id once the new GUI's socket answers, within 10 seconds. Otherwise `list_tabs` shows
  the tab for 60 seconds and `close_tab` can't close it.
- tmux: the server uses the default tmux server. When no client is attached, `open_tab` adds a `note` to
  its reply: run `tmux attach -t agents`. The server records the tmux socket and server pid with each
  window id, so a restarted tmux server doesn't match old ids.
- The Visual Studio extension (Phase 3) opens its tabs through the Windows Terminal support.
- Tested live: WezTerm on Windows 11 and in WSL Ubuntu; kitty 0.49.1 with and without remote control,
  and tmux 3.6 with and without an attached client, in WSL Ubuntu; open, list and close through the MCP
  server for tmux, kitty and WezTerm. Ghostty on Linux accepts its flags, but WSLg's OpenGL 4.1 is below
  Ghostty's 4.3, so no window opened. Untested: Ghostty on macOS, and every driver on a real Mac.

## Messaging

- Claude Code to Claude Code: Claude Code's native `ListAgents` and `SendMessage`. They work across tabs
  and IDEs on the same machine, but not between WSL and native Windows.
- Other CLIs: not decided. The first experiment is a shared MCP mailbox,
  [mcp_agent_mail](https://github.com/Dicklesworthstone/mcp_agent_mail), with Claude Code, Codex and
  Gemini CLI polling one mailbox.

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
- Codex no longer has an MCP server mode (`codex mcp-server` is gone in 0.154), so shelling out is the
  simplest route today.

### Rules the skill follows

- **Prompt in a file, never on the command line.** Each run gets a folder under
  `$TMPDIR/ide-agent-tabs/delegate/` holding `prompt.md`, the final answer, the event log and
  `meta.json` (agent, mode, folder, session id).
- **Read-only by default.** Reviews, questions and second opinions can't change files, so they can run
  next to the calling session. Write tasks run in a separate git worktree (`codex exec --worktree`), or
  only after the user agrees to changes in the current tree. The skill never turns off an agent's
  sandbox or approvals unless the user asks.
- **Long runs go to the background.** Agents often take minutes; a trivial `codex exec` took 106 seconds
  on the build machine. The skill uses the Bash tool's `run_in_background` and waits for the exit
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

## Install (Phase 1)

The repository is a Claude Code plugin marketplace. One set of commands sets up everything:

```sh
claude plugin marketplace add Alexk413x/ide-agent-tabs
claude plugin install ide-agent-tabs@ide-agent-tabs
```

Then, in a session, run `/ide-agent-tabs:setup`. The setup skill:

- finds installed IDEs and installs each extension from the private GitHub Releases (JetBrains IDEs must
  be closed for a command-line install);
- finds installed agent CLIs and reports which profiles work;
- offers to add OpenAI's Codex plugin (`claude plugin marketplace add openai/codex-plugin-cc`, then
  `claude plugin install codex@openai-codex`) when Codex is installed.

Installing the plugin at user scope makes its skills available in every session and every IDE.

The Codex plugin isn't declared as a plugin dependency. A dependency from another marketplace installs
only when this marketplace lists it in `allowCrossMarketplaceDependenciesOn` and the user has already
added OpenAI's marketplace. Otherwise the install is refused, which would break the one-step install.

## Releases and updates (Phase 1)

The repository is private. The Claude Code plugin updates straight from it. The IDE extensions update
from a local folder that an update skill fills from GitHub Releases, because an IDE can't sign in to
download a private release file.

### What updates from where

| Part | Update source | How it updates |
|---|---|---|
| Claude Code plugin | This repository, through the marketplace | `claude plugin update ide-agent-tabs@ide-agent-tabs`, or auto-update turned on for the marketplace in `/plugin` (off by default for a marketplace you add yourself) |
| JetBrains plugin | `~/.ide-agent-tabs/repository/updatePlugins.xml` | The IDE's custom plugin repository, added once as a `file:///` URL. The IDE offers each new version. |
| VS Code extension | GitHub Release `.vsix` | The update skill runs `code --install-extension <vsix> --force` |
| Visual Studio extension | GitHub Release `.vsix` | The update skill runs `VSIXInstaller.exe` |

### Release workflow

A GitHub Actions workflow, `.github/workflows/release.yml`, builds and publishes each part. GitHub
Actions runs on private repositories and bills against the account's monthly minutes. A Linux minute
counts once, a Windows minute twice and a macOS minute ten times, so the workflow uses Linux except where
a part needs Windows.

- **Trigger:** a tag per part and version: `jetbrains-v0.3.0`, `vscode-v0.1.0`, `mcp-v0.1.0`. The version
  in the tag must match the part's own version (`pluginVersion` in `jetbrains/gradle.properties`, or
  `version` in `vscode/package.json` or `mcp/package.json`); the workflow fails otherwise. Visual Studio
  gets a tag when it exists.
- **JetBrains job (Linux):** set up JDK 25, install zsh and fish, run `./gradlew test buildPlugin`, and
  run the Plugin Verifier. It attaches `ide-agent-tabs-<version>.zip` to a GitHub Release named after the
  tag.
- **PowerShell tests:** the Windows-only launch tests run in an optional Windows job, or locally before
  tagging.
- **Checksums:** every release also carries `SHA256SUMS`. The update skill refuses a file whose checksum
  doesn't match.

The build compiles against a local IDE when `studioPath` is set in `~/.gradle/gradle.properties`, and
downloads IntelliJ IDEA 2026.2.2 otherwise, as on a runner. The Plugin Verifier checks IntelliJ IDEA
2026.2.2 and Android Studio 2026.2.2.

The plugin needs build 262.10315 or later (IntelliJ IDEA and Android Studio 2026.2.2). Earlier 2026.2
builds have a different `TerminalViewVirtualFile` constructor, so opening a tab would fail there.

### Update skill

`/ide-agent-tabs:update` uses the GitHub CLI, signed in with read access to the repository:

1. Find the newest release for each part with `gh release list`.
2. Download the files with `gh release download`, and check them against `SHA256SUMS`.
3. JetBrains: copy the zip into `~/.ide-agent-tabs/repository` and rewrite `updatePlugins.xml` with the
   new version and a `file:///` URL. The IDE offers the update the next time it checks, or at once from
   **Settings > Plugins > Installed > Check for Updates**. An update needs an IDE restart.
4. VS Code and Visual Studio: install the downloaded `.vsix` with the editor's own command.
5. Update the Claude Code plugin itself with `claude plugin update`.
6. Report each part's old and new version, and any restart or reload the user must do.

The `publishLocal` Gradle task writes the same folder layout, so a local development build and a
downloaded release share one repository folder. The newest version wins.

The setup skill adds the `file:///` repository URL to each JetBrains IDE once. It can edit the IDE's
settings only while that IDE is closed; otherwise it tells the user which URL to add in **Settings >
Plugins > ⚙ > Manage Plugin Repositories**.

If the repository becomes public, each release can also carry an `updatePlugins.xml` with HTTPS URLs,
and JetBrains IDEs can point at
`https://github.com/Alexk413x/ide-agent-tabs/releases/latest/download/updatePlugins.xml` directly.
Whether the IDE follows GitHub's download redirect is untested.

## Security

- The token limits the API to processes that can read your registry files, which means your own user
  account.
- Any such process can start any agent with any flags, including flags that skip permission prompts.
  This is by design: it is the same power as running the agent yourself.
- The server never passes caller text through a shell parser.

## Phases

0. Done: registry, token, `info` and `agents` routes, and agent profiles in the JetBrains plugin.
   Right-click agent menu.
1. Built: the build without a local IDE, CI and release workflows, the `new-tab`, `setup` and `update`
   skills, and the MCP server with Windows Terminal and Ghostty (macOS) support. Not yet run on GitHub
   or tested on macOS.
2. Built: the VS Code extension, tested in VS Code 1.118 and Antigravity 1.107.
3. Visual Studio extension, opening Windows Terminal tabs.
