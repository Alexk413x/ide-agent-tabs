---
description: Asking to continue in a fresh session fires the handoff skill and calls the handoff tool with a brief.
tags: [trigger, behavior, mocked]
max_turns: 8
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
---

I've been fixing the login timeout on branch `fix/login-timeout`. The fix in `src/auth/session.ts` is done and its test in `test/auth.test.ts` passes; what's left is updating `CHANGELOG.md` and running the full suite. I just updated the Claude Code plugin, and only a new session loads it, so continue this work in a fresh session.
