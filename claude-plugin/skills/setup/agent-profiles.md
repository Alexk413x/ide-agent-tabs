# Agent profiles

Source: the "Agent profiles" section of `docs/design.md` in the Agent Tabs repository. Keep the two in
step.

A profile says how to start one agent CLI. Every IDE uses the same built-in profiles:

| Name | Label | Command | First prompt | Model flag |
|---|---|---|---|---|
| `claude` | Claude Code | `claude` | positional | `--model` |
| `codex` | Codex | `codex` and fixed `args` that give the tab its Agent Tabs server and messaging hooks | positional | `-m` |
| `agy` | Antigravity CLI | `agy` | `-i <prompt>` | `--model` |
| `copilot` | Copilot CLI | `copilot` | `-i <prompt>` | `--model` |
| `gemini` | Gemini CLI | `gemini` | `-i <prompt>` | `-m` |
| `grok` | Grok Build | `grok` | positional | `-m` |
| `pi` | Pi | `pi` | positional | `--model` |
| `hermes` | Hermes | `hermes chat` | `-q <prompt>` | `-m` |
| `opencode` | OpenCode | `opencode` | `--prompt <prompt>` | `-m` |
| `qwen` | Qwen Code | `qwen` | `-i <prompt>` | `-m` |
| `goose` | Goose | `goose run -s`, or `goose session` when there is no prompt | `-t <prompt>` | `--model` |
| `codex-local` | Codex (local) | `codex`, the `codex` profile's fixed `args`, then `--oss --local-provider ollama` | positional | `-m` |

The `grok`, `pi`, `hermes`, `opencode`, `qwen`, `goose` and `codex-local` profiles come from each CLI's
documentation and are untested. Codex (local) needs Ollama 0.13.4 or later. Lists of agents use this
order, then custom profiles.

A profile in `agents.json` with the same name overrides a built-in one. An `agents.json` profile named
`codex` replaces the built-in `args` too, so its tabs lose messaging unless it copies them.

A tab whose command is `claude` also gets `--settings ~/.ide-agent-tabs/mcp/claude-tab-settings.json` after
the profile's `args`, so an `agents.json` override of `claude` keeps the tab's hooks. When a profile or
request passes its own `--settings`, the tab gets one generated file that merges those settings with the
tab's hooks instead.

Add or override profiles in `~/.ide-agent-tabs/agents.json`:

```json
{
  "opencode": {
    "label": "OpenCode",
    "command": "opencode",
    "args": ["--model", "<provider>/<model>"],
    "promptFlag": "--prompt",
    "env": {}
  }
}
```

- `command`: the executable, found on the shell's `PATH`.
- `args`: arguments before the caller's `args`.
- `promptFlag`: the flag placed before the prompt. Leave it out when the prompt is positional. With no
  prompt, neither the flag nor a prompt is passed.
- `modelFlag`: the flag placed before a model that `open_tab` passes. Without it, a model for this
  profile is an error.
- `env`: environment variables for the session. The caller's `env` wins on a clash.
- `icon`: optional path to an SVG file for menus.

A profile is `installed` when its command is on the IDE's `PATH`. On Windows, the IDE also looks for
`.exe`, `.cmd`, `.bat` and `.ps1` files, because npm installs CLIs as `.cmd` and `.ps1` shims.
