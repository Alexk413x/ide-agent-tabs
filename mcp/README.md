# Agent Tabs MCP server

This MCP server lets an agent open, list and close agent tabs, and message other agent sessions. A tab
runs an interactive agent CLI session, such as Claude Code, Codex, Antigravity CLI, Copilot CLI, Gemini CLI, Grok Build,
Pi, Hermes, OpenCode, Qwen Code or Goose. The tab opens in a running IDE that has the Agent Tabs extension, or in a terminal app when no IDE is running.

The server speaks MCP over stdio. It reads the registry and calls each IDE's HTTP API, as described in
[docs/design.md](../docs/design.md). The Claude Code plugin registers it as `ide-agent-tabs` in
[claude-plugin/.mcp.json](../claude-plugin/.mcp.json). Other agent CLIs can use it too; see
[Other agents](#other-agents).

## Tools

| Tool | Input | Returns |
|---|---|---|
| `list_ides` | none | Running IDEs (`id`, `product`, `version`, `projects` with `focused`), the terminals this machine supports with their capabilities, and `shells`: the PowerShell installs a Windows terminal tab can use |
| `list_agents` | none | Profiles (`name`, `label`, `command`, `installed`, `model`: `true` when the profile takes a model, and `ori: true` when Ori can launch it), the `default` agent, `launchVia`, and any warnings about your config files |
| `list_tabs` | `ide` (optional) | Open tabs across all IDEs and terminals, or in one |
| `open_tab` | `path`, and optional `agent`, `prompt`, `args`, `env`, `ide`, `model`, `via`, `focus` | The tab `id`, where it opened (`ide`), the `agent`, the `reason` for the route, `via: "ori"` when the tab started through Ori, and a `note` when you need to act, such as attaching to tmux |
| `close_tab` | `id` (optional) | The closed tab. With no `id`, it closes the caller's own tab through `IDE_AGENT_TABS_ID`. |
| `list_sessions` | none | Live agent sessions in a fixed agent order: `name` (what Claude Code's `SendMessage` takes: a Claude session's native name, else `shortName`), `shortName` (such as `codex-f99f`, which `send_message` also takes), `id`, `session` (the first 8 characters of `id`), `agent`, `harness` (the agent CLI, with ` via OpenRouter` for an Ori launch), `model` and `effort` (`null` when unknown), `route` (`native` or `agent-tabs`), `state`, `tab`, `where` (the IDE product or terminal app, from the live endpoint or the label stored when the tab opened; `null` when neither is known, never a raw host id), `host` (IDE and project, or terminal), `ide` (the host's id), `path` and `folder`, `nativeName` for a Claude session that has one, `via` when known and `startedAt`, with `handedOffTo` for a session that handed its work to another, and `self` for the caller |
| `send_message` | `to` (an `id` or `shortName`), `text`, and optional `replyTo` | The message `id`, and `delivery`: `woken` or `queued`, with a `note` when the recipient's Claude Code mod delivers it |
| `read_messages` | none | The caller's unread messages, marked read, under a `notice` that they come from other agents |
| `wait_for_message` | optional `timeout` (seconds, default 60, at most 600, or 170 in an Antigravity CLI session), `from`, `replyTo` | The first matching message, marked read, or `message: null` on timeout |
| `handoff` | `path`, and `brief` or `goal`, `done`, `next`, `files`, `openQuestions`, and optional `agent`, `model`, `via`, `ide`, `focus` | The handoff `id`, the `brief` path, the `newTab` id, and `next`: the steps the caller follows to wait for the takeover and stop |
| `closed_sessions` | none | The sessions that ended in the last 7 days, newest first: `listing`, grouped by folder with one aligned line each (NAME, AGENT, ENDED, SIZE, MODEL, WHERE, ID), and `sessions` with the full `id`, `folder`, `tokens`, `size`, `model`, `where`, `preview` and `resumable` |
| `resume_tab` | `id`, and optional `ide`, `model`, `focus`, `confirm` | `resumed: true` with the new `tab`, `ide`, `product`, `size`, `age` and `cost`, or `resumed: false` and `needsConfirm: true` with `size`, `age`, `reasons` and a `message`. See [Resume](#resume). |

An IDE's id is its registry file name without `.json`: `<ide>-<pid>`, or `<ide>-<pid>-<window>` for a VS
Code window. A terminal's id is its name: `windows-terminal`, `ghostty`, `iterm2`, `kitty`, `wezterm` or
`tmux`.

When an IDE refuses a request, the tool result is an error that holds the IDE's HTTP status and JSON reply
unchanged.

### Model and Ori

`open_tab` takes two optional fields that change how the agent starts.

- `model` picks the model. The server passes it with the profile's `modelFlag`:

  | Agent | Flag |
  |---|---|
  | `claude` | `--model` |
  | `codex` | `-m` |
  | `agy` | `--model` |
  | `copilot` | `--model` |
  | `gemini` | `-m` |
  | `grok` | `-m` |
  | `pi` | `--model` |
  | `hermes` | `-m` |
  | `opencode` | `-m` |
  | `qwen` | `-m` |
  | `goose` | `--model` |
  | `codex-local` | `-m` |

  A custom profile sets `modelFlag` in `agents.json`. `list_agents` reports `model: true` for a profile
  that has one. A `model` for a profile without a `modelFlag` is an error, never ignored:
  `<agent> has no model option; open it without model, or set modelFlag for it in agents.json`. A model
  id holds 1 to 200 letters, digits and `. _ : / @ + -`.
- `via` is `ori` or `direct`. With `ori`, the tab starts the agent with `ori <agent>`, and the model
  usage is billed through OpenRouter. The result then carries `via: "ori"`. With `model` and `ori`, the
  server passes `--model <model>` to Ori, which takes an OpenRouter model id and replaces the agent's
  own model flag.

The `launchVia` setting in `config.json` (**Launch through OpenRouter (Ori)**) sets `via` for every tab
that doesn't pass one. It is `direct` by default. The VS Code and JetBrains settings show it only when
Ori is detected. An explicit `via` beats the setting.

Ori launches only these agents, and only the ones Ori lists as installed: `claude`, `codex`, `grok`,
`hermes`, `opencode`, `pi` and `prime-agent`. Of the built-in profiles, that leaves `claude`, `codex`,
`grok`, `hermes`, `opencode` and `pi`; Qwen Code, Goose and Codex (local) always start directly, and
`prime-agent` has no profile. `list_agents` marks each one with `ori: true`. When the
setting is `ori` and Ori can't launch the agent, the tab starts directly. When `via: "ori"` is explicit
and Ori can't launch the agent, the call fails with `<agent> can't launch through Ori: <reason>`.

The agent keeps its own identity. `IDE_AGENT_TABS_AGENT` stays `claude` or `codex`, so hooks, wake-ups
and messaging follow that agent's rules.

Ori 0.14.3 on Windows, checked 2026-10-03:

- Ori starts the agent as a child process, and the environment passes through. `IDE_AGENT_TABS_ID`
  reached the Codex MCP server.
- `ori claude` fails here with `401 Missing Authentication header`. Ori's per-launch Claude settings set
  `ANTHROPIC_AUTH_TOKEN` to an empty string. This is an Ori issue, not an Agent Tabs issue.
- Ori refuses an argument that holds `"`, `%`, `^`, `&`, `|`, `<` or `>` when the agent is a `.cmd` shim.
  Codex tab arguments hold Codex's own `<session-flags>` keys, so Codex tabs through Ori don't work
  with an npm (`.cmd`) Codex on Windows. The setting falls back to a direct launch, and an explicit
  `via: "ori"` returns a clear error.

### Focus

`focus: true` brings the new tab to the front, and `focus: false` opens it behind the current one where
the host allows. The `focusNewTabs` setting decides how: `"auto"` (the default) follows the call's `focus`
and opens behind without one, `"always"` always brings the tab to the front, and `"never"` never does. The
server can't tell whether the user or an agent asked for a tab, so the `new-tab` skill passes
`focus: true` when the user asked for the tab, and the `handoff` skill only when the user asks to watch
the new tab. The New Agent Tab button in an IDE always brings its tab to the front.

| Host | With `focus: false` |
|---|---|
| VS Code | Keyboard focus stays in the current editor; the new tab still shows in the active editor group. |
| JetBrains IDEs | Keyboard focus stays where it was; the new tab becomes the selected editor tab. |
| kitty with remote control | `launch --keep-focus`. |
| tmux | `new-window -d`. |
| iTerm2, Ghostty on macOS | The previous tab of that window is selected again. A new window takes focus. |
| Windows Terminal, WezTerm, kitty without remote control, Ghostty on Linux | No effect: the new tab or window comes to the front. |

### How `open_tab` picks a place

1. If you pass `ide`, the tab opens there.
2. With `"tabRouting": "caller"` in `config.json`, the tab opens where the caller runs: in the caller's
   own IDE, even when another IDE has the project open, or in a new tab of the caller's terminal window
   when the caller runs in an Agent Tabs terminal tab. A caller outside an Agent Tabs tab goes on to step 3.
3. Otherwise, the tab opens in the IDE with an open project that contains `path`. The deepest project
   wins; on a tie, the caller's own IDE, then the focused window, then the most recently started IDE.
4. Otherwise, the tab opens in the caller's own IDE, when the caller runs in an Agent Tabs tab of a running
   IDE.
5. Otherwise, the tab opens in the most recently started IDE that has a project open.
6. Otherwise, the tab opens in the terminal named by `"terminal"` in `config.json`.
7. Otherwise, the tab opens in the first installed terminal in the platform's order: Windows Terminal,
   then WezTerm on Windows; Ghostty, iTerm2, kitty, WezTerm, then tmux on macOS; Ghostty, kitty,
   WezTerm, then tmux on Linux.

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
request as JSON on stdin. See [Jev judgments](../docs/design.md#jev-judgments-optional) for
the settings, the key lookup and the ledger.

## Agent support

The built-in agents, in the order every list uses: Claude Code, Codex, Antigravity CLI, Copilot CLI,
Gemini CLI, Grok Build, Pi, Hermes, then the agents built for local models: OpenCode, Qwen Code, Goose and
Codex (local). Custom profiles come last.

| Agent | Tabs | Messaging | State | Model | Via Ori | Icon source | Live-tested |
|---|---|---|---|---|---|---|---|
| Claude Code | Yes | Yes, with the plugin's hooks | `idle`, `busy`, `permission`; input idle signal | `--model` | Yes | Bundled in an earlier release | Yes |
| Codex | Yes | Yes in tabs, which bring their own server and hooks. `--register codex` covers sessions outside tabs on macOS and Linux. | `idle`, `busy`, `permission`; `Interrupt` | `-m` | Yes | Bundled in an earlier release | Yes |
| Antigravity CLI | Yes | Yes, with `--register agy` | `idle`, `busy`; no `permission` | `--model` | No | Bundled in an earlier release | Yes, headless; the interactive tab is partly untested |
| Copilot CLI | Yes | Yes, with `--register copilot` | `idle`, `busy`, `permission`, from docs | `--model` | No | Bundled in an earlier release | Partly: state comes from docs |
| Gemini CLI | Yes | Yes, with `--register gemini` | `idle`, `busy`, `permission` | `-m` | No | Bundled in an earlier release | As in [Tested on](../docs/design.md#tested-on) |
| Grok Build | Yes | Yes, with `--register grok` | `idle`, `busy`, `permission`; input idle signal (`idle_prompt`) | `-m` | Yes | Official SpaceXAI PNGs, embedded unchanged | No |
| Pi | Yes | Tools only, with `--register pi` | None | `--model` | Yes | `pi.dev/favicon.svg` | No |
| Hermes | Yes | Yes, with `--register hermes` | `idle`, `busy`, `permission`; nudges only after code edits | `-m` | Yes | Official `icon-master` (`.svg` and `-dark`) | No |
| OpenCode | Yes | Tools only, with `--register opencode` | None | `-m` | Yes | Official light and dark square SVGs | No |
| Qwen Code | Yes | Yes, with `--register qwen` | `idle`, `busy`, `permission` | `-m` | No | Official #6D44E8 logo, both themes | No |
| Goose | Yes | Yes, with `--register goose` | `idle`, `busy`; no `permission` | `--model` | No | Official `goose.svg`, #101010 in both themes; hard to see on a dark theme | No |
| Codex (local) | Yes | Yes, as for Codex. No registration. | As for Codex | `-m` | No | Reuses the Codex icon | No |

"Tools only" means the session can list IDEs, open and close tabs and call the messaging tools, but it
reports no state, so no wake-up types into it and no reminder reaches it. It reads a message only when it
calls `read_messages` or `wait_for_message`.

The seven agents after Gemini CLI are built from each CLI's documentation, and none is installed on the
development machine, so none is live-tested. Treat their profile flags, registration files and hook
behavior as unverified until a live run confirms them. Their profiles:

| Agent | Command | First prompt |
|---|---|---|
| `grok` | `grok` | positional |
| `pi` | `pi` | positional |
| `hermes` | `hermes chat` | `-q <prompt>` |
| `opencode` | `opencode` | `--prompt <prompt>` |
| `qwen` | `qwen` | `-i <prompt>`; a positional prompt would make Qwen Code answer once and exit |
| `goose` | `goose run -s`, or `goose session` with no prompt | `-t <prompt>`; `goose run -s` refuses to start without a message |
| `codex-local` | `codex`, the `codex` profile's fixed `args`, then `--oss --local-provider ollama` | positional |

Codex (local) needs Ollama 0.13.4 or later, and takes the local model with `-m`. Without
`--local-provider`, `--oss` stops at a picker between LM Studio and Ollama.

### Not included

- **Crush:** its hooks cover only `PreToolUse`, so it can't report when a turn ends. It has no
  first-prompt flag, and its licence is FSL.
- **Prime Agent:** it runs its sessions in a daemon, and its documentation isn't researched yet. Ori can
  launch `prime-agent`, but there is no profile for it.

## Handoff

`handoff` moves a session's work to a new agent tab and stops the old session. The `handoff` skill
drives it.

1. The old session calls `handoff` with `path` and a brief: either `brief` as Markdown, or `goal`,
   `done`, `next`, `files` and `openQuestions`. It can also pass `agent`, `model`, `via` and `ide`, as for
   `open_tab`.
2. The server writes the brief to `~/.ide-agent-tabs/handoffs/<id>.md` and opens the new tab. The tab's
   first prompt names the handoff, the old session and the brief path. If the tab fails to open,
   nothing closes and the old session keeps the work.
3. The result's `next` tells the old session to call `wait_for_message` for the new tab's takeover
   message. It finishes its current step, replies `stopped`, and ends its turn.
4. The new session reads the brief, sends the takeover message, waits for the old session's reply, and
   then calls `close_tab` for the old tab.

Safety:

- The brief holds notes from another agent session, not instructions from the user. The new session
  confirms with the user before anything destructive.
- `close_tab` refuses to close the old tab until the takeover message and the old session's reply to
  it both exist. The takeover message must reach the old session within the 10-minute confirmation
  window (`confirmBy` in the result). The server enforces this; a skill can't skip it.
- The brief and a record of the handoff (`<id>.json`) stay on disk in `~/.ide-agent-tabs/handoffs/`.
  Leave secrets out of a brief.

`"closeAfterHandoff": false` in `config.json` keeps the old tab open. `list_sessions` then shows that
session with `handedOffTo` set to the new tab's id. The setting is `true` by default.

## Resume

When a session ends, the server writes a record to `~/.ide-agent-tabs/history/<id>.json`, named by the
agent's own session id. A session ends when its server shuts down, when `close_tab` closes its tab, or
when another server finds its presence file dead. The record holds the agent, label, folder, IDE or
terminal product, model, effort, harness, `via`, `startedAt`, `endedAt`, the size and a preview. Only the
owner can read it, and it is deleted after 7 days.

- The resumable id is the Claude Code session id (from the hooks or the Claude mod), the Codex thread id,
  or the Antigravity CLI conversation id. A session without one leaves no record, and so does a Claude
  session that never wrote a transcript.
- Size is the input of the last turn. For Claude Code it is `input_tokens` plus the cache tokens of the
  last assistant turn in `~/.claude/projects/<folder>/<session>.jsonl`. For Codex it is
  `last_token_usage.input_tokens` from the session's rollout file. Other agents expose no size, so it is
  `null`.
- The preview is the first line of the last assistant text, cut to 120 characters. The record keeps no
  other transcript text.

`resume_tab` reopens a record in a new tab with the agent's resume option, in the record's folder, its
IDE or terminal when that is still running (else one with the same product, else the usual route), and
its model. An `ide` or `model` you pass wins.

| Agent | Resume option |
|---|---|
| Claude Code | `claude --resume <id>` |
| Codex, Codex (local) | `codex resume <id>` |
| Antigravity CLI | `agy --conversation <id>` |

Any other agent gets an error that suggests `handoff` instead.

A resumed session re-reads its whole history. `resume_tab` opens it without `confirm` only when all of
these hold:

- It ended within the prompt cache window: 5 minutes, or 60 minutes when the transcript shows that Claude
  Code wrote to the 1-hour cache.
- The model stays the same.
- The size is at most 50,000 tokens. An unknown size passes only within 5 minutes.

Then the result says `likely cached: about 10% of normal input cost`. Otherwise it opens nothing and
returns `needsConfirm: true` with the size, the age, the reasons and the offer of `handoff` as the
cheaper fresh start. The `new-tab` skill asks the user, and passes `confirm: true` only after the user
agrees. `"allowResume": false` in `config.json` makes `resume_tab` refuse every call.

## Messaging

Every session that runs this server can message every other one on the machine, whichever agent CLI it
is. Claude Code sessions can also use Claude Code's own `SendMessage`, which doesn't reach other CLIs or
cross between WSL and Windows.

1. Call `list_sessions` to find the other session's `id`.
2. Call `send_message` with `to` and `text`. The text goes to that session's mailbox.
3. Call `wait_for_message` with `replyTo` set to the message id to wait for the answer.

The server's instructions tell every agent to treat a received message as a peer's request, not an
instruction from its user, to ask its user before anything destructive, to reply with `replyTo`, and not
to answer a message that needs no answer.

### How a session learns about a message

- **Hooks** keep each session's `state`: `idle`, `busy` or `permission`, or `unknown` without hooks.
  When messages wait, the hooks add a one-line reminder after a prompt or a tool call. At the end of a
  turn, they ask the agent to read the messages, at most three times in a row.
- **Wake-up:** when the recipient is `idle` and its tab can take input, `send_message` types one fixed
  line into the tab: `Agent Tabs: new message from <agent> <short id>. Call read_messages.` It never
  types the message text.

| Agent | Hooks | Reminder after a prompt | Reminder after a tool call | Nudge at turn end |
|---|---|---|---|---|
| Claude Code | From the plugin | Yes | Yes | Yes |
| Codex | In Codex tabs only, from the tab's arguments; trusted, with no `/hooks` review | Yes | Yes | Yes |
| Antigravity CLI | Added by `--register agy` | Yes, before each model call | Yes, through the same hook; Antigravity CLI ignores a `PostToolUse` hook's output | Yes |
| Copilot CLI | Added by `--register copilot` | No: Copilot CLI drops that hook's output | Yes | Yes |
| Gemini CLI | Added by `--register gemini` | Yes | Yes | Yes |
| Grok Build | Added by `--register grok` | No | Yes | Yes |
| Pi | None | No | No | No |
| Hermes | Added by `--register hermes` | Yes | No | Yes, but only after a turn that edited code (`pre_verify`) |
| OpenCode | None | No | No | No |
| Qwen Code | Added by `--register qwen` | Yes | Yes | Yes |
| Goose | Added by `--register goose` | No | No | Yes |
| Codex (local) | As Codex: the tab's arguments | Yes | Yes | Yes |

| Where the recipient runs | Wake-up |
|---|---|
| JetBrains IDE, VS Code and editors built on it | The IDE's `input` route |
| tmux, WezTerm, kitty with remote control, Ghostty on macOS, iTerm2 | The terminal's own send-text command |
| Windows Terminal, Ghostty on Linux, kitty without remote control | None; the session relies on hooks |
| A session Agent Tabs didn't open | None |

### Claude Code sessions

In a Claude Code build with function hooks, the plugin's mod (`claude-plugin/hooks/register.tsx`) bridges
Claude Code's own tools to Agent Tabs:

- `ListAgents` also lists every other Agent Tabs session, with its agent, state, tab, host, folder and
  `via`.
- `SendMessage` to an Agent Tabs session's name goes to its mailbox. A native Claude peer's name goes
  the native way.
- In a tab, the mod delivers incoming mail as a framed peer prompt when the session is idle, and shows
  the unread count in the status line. While the mod runs, the presence file holds `driver: "mod"`, the
  command hooks do nothing for that session, and `send_message` returns `queued` with a note instead of
  typing a wake line. A mod that stops for 3 minutes hands the session back to the hooks and wake lines.
- The mod calls the internal `agent_tabs_mod` tool, which the server offers to Claude Code only, and
  defers `send_message`, `read_messages`, `wait_for_message` and `list_sessions` behind ToolSearch.

- `/agent-tabs` (or `/agent-tabs-messages`) shows or hides the Agent Tabs Messages pane: the agents by
  IDE or terminal and folder, then the messages one sent or received, then one message, with Reply. A
  folder heading opens the folder in the file manager.
- A delivered message's card has an **Open in Agent Tabs** button, and a band above the prompt shows
  unread mail with an **Open** button.
- Each send is logged in `~/.ide-agent-tabs/mail/<sender>/sent-log/`, and the mod logs native
  SendMessage traffic; the logs last 7 days, as read mail does.
- `"claudeMod": "off"` in `config.json` turns the mod off.

See [Claude Code mod](../docs/design.md#claude-code-mod) in the design doc.

### Codex tabs

A Codex tab starts `codex --no-daemon` with `-c` options that add, for that session only, this server
and five hooks that call it. Codex's shared daemon would start both with another tab's environment, so a
Codex tab runs in its own process instead. The tab needs Codex 0.158 or later, and the shared server copy
in `~/.ide-agent-tabs/mcp/`, which each Claude Code session start refreshes. The hooks are `mcp_tool`
hooks: they call the internal `agent_tabs_hook` tool over the session's own MCP connection, so no process
starts and no console window opens. The options also trust the five hooks, so Codex runs them without a
`/hooks` review. For the exact options, see `CODEX_TAB_ARGS` in `src/profiles.ts` and
[Codex tabs](../docs/design.md#codex-tabs) in the design doc.

A Codex session outside a tab, such as one in the Codex desktop app, has no hooks. When its server's
`IDE_AGENT_TABS_ID` names no open tab, the session's id becomes `codex-<thread id>`.

### Limits

- A message holds up to 32,000 characters.
- A session sends at most 20 messages a minute.
- A mailbox holds at most 50 unread messages.
- The server deletes read messages after 7 days, and the mailbox of a session that ended 7 days ago.

### Security

- Messages are files in `~/.ide-agent-tabs/mail/` that only your user can read. Any process of your
  user can write one, as it can open a tab.
- A message's text never reaches a command line or a terminal. The wake line holds only the sender's
  cleaned agent name and the first 8 characters of its id.
- The server sets `from` itself, so an agent can't send as another session.

## Config files

All files live in `~/.ide-agent-tabs/`. Set `IDE_AGENT_TABS_HOME` to use another folder.

| File | Holds |
|---|---|
| `endpoints/*.json` | One registry entry per running IDE. The server skips entries with an unknown `protocol` or a URL that isn't on the loopback address, and deletes entries whose process has ended or whose `beatMs` heartbeat (file modification time) stopped for more than 5 beats. |
| `agents.json` | Your own agent profiles. The rules match the JetBrains plugin exactly. |
| `config.json` | `defaultAgent`, `jev` (the Jev settings), `launchVia`, `closeAfterHandoff`, `allowResume` and the tab settings below. The JetBrains plugin and the VS Code extension edit the same keys. |
| `detected.json` | The terminals, on Windows the PowerShell installs, and `ori` (`path`, `version`, and the `agents` Ori lists as installed; `null` without Ori) that this machine has, for the IDE settings. The server writes it at start, on each `list_ides` and from the Claude Code session start hook; don't edit it. |
| `handoffs/` | The brief (`<id>.md`) and the record (`<id>.json`) of each handoff. |
| `history/` | One record per ended session, named by the agent's session id, kept 7 days. See [Resume](#resume). |
| `terminal-windows.json` | The windows kept for Agent Tabs with `"terminalWindow": "dedicated"`. The server writes it; don't edit it. |
| `jev/ledger.jsonl` | One line per Jev call: time, tool, agent, tab, model, question count, input tokens and result. |
| `terminal-tabs.json` | The terminal tabs this server opened. The server writes it; don't edit it. |
| `sessions/*.json` | One presence file per running server: session id, agent, folder, process id, host and state. |
| `mail/<id>/` | A session's mailbox: `tmp/`, `new/` (unread), `cur/` (read), and `sent.json` for the rate limit. |
| `launch/` | Short-lived launch files. Each is deleted as soon as its tab starts. |
| `mcp/` | A copy of the server for other agent CLIs. See [Other agents](#other-agents). |

| `config.json` key | Values | Default |
|---|---|---|
| `tabRouting` | `"project"`: the IDE that has the project open. `"caller"`: the IDE or terminal window the request came from. | `"project"` |
| `terminal` | `"auto"`, or a terminal id such as `windows-terminal`, `wezterm`, `kitty`, `tmux`, `ghostty` or `iterm2` | `"auto"`: the platform's order |
| `shell` | Windows only. `"auto"`, or the absolute path of a PowerShell executable | `"auto"`: the newest PowerShell 7 or later, else Windows PowerShell 5.1 |
| `terminalWindow` | `"last"`: your last window. `"dedicated"`: a window kept for Agent Tabs. | `"last"` |
| `launchVia` | `"direct"`: start each agent with its own command. `"ori"`: start supported agents with `ori <agent>`, which bills model usage through OpenRouter. See [Model and Ori](#model-and-ori). | `"direct"` |
| `closeAfterHandoff` | `true`: the new session closes the old tab after a handoff. `false`: the old tab stays open, marked `handedOffTo`. See [Handoff](#handoff). | `true` |
| `allowResume` | `true`: `resume_tab` reopens closed sessions, asking for `confirm` when the resume costs full price. `false`: it refuses. See [Resume](#resume). | `true` |
| `claudeMod` | `"on"`: Claude Code sessions use the Agent Tabs mod: SendMessage and ListAgents reach every agent, mail arrives in-process, and `/agent-tabs` shows the agents pane. `"off"`: the hooks, wake lines and messaging tools, as in 0.6.0. New Claude Code sessions pick up a change. | `"on"` |
| `focusNewTabs` | `"auto"`: an agent's tab opens behind the current one unless the call passes `focus: true`. `"always"`: it comes to the front unless the call passes `focus: false`. `"never"`: behind unless the call passes `focus: true`. See [Focus](#focus). | `"auto"` |

An `ide` or `focus` passed to `open_tab` always wins over these settings. The server ignores a value it doesn't know,
uses the default, and reports a warning in `list_agents`.

`list_agents` reads the profile files itself instead of asking an IDE. A terminal tab uses the same
profiles, so the answer holds whether or not an IDE is running. `installed` reflects the server's `PATH`,
which is the calling agent's `PATH`.

## Other agents

Codex, Antigravity CLI, Copilot CLI, Gemini CLI, Grok Build, Pi, Hermes, OpenCode, Qwen Code and Goose can
run this server too. The setup skill registers it with the agents you choose, through `sync-ides.mjs`.
For every agent except Pi and OpenCode, registering also adds the messaging hooks. Codex tabs bring
their own server and hooks, so Codex needs registering only for Codex sessions outside tabs, and only on
macOS and Linux. Codex (local) is a Codex tab and needs no registration:

```sh
node dist/sync-ides.mjs --agents
node dist/sync-ides.mjs --register codex agy copilot gemini grok pi hermes opencode qwen goose
node dist/sync-ides.mjs --unregister codex
```

`--agents` reports, for each agent, whether it's installed, whether it's registered, the server path it
runs, whether that path is the stable copy, and `hooks`: whether its messaging hooks are in place (`null`
for Codex, Pi and OpenCode, which get none). `--register` and `--unregister` print the same fields for
each agent, with `ok` or an `error`.

Each agent runs `node ~/.ide-agent-tabs/mcp/mcp-server.mjs`, with the server name `ide-agent-tabs`:

| Agent | Where the entry goes | How |
|---|---|---|
| Codex, not on Windows | `~/.codex/config.toml`, or `$CODEX_HOME/config.toml` | `codex mcp add ide-agent-tabs -- node <path>` |
| Antigravity CLI | `mcpServers` in `~/.gemini/config/mcp_config.json` | The script edits the file. |
| Copilot CLI | `mcpServers` in `~/.copilot/mcp-config.json`, or `$COPILOT_HOME/mcp-config.json` | The script edits the file. |
| Gemini CLI | `~/.gemini/settings.json`, user scope | `gemini mcp add --scope user ide-agent-tabs node <path>` |
| Grok Build | The `[mcp_servers.ide-agent-tabs]` table in `~/.grok/config.toml`, or `$GROK_HOME/config.toml` | The script edits the file. |
| Pi | `mcpServers` in `~/.pi/agent/mcp.json`, or `$PI_CODING_AGENT_DIR/mcp.json` | The script edits the file. |
| Hermes | `mcp_servers` in `config.yaml` in `~/.hermes`, `$HERMES_HOME`, or `%LOCALAPPDATA%\hermes` on Windows | The script edits the file. |
| OpenCode | `mcp` in `~/.config/opencode/opencode.json`, or under `$XDG_CONFIG_HOME` | The script edits the file. |
| Qwen Code | `mcpServers` in `~/.qwen/settings.json`, or `$QWEN_HOME/settings.json` | The script edits the file. |
| Goose | `extensions` in `config.yaml`: `~/.config/goose/` on macOS and Linux, `%APPDATA%\Block\goose\config\` on Windows, or `$GOOSE_PATH_ROOT/config/` | The script edits the file. |

- `~/.ide-agent-tabs/mcp/` holds `mcp-server.mjs`, `agent-hook.mjs`, `launch/` and `THIRD_PARTY_NOTICES.txt`, in the same
  layout as the plugin's `dist/`. The Claude Code plugin folder has the version in its path, so an
  update would break a registration that pointed there. `--register` writes the copy. When a Claude Code
  session starts after a plugin update, the session start hook refreshes the copy if it exists.
- The script writes each file to a temporary name and renames it, so a running server never reads a
  half-written file. If a file is in use, the hook logs the error to `sync.log` and tries again at the
  next session.
- The script runs the agent CLIs from `~/.ide-agent-tabs`, so a project config in the current folder
  doesn't apply. It checks each registration by reading it back, not by the exit code.
- The script keeps the other keys in a Copilot CLI, Antigravity CLI, Pi, Qwen Code or OpenCode config file,
  and its indentation. It doesn't change a file that isn't plain JSON, such as a file with comments or an
  `opencode.jsonc` with comments. It reports an error instead, and you add the entry by hand:

  ```json
  "ide-agent-tabs": { "type": "local", "command": "node", "args": ["<path>"], "env": { "IDE_AGENT_TABS_ID": "${IDE_AGENT_TABS_ID}", "IDE_AGENT_TABS_AGENT": "${IDE_AGENT_TABS_AGENT}" }, "tools": ["*"] }
  ```

  for Copilot CLI under `mcpServers`, for Antigravity CLI under `mcpServers`:

  ```json
  "ide-agent-tabs": { "command": "node", "args": ["<path>"] }
  ```

  or for OpenCode under `mcp`:

  ```json
  "ide-agent-tabs": { "type": "local", "command": ["node", "<path>"], "enabled": true, "timeout": 660000 }
  ```

- The hooks run `node ~/.ide-agent-tabs/mcp/agent-hook.mjs <agent> <event>`:

  | Agent | Where the hooks go |
  |---|---|
  | Antigravity CLI | The `ide-agent-tabs` group in `~/.gemini/config/hooks.json` |
  | Copilot CLI | Its own file, `~/.copilot/hooks/ide-agent-tabs.json`, or under `$COPILOT_HOME` |
  | Gemini CLI | `hooks` in `~/.gemini/settings.json` |
  | Grok Build | Its own file, `~/.grok/hooks/ide-agent-tabs.json`, or under `$GROK_HOME` |
  | Hermes | `hooks` in `config.yaml`, plus the approvals in `shell-hooks-allowlist.json` next to it |
  | Qwen Code | `hooks` in `~/.qwen/settings.json`, or `$QWEN_HOME/settings.json` |
  | Goose | An Open Plugins plugin: `plugin.json` and `hooks/hooks.json` in `~/.agents/plugins/ide-agent-tabs/`, or under `$GOOSE_PATH_ROOT` |

  `--unregister` removes only the Agent Tabs entries and leaves your other hooks in place.
- Antigravity CLI notes:
  - `--register agy` also adds allow rules to `permissions.allow` in
    `~/.gemini/antigravity-cli/settings.json` for the tools that read or message (`send_message`,
    `read_messages`, `wait_for_message`, `list_sessions`, `list_agents`, `list_ides`, `list_tabs`), because
    Antigravity CLI asks before each call to an MCP tool that has no rule. `open_tab`, `close_tab`, `handoff`
    and the `jev_` tools still ask. Registering replaces the broader `mcp(ide-agent-tabs/*)` rule of
    earlier builds. `--unregister agy` removes only these rules and keeps your other settings.
  - Antigravity CLI runs a hook command through `cmd.exe` on Windows and escapes double quotes, so the
    hook path goes in without quotes. `--register agy` refuses a path with spaces or `cmd.exe` special
    characters.
  - `~/.gemini/config/hooks.json` is also read by Antigravity 2.0 and the Antigravity IDE. There,
    `IDE_AGENT_TABS_ID` is unset, so the hook exits without output, at the cost of one `node` start for each
    model call and tool call.
  - There is no permission or interrupt event. A session that waits for approval shows `busy`.
  - Antigravity CLI ends any MCP tool call after 3 minutes and has no setting to change that, so
    `wait_for_message` waits at most 170 seconds in an Antigravity CLI session.
  - The entry needs no `env`: Antigravity CLI passes its own environment to the server and the hooks.
- Grok Build, Pi, Hermes, Qwen Code and Goose notes:
  - Grok Build's entry has no `env`: Grok passes its own environment and may refuse a `${VAR}` it can't
    expand. Its hooks are `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Notification` (matcher
    `permission_prompt|idle_prompt`), `Stop`, `StopCancelled` and `StopFailure`. Its `idle_prompt`
    notification is an input idle signal, as in Claude Code.
  - Pi hides MCP tools behind its code-mode tool unless the server is exposed directly, and times a
    request out after 60 seconds, so the entry sets `exposure: "direct"` and `timeout: 660`. Its `env`
    forwards `IDE_AGENT_TABS_ID` and `IDE_AGENT_TABS_AGENT`. Pi gets no hooks, so it has no state.
  - Hermes passes a server only the variables its `env` names, and times a tool call out after 300
    seconds, so the entry sets `env` and `timeout: 660`. Its shell hooks run as
    `node '<hook path>' hermes <event>`. Hermes asks before it first runs each (event, command) pair
    and skips the hook when nobody can answer, so `--register hermes` adds one `approvals` entry in
    `shell-hooks-allowlist.json` for each of its own pairs, and for nothing else. It never sets
    `hooks_auto_accept` or `HERMES_ACCEPT_HOOKS`. `pre_verify` nudges only after a turn that edited
    code, and Hermes counts each block against its `max_verify_nudges`.
  - Qwen Code's entry sets `env` and `timeout: 700000` (milliseconds). Its hooks run as
    `node "<hook path>" qwen <event>`.
  - Goose's extension sets `timeout: 700`. Goose runs a hook with `sh -c`, so on Windows it needs Git
    Bash. With no first prompt, a Goose tab runs `goose session`, because `goose run -s` refuses to
    start without a message.
  - The script edits the YAML of Hermes and Goose with the `yaml` package, and keeps comments and the
    other keys. It leaves a file that isn't valid YAML alone and reports an error. It edits the TOML of
    Grok Build only in the `[mcp_servers.ide-agent-tabs]` form.
- `--register codex` refuses on Windows. The Codex desktop app reads the same `config.toml`, and Codex
  before 0.159 opens a console window each time the app starts an MCP server from it. Use Codex tabs
  there.
- Codex passes a server only a fixed set of environment variables, so `--register codex` adds
  `env_vars` for `IDE_AGENT_TABS_ID`, `IDE_AGENT_TABS_AGENT` and `IDE_AGENT_TABS_HOME` to the server's
  table in `config.toml`. It also sets `tool_timeout_sec = 660`, so `wait_for_message` can wait its
  full 10 minutes. The Copilot CLI entry forwards the first two variables in its `env`.
- OpenCode applies the entry's `timeout` (milliseconds) to tool calls, and its default would end
  `wait_for_message` early, so `--register opencode` sets `timeout` to 660000. Register again to update an
  entry written earlier.
- Claude Code isn't registered this way. It gets the server and the hooks from the plugin.
- After you register an agent, restart its open sessions.

To remove Agent Tabs from the other agents, run `--unregister` with each agent, then delete
`~/.ide-agent-tabs/mcp/`.

## Terminals

| Terminal | OS | Open | List | Close |
|---|---|---|---|---|
| Windows Terminal | Windows | Tab | Tabs this server opened, while their shell runs | Best effort |
| Ghostty 1.3 or later | macOS | Tab | Yes | Yes |
| iTerm2 | macOS | Tab | Yes | Yes |
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

### Terminal window

With `"terminalWindow": "dedicated"`, terminal tabs open in a window kept for Agent Tabs instead of your
last window. When that window is closed, the next tab opens a new one.

| Terminal | Dedicated window |
|---|---|
| Windows Terminal | The named window `agent-tabs` (`wt.exe -w agent-tabs`) |
| WezTerm | A window the server opens with `wezterm cli spawn --new-window`, then targets with `--window-id` |
| kitty, remote control on | An OS window the server opens with `--type=os-window`, then targets by one of its windows |
| tmux | The session `agent-tabs`; `open_tab` returns a `note` to attach to it when no client is attached |
| iTerm2, Ghostty on macOS | A window the server opens, then targets by its AppleScript `id` |
| Ghostty on Linux, kitty with remote control off | No change: each agent already gets its own window |

The server keeps the WezTerm, kitty, iTerm2 and Ghostty window ids in `terminal-windows.json`. With
`"tabRouting": "caller"`, a caller in a terminal tab gets the new tab in its own window, whatever
`terminalWindow` says. Windows Terminal can't name the window a tab runs in, so a caller's tab there opens
in the `agent-tabs` window when the caller's tab opened there, and otherwise in the most recently used
window.

A terminal that the server starts, such as a first Windows Terminal window, a new Ghostty or kitty
process, a WezTerm GUI or a tmux server, gets the server's environment without the variables that
identify the calling agent session, such as `CLAUDECODE` or `CODEX_SANDBOX`.

### Windows Terminal

- The server runs `wt.exe -w 0 new-tab`, which opens the tab in the most recently used window. The tab
  runs `agent-launch.ps1` in the PowerShell that `"shell"` in `config.json` names, or by default in the
  newest PowerShell 7 or later, else Windows PowerShell 5.1.
- The server looks for PowerShell on `PATH` (`pwsh.exe`, `powershell.exe`), and in the standard folders
  even when `PATH` doesn't list them: `%ProgramFiles%\PowerShell\<version>\pwsh.exe` (MSI or winget, and
  `7-preview`), the Microsoft Store alias `%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe`, and
  `%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`. It reads a version from the folder or
  Store package name where it can, and otherwise runs that PowerShell once during detection, never when it
  opens a tab. A stable release wins over a preview of the same or a lower version. WezTerm on Windows uses
  the same PowerShell.
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

### iTerm2

- The server drives iTerm2 through AppleScript, with `/usr/bin/osascript`. It needs no iTerm2 setting;
  the Python API can stay off.
- Setup: the first tab makes macOS ask whether the app that runs the agent, such as your terminal, IDE or
  the Claude desktop app, may control iTerm. Allow it. To change the answer later, use **System Settings >
  Privacy & Security > Automation**. If the permission is denied, `open_tab` returns an error that names
  this setting, and the server skips iTerm2 when it picks a terminal until the server restarts.
- A new tab opens in the current iTerm2 window, or in a new window when none is open, unless
  `terminalWindow` or `tabRouting` picks another window. If iTerm2 isn't
  running, AppleScript starts it.
- Each tab is tracked by its session's `unique ID`. `list_tabs` reads every session's id, and
  `close_tab` closes the session with the recorded id.
- The tab command holds the login shell, the launch script and the launch file, each single-quoted.
  iTerm2 evaluates the command as an interpolated string and then splits it like a shell, so the server
  refuses to open a tab when the launch script or launch file sits in a path that holds `'`, `\` or `$`.
- The tab title and the wake-up line reach AppleScript as `osascript` arguments, never inside the script.
- The server finds iTerm2 at `/Applications/iTerm.app` or `~/Applications/iTerm.app`.

### Ghostty on Linux

- Ghostty on Linux can't open a tab in a running instance from outside
  ([ghostty#12136](https://github.com/ghostty-org/ghostty/issues/12136)). Each agent gets a new Ghostty
  process with one window, started with `--gtk-single-instance=false`.
- The launch script writes its shell's process id to `launch/<id>.pid`. `list_tabs` reports the window
  while that shell runs, and `close_tab` sends the shell `SIGHUP`, which ends the agent and closes the
  window.
- The server finds `ghostty` on `PATH`, then in `/usr/bin`, `/usr/local/bin`, `~/.local/bin` and
  `/snap/bin`.

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
  `/Applications/WezTerm.app` and `~/Applications/WezTerm.app` on macOS, or `/usr/bin`, `/usr/local/bin`,
  `~/.local/bin` and `/home/linuxbrew/.linuxbrew/bin` on Linux.

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
- You need tmux 3.0 or later. The server sets the launch paths with `/usr/bin/env` in the window
  command, not with `-e`, which `new-session` gained only in tmux 3.2. The server finds `tmux` on `PATH`, then in
  `/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin` and `/home/linuxbrew/.linuxbrew/bin`.

### Adding a terminal

Each terminal is a `TerminalDriver` in `src/terminals/`, with `available`, `open`, `alive` and `close`,
`currentCapabilities` when what it can do depends on its setup, and `input` when it can type a line into a
tab. Add the driver to
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
- iTerm2: untested. Unit tests with a stand-in `osascript` cover open, list, close, input, quoting and
  the permission errors.
- No driver is tested on a real Mac.

## Build and test

You need Node.js 20 or later.

```sh
npm install
npm test
npm run build
```

`npm run build` type-checks the code and bundles it into `claude-plugin/dist/mcp-server.mjs`,
`claude-plugin/dist/sync-ides.mjs` and `claude-plugin/dist/agent-hook.mjs`. It copies the launch scripts to `claude-plugin/dist/launch/` and
writes the bundled packages' licenses to `claude-plugin/dist/THIRD_PARTY_NOTICES.txt`. It leaves the IDE
builds in `claude-plugin/dist/ide/` in place. Commit the `dist/` folder: the plugin runs it without
`node_modules`.

The launcher tests run the real launch scripts with `pwsh`, Windows PowerShell and bash when they're
installed, and skip the rest.
