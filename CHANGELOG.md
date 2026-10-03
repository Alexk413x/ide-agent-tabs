# Changelog

Each entry names the Claude Code plugin version (`claude-plugin/.claude-plugin/plugin.json`, which
`mcp/package.json` matches) and the bundled IDE versions when they change. Tags are
`ide-agent-tabs--v<version>`.

## 0.6.0

Plugin and MCP server 0.6.0. The VS Code and JetBrains profile sources add the Codex `Interrupt` hook;
the bundled IDE packages are not rebuilt yet.

### Fixed

- `send_message` wakes a fresh tab. A Claude tab reports idle at `SessionStart`, and a tab that
  `open_tab` starts without a prompt counts as idle 10 seconds after launch.
- The sender retries a queued wake-up every 15 seconds until the recipient reads the message, ends, or
  10 minutes pass. A lost wake line no longer needs `wait_for_message` to be retried.
- `read_messages` and `wait_for_message` return at most 40,000 characters and leave the rest unread. A
  cancelled read puts its messages back. A message file that holds no valid message moves to `bad/` and
  is reported instead of dropped.
- A headless agent started inside a tab no longer changes the tab's state. Only the agent session that
  owns the tab updates it; `/clear` and resume hand ownership on.
- `busy` with no hook activity for 15 minutes counts as idle. An interrupt sets idle: Claude
  `PostToolUseFailure` with `is_interrupt`, Claude `StopFailure`, and Codex `Interrupt`. A restarted
  server resets a dead server's state.
- A wake-up that fails on a cached host looks the host up again and retries once.
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

### Added

- The IDE sync and setup skill support Kiro (`kiro`), Positron (`positron`) and Trae (`trae`).
- `scripts/check.mjs` runs typecheck, tests, the bundle check, the plugin version check and both
  `claude plugin validate --strict` runs.

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
