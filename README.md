# Agent Tabs

Agent Tabs opens AI coding-agent sessions, such as Claude Code, Codex, Gemini CLI and Copilot CLI,
in IDE editor tabs. You open a tab with one button. An agent opens, lists and closes tabs in any IDE
that runs on the same computer.

## Install

The repository is a Claude Code plugin marketplace. Add it and install the plugin:

```sh
claude plugin marketplace add Alexk413x/ide-agent-tabs
claude plugin install ide-agent-tabs@ide-agent-tabs
```

Then, in a Claude Code session, run `/ide-agent-tabs:setup`. The setup skill finds your IDEs, installs
the extension in VS Code and editors built on it, and gives you two one-time steps for each JetBrains
IDE. It also reports which agent CLIs it finds.

The plugin carries the IDE extensions and keeps them up to date. When you update the plugin, the next
Claude Code session updates the extension in each editor that has it. Each JetBrains IDE then offers
the new plugin as a normal plugin update.

## Requirements

- Windows, macOS or Linux. Windows is tested, Linux is tested in WSL, and macOS is untested.
- Claude Code.
- Node.js 20 or later.
- An IDE: IntelliJ IDEA, Android Studio or another JetBrains IDE at build 262.10315 or later (2026.2.2),
  or VS Code 1.100 or later and editors built on it, such as Cursor, Windsurf, VSCodium and Antigravity.
- At least one agent CLI, such as Claude Code, Codex, Gemini CLI or Copilot CLI.

## Parts

| Part | Folder | What it does |
|---|---|---|
| JetBrains plugin | [`jetbrains/`](jetbrains/) | Adds the **New Agent Tab** button and a local HTTP API to IntelliJ IDEA, Android Studio and other JetBrains IDEs. |
| VS Code extension | [`vscode/`](vscode/) | Adds the same button and API to VS Code and editors built on it. |
| MCP server | [`mcp/`](mcp/) | Gives an agent tools to list IDEs and to open, list and close tabs. |
| Claude Code plugin | [`claude-plugin/`](claude-plugin/) | Bundles the MCP server, the skills and the IDE extensions. The marketplace file is in `.claude-plugin/`. |

## Design

[docs/design.md](docs/design.md) describes the registry, the HTTP API, agent profiles, distribution
and the plan for each part. Change the design before you change the protocol.

## License

[MIT](LICENSE)
