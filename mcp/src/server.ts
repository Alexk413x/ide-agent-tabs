import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js';
import { z } from 'zod';
import { MAX_ENTRIES, MAX_PROMPT_CHARS } from './profiles.js';
import type { Service } from './service.js';

export const SERVER_NAME = 'ide-agent-tabs';
export const SERVER_VERSION = '0.3.0';

async function answer(work: () => Promise<unknown>): Promise<CallToolResult> {
  try {
    return { content: [{ type: 'text', text: JSON.stringify(await work(), null, 2) }] };
  } catch (e) {
    return { isError: true, content: [{ type: 'text', text: e instanceof Error ? e.message : String(e) }] };
  }
}

const IDE_ID =
  'An id from list_ides: an IDE such as jetbrains-12345, or a terminal: windows-terminal, ghostty, kitty, wezterm or tmux.';

export function createServer(service: Service): McpServer {
  const server = new McpServer({ name: SERVER_NAME, version: SERVER_VERSION });

  server.registerTool(
    'list_ides',
    {
      title: 'List IDEs and terminals',
      description:
        'List the running IDEs that can host agent tabs (id, product, version, open projects and which one is focused) and the terminal apps open_tab can use, with what each terminal can do.',
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    () => answer(() => service.listIdes()),
  );

  server.registerTool(
    'list_agents',
    {
      title: 'List agent profiles',
      description: 'List the agent profiles open_tab accepts (name, label, command), whether each command is installed, and the default agent.',
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    () => answer(() => service.listAgents()),
  );

  server.registerTool(
    'list_tabs',
    {
      title: 'List agent tabs',
      description: 'List open agent tabs that Agent Tabs started, across all IDEs and terminals, or in one.',
      inputSchema: { ide: z.string().optional().describe(`${IDE_ID} Leave out to list every tab.`) },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    ({ ide }) => answer(() => service.listTabs(ide)),
  );

  server.registerTool(
    'open_tab',
    {
      title: 'Open an agent tab',
      description:
        'Open a new tab running an interactive agent CLI session (Claude Code, Codex, Gemini CLI, Copilot CLI or a custom profile) in an IDE or a terminal. ' +
        'Without ide, it opens in the IDE whose open project contains path, else the most recently started IDE, else a terminal. ' +
        'Returns the tab id, where it opened, and the agent, plus a note to pass on when the user must act, such as attaching to tmux.',
      inputSchema: {
        path: z.string().describe('Absolute path of an existing folder. The session starts there.'),
        agent: z.string().optional().describe('Profile name from list_agents. Defaults to the configured default agent.'),
        prompt: z.string().max(MAX_PROMPT_CHARS).optional().describe('First message sent to the agent.'),
        args: z
          .array(z.string())
          .max(MAX_ENTRIES)
          .optional()
          .describe("Extra agent CLI arguments, placed after the profile's own arguments and before the prompt."),
        env: z
          .record(z.string(), z.string())
          .optional()
          .describe('Environment variables for the session. Names starting with IDE_AGENT_TABS_ or JEDITERM_SOURCE are refused.'),
        ide: z.string().optional().describe(`${IDE_ID} Leave out to route automatically.`),
      },
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false },
    },
    (input) => answer(() => service.openTab(input)),
  );

  server.registerTool(
    'close_tab',
    {
      title: 'Close an agent tab',
      description:
        "Close an agent tab by id, which ends its session. Leave out id to close the caller's own tab (the session's IDE_AGENT_TABS_ID).",
      inputSchema: { id: z.string().optional().describe('Tab id from open_tab or list_tabs.') },
      annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: false },
    },
    ({ id }) => answer(() => service.closeTab(id)),
  );

  return server;
}
