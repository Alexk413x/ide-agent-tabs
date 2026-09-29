export const CHAR_DELAY_MS = 20;
export const ENTER_DELAY_MS = 300;

// Codex reads characters that arrive under 8 ms apart as a paste and turns a following Enter into a
// newline (paste_burst.rs), even 500 ms later on Windows, so the line is typed one character at a time.
export function typeLine(
  send: (data: string) => void,
  text: string,
  stillOpen: () => boolean,
  charDelayMs = CHAR_DELAY_MS,
  enterDelayMs = ENTER_DELAY_MS,
): void {
  const keys = [...text, '\r'];
  const press = (index: number) => {
    if (!stillOpen()) return;
    send(keys[index]!);
    if (index + 1 < keys.length) setTimeout(() => press(index + 1), index + 2 === keys.length ? enterDelayMs : charDelayMs);
  };
  press(0);
}
