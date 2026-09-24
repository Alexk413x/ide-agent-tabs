# Handoff — remaining steps

Status 2026-09-24: the plugin passes in the sandbox (`gradlew runIdeWithClaude`). Alex is installing
`build/distributions/claude-studio-tabs-0.1.0.zip` in his real Android Studio 2026.2.2 (build 262).
Delete this file when the steps below are done.

## Verified in the sandbox

- The button opens a new editor tab running `claude` in `pwsh`, with normal color, connected to the
  IDE (`/ide`).
- The endpoint answers 200 in under a second and starts `claude "<prompt>"`; the prompt arrives as
  one intact argument.
- The Claude Code plugin's `CLAUDE_CODE_SSE_PORT` and `ENABLE_IDE_INTEGRATION` reach the session.
- Refused: non-loopback addresses (connection refused; the built-in server binds 127.0.0.1), GET
  (405), `Origin` or `Referer` headers and a `Mozilla/5.0` User-Agent without `Origin` (the
  platform's 404), non-JSON content type (415), bad bodies and paths (400).

Not exercised live: the 503 answer when a modal dialog blocks the IDE.

## 1. Check the endpoint in the real IDE

```powershell
$ep = Get-Content "$HOME\.claude-studio-tabs\endpoint.json" | ConvertFrom-Json
Get-Process -Id $ep.pid   # must be the real Studio, not a sandbox
$body = @{ path = 'C:\Users\Alexk\Projects\Plugins\claude-studio-tabs'; prompt = 'Reply with exactly: endpoint OK. Do not run any tools.' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri $ep.url -ContentType 'application/json' -Body $body -UserAgent 'claude-studio-tabs'
```

Expect `ok: true`, a new editor tab, no focus change. Ask Alex to confirm the tab shows colors and
`/ide` connected. The button is on the main toolbar (terminal icon with an orange spark) and in Tools.

## 2. Switch `/new-tab` (after step 1 passes)

`~/.claude/commands/new-tab.md` opens a Windows Terminal tab with `wt.exe`. Change it so that when
`TERMINAL_EMULATOR` is `JetBrains-JediTerm` and `~/.claude-studio-tabs/endpoint.json` exists, it
POSTs `{path, prompt}` to the endpoint instead (non-browser User-Agent, `Content-Type:
application/json`), and falls back to `wt.exe` if the request fails. An optional first message
(`/new-tab <folder> -- <message>`) maps to `prompt`.

`~/.claude/scripts/new-studio-tab.ps1`, which the plan says to delete, is already gone.

## 3. Report

Message `accessibility-tools-15` with the result. It owns
`accessibility-tools/plans/claude-studio-tabs.md`; do not edit anything in accessibility-tools.
Nothing is pushed; the work is on branch `feat/initial-plugin`.
