import type { ClientModule, JsonValue } from 'claude-code'

export type ListPart = {
  text: string
  color?: string
  dim?: boolean
  bold?: boolean
  italic?: boolean
  underline?: boolean
  item?: string
  revealOn?: string[]
}
export type ListLine = { item?: string; indent: number; mark?: number; parts: ListPart[] }
export type ListGroup = { border: boolean; lines: ListLine[] }
export type ListProps = { groups: ListGroup[]; acts: Record<string, JsonValue>; width?: number }
type ListState = { hovered: string | null; focused: string | null; linger: number }

export type ListRow = { edge: 'top' | 'bottom'; group: number } | { edge?: undefined; group: number; index: number; line: ListLine; offset: number; border: boolean }

const MARK = '▎'
const IDLE: ListState = { hovered: null, focused: null, linger: 0 }
const TICK_MS = 50
export const LINGER_MS = 300
const LINGER_TICKS = LINGER_MS / TICK_MS

const itemOf = (line: ListLine, part: ListPart) => part.item ?? line.item

export function listRows(groups: readonly ListGroup[]): ListRow[] {
  return groups.flatMap((g, group): ListRow[] => [
    ...(g.border ? [{ edge: 'top' as const, group }] : []),
    ...g.lines.map((line, index) => ({ group, index, line, offset: g.border ? 2 : 0, border: g.border })),
    ...(g.border ? [{ edge: 'bottom' as const, group }] : []),
  ])
}

const revealed = (part: ListPart, active: string | null) => part.revealOn === undefined || (active !== null && part.revealOn.includes(active))

export function itemAt(groups: readonly ListGroup[], x: number, y: number, active: string | null): string | null {
  const row = listRows(groups)[y]
  if (row === undefined || row.edge !== undefined) return null
  const { line, offset } = row
  let col = offset + line.indent
  for (const part of line.parts) {
    if (!revealed(part, active)) continue
    if (x >= col && x < col + part.text.length && itemOf(line, part) !== undefined) return itemOf(line, part)!
    col += part.text.length
  }
  if (line.item !== undefined && line.parts.every(p => p.item === undefined)) return line.item
  const opened = line.parts.some(p => p.revealOn !== undefined && revealed(p, active))
  return opened ? active : null
}

const reveals = (groups: readonly ListGroup[], item: string) => groups.some(g => g.lines.some(l => l.parts.some(p => p.revealOn?.includes(item))))

const List: ClientModule<ListProps, ListState> = (props, surface) => {
  const { Box, Text } = surface.elements
  const items = [...new Set(props.groups.flatMap(g => g.lines.flatMap(l => [l.item, ...l.parts.filter(p => p.revealOn === undefined).map(p => p.item)].filter((i): i is string => i !== undefined))))]
  const post = (item: string | null) => {
    const act = item === null ? undefined : props.acts[item]
    if (act !== undefined) surface.post(act)
  }
  if (surface.state === undefined) {
    surface.setState(IDLE)
    surface.every(TICK_MS, () => {
      const state = surface.state ?? IDLE
      if (state.linger === 0) return
      surface.setState(state.linger === 1 ? { ...state, linger: 0, hovered: null } : { ...state, linger: state.linger - 1 })
    })
  }
  surface.onPointer(e => {
    const state = surface.state ?? IDLE
    if (e.type === 'leave') {
      if (state.hovered === null) return
      surface.setState(reveals(props.groups, state.hovered) ? { ...state, linger: LINGER_TICKS } : { ...state, hovered: null, linger: 0 })
    } else if (e.type === 'move' || e.type === 'enter') {
      const hovered = itemAt(props.groups, e.x, e.y, state.hovered ?? state.focused)
      if (hovered !== state.hovered || state.linger !== 0) surface.setState({ ...state, hovered, linger: 0 })
    } else if (e.type === 'up' && e.button === 'left') {
      post(itemAt(props.groups, e.x, e.y, state.hovered ?? state.focused))
    }
  })
  surface.onKey(k => {
    const state = surface.state ?? IDLE
    if (!items.length) return
    const at = state.focused === null ? -1 : items.indexOf(state.focused)
    if (k.key === 'down' || k.key === 'up' || k.key === 'tab') {
      const step = k.key === 'up' || (k.key === 'tab' && k.shift) ? -1 : 1
      const next = at === -1 ? (step === 1 ? 0 : items.length - 1) : (at + step + items.length) % items.length
      surface.setState({ ...state, focused: items[next]! })
    } else if ((k.key === 'return' || k.key === ' ') && state.focused !== null) {
      post(state.focused)
    }
  })

  const state = surface.state ?? IDLE
  const lit = (item: string | undefined): boolean => item !== undefined && (item === state.hovered || item === state.focused)
  const width = props.width ?? surface.columns
  const inner = Math.max(0, width - 4)
  const drawRow = (row: ListRow, y: number) => {
    if (row.edge !== undefined) {
      const [left, right] = row.edge === 'top' ? ['╭', '╮'] : ['╰', '╯']
      return (
        <Box key={`edge-${y}`} flexDirection="row" height={1}>
          <Text>{`${left}${'─'.repeat(Math.max(0, width - 2))}${right}`}</Text>
        </Box>
      )
    }
    const { line } = row
    const marked = line.mark !== undefined && lit(line.item)
    const lead = ' '.repeat(line.indent)
    const prefix = marked ? `${lead.slice(0, line.mark)}${MARK}${lead.slice(line.mark! + 1)}` : lead
    const parts = line.parts.filter(part => !part.revealOn || part.revealOn.some(lit))
    const used = prefix.length + parts.reduce((n, p) => n + p.text.length, 0)
    return (
      <Box key={`row-${y}`} flexDirection="row" height={1} overflow="hidden">
        {row.border && <Text>{'│ '}</Text>}
        <Box key={`line-${row.group}-${row.index}`} flexDirection="row">
          <Text>{prefix}</Text>
          {parts.map((part, i) => (
            <Text
              key={`part-${y}-${i}`}
              {...(part.color !== undefined ? { color: part.color } : {})}
              {...(part.dim ? { dimColor: true } : {})}
              {...(part.bold ? { bold: true } : {})}
              {...(part.italic ? { italic: true } : {})}
              {...(lit(itemOf(line, part)) && part.underline ? { underline: true } : {})}
            >
              {part.text}
            </Text>
          ))}
        </Box>
        {/* Never an empty Text: the terminal gives one no height, and every row below would drift up. */}
        <Text>{row.border ? `${' '.repeat(Math.max(0, inner - used))} │` : ' '}</Text>
      </Box>
    )
  }
  return <Box flexDirection="column">{listRows(props.groups).map(drawRow)}</Box>
}

export default List
