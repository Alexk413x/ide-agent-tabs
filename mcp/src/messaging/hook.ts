import { peekUnread } from './mailbox.js';
import { unreadReminder } from './notice.js';
import { isSessionId, updatePresence, withState, type PresenceFile, type SessionState } from './sessions.js';

export const HOOK_CLIS = ['claude', 'codex', 'gemini', 'copilot'] as const;
export type HookCli = (typeof HOOK_CLIS)[number];
export const MAX_NUDGES = 3;

interface Action {
  state?: SessionState;
  prompt?: boolean;
  remind?: boolean;
  stop?: boolean;
  notification?: boolean;
}

const BUSY: Action = { state: 'busy' };
const PROMPT: Action = { state: 'busy', prompt: true, remind: true };
const AFTER_TOOL: Action = { state: 'busy', remind: true };
const STOP: Action = { stop: true };

export const HOOK_EVENTS: Record<HookCli, Record<string, Action>> = {
  claude: { UserPromptSubmit: PROMPT, PostToolUse: AFTER_TOOL, Notification: { notification: true }, Stop: STOP },
  codex: { UserPromptSubmit: PROMPT, PermissionRequest: { state: 'permission' }, PostToolUse: AFTER_TOOL, Stop: STOP },
  gemini: { BeforeAgent: PROMPT, BeforeTool: BUSY, Notification: { notification: true }, AfterTool: AFTER_TOOL, AfterAgent: STOP },
  copilot: {
    userPromptSubmitted: { state: 'busy', prompt: true },
    preToolUse: BUSY,
    notification: { notification: true },
    postToolUse: AFTER_TOOL,
    agentStop: STOP,
  },
};

export const isHookCli = (cli: string): cli is HookCli => (HOOK_CLIS as readonly string[]).includes(cli);

function notificationState(input: Record<string, unknown>): SessionState | undefined {
  const type = [input.notification_type, input.notificationType, input.type].find((v) => typeof v === 'string') as string | undefined;
  if (type === 'permission_prompt' || type === 'ToolPermission') return 'permission';
  if (type === 'idle_prompt' || type === 'agent_idle') return 'idle';
  return undefined;
}

function contextOutput(cli: HookCli, event: string, text: string): object {
  return cli === 'copilot' ? { additionalContext: text } : { hookSpecificOutput: { hookEventName: event, additionalContext: text } };
}

function stopOutput(cli: HookCli, reason: string): object {
  return { decision: cli === 'gemini' ? 'deny' : 'block', reason };
}

export interface HookRun {
  cli: string;
  event: string;
  input: Record<string, unknown>;
  home: string;
  sessionId: string | undefined;
  now?: number;
}

export async function runHook(run: HookRun): Promise<object | undefined> {
  const { cli, event, home, sessionId } = run;
  if (!sessionId || !isSessionId(sessionId) || !isHookCli(cli)) return undefined;
  const action = HOOK_EVENTS[cli][event];
  if (!action) return undefined;
  const now = run.now ?? Date.now();
  const state = action.notification ? notificationState(run.input) : action.state;
  const unread = action.remind || action.stop ? await peekUnread(home, sessionId) : [];
  const reminder = unreadReminder(unread);

  let block = false;
  let remind = false;
  await updatePresence(home, sessionId, (current) => {
    const base: PresenceFile = current ?? { id: sessionId };
    if (action.stop) {
      const nudges = base.nudges ?? 0;
      block = reminder !== undefined && nudges < MAX_NUDGES;
      return block ? withState(base, 'busy', now, nudges + 1) : withState(base, 'idle', now, reminder === undefined ? 0 : nudges);
    }
    if (state === undefined) return current;
    const next = withState(base, state, now, action.prompt ? 0 : undefined);
    if (!action.remind) return next;
    const seen = new Set(base.reminded);
    remind = unread.some((m) => !seen.has(m.id));
    const { reminded: _, ...rest } = next;
    return unread.length ? { ...rest, reminded: unread.map((m) => m.id) } : rest;
  });

  if (action.stop) return block ? stopOutput(cli, `Agent Tabs kept this turn open. ${reminder}`) : undefined;
  if (remind && reminder !== undefined) return contextOutput(cli, event, reminder);
  return undefined;
}
