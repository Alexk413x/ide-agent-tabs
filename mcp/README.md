# Agent Tabs MCP server

This MCP server lets an agent open, list and close agent tabs. A tab runs an interactive agent CLI
session, such as Claude Code, Codex, Gemini CLI or Copilot CLI. The tab opens in a running IDE that has
the Agent Tabs extension, or in a terminal app when no IDE is running.

The server speaks MCP over stdio. It reads the registry and calls each IDE's HTTP API, as described in
[docs/design.md](../docs/design.md). The Claude Code plugin registers it as `ide-agent-tabs` in
[claude-plugin/.mcp.json](../claude-plugin/.mcp.json). Other agent CLIs can use it too; see
[Other agents](#other-agents).

## Tools

| Tool | Input | Returns |
|---|---|---|
| `list_ides` | none | Running IDEs (`id`, `product`, `version`, `projects` with `focused`), and the terminals this machine supports with their capabilities |
| `list_agents` | none | Profiles (`name`, `label`, `command`, `installed`), the `default` agent, and any warnings about your config files |
| `list_tabs` | `ide` (optional) | Open tabs across all IDEs and terminals, or in one |
| `open_tab` | `path`, and optional `agent`, `prompt`, `args`, `env`, `ide` | The tab `id`, where it opened (`ide`), the `agent`, the `reason` for the route, and a `note` when you need to act, such as attaching to tmux |
| `close_tab` | `id` (optional) | The closed tab. With no `id`, it closes the caller's own tab through `IDE_AGENT_TABS_ID`. |

An IDE's id is its registry file name without `.json`: `<ide>-<pid>`, or `<ide>-<pid>-<window>` for a VS
Code window. A terminal's id is its name: `windows-terminal`, `ghostty`, `kitty`, `wezterm` or `tmux`.

When an IDE refuses a request, the tool result is an error that holds the IDE's HTTP status and JSON reply
unchanged.

### How `open_tab` picks a place

1. If you pass `ide`, the tab opens there.
2. Otherwise, the tab opens in the IDE with an open project that contains `path`. A focused project wins,
   then the deepest project, then the most recently started IDE.
3. Otherwise, the tab opens in the most recently started IDE that has a project open.
4. Otherwise, the tab opens in the terminal named by `"terminal"` in `config.json`.
5. Otherwise, the tab opens in the first installed terminal in the platform's order: Windows Terminal,
   then WezTerm on Windows; Ghostty, kitty, WezTerm, then tmux on macOS and Linux.

The server skips an IDE that doesn't answer `info`, so an IDE stuck behind a modal dialog doesn't block the
route.

### Jev tools

With `"jev": {"enabled": true}` in `config.json`, the server also lists tools that ask TypeSafe's Jev
model for a typed judgment, and sends clients instructions on when to use them. With Jev off, the
server lists none of them and makes no network call. Everything a caller puts in a Jev request goes to
TypeSafe's API.

| Tool | Input | Returns |
|---|---|---|
| `jev_status` | none | Where the key came from (`env`, `credential-store` or `missing`), the last model seen, and today's calls, input tokens and estimated cost |
| `jev_ask` | `state`, `questions` in the API's form | `model`, `answers`, `usage`, `cost_usd` |
| `jev_choose` | `instruction`, `options`, and optional `state`, `no_match` | `choice`, `probabilities`, `confidence`, `band` (`sure`, `unsure` or `no-match`), `runner_up` |
| `jev_check` | `state`, `conditions` | The probability of yes for each condition |
| `jev_rank` | `query`, `items`, and optional `top` | The items by relevance, each with its probability |
| `jev_route` | `task` | The tier from `jev.tiers` whose agent is installed, the runner-up, the probabilities and `band` |

The server reads the API key from `TYPESAFE_API_KEY`, or from the operating system's credential store
under service `typesafe`, account `api_key`. Each call appends a line without request contents to
`jev/ledger.jsonl`. `node mcp-server.mjs jev <status|ask|choose|check|rank|route>` takes the same
request as JSON on stdin. See [Jev judgments](../docs/design.md#jev-judgments-phase-4-optional) for
the settings, the key lookup and the ledger.

## Config files

All files live in `~/.ide-agent-tabs/`. Set `IDE_AGENT_TABS_HOME` to use another folder.

| File | Holds |
|---|---|
| `endpoints/*.json` | One registry entry per running IDE. The server skips entries with an unknown `protocol` or a URL that isn't on the loopback address, and deletes entries whose process has ended. |
| `agents.json` | Your own agent profiles. The rules match the JetBrains plugin exactly. |
| `config.json` | `defaultAgent`; `terminal`, the preferred terminal when no IDE is running; and `jev`, the Jev settings. |
| `jev/ledger.jsonl` | One line per Jev call: time, tool, agent, tab, model, question count, input tokens and result. |
| `terminal-tabs.json` | The terminal tabs this server opened. The server writes it; don't edit it. |
| `launch/` | Short-lived launch files. Each is deleted as soon as its tab starts. |
| `mcp/` | A copy of the server for other agent CLIs. See [Other agents](#other-agents). |

`list_agents` reads the profile files itself instead of asking an IDE. A terminal tab uses the same
profiles, so the answer holds whether or not an IDE is running. `installed` reflects the server's `PATH`,
which is the calling agent's `PATH`.

## Other agents

Codex, Gemini CLI, Copilot CLI and OpenCode can run this server too. The setup skill registers it with
the agents you choose, through `sync-ides.mjs`:

```sh
node dist/sync-ides.mjs --agents
node dist/sync-ides.mjs --register codex gemini copilot opencode
node dist/sync-ides.mjs --unregister codex
```

`--agents` reports, for each agent, whether it's installed, whether it's registered, the server path it
runs, and whether that path is the stable copy. `--register` and `--unregister` print the same fields for
each agent, with `ok` or an `error`.

Each agent runs `node ~/.ide-agent-tabs/mcp/mcp-server.mjs`, with the server name `ide-agent-tabs`:

| Agent | Where the entry goes | How |
|---|---|---|
| Codex | `~/.codex/config.toml`, or `$CODEX_HOME/config.toml` | `codex mcp add ide-agent-tabs -- node <path>` |
| Gemini CLI | `~/.gemini/settings.json`, user scope | `gemini mcp add --scope user ide-agent-tabs node <path>` |
| Copilot CLI | `mcpServers` in `~/.copilot/mcp-config.json`, or `$COPILOT_HOME/mcp-config.json` | The script edits the file. |
| OpenCode | `mcp` in `~/.config/opencode/opencode.json`, or under `$XDG_CONFIG_HOME` | The script edits the file. |

- `~/.ide-agent-tabs/mcp/` holds `mcp-server.mjs`, `launch/` and `THIRD_PARTY_NOTICES.txt`, in the same
  layout as the plugin's `dist/`. The Claude Code plugin folder has the version in its path, so an
  update would break a registration that pointed there. `--register` writes the copy. When a Claude Code
  session starts after a plugin update, the session start hook refreshes the copy if it exists.
- The script writes each file to a temporary name and renames it, so a running server never reads a
  half-written file. If a file is in use, the hook logs the error to `sync.log` and tries again at the
  next session.
- The script runs the agent CLIs from `~/.ide-agent-tabs`, so a project config in the current folder
  doesn't apply. It checks each registration by reading it back, not by the exit code.
- The script keeps the other keys in a Copilot CLI or OpenCode config file, and its indentation. It
  doesn't change a file that isn't plain JSON, such as a file with comments or an `opencode.jsonc`
  with comments. It reports an error instead, and you add the entry by hand:

  ```json
  "ide-agent-tabs": { "type": "local", "command": "node", "args": ["<path>"], "env": {}, "tools": ["*"] }
  ```

  for Copilot CLI under `mcpServers`, or for OpenCode under `mcp`:

  ```json
  "ide-agent-tabs": { "type": "local", "command": ["node", "<path>"], "enabled": true }
  ```

- Claude Code isn't registered this way. It gets the server from the plugin.
- After you register an agent, restart its open sessions.

To remove Agent Tabs from the other agents, run `--unregister` with each agent, then delete
`~/.ide-agent-tabs/mcp/`.

## Terminals

| Terminal | OS | Open | List | Close |
|---|---|---|---|---|
| Windows Terminal | Windows | Tab | Tabs this server opened, while their shell runs | Best effort |
| Ghostty 1.3 or later | macOS | Tab | Yes | Yes |
| Ghostty | Linux | New window | Windows this server opened, while their shell runs | Best effort |
| WezTerm | Windows, macOS, Linux | Tab | Yes | Yes |
| kitty, remote control on | macOS, Linux | Tab | Yes | Yes |
| kitty, remote control off | macOS, Linux | New window | Windows this server opened, while their shell runs | Best effort |
| tmux 3.0 or later | macOS, Linux | Tab (a tmux window) | Yes | Yes |

A terminal tab never receives caller text on a command line. The server writes the command, arguments,
prompt and environment variables to a launch file that only your user can read. The tab runs a fixed
launch script from `dist/launch/`, which reads the file, deletes it, sets `IDE_AGENT_TABS_ID` and
`IDE_AGENT_TABS_AGENT`, changes to `path`, and starts the agent with the prompt as the last argument.
A command line holds only fixed flags, the server's own paths, `path` and a cleaned tab title. The server
refuses a path that holds a control character.

On macOS and Linux, the tab runs your login shell (bash, zsh or fish; otherwise zsh on macOS and bash on
Linux) with `-l -i -c`. It sources `agent-launch.sh` or `agent-launch.fish`, and then replaces itself
with an interactive login shell, so the tab stays open after the agent exits. The environment variable
names you pass in `env` must be shell identifiers.

A terminal that the server starts, such as a first Windows Terminal window, a new Ghostty or kitty
process, a WezTerm GUI or a tmux server, gets the server's environment without the variables that
identify the calling agent session, such as `CLAUDECODE` or `CODEX_SANDBOX`.

### Windows Terminal

- The server runs `wt.exe -w 0 new-tab`, which opens the tab in the most recently used window. The tab
  starts `pwsh` if it's installed, or Windows PowerShell otherwise, with `agent-launch.ps1`.
- A new tab starts in a Windows Terminal process that is already running, so it doesn't inherit the
  server's environment. The launch file carries everything the tab needs.
- If Windows Terminal isn't running, `wt.exe` starts it with the server's environment, and every later
  tab in that window inherits it. The server drops the variables that identify the calling agent
  session, such as `CLAUDECODE` or `CODEX_SANDBOX`, before it runs `wt.exe`.
- Windows Terminal has no API to list or close tabs. The launch script writes its process id to
  `launch/<id>.pid`. `list_tabs` reports a tab while that `pwsh` process runs, and `close_tab` ends the
  process and the agent under it. The tab can stay open and show an exit message, depending on your
  profile's `closeOnExit` setting.
- Paths that contain `;` can't be passed to `wt.exe`, because it splits its command line at every `;`.
  The server refuses to open a tab when its own launch script or launch file sits in such a path.

### Ghostty on macOS

- The server drives Ghostty through AppleScript, which Ghostty 1.3 added. The first call asks you to allow
  the calling app to control Ghostty, in **System Settings > Privacy & Security > Automation**.

### Ghostty on Linux

- Ghostty on Linux can't open a tab in a running instance from outside
  ([ghostty#12136](https://github.com/ghostty-org/ghostty/issues/12136)). Each agent gets a new Ghostty
  process with one window, started with `--gtk-single-instance=false`.
- The launch script writes its shell's process id to `launch/<id>.pid`. `list_tabs` reports the window
  while that shell runs, and `close_tab` sends the shell `SIGHUP`, which ends the agent and closes the
  window.
- The server finds Ghostty as `ghostty` on `PATH`.

### WezTerm

- The server runs `wezterm cli spawn`, which opens a tab in the most recently used WezTerm window and
  prints its pane id. `list_tabs` reads `wezterm cli list`, and `close_tab` runs `wezterm cli kill-pane`.
- Each WezTerm GUI listens on a socket named `gui-sock-<pid>`. The server picks the newest one whose GUI
  still runs and passes its full path in `WEZTERM_UNIX_SOCKET`, because on Windows `wezterm cli` can't
  find the GUI by itself ([wezterm#4456](https://github.com/wezterm/wezterm/issues/4456)). Every call
  uses `--no-auto-start`, so `wezterm cli` never starts a hidden mux server.
- The server records the socket with each pane id, so a restarted GUI doesn't match old pane ids.
- A new pane gets the WezTerm GUI's environment, not the server's, so the launch script and launch file
  paths are arguments of the shell. With fish, this needs fish 3.2 or later. On Windows, the pane runs
  `pwsh` or Windows PowerShell with `agent-launch.ps1`, as in Windows Terminal.
- If no WezTerm GUI runs, the server starts one with `wezterm start` and records the pane id once the new
  GUI's socket answers. If that takes more than 10 seconds, `list_tabs` shows the tab for 60 seconds and
  `close_tab` can't close it.
- The most recent stable WezTerm release is 20240203. The server uses only commands that release has.
- The server finds `wezterm` on `PATH`, then in `%ProgramFiles%\WezTerm` on Windows or
  `/Applications/WezTerm.app` and `~/Applications/WezTerm.app` on macOS.

### kitty

- kitty needs remote control to open tabs. Add these lines to `kitty.conf` and restart kitty:

  | OS | Lines |
  |---|---|
  | Linux | `allow_remote_control socket-only` and `listen_on unix:${XDG_RUNTIME_DIR}/kitty-agent-tabs` |
  | macOS | `allow_remote_control socket-only` and `listen_on unix:${TMPDIR}/kitty-agent-tabs` |

- `socket-only` accepts commands only through that socket, which only your user can reach. kitty adds
  `-<pid>` to the socket name. The server uses `KITTY_LISTEN_ON` if it's set, and otherwise the newest
  `kitty-agent-tabs-*` socket that answers.
- With remote control, the server runs `kitten @ launch --type=tab`, `kitten @ ls` and
  `kitten @ close-window`.
- Without it, each agent gets a new kitty process with one window, tracked like Ghostty on Linux.
  `list_ides` reports the capabilities of the mode a new tab would use.
- The server finds `kitty` on `PATH`, then in `/Applications/kitty.app`, `~/Applications/kitty.app` and
  `~/.local/kitty.app/bin`. It uses the `kitten` next to it.

### tmux

- The server uses your default tmux server. It opens a new window in the session that has a client
  attached and was attached most recently, and makes that window current.
- If no client is attached, or no tmux server runs, the server opens the window in a detached session
  named `agents`, and `open_tab` returns a `note`: run `tmux attach -t agents`.
- `list_tabs` reads `tmux list-windows -a`, and `close_tab` runs `tmux kill-window`. The server records
  the tmux socket and server process id with each window id, so a restarted tmux server doesn't match old
  ids.
- tmux reads `#` in a window name as a format and an argument that ends in `;` as a command separator,
  so the tab title drops both. tmux also expands formats in `-c`, so the server doesn't pass the folder
  there; the launch script changes to it.
- You need tmux 3.0 or later, for `new-window -e`. The server finds `tmux` on `PATH`, then in
  `/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin` and `/home/linuxbrew/.linuxbrew/bin`.

### Adding a terminal

Each terminal is a `TerminalDriver` in `src/terminals/`, with `available`, `open`, `alive` and `close`,
and `currentCapabilities` when what it can do depends on its setup. Add the driver to
`TERMINAL_DRIVERS`, and to the platform order in `src/terminals/index.ts`. Shared pieces live in
`shell.ts` (login shell, env mode and argv mode commands, path checks, titles) and `processes.ts`
(environment, detached start, pid tracking).

### Tested on

- WezTerm: the 20260917 nightly on Windows 11, and the 20260802 nightly in WSL Ubuntu, with and without a
  GUI already running.
- kitty: 0.49.1 in WSL Ubuntu, with and without remote control.
- tmux: 3.6 with bash in WSL Ubuntu, with and without an attached client.
- Ghostty on Linux: partly. In WSL Ubuntu, Ghostty 1.3.1 accepts the flags, but it needs OpenGL 4.3 and
  WSLg offers 4.1, so no window opens. The same start, pid and `SIGHUP` path passes with kitty.
- Ghostty on macOS: untested. Unit tests cover the AppleScript and command generation.
- No driver is tested on a real Mac.

## Build and test

You need Node.js 20 or later.

```sh
npm install
npm test
npm run build
```

`npm run build` type-checks the code and bundles it into `claude-plugin/dist/mcp-server.mjs` and
`claude-plugin/dist/sync-ides.mjs`. It copies the launch scripts to `claude-plugin/dist/launch/` and
writes the bundled packages' licenses to `claude-plugin/dist/THIRD_PARTY_NOTICES.txt`. It leaves the IDE
builds in `claude-plugin/dist/ide/` in place. Commit the `dist/` folder: the plugin runs it without
`node_modules`.

The launcher tests run the real launch scripts with `pwsh`, Windows PowerShell and bash when they're
installed, and skip the rest.
