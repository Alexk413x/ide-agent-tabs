# System One judgments with Jev

Status: J0, J1 and J2 are built. J3 to J5 are plans.

This plan adds an optional TypeSafe Jev integration to Agent Tabs. Every agent that Agent Tabs opens or
delegates to can then ask Jev a question, not only Claude Code. The goal is to move the calls that
are really judgments off the large model, so that a session spends fewer large-model tokens and waits
less.

`docs/design.md`, section "Jev judgments", is the contract: settings, key, tools, ledger and command line. This file keeps the reasons, the evidence and the phases.

## What Jev is, and what it is not

Jev is a "System One" model. It reads text and returns one of three typed answers:

| Primitive | Returns | Use it for |
|---|---|---|
| Choice | One of up to 255 options, a probability for each, and a confidence | Pick one: an agent, a file, a next step |
| Noul | One probability of yes | Whether a condition holds |
| Score | A position on 2 to 10 described levels, with probabilities | How much, on a scale you define |

Jev writes no text, no code and no reasons. It cannot replace the model behind a coding agent.
TypeSafe's own docs say so. What it can replace is a large-model call whose output is a pick, a
yes or no, or a grade. Those calls are common in agent work:

- Which agent or model tier should take this task?
- Which of these 40 files matter for this question?
- Is this shell command about to delete, publish or spend something?
- Does this answer address what was asked, or is the task still open?
- Which of these review comments are about correctness?

TypeSafe reports 70 to 500 ms per call. Jev charges only for input tokens, at $0.042 per million, and
output tokens are free. A large-model call that makes the same pick takes several seconds and costs
far more.

## What earlier tests measured

Twenty small tests ran Jev on real app screens and code. The rules below come from those tests and
bind every tool in this plan.

1. **Narrow questions work and broad ones fail.** "Is this control unsafe?" scored "Transfer funds"
   the same as "Reset form". A Choice among six named consequences separated all 40 labels. "Does
   this code work?" could not tell working code from broken code. Ten narrow questions, one per kind
   of defect, found 9 of 10 bugs. When a question underperforms, split it before giving up.
2. **Scores are not calibrated.** Rank and triage with them. Never treat one as a verdict.
3. **Criteria that quote example labels match only those labels.** Describe the meaning instead.
4. **Items that explain each other belong in one request.** One request per screen got 22 of 22
   right. One request per control got 11 of 22.
5. **Less state is better.** Unrelated text distracts Jev and can flip right answers.
6. **Answers are stable when the input is stable.** Sort items and keep options in a fixed order.
7. **Jev can follow instructions placed in its state.** TypeSafe advises stating that risk in the criteria. Say the state is data, and never let a Jev answer alone approve anything.

## Design decisions

### One server, not two

The Jev tools live in `mcp/src/jev/` and the existing `ide-agent-tabs` MCP server serves them. An
earlier draft used a second server. That changed on 2026-09-28, once the server could be registered
with Codex, Gemini CLI, Copilot CLI and OpenCode (commit 7cef357):

- Every agent that has the tab tools gets the Jev tools too, with no second registration.
- With Jev off, the server lists no Jev tools, sends no Jev instructions and makes no network call. A
  session pays nothing for a feature it doesn't use.
- `design.md`'s Security section names the one outbound call.

The command line is a subcommand of the same bundle, `node mcp-server.mjs jev <tool>`, so the copy in
`~/.ide-agent-tabs/mcp/` that other agents register carries it too.

### The SDK

TypeSafe's JavaScript SDK, `@typesafe-ai/sdk`, pinned to exactly 0.6.0 (MIT, no dependencies, Node 20
or later). It retries 408, 429 and 5xx with backoff, honours `Retry-After`, and reads
`TYPESAFE_BASE_URL`, which lets the tests point it at a fake server on loopback.

### Why build this and not use an existing server

Community MCP servers already wrap the TypeSafe API (`itsmostafa/system-one-connector`, formerly `typesafe-mcp`, `codaaiteam/jev-mcp`,
`jkudish/jev-mcp`). TypeSafe ships a Claude Code skill, not an MCP server. The useful parts here are
Agent Tabs parts: registration with every agent, the tiers `jev_route` reads beside the agent profiles,
the key in the credential store that Python's `keyring` also reads, and the ledger. `jev_ask` is the same idea as `system-one-connector`'s
`evaluate`.

## How agents use Jev on their own

The owner asked for agents to use Jev when asked, and on their own when a step has a deterministic
shape: solid input and a closed set of options. Three layers carry that, from the most general to the
most specific:

1. **The server's instructions.** MCP lets a server send instructions to every client at connection.
   When Jev is on, they say when to reach for it and when not to. Support differs by client, as of
   2026-09-28: Gemini CLI and OpenCode add them to the system prompt. Copilot CLI supports them, and a
   flag, `--allow-all-mcp-server-instructions`, suggests they are gated by default. Codex uses them as
   the tool group's description, cut to 250 characters in its short summaries. So the first 250
   characters must carry the rule on their own.
2. **Tool descriptions.** Each tool's description repeats its own "use when" and "don't use when" in
   one or two sentences. This reaches clients that ignore server instructions.
3. **The `jev` skill** in the Claude Code plugin. Its description triggers on judgment-shaped steps,
   and its body teaches how to write a question Jev answers well, from the rules above.

### When an agent should reach for Jev

All three must hold:

- The answer is one of a set the agent can write down: options, yes or no, or levels it can describe.
- The agent holds the text the judgment rests on, and it fits in one request.
- Getting it slightly wrong is cheap, or the agent checks the result another way.

Typical cases: pick the file, test, agent or next step out of a list; filter or rank search results
before reading them; triage review comments, log lines or issues into named kinds; ask whether a
diff, answer or document meets each of several narrow conditions.

### When it should not

- The answer is text, code, a number, a count or a plan.
- Code can compute it: a regex, a parser, a lookup, arithmetic.
- The result is the final word on something that matters: a merge, a deletion, a permission, a
  verdict a person relies on. A probability ranks options. It isn't proof.
- The text is secret. Everything in a request leaves the machine.

## Phases

| Phase | What | Needs | Done when |
|---|---|---|---|
| J0 | The contract in `docs/design.md` | Nothing | Done |
| J1 | `mcp/src/jev/`: the key lookup, the client, `jev_status`, `jev_ask`, `jev_choose`, `jev_check`, `jev_rank`, `jev_route`, the ledger, the server instructions and the `jev` subcommand. Tests use a fake TypeSafe server on loopback | Nothing | `npm test` and `npm run build` pass; one live call with the real key |
| J2 | The `jev` skill, the `delegate` change and the `setup` step | J1 | Codex, from a tab, calls `jev_status` and gets an answer |
| J3 | A routing bench: about 30 past delegate tasks, each labelled with the right tier by the owner | J1 | Agreement and the `sure` share reported, and `sure` re-set from the data |
| J4 | `jev_gate` and an opt-in `PreToolUse` guard hook for Claude Code, which can only answer `ask`, never `allow` | A bench of 40 harmless and 40 destructive shell commands, written before the question is tuned | No destructive command under the threshold |
| J5 | A cost report: the same fixed tasks with and without Jev for routing and ranking | J3 | Tokens and wall time per task, both ways |

Each bench is written, and labelled by a person, before its tool is tuned. Some wordings in the
earlier tests were tuned on the cases they were scored on, and this plan counts those results as
indications only.

`jev_gate` waits for J4 because the earlier consequence question was tested on 40 app control
labels, not on shell commands, and its gap between harmless and destructive was narrow.

## What stays out

- Jev as a chat model or an agent profile. Jev can't hold a session.
- Jev writing text, code, commit messages or plans.
- Jev approving a permission, a delegate result or a merge.
- Any change to the HTTP API between the MCP server and the IDEs.
- A hard-coded list of models or tiers.

## Open decisions

1. Whether `jev_route` also picks a model inside Claude Code, for a subagent's `model`, or only an
   agent profile. The tiers already allow `claude:<model>`.
2. Whether the J4 guard hook covers only `Bash`, or also file writes outside the project.

## Sources

- TypeSafe docs index: https://docs.typesafe.ai/llms.txt
- Models, limits and pricing: https://docs.typesafe.ai/models.md
- JavaScript SDK: https://docs.typesafe.ai/sdk/javascript.md
- Jev and coding agents: https://docs.typesafe.ai/introduction/coding-agents.md
- LangChain, Jev for model routing and tool-call gating: https://www.langchain.com/blog/building-a-harness-with-jev
- Python `keyring`'s Windows layout: https://github.com/jaraco/keyring/blob/main/keyring/backends/Windows.py
