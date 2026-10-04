# Agent Tabs

Agent Tabs opens AI coding-agent sessions, such as Claude Code, Codex, Antigravity CLI, Copilot CLI, Gemini CLI,
Grok Build, Pi, Hermes, OpenCode, Qwen Code and Goose, in IDE editor tabs. You open a tab with one button. An agent opens, lists and closes tabs in any IDE
that runs on the same computer.

## Install

The repository is a Claude Code plugin marketplace. Add it and install the plugin:

```sh
claude plugin marketplace add Alexk413x/ide-agent-tabs
claude plugin install ide-agent-tabs@ide-agent-tabs
```

The repository is private, so installing needs read access to it: sign in to GitHub with an account
that has access, for example with `gh auth login`.

Then, in a Claude Code session, run `/ide-agent-tabs:setup`. The setup skill finds your IDEs, installs
the extension in VS Code and editors built on it, and gives you two one-time steps for each JetBrains
IDE. It also reports which agent CLIs it finds.

The plugin carries the IDE extensions and keeps them up to date. When you update the plugin, the next
Claude Code session updates the extension in each editor that has it. Each JetBrains IDE then offers
the new plugin as a normal plugin update.

## Requirements

- Windows, macOS or Linux. Windows is tested, Linux is tested in WSL, and macOS is tested with tmux
  only.
- Claude Code.
- Node.js 20 or later.
- An IDE: IntelliJ IDEA, Android Studio or another JetBrains IDE at build 262.10315 or later (2026.2.2),
  or VS Code 1.100 or later and editors built on it, such as Cursor, Windsurf, VSCodium, Antigravity, Kiro,
  Positron and Trae.
- Or, without an IDE, a terminal: Windows Terminal or WezTerm on Windows; Ghostty, iTerm2, kitty, WezTerm
  or tmux on macOS; Ghostty, kitty, WezTerm or tmux on Linux. iTerm2 and Ghostty on macOS need the
  Automation permission that macOS asks for on the first tab.
- At least one agent CLI, such as Claude Code, Codex, Antigravity CLI, Copilot CLI, Gemini CLI, Grok Build, Pi,
  Hermes, OpenCode, Qwen Code or Goose. Codex (local) runs Codex with a local Ollama model and needs
  Ollama 0.13.4 or later.

## Where it works

The plugin needs a computer with IDEs or a terminal, and agent CLIs, that it can reach.

| Surface | What works |
|---|---|
| Claude Code: terminal, IDE extensions, desktop app | Everything. |
| Cowork, local session in the desktop app | The MCP server runs on your computer, so opening, listing and closing tabs, messaging and the Jev tools reach your IDEs and terminals. `delegate`, `setup` and `update` run their shell steps in Cowork's Linux VM, which has no IDE and may lack the agent CLIs. |
| claude.ai chat, cloud Cowork | Nothing. These surfaces don't run the local MCP server or the hooks, and have no local shell. |

Where local Cowork runs a plugin hook is not documented. The `SessionStart` sync always exits 0, and
the messaging hooks do nothing outside an agent tab, so a hook that runs in the VM changes nothing.

## Parts

| Part | Folder | What it does |
|---|---|---|
| JetBrains plugin | [`jetbrains/`](jetbrains/) | Adds the **New Agent Tab** button and a local HTTP API to IntelliJ IDEA, Android Studio and other JetBrains IDEs. |
| VS Code extension | [`vscode/`](vscode/) | Adds the same button and API to VS Code and editors built on it. |
| MCP server | [`mcp/`](mcp/) | Gives an agent tools to list IDEs, to open, list and close tabs, to message other sessions, and to hand its work to a new tab. |
| Claude Code plugin | [`claude-plugin/`](claude-plugin/) | Bundles the MCP server, the skills and the IDE extensions. The marketplace file is in `.claude-plugin/`. |

## Models, Ori and handoff

- **Model.** `open_tab` takes a `model` and passes it with the agent's model flag: `--model` for Claude
  Code, Antigravity CLI, Copilot CLI, Pi and Goose, and `-m` for Codex, Gemini CLI, Grok Build, Hermes,
  OpenCode, Qwen Code and Codex (local). A custom profile sets `modelFlag` in
  `~/.ide-agent-tabs/agents.json`. A model for a profile without one is an error.
- **Ori.** With Ori installed, `open_tab` takes `via: "ori"` to start a supported agent with
  `ori <agent>`. Ori bills model usage through OpenRouter. The setting **Launch through OpenRouter (Ori)**
  (`launchVia` in `config.json`) makes it the default. It is off by default and shows only when Ori is
  detected. See [Model and Ori](mcp/README.md#model-and-ori).
- **Handoff.** The `handoff` tool and skill move a session's work to a new tab with a written brief, then
  close the old tab once both sessions confirm. The `closeAfterHandoff` setting, on by default, controls
  the close. See [Handoff](mcp/README.md#handoff).

## Agents

The built-in agents are Claude Code, Codex, Antigravity CLI, Copilot CLI, Gemini CLI, Grok Build, Pi,
Hermes, and the agents built for local models: OpenCode, Qwen Code, Goose and Codex (local). Grok Build,
Pi, Hermes, OpenCode, Qwen Code, Goose and Codex (local) come from each CLI's documentation and haven't had
a live test. Crush and Prime Agent aren't included. See [Agent support](mcp/README.md#agent-support) for
what each agent gets and why.

## Design

[docs/design.md](docs/design.md) describes the registry, the HTTP API, agent profiles, distribution
and possible future work. Change the design before you change the protocol.

## Checks

Before you commit, run `node scripts/check.mjs` from the repo root. It runs the MCP typecheck and tests,
confirms `claude-plugin/dist` matches a fresh bundle, checks the plugin version, runs
`claude plugin validate --strict` and the mod tests, then the JetBrains and VS Code tests. Gradle runs
without its build cache, so a stale cache entry can't fail or skip a test. The JetBrains tests need JDK 25
or later, found the way `scripts/pack-ides.mjs` finds it. Pass `--skip-ide` to skip the IDE tests, or
`--skip-tests` to skip every test.

## License

Proprietary. See [LICENSE](LICENSE). The bundled third-party licenses are in
`claude-plugin/dist/THIRD_PARTY_NOTICES.txt`.
