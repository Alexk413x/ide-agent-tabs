# Changelog

Each entry names the Claude Code plugin version (`claude-plugin/.claude-plugin/plugin.json`, which
`mcp/package.json` matches) and the bundled IDE versions when they change. Tags are
`ide-agent-tabs--v<version>`.

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
