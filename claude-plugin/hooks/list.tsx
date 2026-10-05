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
export type ListProps = { groups: ListGroup[]; acts: Record<string, JsonValue> }
type ListState = { hovered: string | null; focused: string | null }

const MARK = '▎'
const IDLE: ListState = { hovered: null, focused: null }

const itemOf = (line: ListLine, part: ListPart) => part.item ?? line.item

export function lineAt(groups: readonly ListGroup[], y: number): { line: ListLine; offset: number } | undefined {
  let top = 0
  for (const g of groups) {
    const edge = g.border ? 1 : 0
    const inside = y - top - edge
    if (inside >= 0 && inside < g.lines.length) return { line: g.lines[inside]!, offset: edge * 2 }
    top += g.lines.length + edge * 2
  }
  return undefined
}

export function itemAt(groups: readonly ListGroup[], x: number, y: number, active: string | null): string | null {
  const at = lineAt(groups, y)
  if (at === undefined) return null
  const { line, offset } = at
  let col = offset + line.indent
  for (const part of line.parts) {
    if (part.revealOn && (active === null || !part.revealOn.includes(active))) continue
    if (x >= col && x < col + part.text.length && itemOf(line, part) !== undefined) return itemOf(line, part)!
    col += part.text.length
  }
  return line.item !== undefined && line.parts.every(p => p.item === undefined) && x >= offset ? line.item : null
}

const List: ClientModule<ListProps, ListState> = (props, surface) => {
  const { Box, Text } = surface.elements
  const items = [...new Set(props.groups.flatMap(g => g.lines.flatMap(l => [l.item, ...l.parts.filter(p => p.revealOn === undefined).map(p => p.item)].filter((i): i is string => i !== undefined))))]
  const post = (item: string | null) => {
    const act = item === null ? undefined : props.acts[item]
    if (act !== undefined) surface.post(act)
  }
  surface.onPointer(e => {
    const state = surface.state ?? IDLE
    if (e.type === 'leave') {
      if (state.hovered !== null) surface.setState({ ...state, hovered: null })
    } else if (e.type === 'move' || e.type === 'enter') {
      const hovered = itemAt(props.groups, e.x, e.y, state.hovered ?? state.focused)
      if (hovered !== state.hovered) surface.setState({ ...state, hovered })
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
  const drawLine = (line: ListLine, y: string) => {
    const marked = line.mark !== undefined && lit(line.item)
    const lead = ' '.repeat(line.indent)
    const prefix = marked ? `${lead.slice(0, line.mark)}${MARK}${lead.slice(line.mark! + 1)}` : lead
    return (
      <Box key={`line-${y}`} flexDirection="row">
        <Text>{prefix}</Text>
        {line.parts.flatMap((part, i) => {
          const on = lit(itemOf(line, part))
          if (part.revealOn && !part.revealOn.some(lit)) return []
          return [
            <Text
              key={`part-${y}-${i}`}
              {...(part.color !== undefined ? { color: part.color } : {})}
              {...(part.dim ? { dimColor: true } : {})}
              {...(part.bold ? { bold: true } : {})}
              {...(part.italic ? { italic: true } : {})}
              {...(on && part.underline ? { underline: true } : {})}
            >
              {part.text}
            </Text>,
          ]
        })}
      </Box>
    )
  }
  return (
    <Box flexDirection="column">
      {props.groups.map((g, gi) =>
        g.border ? (
          <Box key={`group-${gi}`} flexDirection="column" borderStyle="round" paddingX={1}>
            {g.lines.map((line, li) => drawLine(line, `${gi}-${li}`))}
          </Box>
        ) : (
          <Box key={`group-${gi}`} flexDirection="column">
            {g.lines.map((line, li) => drawLine(line, `${gi}-${li}`))}
          </Box>
        ),
      )}
    </Box>
  )
}

export default List
