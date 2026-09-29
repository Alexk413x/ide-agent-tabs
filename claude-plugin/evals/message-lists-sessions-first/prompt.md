---
description: message finds the recipient with list_sessions before send_message, then waits for the reply.
tags: [trigger, behavior, mocked]
max_turns: 10
timeout_seconds: 240
allowed_tools: [Read, Glob, Grep, Skill]
---

Send the Codex session that works on this repo a message asking it to run the test suite, and wait for its answer.
