# IDE Agent Tabs design

IDE Agent Tabs opens AI coding-agent sessions (Claude Code, Codex, Gemini CLI, Copilot CLI and others) in
IDE editor tabs. A person opens them with one button. An agent opens, lists and closes them in any IDE
running on the same machine.

This document is the contract every part builds against. Change it before you change the protocol.

## Parts

| Part | Status | Location |
|---|---|---|
| JetBrains plugin | Built. Moves to `jetbrains/` when the second part lands. | Repository root |
| Protocol: registry and HTTP API | Phase 0 | This document |
| Claude Code plugin: MCP server, `/new-tab` skill, setup skill | Phase 1 | `claude-plugin/`, `mcp/` |
| VS Code extension | Phase 2 | `vscode/` |
| Visual Studio extension (Windows Terminal tabs first) | Phase 3 | `visualstudio/` |

Messaging between agents is out of scope. Claude Code sessions message each other with Claude Code's
own `ListAgents` and `SendMessage`. For other CLIs, see [Messaging](#messaging).

## Registry

Each IDE process writes one file to `~/.ide-agent-tabs/endpoints/`. A VS Code extension writes one file
per window. Name the file `<ide>-<pid>.json`, or `<ide>-<pid>-<window>.json` for VS Code.

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

Set the Java system property `ide.agent.tabs.home` (JetBrains) to use a folder other than
`~/.ide-agent-tabs`. The sandbox IDE uses this so it never mixes with real IDEs.

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

`open_tab` routing: use the IDE named by `ide`, as `<ide>-<pid>` from `list_ides`. Otherwise, use the IDE
with an open project that contains `path`, preferring the focused one. Otherwise, use the most recently
started IDE.

## Messaging

- Claude Code to Claude Code: Claude Code's native `ListAgents` and `SendMessage`. They work across tabs
  and IDEs on the same machine, but not between WSL and native Windows.
- Other CLIs: not decided. The first experiment is a shared MCP mailbox,
  [mcp_agent_mail](https://github.com/Dicklesworthstone/mcp_agent_mail), with Claude Code, Codex and
  Gemini CLI polling one mailbox.

## Install (Phase 1)

The repository is a Claude Code plugin marketplace. One set of commands sets up everything:

```sh
claude plugin marketplace add Alexk413x/ide-agent-tabs
claude plugin install ide-agent-tabs@ide-agent-tabs
```

Then, in a session, run `/ide-agent-tabs:setup`. The setup skill finds installed IDEs and installs each
extension from the private GitHub Releases. JetBrains IDEs must be closed for a command-line install.

## Security

- The token limits the API to processes that can read your registry files, which means your own user
  account.
- Any such process can start any agent with any flags, including flags that skip permission prompts.
  This is by design: it is the same power as running the agent yourself.
- The server never passes caller text through a shell parser.

## Phases

0. Registry, token, `info` and `agents` routes, and agent profiles in the JetBrains plugin.
   Right-click agent menu.
1. Claude Code plugin with the MCP server, `/new-tab` and setup skills, and GitHub Releases.
2. VS Code extension.
3. Visual Studio extension, opening Windows Terminal tabs.
