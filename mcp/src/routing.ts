import path from 'node:path';

export interface Project {
  name: string;
  path: string;
  focused: boolean;
}

export interface IdeCandidate {
  id: string;
  startedAt: number;
  projects: Project[];
}

export interface IdeChoice {
  id: string;
  reason: string;
}

function segments(p: string, isWindows: boolean): string[] {
  const api = isWindows ? path.win32 : path.posix;
  const normal = api.resolve(p);
  const parts = normal.split(isWindows ? /[\\/]+/ : /\/+/).filter((s) => s !== '');
  return isWindows ? parts.map((s) => s.toLowerCase()) : parts;
}

export function projectDepth(base: string, target: string, isWindows: boolean): number | undefined {
  if (base.trim() === '') return undefined;
  const b = segments(base, isWindows);
  const t = segments(target, isWindows);
  if (b.length > t.length) return undefined;
  return b.every((s, i) => s === t[i]) ? b.length : undefined;
}

interface Match {
  candidate: IdeCandidate;
  project: Project;
  depth: number;
}

export function chooseIde(candidates: IdeCandidate[], target: string, isWindows: boolean, callerIde?: string): IdeChoice | undefined {
  const matches: Match[] = [];
  for (const candidate of candidates) {
    let best: Match | undefined;
    for (const project of candidate.projects) {
      const depth = projectDepth(project.path, target, isWindows);
      if (depth === undefined) continue;
      if (!best || depth > best.depth || (depth === best.depth && project.focused && !best.project.focused)) {
        best = { candidate, project, depth };
      }
    }
    if (best) matches.push(best);
  }
  if (matches.length > 0) {
    matches.sort(
      (a, b) =>
        b.depth - a.depth ||
        Number(b.candidate.id === callerIde) - Number(a.candidate.id === callerIde) ||
        Number(b.project.focused) - Number(a.project.focused) ||
        b.candidate.startedAt - a.candidate.startedAt,
    );
    const m = matches[0]!;
    const caller = m.candidate.id === callerIde && matches.some((o) => o !== m && o.depth === m.depth) ? "; the caller's IDE" : '';
    return { id: m.candidate.id, reason: `open project ${m.project.name} contains the path${caller}` };
  }
  const recent = candidates.filter((c) => c.projects.length > 0).sort((a, b) => b.startedAt - a.startedAt)[0];
  return recent ? { id: recent.id, reason: 'no open project contains the path; most recently started IDE' } : undefined;
}

export interface TerminalChoice {
  name: string;
  reason: string;
}

export function chooseTerminal(
  preferred: string | undefined,
  defaultTerminal: string | undefined,
  known: string[],
): TerminalChoice | { error: string } {
  if (preferred !== undefined) {
    if (!known.includes(preferred)) {
      return { error: `config.json names terminal "${preferred}", which this server can't drive on this OS; supported: ${known.join(', ') || 'none'}` };
    }
    return { name: preferred, reason: 'no IDE is running; preferred terminal from config.json' };
  }
  if (defaultTerminal !== undefined) return { name: defaultTerminal, reason: 'no IDE is running; platform default terminal' };
  return { error: 'no IDE is running and no supported terminal is available on this OS' };
}
