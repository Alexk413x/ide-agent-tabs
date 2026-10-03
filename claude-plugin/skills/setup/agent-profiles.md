# Agent profiles

Source: the "Agent profiles" section of `docs/design.md` in the Agent Tabs repository. Keep the two in
step.

A profile says how to start one agent CLI. Every IDE uses the same built-in profiles:

| Name | Label | Command | First prompt |
|---|---|---|---|
| `claude` | Claude Code | `claude` | positional |
| `codex` | Codex | `codex` and fixed `args` that give the tab its Agent Tabs server and messaging hooks | positional |
| `agy` | Antigravity CLI | `agy` | `-i <prompt>` |
| `copilot` | Copilot CLI | `copilot` | `-i <prompt>` |
| `gemini` | Gemini CLI | `gemini` | `-i <prompt>` |

A profile in `agents.json` with the same name overrides a built-in one. An `agents.json` profile named
`codex` replaces the built-in `args` too, so its tabs lose messaging unless it copies them.

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
- `env`: environment variables for the session. The caller's `env` wins on a clash.
- `icon`: optional path to an SVG file for menus.

A profile is `installed` when its command is on the IDE's `PATH`. On Windows, the IDE also looks for
`.exe`, `.cmd`, `.bat` and `.ps1` files, because npm installs CLIs as `.cmd` and `.ps1` shims.
