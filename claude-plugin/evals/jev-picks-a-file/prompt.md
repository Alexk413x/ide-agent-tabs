---
description: An explicit Jev request with listed options calls a jev_ tool.
tags: [trigger, mocked]
max_turns: 6
timeout_seconds: 240
allowed_tools: [Read, Glob, Grep, Skill]
---

Use Jev to pick which of these files most likely handles login: src/auth/session.ts, src/ui/theme.ts, src/db/migrate.ts.
