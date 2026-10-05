---
expect:
  id: string
---

{
  "resumed": false,
  "needsConfirm": true,
  "id": "01a10626-3892-7963-938c-a326a5769d94",
  "agent": "codex",
  "folder": "/work/app",
  "tokens": 84000,
  "size": "84k tokens",
  "age": "3h ago",
  "endedAt": "2026-09-29T06:00:00.000Z",
  "reasons": ["it ended past the 5-minute prompt cache window", "it holds over 50,000 tokens"],
  "message": "Not resumed: it ended past the 5-minute prompt cache window; it holds over 50,000 tokens. Resuming makes Codex re-read the full history, 84k tokens, at full input price. It ended 3h ago. Handoff is the cheaper option: a fresh session that starts from a short brief. Ask the user which they want, and call resume_tab again with confirm: true only after they agree to the cost."
}
