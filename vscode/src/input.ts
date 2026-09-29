export const ENTER_DELAY_MS = 500;

// Agent TUIs treat an Enter that arrives in the same burst as typed text as a pasted newline, not a
// submit (Codex's paste_burst.rs holds that window for 120 ms), so Enter goes on its own after a pause.
export function typeLine(send: (data: string) => void, text: string, stillOpen: () => boolean, delayMs = ENTER_DELAY_MS): void {
  send(text);
  setTimeout(() => {
    if (stillOpen()) send('\r');
  }, delayMs);
}
