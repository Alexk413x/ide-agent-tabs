export type AgentTabsActivity = 'idle' | 'busy' | 'permission'

export type AgentTabsSelf = {
  server: string
  id: string
  name: string
  isDriver: boolean
  mailbox: string | null
}

export type AgentTabsView = 'agents' | 'messages' | 'detail'

export type AgentTabsPick = {
  key: string
  name: string
  id: string | null
  names: string[]
}

export type AgentTabsPane = {
  view: AgentTabsView
  agent: AgentTabsPick | null
  message: string | null
  focus: { agents: string | null; messages: string | null; detail: string | null }
}

export type AgentTabsPaneRow = {
  key: string
  name: string
  agent: string
  state: string
  started: string | null
  harness: string
  model: string | null
  effort: string | null
  where: string | null
  session: string | null
  id: string | null
  names: string[]
}

export type AgentTabsPaneGroup = { heading: string; rows: AgentTabsPaneRow[] }

export type AgentTabsParty = { id?: string; name?: string; agent?: string; path?: string }

export type AgentTabsMessage = {
  id: string
  at: string
  direction: 'sent' | 'received'
  route: 'agent-tabs' | 'native'
  from: AgentTabsParty
  to: AgentTabsParty
  peer: AgentTabsParty
  text: string
  replyTo?: string
  delivery?: string
  status?: 'unread' | 'delivering' | 'read'
}

declare module 'claude-code' {
  interface PluginState {
    'ide-agent-tabs': {
      self: AgentTabsSelf | null
      activity: AgentTabsActivity
      pane: AgentTabsPane
      paneOpen: boolean
      paneAgents: AgentTabsPaneGroup[]
      paneHistory: AgentTabsMessage[]
    }
  }
}
