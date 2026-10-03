import { peekUnread } from './mailbox.js';
import { unreadReminder } from './notice.js';
import { isSessionId, updatePresence, withState, type PresenceFile, type SessionState } from './sessions.js';

export const HOOK_CLIS = ['claude', 'codex', 'gemini', 'copilot', 'agy'] as const;
export type HookCli = (typeof HOOK_CLIS)[number];
export const MAX_NUDGES = 3;

interface Action {
  start?: boolean;
  state?: SessionState;
  prompt?: boolean;
  remind?: boolean;
  stop?: boolean;
  notification?: boolean;
  failure?: boolean;
  invocation?: boolean;
}

const BUSY: Action = { state: 'busy' };
const PROMPT: Action = { state: 'busy', prompt: true, remind: true };
const AFTER_TOOL: Action = { state: 'busy', remind: true };
const STOP: Action = { stop: true };
const STARTED: Action = { start: true, state: 'idle', remind: true };
const TOOL_FAILED: Action = { failure: true, remind: true };
const INVOCATION: Action = { invocation: true, state: 'busy', remind: true };

export const HOOK_EVENTS: Record<HookCli, Record<string, Action>> = {
  claude: {
    SessionStart: STARTED,
    UserPromptSubmit: PROMPT,
    PostToolUse: AFTER_TOOL,
    PostToolUseFailure: TOOL_FAILED,
    Notification: { notification: true },
    Stop: STOP,
    StopFailure: { state: 'idle' },
  },
  codex: { UserPromptSubmit: PROMPT, PermissionRequest: { state: 'permission' }, PostToolUse: AFTER_TOOL, Stop: STOP, Interrupt: { state: 'idle' } },
  gemini: { BeforeAgent: PROMPT, BeforeTool: BUSY, Notification: { notification: true }, AfterTool: AFTER_TOOL, AfterAgent: STOP },
  copilot: {
    sessionStart: STARTED,
    userPromptSubmitted: { state: 'busy', prompt: true },
    preToolUse: BUSY,
    notification: { notification: true },
    postToolUse: AFTER_TOOL,
    agentStop: STOP,
  },
  agy: { PreInvocation: INVOCATION, PostToolUse: BUSY, Stop: STOP },
};

export const isHookCli = (cli: string): cli is HookCli => (HOOK_CLIS as readonly string[]).includes(cli);

function notificationState(input: Record<string, unknown>): SessionState | undefined {
  const type = [input.notification_type, input.notificationType, input.type].find((v) => typeof v === 'string') as string | undefined;
  if (type === 'permission_prompt' || type === 'ToolPermission') return 'permission';
  if (type === 'idle_prompt' || type === 'agent_idle') return 'idle';
  return undefined;
}

function contextOutput(cli: HookCli, event: string, text: string): object {
  if (cli === 'agy') return { injectSteps: [{ ephemeralMessage: text }] };
  return cli === 'copilot' ? { additionalContext: text } : { hookSpecificOutput: { hookEventName: event, additionalContext: text } };
}

function stopOutput(cli: HookCli, reason: string): object {
  return { decision: cli === 'gemini' ? 'deny' : cli === 'agy' ? 'continue' : 'block', reason };
}

const OWNED_CLIS: readonly HookCli[] = ['claude', 'copilot'];

// Only CLIs whose start hook fires again when the tab switches sessions can hand ownership on; elsewhere a
// new session id would lock the tab out of its own hooks.
function agentSession(cli: HookCli, input: Record<string, unknown>): string | undefined {
  if (!OWNED_CLIS.includes(cli)) return undefined;
  const id = [input.session_id, input.sessionId].find((v) => typeof v === 'string' && v !== '');
  return id as string | undefined;
}

// A headless agent started from inside a tab inherits IDE_AGENT_TABS_ID, so its hooks name the tab too; only
// the agent session that claimed the tab first may change it, until a clear or resume in the tab hands it on.
function ownedBy(cli: HookCli, base: PresenceFile, action: Action, input: Record<string, unknown>): string | undefined | false {
  const session = agentSession(cli, input);
  if (session === undefined || base.owner === undefined || base.owner === session) return session;
  return action.start && input.source !== undefined && input.source !== 'startup' ? session : false;
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
  const state = action.notification ? notificationState(run.input) : action.failure ? (run.input.is_interrupt === true ? 'idle' : 'busy') : action.state;
  const unread = action.remind || action.stop ? await peekUnread(home, sessionId) : [];
  const reminder = unreadReminder(unread);

  let block = false;
  let remind = false;
  let foreign = false;
  await updatePresence(home, sessionId, (current) => {
    const owned = ownedBy(cli, current ?? { id: sessionId }, action, run.input);
    if (owned === false) {
      foreign = true;
      return current;
    }
    const base: PresenceFile = { ...(current ?? { id: sessionId }), ...(owned !== undefined ? { owner: owned } : {}) };
    if (action.stop) {
      const nudges = base.nudges ?? 0;
      block = reminder !== undefined && nudges < MAX_NUDGES;
      return block ? withState(base, 'busy', now, nudges + 1) : withState(base, 'idle', now, reminder === undefined ? 0 : nudges);
    }
    if (state === undefined) return owned === undefined || current?.owner === owned ? current : base;
    const prompt = action.prompt || (action.invocation && run.input.invocationNum === 0);
    const next = withState(base, state, now, prompt ? 0 : undefined);
    if (!action.remind) return next;
    const seen = new Set(base.reminded);
    remind = unread.some((m) => !seen.has(m.id));
    const { reminded: _, ...rest } = next;
    return unread.length ? { ...rest, reminded: unread.map((m) => m.id) } : rest;
  });

  if (foreign) return undefined;
  if (action.stop) return block ? stopOutput(cli, `Agent Tabs kept this turn open. ${reminder}`) : undefined;
  if (remind && reminder !== undefined) return contextOutput(cli, event, reminder);
  return undefined;
}
