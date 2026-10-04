import { peekUnread } from './mailbox.js';
import { unreadReminder } from './notice.js';
import { effectiveState, isEffort, isModDriven, isModel, isSessionId, readPresence, updatePresence, withState, type PresenceFile, type SessionState } from './sessions.js';

export const HOOK_CLIS = ['claude', 'codex', 'gemini', 'copilot', 'agy', 'grok', 'hermes', 'qwen', 'goose'] as const;
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
  inputIdle?: boolean;
}

const BUSY: Action = { state: 'busy' };
const PROMPT: Action = { state: 'busy', prompt: true, remind: true };
const AFTER_TOOL: Action = { state: 'busy', remind: true };
const STOP: Action = { stop: true };
const STARTED: Action = { start: true, state: 'idle', remind: true };
const TOOL_FAILED: Action = { failure: true, remind: true };
const INVOCATION: Action = { invocation: true, state: 'busy', remind: true };
const SETTLED: Action = { state: 'idle' };
const PERMISSION: Action = { state: 'permission' };

export const HOOK_EVENTS: Record<HookCli, Record<string, Action>> = {
  claude: {
    SessionStart: { ...STARTED, inputIdle: false },
    UserPromptSubmit: { ...PROMPT, inputIdle: false },
    PostToolUse: AFTER_TOOL,
    PostToolUseFailure: TOOL_FAILED,
    Notification: { notification: true },
    Stop: { ...STOP, inputIdle: false },
    StopFailure: { state: 'idle', inputIdle: false },
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
  grok: {
    UserPromptSubmit: { state: 'busy', prompt: true, inputIdle: false },
    PreToolUse: BUSY,
    PostToolUse: AFTER_TOOL,
    Notification: { notification: true },
    Stop: { ...STOP, inputIdle: false },
    StopCancelled: { ...SETTLED, inputIdle: false },
    StopFailure: { ...SETTLED, inputIdle: false },
  },
  hermes: {
    pre_llm_call: PROMPT,
    post_tool_call: BUSY,
    pre_approval_request: PERMISSION,
    post_approval_response: BUSY,
    pre_verify: STOP,
    on_session_end: SETTLED,
  },
  qwen: { UserPromptSubmit: PROMPT, PreToolUse: BUSY, PostToolUse: AFTER_TOOL, PermissionRequest: PERMISSION, Notification: { notification: true }, Stop: STOP },
  goose: { UserPromptSubmit: { state: 'busy', prompt: true }, PostToolUse: BUSY, Stop: STOP },
};

// Claude Code and Grok Build send idle_prompt once the input has sat unused after a turn end.
const INPUT_IDLE_CLIS: readonly HookCli[] = ['claude', 'grok'];

export const isHookCli = (cli: string): cli is HookCli => (HOOK_CLIS as readonly string[]).includes(cli);

function notificationState(input: Record<string, unknown>): SessionState | undefined {
  const type = [input.notification_type, input.notificationType, input.type].find((v) => typeof v === 'string') as string | undefined;
  if (type === 'permission_prompt' || type === 'ToolPermission' || type === 'elicitation_dialog') return 'permission';
  if (type === 'idle_prompt') return 'idle';
  return undefined;
}

function contextOutput(cli: HookCli, event: string, text: string): object {
  if (cli === 'agy') return { injectSteps: [{ ephemeralMessage: text }] };
  if (cli === 'hermes') return { context: text };
  return cli === 'copilot' ? { additionalContext: text } : { hookSpecificOutput: { hookEventName: event, additionalContext: text } };
}

function stopOutput(cli: HookCli, reason: string): object {
  return { decision: cli === 'gemini' ? 'deny' : cli === 'agy' ? 'continue' : 'block', reason };
}

const SESSION_FIELDS = ['session_id', 'sessionId', 'conversationId'];
const IN_TURN: readonly SessionState[] = ['busy', 'permission'];

function agentSession(input: Record<string, unknown>): string | undefined {
  return SESSION_FIELDS.map((f) => input[f]).find((v): v is string => typeof v === 'string' && v !== '');
}

// A headless agent started from inside a tab inherits IDE_AGENT_TABS_ID, so its hooks name the tab too. Such a
// child runs inside the tab agent's turn, while a session switch in the tab (/clear, /new, resume) happens
// between turns, so a new agent session takes the tab over only while it is not mid-turn. That needs no start
// hook, which Antigravity CLI lacks and Codex fires only at the first turn.
function ownedBy(cli: HookCli, base: PresenceFile, action: Action, input: Record<string, unknown>, now: number): string | undefined | false {
  if (base.agent !== undefined && isHookCli(base.agent) && base.agent !== cli) return false;
  const session = agentSession(input);
  if (session === undefined || base.owner === undefined || base.owner === session) return session;
  if (action.start && input.source !== undefined && input.source !== 'startup') return session;
  return IN_TURN.includes(effectiveState(base, now)) ? false : session;
}

// A turn end comes while the user may already be typing the next prompt; the idle_prompt notification of
// Claude Code and Grok Build is the one signal that the input has sat unused. A session that just started has no typing yet.
function inputIdleAfter(cli: HookCli, action: Action, input: Record<string, unknown>): boolean | undefined {
  if (action.notification) return INPUT_IDLE_CLIS.includes(cli) && notificationState(input) === 'idle' ? true : undefined;
  if (action.start && action.inputIdle !== undefined) return input.source === 'startup';
  return action.inputIdle;
}

export function reportedModel(input: Record<string, unknown>): Pick<PresenceFile, 'model' | 'effort'> {
  const model = [input.model, input.modelName].find((v): v is string => typeof v === 'string' && isModel(v));
  const level = (v: unknown) => (typeof v === 'object' && v !== null ? (v as { level?: unknown }).level : v);
  const effort = [level(input.effort), input.reasoning_effort].find((v): v is string => typeof v === 'string' && isEffort(v));
  return { ...(model !== undefined ? { model } : {}), ...(effort !== undefined ? { effort } : {}) };
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
  const before = await readPresence(home, sessionId);
  if (before && isModDriven(before, now)) return undefined;
  const inputIdle = inputIdleAfter(cli, action, run.input);
  const state = action.notification ? notificationState(run.input) : action.failure ? (run.input.is_interrupt === true ? 'idle' : 'busy') : action.state;
  const unread = action.remind || action.stop ? await peekUnread(home, sessionId) : [];
  const reminder = unreadReminder(unread);
  const reported = reportedModel(run.input);

  let block = false;
  let remind = false;
  let foreign = false;
  await updatePresence(home, sessionId, (current) => {
    const owned = current && isModDriven(current, now) ? false : ownedBy(cli, current ?? { id: sessionId }, action, run.input, now);
    if (owned === false) {
      foreign = true;
      return current;
    }
    const base: PresenceFile = {
      ...(current ?? { id: sessionId }),
      ...(owned !== undefined ? { owner: owned } : {}),
      ...(inputIdle !== undefined ? { inputIdle } : {}),
      ...reported,
    };
    const learned = (Object.keys(reported) as (keyof typeof reported)[]).some((k) => current?.[k] !== reported[k]);
    if (action.stop) {
      const nudges = base.nudges ?? 0;
      block = reminder !== undefined && nudges < MAX_NUDGES;
      return block ? withState(base, 'busy', now, nudges + 1) : withState(base, 'idle', now, reminder === undefined ? 0 : nudges);
    }
    if (state === undefined) return !learned && (owned === undefined || current?.owner === owned) && (inputIdle === undefined || current?.inputIdle === inputIdle) ? current : base;
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
