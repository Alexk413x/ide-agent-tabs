import os from 'node:os';
import path from 'node:path';

export function agentTabsHome(env: NodeJS.ProcessEnv = process.env): string {
  return env.IDE_AGENT_TABS_HOME || path.join(os.homedir(), '.ide-agent-tabs');
}
