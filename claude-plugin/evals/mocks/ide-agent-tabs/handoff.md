---
expect:
  path: string
---

{
  "handoff": "h-0123456789ab",
  "brief": "/home/user/.ide-agent-tabs/handoffs/h-0123456789ab.md",
  "newTab": "tab-9c2e",
  "ide": "vscode-4242-ab12cd34",
  "agent": "claude",
  "oldTab": "tab-self-01",
  "closeAfter": true,
  "confirmBy": "2026-09-29T09:10:00.000Z",
  "next": "Call wait_for_message with from set to tab-9c2e and timeout 600; wait again if it returns empty before 2026-09-29T09:10:00.000Z. When the takeover message arrives, finish only the current step, so no command runs and no file is half-written, then call send_message to the sender with replyTo set to the message id and text \"stopped\", end your turn, and do nothing more on this work. The new session then closes this tab. If no takeover message comes by 2026-09-29T09:10:00.000Z, tell your user the new session never confirmed; this tab stays open and keeps the work, and a later takeover message needs your user's OK."
}
