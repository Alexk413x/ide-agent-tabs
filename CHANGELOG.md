# Changelog

Each entry names the Claude Code plugin version (`claude-plugin/.claude-plugin/plugin.json`, which
`mcp/package.json` matches) and the bundled IDE versions when they change. Tags are
`ide-agent-tabs--v<version>`.

## 0.6.1

Plugin and MCP server 0.6.1. The IDE packages are unchanged.

### Changed

- The Claude Code mod's pane is **Agent Tabs Messages**, in its title and at the top of the agents view.
  `/agent-messages` opens and closes it; it replaces `/agent-tabs`. Nothing in the pane is pre-selected.
- On the terminal and desktop, the pane's rows are drawn by a `Client` module, so nothing in it is
  inverted under the pointer or the focus. Hovering a session marks both its lines with `▎` and
  underlines both; a left click on either line opens its messages. Once a click gives the list the keys,
  the arrow keys or Tab move a focus that looks the same, and Enter opens it. Message rows, folder
  headings and the Back and Reply chips work the same way. VS Code and mobile keep the Buttons. The list
  draws one row per line, box edges included, so the pointer lights the line under it; a folder's
  revealed path stays lit across the gap and for 300 ms after the pointer leaves.
- An agent opens the pane when the user asks, through the mod's `open_agent_messages` tool, optionally on
  one agent's messages (`agent`: a name from ListAgents).
- A message line reads `HH:MM  ↑ name · text…`: the peer's name without its `[ref]`, then the first line
  of the text, cut to fit.
- The agents view groups sessions by IDE or terminal (`Antigravity IDE`, `Windows Terminal`, and so on),
  each in a rounded box with its name as the first line, with `Other` for an unknown host, then
  `Remote Control` and the cloud group last. Under each come the folders open there, by base name
  (`▸ ide-agent-tabs`), with a blank line between them.
- Each session takes two lines: a coloured state dot, an agent glyph in the agent's colour (Claude `✻`,
  Codex `◆`, Antigravity `▲`, others `•`) and the name; then, dim, the state, time since start, harness,
  model (without a leading `claude-` or `gpt-`) and effort, leaving out unknown parts. Either line opens
  the session. Nothing is selected when the pane opens. The session id moves to the messages and
  detail views.
- A folder heading shows the folder's base name. Pressing it opens the folder in the file manager:
  `explorer.exe` on Windows, `open` on macOS, `xdg-open` on Linux, run by argv with no shell. On Windows
  the folder window opens behind the IDE, because Windows doesn't let a background process take the
  foreground. Hovering the heading shows the full path to its right; pressing the path copies the
  path to the clipboard.
- Actions in the pane report on a line under its title for 6 seconds, because the pane holds toasts while
  it's open.
- The card of a delivered peer message has an **Open in Agent Tabs** button, which opens the pane on that
  message.
- While the session has unread Agent Tabs mail, a band above the prompt shows
  `✉ <n> new from <names>` and an **Open** button, which opens the pane on the newest sender's messages.
  Its hotkey is `o` once the band has the keyboard (ctrl+x tab or a click). The band hides while the
  pane is open and once nothing is unread.
- The arrival toast ends with `· /agent-messages to view`, and the toast and status line name the sender as
  `list_sessions` does.
- `ListAgents` in a Claude Code session uses the pane's layout, one line per session: IDE or terminal
  headings, folder base names indented two, session lines indented four with no `WHERE` column, then
  `Remote Control` and cloud. The native `This session is …` line stays first. `list_sessions` keeps
  the full paths.
- The pane and `ListAgents` list the calling session too, marked `(this session)`.
- Every session has a name in Claude Code's native style, such as `plugins-82`: a Claude session's
  native name, else the folder's base name, lowercased, and 2 hex characters of the session id, longer
  only when two listed names would collide. `list_sessions` gives it as `name` and `shortName`, and the
  older `codex-c66c` style as `legacyName`. `send_message`, the mod's `send` op and SendMessage through
  the mod take the name, the legacy name or the full id.

- A Claude session started as an agent type (`--agent` or the `agent` setting) records the type and
  its definition's `color` in presence. `list_sessions` shows `agentType` and `agentColor`, the pane
  draws the session's name in that colour, and line 2 shows `Claude Code (<type>)`.

### Fixed

- A Claude tab on 0.5.3, which reports no native name, no longer shows twice. The mod joins it to the
  native peer whose name, without its suffix, matches the tab's folder and whose start time agrees
  within 2 minutes plus the native listing's precision, when exactly one peer and one tab fit. The
  joined line shows the native name with the tab's host, folder and session data.

## 0.6.0

Plugin and MCP server 0.6.0, with VS Code extension 0.1.27 and JetBrains plugin 0.4.10 bundled. Both IDE
packages add the `agy` profile and icon, the profiles and icons of seven more agents, the Codex
`Interrupt` hook, the tab settings and the **Allow resuming closed sessions** setting.

### Fixed

- A crashed IDE's endpoint no longer looks alive when Windows reuses its pid. Both IDEs write `beatMs`
  and `startedAt` into the endpoint file and touch the file every 60 seconds. The JetBrains plugin
  writes the file again if it is deleted. The MCP server skips and deletes an endpoint with `beatMs`
  that missed 5 beats, and keeps the pid-only rule for endpoints from older IDE builds.
- `send_message` wakes a fresh tab. A Claude tab reports idle at `SessionStart` with source `startup`,
  and a tab that `open_tab` starts without a prompt counts as idle 10 seconds after launch.
- A wake line no longer lands in a prompt the user is typing in a Claude tab. Claude Code's `Stop` fires
  while the user may already be typing, so `Stop`, `StopFailure` and `SessionStart` with source `clear`
  or `resume` record `inputIdle: false`, and a sender queues the message instead of typing. The
  follow-up retry types the wake line after Claude's `idle_prompt` notification (about 60 seconds with no
  input) sets `inputIdle: true`. Codex, Gemini CLI, Copilot CLI and Antigravity CLI expose no input idle
  signal, so they wake at turn end, and a line can still land in a prompt the user is typing.
- Copilot CLI's `agent_idle` notification reports a background agent, so it no longer marks the session
  idle. `elicitation_dialog` counts as `permission`, so no wake line answers a question.
- The VS Code status bar tooltip puts each line on its own line.
- The sender retries a queued wake-up every 15 seconds until the recipient reads the message, ends, or
  10 minutes pass. A lost wake line no longer needs `wait_for_message` to be retried.
- `read_messages` and `wait_for_message` return at most 40,000 characters and leave the rest unread. A
  cancelled read puts its messages back. A message file that holds no valid message moves to `bad/` and
  is reported instead of dropped.
- A headless agent started inside a tab no longer changes the tab's state. Only the agent session that
  owns the tab updates it. Another session takes the tab over only between turns, as after `/clear`,
  `/new` or resume, so this works for every CLI, including Codex and Antigravity CLI, which have no
  usable session-start hook. Hooks from a different CLI than the tab's agent are ignored, and the
  `delegate` skill clears `IDE_AGENT_TABS_ID` for its runs.
- `busy` with no hook activity for 15 minutes counts as idle. An interrupt sets idle: Claude
  `PostToolUseFailure` with `is_interrupt`, Claude `StopFailure`, and Codex `Interrupt`. A restarted
  server resets a dead server's state.
- A wake-up that fails on a cached host looks the host up again and retries once.
- Agent tabs stay listed after a VS Code extension host restart, as after an extension update, and after
  a JetBrains plugin update or reload. The terminals outlived the restart, but the IDE side forgot them,
  so `list_tabs`, `close_tab` and wake-ups failed for them. Both IDE packages find them again by their
  `IDE_AGENT_TABS_ID`, and open no startup tab when they find any.
- A failed delivery gives its rate-limit slot back. The same message sent again within 60 seconds
  returns the first id with `duplicate: true` and is not delivered twice.
- File locks record their owner. A lock whose owner is dead is broken at once, and waiters wait longer
  than the stale limit.
- Each server refreshes its presence every 60 seconds, so a reused pid no longer keeps a dead session
  alive.
- Copilot CLI tabs map `sessionStart` to idle; existing Copilot installs re-sync their hook file.
- A Windows test race that wrote presence without the lock.
- The IDE sync finds VS Code Insiders and Cursor on macOS: their app bundles name the command-line
  tool `bin/code`, not `bin/<cli>`. Cursor, Windsurf and Trae bundles are checked under both names.
- The IDE sync looks for editor command-line tools on Linux when they aren't on `PATH`: in
  `/usr/share/<cli>/bin`, `/opt/<cli>/bin`, `~/.local/bin`, `/snap/bin` for VS Code, VS Code Insiders
  and VSCodium, and the Flatpak exports of VS Code and VSCodium.
- The first tmux tab works on tmux 3.0 and 3.1. The launch paths reach the window through
  `/usr/bin/env` instead of `new-session -e`, which needs tmux 3.2.
- Ghostty and WezTerm on Linux are found in `/usr/bin`, `/usr/local/bin` and `~/.local/bin` when they
  aren't on `PATH`, and also in `/snap/bin` (Ghostty) and `/home/linuxbrew/.linuxbrew/bin` (WezTerm).
- The setup skill looks for JetBrains Toolbox 1.x installs, including the macOS Toolbox folder.
- The OpenCode MCP entry sets `timeout: 660000` (milliseconds). OpenCode applies it to tool calls, and
  its default would end `wait_for_message` early. Run `--register opencode` again to update an entry
  written earlier.
- A tab that an agent opens no longer takes focus by default. In kitty with remote control it opens with
  `--keep-focus`, in tmux with `new-window -d`, and in iTerm2 and Ghostty on macOS the previous tab is
  selected again. Windows Terminal and WezTerm have no background option, so their new tab still comes
  to the front.
- A server that fails to register its session, such as on a lock timeout, retries after 1, 3 and 9
  seconds and logs each failure to stderr. If every attempt fails, `list_sessions`, `send_message` and
  `read_messages` return a `warnings` entry, `this session isn't registered: <error>`, instead of
  leaving the session silently missing from the list.

### Added

- Resume closed sessions.
  - When a session ends (its server shuts down, `close_tab` closes it, or another server finds it dead),
    the server writes `~/.ide-agent-tabs/history/<id>.json`, named by the agent's own session id: agent,
    label, folder, product, model, effort, harness, `via`, start and end times, the last turn's input
    tokens (from the Claude Code transcript or the Codex rollout) and a 120-character preview of the
    last answer. Only the owner can read it, it holds no other transcript text, and it is deleted after
    7 days. The Claude mod reports the Claude session id for this.
  - `closed_sessions` lists them newest first, grouped by folder, one aligned line each: NAME, AGENT,
    ENDED, SIZE, MODEL, WHERE and ID.
  - `resume_tab` reopens one in its folder, IDE and model with `claude --resume`, `codex resume` or
    `agy --conversation`. Other agents get an error that suggests `handoff`.
  - A cost guard: a resume opens at once only within the prompt cache window (5 minutes, or 60 when the
    transcript shows the 1-hour cache), with the same model and at most 50,000 tokens, and then says
    "likely cached: about 10% of normal input cost". Any other resume needs `confirm: true` and returns
    the size, the age and the offer of `handoff` as the cheaper fresh start.
  - The `new-tab` skill handles "reopen my <agent> session from <when>", and asks the user before it
    passes `confirm`.
  - The setting **Allow resuming closed sessions** (`allowResume`, on by default) in VS Code, JetBrains
    and the `setup` skill. Off, `resume_tab` refuses.
- A Claude Code mod, loaded from `hooks/hooks.json` `modules` in Claude Code builds with function hooks.
  - `ListAgents` returns one list of every session that can take a message now: live native Claude
    peers and every Agent Tabs session, grouped by folder with the caller's folder first and cloud
    sessions last. Each line shows the name to message, state, time since start, harness, model,
    effort, IDE or terminal, and the session id's first 8 characters, aligned across groups. Within a
    folder, each agent's newest session comes first. A Claude tab appears once, under
    its native name. Offline sessions, including offline Remote Control ones, are left out and counted
    on the last line; `/list-agents` still shows them. `SendMessage` reaches every listed name, and
    native Claude peers still go the native way.
  - `list_sessions` adds `shortName` (such as `codex-f99f`), `session`, `harness`, `model`, `effort`,
    `where`, `folder` and `nativeName`, and `send_message` takes a `shortName`. The model comes from
    `open_tab`, hook payloads, the Claude mod, or a Codex session's `config.toml`.
  - `list_sessions` never shows a raw IDE host id. `open_tab` stores the IDE product or terminal label
    in the presence file, so a tab whose endpoint is gone, as after an extension-host restart, still
    names its IDE, and a tab found under a new host takes that host's label.
  - In a tab, the mod reports the session's state and delivers mail with `$.prompt.submit` as a framed
    peer prompt once the session is idle. A failed submit returns the message to unread.
  - The status line shows the unread count and the first sender, a toast announces each arrival, and the
    transcript draws each delivered message as a compact card.
  - The Agent Tabs messaging tools move behind ToolSearch in Claude Code.
  - `/agent-messages` shows or hides the agents pane: the merged agent list with coloured states, the
    messages the chosen agent sent or received, oldest first, and one message's detail with Reply, which
    fills the prompt. Arrow keys, Enter, Back and Esc navigate; it reads nothing while closed.
  - Each send writes an owner-only entry to `mail/<sender>/sent-log/`, and the mod logs native
    SendMessage traffic; `cleanMail` drops entries after 7 days.
  - The setting **Use the Claude Code mod (in-process messaging)** (`claudeMod` in `config.json`, `on` by
    default) in VS Code (`ideAgentTabs.claudeMod`), JetBrains and the setup skill. `off` leaves the mod
    inert, so the 0.6.0 hooks and wake lines apply.
  - While the mod runs, the presence file holds `driver: "mod"`; the command hooks skip that session and
    `send_message` types no wake line into it. A mod silent for 3 minutes, or no mod at all, leaves the
    command hooks and wake lines working as before.
- The internal `agent_tabs_mod` tool (`presence`, `send`, `take`, `ack`, `release`, `sessions`, `log`,
  `history`, `settings`) for the mod, offered to Claude Code clients only.
- `list_sessions` rows add `name`, `route`, `tab`, `ide`, `via`, and a `host` that names the IDE product
  and project or the terminal. Rows come in a fixed agent order.
- Four tab settings in `~/.ide-agent-tabs/config.json`, shared by the VS Code extension, the JetBrains
  plugin, the MCP server and the setup skill. VS Code groups them as **Agent Tabs: IDE tabs** and
  **Agent Tabs: Terminal tabs**, and JetBrains as **IDE tabs** and **Terminal tabs**. An explicit `ide`,
  agent or terminal name always wins.
  - `tabRouting`: `project` (default) or `caller`. With `caller`, a new tab opens in the IDE the request
    came from, and a request from a terminal tab opens in that terminal window.
  - `terminal`: `auto` or a detected terminal id.
  - `shell` (Windows): `auto` or the path of a PowerShell executable.
  - `terminalWindow`: `last` (default) or `dedicated`.
  VS Code settings are machine-scoped, and only the user-level value reaches `config.json`.
- `~/.ide-agent-tabs/detected.json` lists the detected terminals and PowerShell installs for the IDE
  settings. The server writes it at start, on `list_ides` and from the session start hook. `list_ides`
  also returns `shells`.
- PowerShell detection on Windows: `pwsh.exe` and `powershell.exe` on `PATH`, plus the MSI, winget,
  preview and Microsoft Store installs and Windows PowerShell 5.1 in their standard folders. `auto`
  picks the newest PowerShell 7 or later, else Windows PowerShell 5.1.
- Dedicated window mode, `"terminalWindow": "dedicated"`: Windows Terminal uses the window `agent-tabs`,
  WezTerm and kitty with remote control a window the server remembers, tmux the session `agent-tabs`,
  and iTerm2 and Ghostty on macOS a window the server remembers. Ghostty on Linux and kitty without
  remote control are unchanged.
- Agents appear in one order everywhere: Claude, Codex, Antigravity CLI, Copilot CLI, Gemini CLI, Grok
  Build, Pi, Hermes, then the agents built for local models (OpenCode, Qwen Code, Goose and Codex
  (local)), then custom profiles.
- Antigravity CLI (`agy`) is a built-in agent profile, labelled "Antigravity CLI", that starts with
  `agy -i <prompt>`. `--register agy` writes the server entry to `~/.gemini/config/mcp_config.json`, the
  `ide-agent-tabs` hook group (`PreInvocation`, `PostToolUse`, `Stop`) to `~/.gemini/config/hooks.json`,
  and allow rules to `permissions.allow` in `~/.gemini/antigravity-cli/settings.json` for the tools that
  read or message (`send_message`, `read_messages`, `wait_for_message`, `list_sessions`, `list_agents`,
  `list_ides`, `list_tabs`). `open_tab`, `close_tab`, `handoff` and the `jev_` tools still ask, and
  registering replaces the broader `mcp(ide-agent-tabs/*)` rule of earlier builds. `--unregister agy` removes only those. The hook path goes in
  unquoted, because Antigravity CLI escapes quotes in a `cmd.exe` command, so registration refuses a path
  with spaces or `cmd.exe` special characters.
- Antigravity CLI hooks set `busy` at `PreInvocation` and `PostToolUse`, add the unread-message reminder
  at `PreInvocation`, and nudge at `Stop` with `decision: "continue"`. A `PreInvocation` with
  `invocationNum` 0 counts as a new prompt. Antigravity CLI has no permission or interrupt event, so a
  session that waits for approval shows `busy`.
- `wait_for_message` waits at most 170 seconds in an Antigravity CLI session, because Antigravity CLI ends
  any MCP tool call after 3 minutes.
- The `delegate` skill runs Antigravity CLI headless with `agy -p`.
- Codex tabs report an Esc interrupt through the `Interrupt` hook (see Fixed).
- The IDE sync and setup skill support Kiro (`kiro`), Positron (`positron`) and Trae (`trae`).
- iTerm2 on macOS as a terminal host, `iterm2`: open, list and close tabs, and the messaging wake-up,
  through AppleScript. The first tab asks for the macOS Automation permission. A denied permission
  returns an error that names the setting, and later tabs open in the next terminal in the platform
  order until the server restarts.
- `open_tab` takes `model`, which the server passes with the profile's `modelFlag`: `--model` for
  `claude`, `agy`, `copilot`, `pi` and `goose`, `-m` for `codex`, `gemini`, `grok`, `hermes`, `opencode`,
  `qwen` and `codex-local`. A custom profile sets `modelFlag` in
  `agents.json`. A `model` for a profile without one is an error, never ignored. `list_agents` reports
  `model: true` for a profile that takes one.
- Launching through Ori (`ori <agent>`), which bills model usage through OpenRouter:
  - `open_tab` takes `via: "ori"` or `"direct"`, and the result carries `via: "ori"` for a tab started
    through Ori. The tab's identity stays the inner agent (`claude`, `codex`), so hooks, wake rules and
    messaging don't change.
  - The setting **Launch through OpenRouter (Ori)**, `launchVia` in `config.json` (`direct` by default),
    sets the launch for tabs that pass no `via`. It shows in the VS Code and JetBrains settings only when
    Ori is detected. An explicit `via` beats the setting. When Ori can't launch the agent, the setting
    falls back to a direct launch, and an explicit `via: "ori"` returns an error.
  - `detected.json` has an `ori` field (`path`, `version` and `agents`, or `null`), and `list_agents`
    marks launchable agents with `ori: true` and reports `launchVia`. Ori launches `claude`, `codex`,
    `grok`, `hermes`, `opencode`, `pi` and `prime-agent`, and only those Ori lists as installed.
  - Checked on Ori 0.14.3 on Windows (2026-10-03): Ori starts the agent as a child process and the
    environment passes through, so `IDE_AGENT_TABS_ID` reached the Codex MCP server. `ori claude` fails
    with `401 Missing Authentication header`, because Ori's per-launch Claude settings set
    `ANTHROPIC_AUTH_TOKEN` to an empty string. That is an Ori issue. On Windows, Ori refuses an argument
    that holds `"`, `%`, `^`, `&`, `|`, `<` or `>` when the agent is a `.cmd` shim, and Codex tab
    arguments hold `<session-flags>` keys, so Codex tabs through Ori don't work with an npm (`.cmd`)
    Codex on Windows.
- The `handoff` tool and skill move a session's work to a new tab: the old session writes a brief to
  `~/.ide-agent-tabs/handoffs/`, the server opens the new tab, and the new session takes over and closes
  the old tab.
  - The brief holds notes, not instructions. The new session confirms with the user before anything
    destructive.
  - `close_tab` refuses the old tab until the takeover message and the old session's reply that it
    stopped both exist, within a 10-minute confirmation window. If the new tab fails to open, nothing
    closes. The brief and the record stay on disk.
  - `closeAfterHandoff` in `config.json` (`true` by default) controls the close. With `false`, the old
    tab stays open and `list_sessions` shows it with `handedOffTo`.
- Seven built-in agents. All seven are untested: they come from each CLI's documentation, and none is
  installed on the development machine. A table of what each agent gets is in
  [Agent support](mcp/README.md#agent-support).
  - Grok Build (`grok`): `grok`, positional prompt, `-m`. `--register grok` writes the
    `[mcp_servers.ide-agent-tabs]` table to `$GROK_HOME/config.toml` and the hooks to
    `$GROK_HOME/hooks/ide-agent-tabs.json`. Its hooks set `idle`, `busy` and `permission`, remind after a
    tool call and nudge at `Stop`. Its `idle_prompt` notification is an input idle signal, as in Claude
    Code. Ori launches it.
  - Pi (`pi`): `pi`, positional prompt, `--model`. `--register pi` writes `mcpServers.ide-agent-tabs` to
    `~/.pi/agent/mcp.json` with `timeout: 660`, `exposure: "direct"` and an `env` that forwards the tab
    variables. Pi gets no hooks, so it has no state. Ori launches it.
  - Hermes (`hermes`): `hermes chat -q <prompt>`, `-m`. `--register hermes` edits `config.yaml` under
    `HERMES_HOME` (an `env` map, `timeout: 660`, and shell hooks), and adds an allowlist entry for each
    of its own hook commands and nothing else. It never sets `hooks_auto_accept` or `HERMES_ACCEPT_HOOKS`.
    Hermes nudges only after a turn that edited code, through `pre_verify`, and counts the blocks against
    its `max_verify_nudges`. Ori launches it.
  - OpenCode (`opencode`): `opencode --prompt <prompt>`, `-m`. Its MCP registration was already there,
    now with `timeout: 660000` and no state. Ori launches it.
  - Qwen Code (`qwen`): `qwen -i <prompt>`, `-m`. `--register qwen` edits `~/.qwen/settings.json`: an
    `env` map, `timeout: 700000` and hooks. It reports `idle`, `busy` and `permission`, reminds after a
    prompt and a tool call, and nudges at `Stop`.
  - Goose (`goose`): `goose run -s -t <prompt>`, or `goose session` when there is no prompt, because
    `goose run -s` refuses to start without a message. `--model`. `--register goose` adds an extension
    with `timeout: 700` to `config.yaml`, and the hooks as an Open Plugins plugin in
    `~/.agents/plugins/ide-agent-tabs/`. Goose runs the hooks with `sh -c`, so Windows needs Git Bash.
  - Codex (local) (`codex-local`): the Codex tab arguments plus `--oss --local-provider ollama`, `-m`. It
    needs Ollama 0.13.4 or later, and needs no registration. Its hooks and state are Codex's.
  - Hook support for `grok`, `hermes`, `qwen` and `goose` in `agent-hook.mjs`, and the MCP client names
    `grok`, `qwen`, `goose` and `pi` map to their agents.
  - Icons: Grok Build uses the official SpaceXAI PNGs, embedded unchanged. Pi uses `pi.dev/favicon.svg`.
    Hermes uses the official `icon-master` (`.svg` and `-dark`). OpenCode uses its official light and
    dark square SVGs. Qwen Code uses the official #6D44E8 logo in both themes. Goose uses the official
    `goose.svg` (#101010) in both themes, which is hard to see on a dark theme. Codex (local) reuses the
    Codex icon.
  - `--agents` reports `hooks: null` for Codex, Pi and OpenCode, which get none.
  - New dependency: the `yaml` npm package (ISC), which edits the Hermes and Goose YAML and keeps
    comments and other keys.
- Two agents are not included. Crush's hooks cover only `PreToolUse`, it has no first-prompt flag, and
  its licence is FSL. Prime Agent runs its sessions in a daemon, and its documentation isn't researched.
  Ori can launch `prime-agent`, but there is no profile for it.
- `scripts/check.mjs` runs typecheck, tests, the bundle check, the plugin version check and both
  `claude plugin validate --strict` runs.
- `open_tab` and `handoff` take `focus`. `true` brings the new tab to the front; `false` opens it behind
  the current one where the host allows. The IDE `open` route takes `focus` too, and treats a missing
  value as `false`.
- The setting **Bring new agent tabs to the front** (`focusNewTabs` in `config.json`), under IDE tabs in
  VS Code (`ideAgentTabs.focusNewTabs`) and JetBrains: `auto` (default) follows the call's `focus`; `always` and
  `never` ignore it. With `auto`, the `new-tab` skill passes `focus: true` when the user asked
  for the tab, and the `handoff` skill only when the user asks to watch the new tab. The New Agent Tab
  button always brings its tab to the front.

## 0.5.3

Plugin and MCP server 0.5.3. The IDE extensions are unchanged.

### Fixed

- `open_tab` without `ide`, for a folder that no open project contains, opens the tab in the caller's own
  IDE when the caller runs in an Agent Tabs tab. Before, it opened in the most recently started IDE,
  which could be an unrelated window. The most recently started IDE stays the fallback when there is no
  caller IDE or it isn't running.

### Changed

- The `new-tab` and `message` skills prefer a subagent in the caller's session when the new session
  would work in the same folder or project and only its result is needed. Tabs stay the choice for
  other repositories, sessions the user works in, and handoffs that need a fresh session.

## 0.5.2

Plugin and MCP server 0.5.2. The IDE extensions are unchanged.

### Fixed

- The server and the sync hook no longer stop with `write EPIPE` when an editor CLI, an agent CLI or a
  terminal CLI exits before it reads its input.

## 0.5.1

Plugin and MCP server 0.5.1. VS Code extension 0.1.19. JetBrains plugin 0.4.2, unchanged.

### Changed

- The server instructions start with the tab tools and stay under Claude Code's 2,048-character cut
  with Jev on (1,965 characters); a test guards the limit.
- Each tool description says what it returns, when to use it, and when another tool fits better.
  `open_tab` says that it returns no agent output.
- IDE errors end with the next step for 401, 409, 503, a timeout and an unknown agent. On 401 the server
  rereads the registry and retries once when the endpoint holds a new token.
- An unread message gets one factual reminder instead of an order after every tool call.
- Results are compact JSON, and `list_ides` no longer returns `pid` or `startedAt`.
- `setup` and `update` run only when you type them. The `jev` skill description is shorter.
- `delegate` passes the sandbox or permission mode again on a follow-up, offers `--max-budget-usd`,
  reports a Claude run's cost, hands Codex work to the Codex plugin's `codex:codex-rescue` subagent,
  and suggests a subagent for a second Claude that needs nothing different.
- `update` reads the newest version from the marketplace clone.
- Skills name the MCP tools by their full callable names. `setup` carries the agent profile format and
  a Sonnet tier example for Jev.
- The server and its User-Agent report the version from `mcp/package.json`.
- The licence is proprietary. The READMEs say the repository is private.

### Added

- An eval suite in `claude-plugin/evals/`: trigger, negative and behavior cases for all six skills.
- `scripts/check-plugin-version.mjs`, which fails when `claude-plugin/` changed since the last release
  tag without a version bump and a CHANGELOG entry.
- A "Where it works" section in the README.

## 0.5.0

Plugin and MCP server 0.5.0. VS Code extension 0.1.18. JetBrains plugin 0.4.2.

Cross-agent messaging (`list_sessions`, `send_message`, `read_messages`, `wait_for_message`, the
`message` skill and the messaging hooks), Codex tabs, and IDE routing by the closest project.
