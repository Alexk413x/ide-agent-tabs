export type AgentTabsActivity = 'idle' | 'busy' | 'permission'

export type AgentTabsSelf = {
  server: string
  id: string
  name: string
  isDriver: boolean
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
  notice?: string | null
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
  session: string | null
  id: string | null
  names: string[]
  self: boolean
  messages: number | null
  agentType: string | null
  agentColor: string | null
}

export type AgentTabsPaneFolder = { path: string | null; heading: string | null; rows: AgentTabsPaneRow[] }

export type AgentTabsPaneHost = { heading: string; folders: AgentTabsPaneFolder[] }

export type AgentTabsSender = { id: string; name: string }

export type AgentTabsInbox = { count: number; senders: AgentTabsSender[] }

export type AgentTabsParty = { id?: string; name?: string; agent?: string; path?: string }

export type AgentTabsFullText = { id: string; text: string; total: number; error: string | null }

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
  textLength?: number
}

declare module 'claude-code' {
  interface PluginState {
    'ide-agent-tabs': {
      self: AgentTabsSelf | null
      activity: AgentTabsActivity
      pane: AgentTabsPane
      paneOpen: boolean
      paneHosts: AgentTabsPaneHost[]
      inbox: AgentTabsInbox | null
      paneHistory: AgentTabsMessage[]
      paneTotal: number | null
      paneMessage: AgentTabsFullText | null
      paneOlder: number | null
      paneHistoryError: string | null
    }
  }
}
