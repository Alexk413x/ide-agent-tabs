# Agent Tabs for VS Code

Agent Tabs opens AI coding-agent sessions, such as Claude Code, Codex, Gemini CLI and Copilot CLI, in
editor tabs. You open a tab with one button. Another agent opens, lists and closes tabs through a local
HTTP API, the same API the JetBrains plugin serves.

The extension runs in VS Code 1.100 or later, and in editors built on it, such as Antigravity.

## Install

Build the `.vsix` file, or download it from a release, then install it from the command line:

```sh
code --install-extension ide-agent-tabs-0.1.0.vsix
antigravity-ide --install-extension ide-agent-tabs-0.1.0.vsix
```

To build it yourself, you need Node.js 20 or later:

```sh
npm ci
npm test
npm run package
```

## Open a tab

- When a window opens a project with a `.claude` folder, the default agent opens in an editor tab.
- Click **New Agent Tab** in the editor title bar or the agent name at the right of the status bar, or press
  **Ctrl+Alt+A** (**⌘⌥A** on macOS). The default agent
  opens in a terminal tab in the editor area. The tab starts in the folder of the active editor, or in the
  first workspace folder.
- To open a different agent, click the arrow next to **New Agent Tab** and pick one from the menu. The
  menu lists each agent whose command is on your `PATH`. Custom agents from `agents.json` are under
  **New Agent Tab With…**, which the Command Palette also has.
- Hover over the status bar item to see a link, with its logo, for each installed agent. Click one to open it. The
  tooltip also names supported agents that aren't installed.
- The arrow menu and the status bar tooltip both have a **Settings** link to this extension's settings.

Opening an agent from a menu or a link doesn't change the default agent. To change it, use the
`ideAgentTabs.defaultAgent` setting.

## Settings

| Setting | Values | Default |
| --- | --- | --- |
| `ideAgentTabs.defaultAgent` | `claude`, `codex`, `gemini`, `copilot` | `claude` |
| `ideAgentTabs.openOnStartup` | `claudeFolder` (the project has a `.claude` folder), `always`, `never` | `claudeFolder` |

The editor title button, the status bar item and startup all use `ideAgentTabs.defaultAgent`. The
setting stays in sync with `defaultAgent` in `~/.ide-agent-tabs/config.json`, so a change in the JetBrains
plugin or any other window shows up here too, and the MCP server uses the same default.

## Agent profiles

The built-in profiles are `claude`, `codex`, `gemini` and `copilot`. To add a profile or change a built-in
one, edit `~/.ide-agent-tabs/agents.json`:

```json
{
  "opencode-local": {
    "label": "OpenCode (LM Studio)",
    "command": "opencode",
    "args": ["--model", "lmstudio/qwen3-coder"],
    "promptFlag": "--prompt",
    "icon": "icons/opencode.svg"
  }
}
```

A relative `icon` path starts from `~/.ide-agent-tabs`. The extension reads both files again when they
change.

## API for agents

Each VS Code window runs its own server on `127.0.0.1` and writes a registry file to
`~/.ide-agent-tabs/endpoints/vscode-<pid>-<window>.json`. The file holds the server URL and a token. The
window deletes its file when it closes.

The routes are `info`, `agents`, `open`, `close` and `list`. The
[design document](https://github.com/Alexk413x/ide-agent-tabs/blob/main/docs/design.md) describes the
registry, each route, the status codes and the security rules.

- `open` opens the tab in the window that serves the request. The caller picks the window from the
  registry, for example by the folders that `info` lists.
- A tab opened through the API doesn't take focus. A tab opened from the button does.
- `info` reports every workspace folder in the window. Each folder's `focused` value is `true` when the
  window has focus.

Set the `IDE_AGENT_TABS_HOME` environment variable before you start the editor to use a folder other than
`~/.ide-agent-tabs`.

## How a tab starts the agent

The extension never builds a shell command from caller text. It passes the command, arguments and prompt
in environment variables, and a fixed launch script in the extension reads them:

- Windows: PowerShell 7 (`pwsh`) runs `resources/launch/agent.ps1`, or Windows PowerShell 5.1 when
  `pwsh` isn't on `PATH`.
- macOS and Linux: your login shell from `$SHELL`. bash and zsh source `agent.sh`, then start a new
  interactive shell. fish sources `agent.fish`. Any other shell falls back to bash.

When the agent exits, the tab stays open at a shell prompt. `IDE_AGENT_TABS_ID` stays set, so the session
can close its own tab.

## Limits

- A prompt holds up to 30,000 characters. `args` and `env` hold up to 64 entries each.
- On Windows, a single environment variable holds up to 32,767 characters, so very long `args` lists can
  fail to start.
- Agent tabs don't survive a window reload. The tab closes, and its id stops working.
- Windows PowerShell 5.1 drops empty arguments and can split arguments that contain double quotes.
  Install PowerShell 7 to avoid this.
- Tested on Windows. The bash launcher is tested through WSL. zsh, fish, macOS and remote workspaces
  are untested.
