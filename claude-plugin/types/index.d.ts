export type AgentTabsActivity = 'idle' | 'busy' | 'permission'

export type AgentTabsSelf = {
  server: string
  token: string
  id: string
  name: string
  isDriver: boolean
  mailbox: string | null
}

declare module 'claude-code' {
  interface PluginState {
    'ide-agent-tabs': {
      self: AgentTabsSelf | null
      activity: AgentTabsActivity
    }
  }
}
