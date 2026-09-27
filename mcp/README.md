# IDE Agent Tabs MCP server

This MCP server lets an agent open, list and close agent tabs. A tab runs an interactive agent CLI
session, such as Claude Code, Codex, Gemini CLI or Copilot CLI. The tab opens in a running IDE that has
the IDE Agent Tabs extension, or in a terminal app when no IDE is running.

The server speaks MCP over stdio. It reads the registry and calls each IDE's HTTP API, as described in
[docs/design.md](../docs/design.md). The Claude Code plugin registers it as `ide-agent-tabs` in
[claude-plugin/.mcp.json](../claude-plugin/.mcp.json).

## Tools

| Tool | Input | Returns |
|---|---|---|
| `list_ides` | none | Running IDEs (`id`, `product`, `version`, `projects` with `focused`), and the terminals this machine supports with their capabilities |
| `list_agents` | none | Profiles (`name`, `label`, `command`, `installed`), the `default` agent, and any warnings about your config files |
| `list_tabs` | `ide` (optional) | Open tabs across all IDEs and terminals, or in one |
| `open_tab` | `path`, and optional `agent`, `prompt`, `args`, `env`, `ide` | The tab `id`, where it opened (`ide`), the `agent` and the `reason` for the route |
| `close_tab` | `id` (optional) | The closed tab. With no `id`, it closes the caller's own tab through `IDE_AGENT_TABS_ID`. |

An IDE's id is its registry file name without `.json`: `<ide>-<pid>`, or `<ide>-<pid>-<window>` for a VS
Code window. A terminal's id is its name: `windows-terminal` or `ghostty`.

When an IDE refuses a request, the tool result is an error that holds the IDE's HTTP status and JSON reply
unchanged.

### How `open_tab` picks a place

1. If you pass `ide`, the tab opens there.
2. Otherwise, the tab opens in the IDE with an open project that contains `path`. A focused project wins,
   then the deepest project, then the most recently started IDE.
3. Otherwise, the tab opens in the most recently started IDE that has a project open.
4. Otherwise, the tab opens in the terminal named by `"terminal"` in `config.json`.
5. Otherwise, the tab opens in the platform's default terminal: Windows Terminal on Windows, Ghostty on
   macOS.

The server skips an IDE that doesn't answer `info`, so an IDE stuck behind a modal dialog doesn't block the
route.

## Config files

All files live in `~/.ide-agent-tabs/`. Set `IDE_AGENT_TABS_HOME` to use another folder.

| File | Holds |
|---|---|
| `endpoints/*.json` | One registry entry per running IDE. The server skips entries with an unknown `protocol` or a URL that isn't on the loopback address, and deletes entries whose process has ended. |
| `agents.json` | Your own agent profiles. The rules match the JetBrains plugin exactly. |
| `config.json` | `defaultAgent`, and `terminal`, the preferred terminal when no IDE is running. |
| `terminal-tabs.json` | The terminal tabs this server opened. The server writes it; don't edit it. |
| `launch/` | Short-lived launch files. Each is deleted as soon as its tab starts. |

`list_agents` reads the profile files itself instead of asking an IDE. A terminal tab uses the same
profiles, so the answer holds whether or not an IDE is running. `installed` reflects the server's `PATH`,
which is the calling agent's `PATH`.

## Terminals

| Terminal | OS | Open | List | Close |
|---|---|---|---|---|
| Windows Terminal | Windows | Tab | Tabs this server opened, while their shell runs | Best effort |
| Ghostty 1.3 or later | macOS | Tab | Yes | Yes |

A terminal tab never receives caller text on a command line. The server writes the command, arguments,
prompt and environment variables to a launch file that only your user can read. The tab runs a fixed
launch script from `dist/launch/`, which reads the file, deletes it, sets `IDE_AGENT_TABS_ID` and
`IDE_AGENT_TABS_AGENT`, changes to `path`, and starts the agent with the prompt as the last argument.

### Windows Terminal

- The server runs `wt.exe -w 0 new-tab`, which opens the tab in the most recently used window. The tab
  starts `pwsh` if it's installed, or Windows PowerShell otherwise, with `agent-launch.ps1`.
- A new tab starts in a Windows Terminal process that is already running, so it doesn't inherit the
  server's environment. The launch file carries everything the tab needs.
- If Windows Terminal isn't running, `wt.exe` starts it with the server's environment, and every later
  tab in that window inherits it. The server drops the variables that identify the calling Claude Code
  session, such as `CLAUDECODE`, before it runs `wt.exe`.
- Windows Terminal has no API to list or close tabs. The launch script writes its process id to
  `launch/<id>.pid`. `list_tabs` reports a tab while that `pwsh` process runs, and `close_tab` ends the
  process and the agent under it. The tab can stay open and show an exit message, depending on your
  profile's `closeOnExit` setting.
- Paths that contain `;` can't be passed to `wt.exe`, because it splits its command line at every `;`.
  The server refuses to open a tab when its own launch script or launch file sits in such a path.

### Ghostty on macOS

- The server drives Ghostty through AppleScript, which Ghostty 1.3 added. The first call asks you to allow
  the calling app to control Ghostty, in **System Settings > Privacy & Security > Automation**.
- The tab's command is your login shell (bash, zsh or fish; zsh otherwise) with `-l -i -c`. It sources
  `agent-launch.sh` or `agent-launch.fish`, and then replaces itself with an interactive login shell, so
  the tab stays open after the agent exits.
- The environment variable names you pass in `env` must be shell identifiers.
- Untested: no Mac was available to build this. Unit tests cover the AppleScript and command generation.

### Adding a terminal

Each terminal is a `TerminalDriver` in `src/terminals/`, with `available`, `open`, `alive` and `close`.
Add the driver to `TERMINAL_DRIVERS` in `src/terminals/index.ts`. WezTerm, kitty and tmux fit this shape
through their command-line interfaces. Linux has no default terminal yet.

## Build and test

You need Node.js 20 or later.

```sh
npm install
npm test
npm run build
```

`npm run build` type-checks the code and bundles it into `claude-plugin/dist/mcp-server.mjs`, with the
launch scripts in `claude-plugin/dist/launch/` and the bundled packages' licenses in
`claude-plugin/dist/THIRD_PARTY_NOTICES.txt`. Commit the `dist/` folder: the plugin runs it without
`node_modules`.

The launcher tests run the real launch scripts with `pwsh`, Windows PowerShell and bash when they're
installed, and skip the rest.
