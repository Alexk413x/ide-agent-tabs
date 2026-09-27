# Agent Tabs

Agent Tabs opens AI coding-agent sessions, such as Claude Code, Codex, Gemini CLI and Copilot CLI,
in IDE editor tabs. You open a tab with one button. An agent opens, lists and closes tabs in any IDE
that runs on the same computer.

## Parts

| Part | Folder | What it does |
|---|---|---|
| JetBrains plugin | [`jetbrains/`](jetbrains/) | Adds the **New Agent Tab** button and a local HTTP API to Android Studio and other JetBrains IDEs. |
| VS Code extension | [`vscode/`](vscode/) | Adds the same button and API to VS Code. |
| MCP server | [`mcp/`](mcp/) | Gives an agent tools to list IDEs and to open, list and close tabs. |
| Claude Code plugin | [`claude-plugin/`](claude-plugin/) | Bundles the MCP server and the skills, such as `delegate` and `setup`. The marketplace file is in `.claude-plugin/`. |

## Install

The repository is a Claude Code plugin marketplace. Add it and install the plugin:

```sh
claude plugin marketplace add Alexk413x/ide-agent-tabs
claude plugin install ide-agent-tabs@ide-agent-tabs
```

Then, in a Claude Code session, run `/ide-agent-tabs:setup`. The setup skill finds your IDEs and
installs each extension, and reports which agent CLIs it finds.

## Design

[docs/design.md](docs/design.md) describes the registry, the HTTP API, agent profiles, releases and
the plan for each part. Change the design before you change the protocol.

## License

[MIT](LICENSE)
