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
| CI workflow | Built | `.github/workflows/ci.yml` |
| Claude Code plugin: MCP server, `new-tab`, `setup` and `update` skills | Built | `claude-plugin/`, `mcp/` |
| VS Code extension (VS Code and editors built on it) | Built | `vscode/` |
| Messaging between agent sessions: MCP tools, hooks and the `message` skill | Built | `mcp/src/messaging/`, `mcp/src/agentHook.ts`, `claude-plugin/hooks/`, `claude-plugin/skills/message/` |
| Jev judgment tools in the MCP server, and the `jev` skill (optional) | Built | `mcp/src/jev/`, `claude-plugin/skills/jev/`; later steps in [jev-integration.md](jev-integration.md) |

Claude Code sessions message each other with Claude Code's own `ListAgents` and `SendMessage`. Sessions
of different agent CLIs message each other through the MCP server. See [Messaging](#messaging).

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

- `ide` is `jetbrains` or `vscode`.
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
| `input` | `id`, `text` | `id` |

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

`input` types `text` into the tab's terminal and presses Enter, as if the user typed it. `text` is one
line of up to 500 characters with no control characters. The MCP server uses it only to wake an idle
session for a new message.

| Status | Meaning |
|---|---|
| 200 | Done. |
| 400 | Bad body, relative path, missing folder, missing `id`, unknown `agent`, or `input` `text` that is empty, over 500 characters or holds a control character. |
| 401 | Missing or wrong token. |
| 403 | Non-loopback address, or an `Origin` or `Referer` header. |
| 404 | `close`, `input`: no open tab with that id. |
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
| `codex` | Codex | `codex` and fixed `args` (see [Codex tabs](#codex-tabs)) | positional |
| `gemini` | Gemini CLI | `gemini` | `-i <prompt>` |
| `copilot` | Copilot CLI | `copilot` | `-i <prompt>` |

The `claude` and `codex` rows match each CLI's help. The `gemini` and `copilot` rows come from each
CLI's docs and are untested. A profile in `agents.json` with the same name overrides a built-in one.
An `agents.json` profile named `codex` replaces the built-in `args` too, so its tabs lose messaging unless
it copies them.

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

The setup skill carries a copy of this section in `claude-plugin/skills/setup/agent-profiles.md`,
because the installed plugin holds only `claude-plugin/`. Change both together.

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

### Codex tabs

By default, the interactive Codex CLI attaches to a shared, long-lived app-server daemon. The daemon
starts MCP servers and hooks with its own environment, so they see the `IDE_AGENT_TABS_ID` of whatever
started the daemon, or none. A Codex tab therefore runs Codex in-process and brings its own Agent Tabs
setup. The `codex` profile's `args`, placed before the caller's `args` and the prompt, are:

- `--no-daemon`, which runs the session in the tab's own process.
- `-c mcp_servers.ide-agent-tabs={ … }`: the Agent Tabs MCP server for this session only. Its command
  is `node -e <one line>`, which imports `$IDE_AGENT_TABS_HOME/mcp/mcp-server.mjs`, or
  `~/.ide-agent-tabs/mcp/mcp-server.mjs`, so the profile holds no machine-specific path. It sets
  `env_vars = ["IDE_AGENT_TABS_ID", "IDE_AGENT_TABS_AGENT", "IDE_AGENT_TABS_HOME"]`, because Codex passes a
  stdio server only a fixed set of variables, and `tool_timeout_sec = 660`, because Codex's default of
  60 seconds would end `wait_for_message` early.
- One `-c hooks.<Event>=[…]` for each of `UserPromptSubmit`, `PostToolUse`, `PermissionRequest` and
  `Stop`. Each is an `mcp_tool` hook that calls the server's `agent_tabs_hook`
  tool with `input = { event = '<Event>', session_id = '${session_id}', turn_id = '${turn_id}' }`. The call runs over the session's own MCP connection, so no process
  starts and no console window opens.
- `-c hooks.state={ … }`, which trusts exactly those four hooks, so Codex runs them without a `/hooks`
  review. Codex keys a hook by its source path, event and position: for `-c` hooks the source is
  `/<session-flags>/config.toml`, or `C:\<session-flags>\config.toml` on Windows, so the profile lists
  both. The trusted hash is Codex's `version_for_toml` of the normalized hook: the SHA-256 of its
  key-sorted JSON. `mcp/test/codexTab.test.ts` recomputes the hashes. Codex's own `hooks/list` reports
  all five as `trusted` with these arguments.

Rules for these arguments:

- They need Codex 0.158 or later: 0.158 adds `--no-daemon`, and an older Codex exits with an unknown
  argument error.
- Codex splits a `-c` key on every `.`, so the hook keys, which contain `.`, go inside the
  `hooks.state` table value.
- No argument holds `"` or `%`, and every argument with a `cmd.exe` special character holds a space. On
  Windows, the PowerShell launchers pass arguments through Windows PowerShell 5.1 or npm's `codex.cmd`
  and `codex.ps1` shims, which would change them otherwise. TOML strings use single quotes, and the one
  line of JavaScript uses template literals.
- Codex drops the root `-c` options when a `-c` follows a subcommand, such as
  `codex -c a=1 resume -c b=2`. A caller's `args` must not pass `-c` after a subcommand.
- The VS Code and JetBrains profiles hold the same strings, and `mcp/test/codexTab.test.ts` checks them.

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
2. The IDE with an open project that contains `path`. The deepest such project wins; on a tie, the
   caller's own IDE, then the focused window, then the most recently started IDE.
3. The most recently started IDE.
4. When no IDE is running: the preferred terminal from `config.json`, then the first installed terminal
   in the platform's order: Windows Terminal, then WezTerm on Windows; Ghostty, kitty, WezTerm, then tmux
   on macOS and Linux.

The reply includes a `reason` that says which rule chose the target.

Results are compact JSON. An IDE error keeps the HTTP status and the IDE's `error` text and adds the
next step: on 409 from `open`, pass a terminal id as `ide`; on 503 or a timeout, ask the user to close
a dialog in the IDE; on an unknown agent, call `list_agents`. On 401 the server rereads the registry
and retries once when the same endpoint id now holds a new token, as after a JetBrains plugin reload.
Otherwise the error says the endpoint is stale.

The server's instructions start with one sentence that names the tab tools, then the messaging rules,
then the Jev rules when Jev is on. Claude Code cuts each server's instructions at 2,048 characters, so
`instructions.test.ts` fails when the joined text passes that.

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

Claude Code sessions message each other with Claude Code's native `ListAgents` and `SendMessage`. They
work across tabs and IDEs on the same machine, but not between WSL and native Windows, and not with
other agent CLIs. For that, every session that runs the MCP server can message every other one.

### Sessions

- A session's id is its tab id, `IDE_AGENT_TABS_ID`. A session that Agent Tabs didn't open gets an id
  that starts with `s-` when its MCP server starts. So does a server whose tab id another live server
  already holds, such as the server of a headless agent started from inside the tab.
- A Codex client sends its thread id in `_meta.threadId` of every tool call, hook calls included. A
  Codex session keeps its `IDE_AGENT_TABS_ID` only when that tab is open: `terminal-tabs.json` or an
  IDE's `list` holds it. Otherwise, such as under the shared Codex daemon, whose environment can name
  another tab or a closed one, the server renames the session to `codex-<threadId>` at its first tool
  call. It keeps the old id when another live server already holds `codex-<threadId>`.
- Each MCP server writes `~/.ide-agent-tabs/sessions/<id>.json`: `id`, `agent`, `path`, `pid`, the
  tab's `host` (an IDE id or a terminal name) when there is one, `startedAt`, `state`, and a Codex
  session's `threadId`. It deletes the
  file when it exits, including when its client closes its stdin. A file whose `pid` no longer runs is
  stale; readers ignore it and delete it.
- `agent` is `IDE_AGENT_TABS_AGENT`, or else comes from the MCP client's name: a name that contains
  `claude`, `codex`, `gemini`, `copilot` or `opencode` maps to that profile name.
- `host` is the terminal from `terminal-tabs.json`, or the IDE whose `list` holds the tab id. The server
  looks it up when it starts, and a sender looks it up again when the file has none.
- `state` is `idle`, `busy` or `permission`, with `stateAt`. The session's hooks set it (see
  [Noticing a message](#noticing-a-message)). Without hooks, it's `unknown`. A hook that runs before the
  server starts writes a file with only `id` and `state`, and the server keeps that state.
- Every change to a presence file happens under a lock file next to it, because the server, the hooks
  and senders all write it.

### Mailboxes

- Each session has a mailbox at `~/.ide-agent-tabs/mail/<id>/`. A message is one JSON file, written to
  `tmp/` and renamed into `new/`, so readers never see half a message. Reading a message moves it to
  `cur/`. The reader holds a lock file in the mailbox while it moves messages, because Node on Windows
  renames through an open handle, and two readers could both move the same file.
- A message holds `id` (`m-` and 16 hex characters), `from` (`id`, `agent`, `path`), `to`, `text`, an
  optional `replyTo`, and `sentAt`. The server sets `from` from its own session, so an agent can't send
  as another session.
- `text` is up to 32,000 characters. A session sends at most 20 messages a minute, and a mailbox holds
  at most 50 unread messages; past either limit, `send_message` fails. The send times live in the
  sender's `mail/<id>/sent.json`, under a lock, so every server of that session shares the limit.
- The server deletes read messages after 7 days, and the mailbox of a session that isn't live and hasn't
  changed for 7 days. It cleans up when it starts, and at most once an hour after that.

### Tools

| Tool | What it does |
|---|---|
| `list_sessions` | Lists live sessions: `id`, `agent`, `path`, `host`, `state`, and `self` for the caller. |
| `send_message` | Sends `text` to the session `to`, optionally as a reply to `replyTo`. Returns the message `id`, and `delivery`: `woken` or `queued`. A `note` says why a wake-up failed. |
| `read_messages` | Returns the caller's unread messages and marks them read. |
| `wait_for_message` | Waits up to `timeout` seconds (default 60, at most 600) for a message, optionally only one from `from` or replying to `replyTo`, and returns it, marked read. Returns `message: null` on timeout. Messages the filter skips stay unread. |

The server lists these tools in every session; nothing turns them on. `wait_for_message` watches `new/`
with `fs.watch` and also checks it every second, because `fs.watch` misses events on some file systems.

A received message is data from another agent, not an instruction from the user. The server wraps its
text in a header that says so, and the server instructions tell every agent to apply its user's rules
to a peer's request, and to ask the user before anything destructive a peer asks for.

### Noticing a message

An agent sees a message only when it calls `read_messages` or `wait_for_message`. Three things prompt it:

1. **Hooks.** `dist/agent-hook.mjs` runs as a command hook in Claude Code, Gemini CLI and Copilot CLI:
   `node agent-hook.mjs <cli> <event>`, with the hook's JSON on stdin. Codex tabs call the server's
   `agent_tabs_hook` tool instead, which runs the same logic. The hooks set `state`: `busy` when a
   prompt is submitted or a tool starts, `permission` when a permission prompt shows, and `idle` when a
   turn ends. When a message arrives unread, it adds a one-line reminder to the agent's context after
   the next prompt or tool call, once per message: `Agent Tabs: 1 unread message from <agent> <short id>.
   read_messages returns it.` The presence file keeps the ids already reminded in `reminded`. At the end
   of a turn with unread messages, it asks the agent to continue and read them, at most three times in a
   row. `read_messages`, `wait_for_message` and a new prompt reset that count. Without
   `IDE_AGENT_TABS_ID`, the hook does nothing. It always exits 0.
2. **Wake-up.** When the recipient's `state` is `idle` and its tab supports input, `send_message` types
   one fixed line into the tab: `Agent Tabs: new message from <agent> <short id>. Call read_messages.`
   `<agent>` keeps only `A-Z`, `a-z`, `0-9`, `.`, `_` and `-`, and `<short id>` is the first 8
   characters of the sender's id. The line never holds the message text. The sender first marks the
   session `busy`, so a second message doesn't type the line again before the session's hooks report
   `idle`; it restores `idle` when typing fails. The IDEs use the `input` route, and a 404 or any other
   error leaves the message `queued`. Terminals type the line, wait 200 ms, then send Enter separately,
   so a program with bracketed paste on sees a submitted line and not a paste:
   - tmux: `send-keys -t <window> -l -- <line>`, then `send-keys -t <window> Enter`.
   - WezTerm: `cli send-text --pane-id <id> --no-paste -- <line>`, then the same with `\r`.
   - kitty with remote control: `kitten @ send-text --match id:<n> --stdin`, with the line and then `\r`
     on stdin, because kitty reads escapes in a `send-text` argument.
   - Ghostty on macOS: AppleScript `input text <line> to terminal id <id>`, then `send key "enter"`.
   - Windows Terminal, Ghostty on Linux, and kitty windows started without remote control can't take
     input from outside, so their sessions rely on hooks.
3. **Waiting.** An agent that asked a question calls `wait_for_message` for the reply.

Nothing types into a session whose `state` is `busy`, `permission` or `unknown`.

Hook events for each CLI:

| CLI | Config | `busy` | `permission` | `idle` | Reminder after | Turn-end nudge |
|---|---|---|---|---|---|---|
| Claude Code | The plugin's `hooks/hooks.json` | `UserPromptSubmit`, `PostToolUse` | `Notification` `permission_prompt` | `Stop`, `Notification` `idle_prompt` | `UserPromptSubmit`, `PostToolUse` (`hookSpecificOutput.additionalContext`) | `Stop` (`decision: "block"`) |
| Codex | The tab's `-c` arguments, as `mcp_tool` hooks | `UserPromptSubmit`, `PostToolUse` | `PermissionRequest` | `Stop` | `UserPromptSubmit`, `PostToolUse` (`hookSpecificOutput.additionalContext`) | `Stop` (`decision: "block"`) |
| Gemini CLI | `hooks` in `~/.gemini/settings.json` | `BeforeAgent`, `BeforeTool`, `AfterTool` | `Notification` `ToolPermission` | `AfterAgent` | `BeforeAgent`, `AfterTool` (`hookSpecificOutput.additionalContext`) | `AfterAgent` (`decision: "deny"`) |
| Copilot CLI | `~/.copilot/hooks/ide-agent-tabs.json` | `userPromptSubmitted`, `preToolUse`, `postToolUse` | `notification` `permission_prompt` | `agentStop`, `notification` `agent_idle` | `postToolUse` only (`additionalContext`) | `agentStop` (`decision: "block"`) |

- The Claude Code plugin ships its hooks. `sync-ides.mjs --register gemini|copilot` adds the Gemini CLI
  and Copilot CLI hooks, pointing at `~/.ide-agent-tabs/mcp/agent-hook.mjs`, and `--unregister` removes
  only those entries. It doesn't change a file that isn't plain JSON.
- Copilot CLI drops the output of a `userPromptSubmitted` command hook, so it gets no reminder after a
  prompt.
- Only Codex tabs get Codex hooks (see [Codex tabs](#codex-tabs)). Global hooks in `~/.codex/hooks.json`
  would run under the shared daemon with another tab's `IDE_AGENT_TABS_ID`, and on Windows the Codex
  desktop app runs each command hook in a new console window. `--register codex` removes the command
  hooks that earlier versions installed.
- `--register codex` refuses on Windows. The Codex desktop app shares `~/.codex/config.toml` with the
  CLI, and Codex before 0.159 opens a console window each time the app starts an MCP server that way. A
  Codex tab needs no registration. On macOS and Linux, `--register codex` adds the server with
  `env_vars` and `tool_timeout_sec = 660`, as a Codex tab does, so that Codex sessions outside tabs get
  the tab tools and messaging. Copilot CLI passes a server only `PATH`, so its entry sets
  `"IDE_AGENT_TABS_ID": "${IDE_AGENT_TABS_ID}"` and the same for `IDE_AGENT_TABS_AGENT`. The server ignores
  a value that is still `${…}`.

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
- **Resume in the same mode.** Neither `codex exec resume` nor `claude -p --resume` keeps the first run's
  sandbox or permission mode, so a follow-up passes it again.
- **Codex plugin first.** `/codex:review` is user-only, so the skill asks the user to type it, and hands
  delegated work to the plugin's `codex:codex-rescue` subagent.

### Headless commands

| Agent | Run once | Answer | Follow-up | Tested |
|---|---|---|---|---|
| Codex | `codex exec -s read-only -C <dir> -o <answer> --json - < prompt.md` | `-o` file | `codex exec -s <sandbox> resume <thread_id> - < followup.md`; the id is in the `thread.started` event, and `-s` must come before `resume` | Yes, 0.154.0 |
| Claude | `claude -p --output-format json --permission-mode plan < prompt.md` | `.result`; cost in `.total_cost_usd` | `--permission-mode <mode> --resume <session_id>` | Flags checked in help |
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
| `agent-hook.mjs` | The messaging hook script, from `mcp/src/agentHook.ts` | `mcp/build.mjs` |
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
| MCP server for other agents | `~/.ide-agent-tabs/mcp/`, a copy of `mcp-server.mjs`, `agent-hook.mjs`, `launch/` and `THIRD_PARTY_NOTICES.txt` | Codex, Gemini CLI, Copilot CLI and OpenCode run this copy, because the plugin's own path changes with each version. The session start hook refreshes it when the bundled server changes and the folder exists. `version.json` records the plugin version it came from, and an older plugin never replaces a copy from a newer one, because every Claude Code install on the machine shares the copy. |

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

1. Runs `claude plugin marketplace update ide-agent-tabs`, reads the available version from the
   marketplace clone's `claude-plugin/.claude-plugin/plugin.json` and the installed one from
   `claude plugin list --json`, runs `sync-ides.mjs --status`, and reports the versions.
2. If a newer plugin version exists, runs `claude plugin update ide-agent-tabs@ide-agent-tabs`, then asks
   the user to run `/reload-plugins` and the skill again. The loaded skill's paths point to the old
   version's files.
3. Runs `sync-ides.mjs --hook` to bring each set-up IDE to the bundled versions.
4. Reports each part's old and new version, and the reload or restart each IDE needs.

An IDE that was never set up needs the setup skill, not the update skill.

`setup` and `update` set `disable-model-invocation: true`: they change the machine, so only the user
starts them, and their descriptions cost no context until then.

The build compiles against a local IDE when `studioPath` is set in `~/.gradle/gradle.properties`, and
downloads IntelliJ IDEA 2026.2.2 otherwise. The Plugin Verifier checks
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
- A message's text never reaches a command line or a terminal. The only line the server types into a
  session is the fixed wake line, built from the sender's cleaned agent name and id.
- Any process of your user can write to any mailbox, as it can open tabs. Every agent treats a message
  as a peer's request, applies its user's rules to it, and asks its user before anything destructive.
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
| Messaging | Two servers over stdio on Windows 11; the tmux wake-up with a stand-in agent in WSL Ubuntu; `--register codex` against Codex 0.157.1 in a temporary `CODEX_HOME`; the Codex tab arguments with headless `codex exec` 0.158.0 on Windows 11 in a temporary `CODEX_HOME`: the server starts, the `UserPromptSubmit`, `PostToolUse` and `Stop` hooks run trusted, a waiting message is read and answered, a `Stop` block, and the rename to `codex-<threadId>`; interactive Codex 0.158.0 tabs in Antigravity on Windows 11: a message read mid-task through the hooks, and an idle tab woken by the typed line through the `input` route, each answered | The `PermissionRequest` hook; the hook keys on macOS and Linux; wake-up in WezTerm, kitty, Ghostty and the IDEs; hooks inside a real Gemini CLI or Copilot CLI session |

Open, list and close through the MCP server pass for tmux, kitty and WezTerm. No part is tested on a real
Mac. For the headless delegation commands, see the **Tested** column in
[Headless commands](#headless-commands).

## Possible future work

None of these is scheduled.

- **Visual Studio extension:** the **New Agent Tab** button and editor tabs in Visual Studio on Windows.
  Until then, the MCP server opens tabs for Visual Studio users in Windows Terminal.
- **Terminal and iTerm2 on macOS:** terminal drivers through AppleScript.
- **Messaging hooks for OpenCode:** state and reminders through an OpenCode plugin. Without hooks, an
  OpenCode session's `state` stays `unknown`, so it gets no wake-up and sees a message only when it calls
  `read_messages` or `wait_for_message`.
- **Jev steps J3 to J5:** a routing bench, a guard hook and a cost report. See
  [jev-integration.md](jev-integration.md#phases).
