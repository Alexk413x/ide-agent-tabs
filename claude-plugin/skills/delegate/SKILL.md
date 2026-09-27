---
name: delegate
description: Hand a task, review, or second opinion to another agent CLI (Codex, Gemini CLI, Copilot CLI, OpenCode, or a second Claude) in headless mode and bring its answer back. Use when the user asks to "ask Codex", "have Gemini look at this", "get a second opinion from another model", or to delegate work to another agent.
argument-hint: "[codex|gemini|copilot|opencode|claude] <task>"
---

Run another agent CLI once, without a terminal UI, and report what it returns. The other agent works on
its own; you stay responsible for checking its answer.

Arguments: `$ARGUMENTS`

## 1. Pick the agent

- Use the agent named in the arguments. If none is named, ask which one, and list the installed ones.
- Check that the CLI is installed: `command -v <cli>` in Bash, or `Get-Command <cli>` in PowerShell. If
  it isn't, say so and stop.
- For Codex, prefer OpenAI's Codex plugin when its commands are available: `/codex:review` for reviews
  and `/codex:rescue` for delegated work. It manages background jobs and follow-ups for you. Use the
  steps below only when that plugin isn't installed or the user asks for `codex exec` directly.
- Before the first run of a CLI in this session, run `<cli> --help` (and `<cli> exec --help` for Codex)
  and confirm the flags in the table below. CLI flags change between releases. The installed help wins
  over this table.

## 2. Pick the mode

| Mode | Use for | Rule |
|---|---|---|
| Read-only (default) | Reviews, questions, second opinions, plans | The agent can read files but not change them. Safe to run next to your own work. |
| Write | Implementing or fixing something | Run in a separate git worktree, or ask the user before letting it write to the current tree. |

Never use a mode that turns off the agent's sandbox or approvals unless the user asks for it in this
conversation.

## 3. Write the prompt to a file

Make a run folder and write a self-contained prompt. The other agent has none of your context.

```sh
RUN="${TMPDIR:-/tmp}/ide-agent-tabs/delegate/$(date +%Y%m%d-%H%M%S)-<agent>"
mkdir -p "$RUN"
```

Write `$RUN/prompt.md` with the Write tool. Include the goal, the absolute paths of the files that
matter, the constraints, and the exact shape of the answer you want back. Never put the prompt on the
command line: long prompts break shell quoting, especially on Windows.

## 4. Run it

Pick the command for the agent and mode. `<dir>` is the absolute path of the repository to work in.

| Agent | Read-only | Write | Final answer | Session id for follow-ups |
|---|---|---|---|---|
| Codex | `codex exec -s read-only -C "<dir>" -o "$RUN/result.md" --json - < "$RUN/prompt.md" > "$RUN/events.jsonl"` | Same, with `-s workspace-write --worktree` | `$RUN/result.md` | `thread_id` of the `thread.started` event in `events.jsonl` |
| Claude | `claude -p --output-format json --permission-mode plan < "$RUN/prompt.md" > "$RUN/result.json"` | Same, with `--permission-mode acceptEdits`, run from a worktree | `.result` in `result.json` | `.session_id` in `result.json` |
| Gemini CLI | `gemini -p "Follow the instructions on stdin." --output-format json < "$RUN/prompt.md" > "$RUN/result.json"` | Same, with `--approval-mode auto_edit`, run from a worktree | `.response` in `result.json` | Not reliable in headless mode. Treat each run as new. |
| Copilot CLI | Check `copilot --help` for the prompt, tool-permission and output flags | Check `copilot --help` | Its output | Treat each run as new. Resume has open bugs on Windows. |
| OpenCode | Check `opencode run --help` for the prompt, model and `--format json` flags | Check `opencode run --help` | Its output | `opencode run -c` continues the last session |

Add `--skip-git-repo-check` to Codex when `<dir>` isn't a git repository. Leave the model flag off unless
the user names a model: each CLI's default follows the user's own login and plan.

Agents often take several minutes, and Codex can take a minute or two just to start. Run the command
with the Bash tool's `run_in_background` option unless you expect it to finish in under a minute. You get
a notification when it exits. Don't poll in a loop.

## 5. Check the result

- A zero exit code isn't enough. The final-answer file must exist and be non-empty. Some CLIs exit 0
  after doing nothing.
- On failure, read the end of `events.jsonl`, `result.json` or the command's stderr, and report the
  error as it appears.
- For Write mode, run `git -C <worktree> status` and `git -C <worktree> diff --stat` to see what changed.

## 6. Report

- Lead with the agent's answer or verdict, in a few lines. Quote the parts that matter.
- Say which agent and mode you used, and give the run folder so the user can read everything.
- Check factual claims about the code before repeating them as true. Flag anything you couldn't check.
- Don't apply changes from a write-mode worktree to the user's tree without asking.
- Save `$RUN/meta.json` with `agent`, `mode`, `dir`, and the session id, so a follow-up can find it.

## Follow-ups

When the user wants to continue with the same agent, read the newest matching `meta.json`, write the
follow-up to `$RUN/followup.md`, and resume the session:

- Codex: `codex exec resume <thread_id> -o "$RUN/result-2.md" - < "$RUN/followup.md"`
- Claude: `claude -p --output-format json --resume <session_id> < "$RUN/followup.md"`
- Others: start a new run, and include the earlier answer in the prompt.
