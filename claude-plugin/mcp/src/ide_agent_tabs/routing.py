from __future__ import annotations

import re
from typing import NamedTuple

from .jsjson import js_trim
from .jspath import posix_resolve, win32_resolve


class Project(NamedTuple):
    name: str
    path: str
    focused: bool

    def to_json(self) -> dict[str, object]:
        return {"name": self.name, "path": self.path, "focused": self.focused}


class IdeCandidate(NamedTuple):
    id: str
    started_at: float
    projects: list[Project]


class Choice(NamedTuple):
    name: str
    reason: str


class ChoiceError(NamedTuple):
    error: str


_WIN_SEPS = re.compile(r"[\\/]+")
_POSIX_SEPS = re.compile(r"/+")


def _segments(p: str, is_windows: bool) -> list[str]:
    normal = win32_resolve(p) if is_windows else posix_resolve(p)
    parts = [s for s in (_WIN_SEPS if is_windows else _POSIX_SEPS).split(normal) if s != ""]
    return [s.lower() for s in parts] if is_windows else parts


def project_depth(base: str, target: str, is_windows: bool) -> int | None:
    if js_trim(base) == "":
        return None
    b = _segments(base, is_windows)
    t = _segments(target, is_windows)
    if len(b) > len(t):
        return None
    return len(b) if all(s == t[i] for i, s in enumerate(b)) else None


def choose_ide(
    candidates: list[IdeCandidate],
    target: str,
    is_windows: bool,
    caller_ide: str | None = None,
    routing: str = "project",
) -> Choice | None:
    if routing == "caller" and any(c.id == caller_ide and c.projects for c in candidates):
        assert caller_ide is not None
        return Choice(caller_ide, "tabRouting is caller; the caller's IDE")
    matches: list[tuple[IdeCandidate, Project, int]] = []
    for candidate in candidates:
        best: tuple[IdeCandidate, Project, int] | None = None
        for project in candidate.projects:
            depth = project_depth(project.path, target, is_windows)
            if depth is None:
                continue
            if best is None or depth > best[2] or (depth == best[2] and project.focused and not best[1].focused):
                best = (candidate, project, depth)
        if best is not None:
            matches.append(best)
    if matches:
        matches.sort(key=lambda m: (-m[2], -(m[0].id == caller_ide), -m[1].focused, -m[0].started_at))
        m = matches[0]
        caller = "; the caller's IDE" if m[0].id == caller_ide and any(o is not m and o[2] == m[2] for o in matches) else ""
        return Choice(m[0].id, f"open project {m[1].name} contains the path{caller}")
    opened = [c for c in candidates if c.projects]
    own = next((c for c in opened if c.id == caller_ide), None)
    if own is not None:
        return Choice(own.id, "no open project contains the path; the caller's IDE")
    recent = sorted(opened, key=lambda c: -c.started_at)
    return Choice(recent[0].id, "no open project contains the path; most recently started IDE") if recent else None


def choose_terminal(preferred: str | None, default_terminal: str | None, known: list[str]) -> Choice | ChoiceError:
    if preferred is not None:
        if preferred not in known:
            supported = ", ".join(known) or "none"
            return ChoiceError(
                f'config.json names terminal "{preferred}", which this server can\'t drive on this OS; supported: {supported}'
            )
        return Choice(preferred, "no IDE is running; preferred terminal from config.json")
    if default_terminal is not None:
        return Choice(default_terminal, "no IDE is running; platform default terminal")
    return ChoiceError("no IDE is running and no supported terminal is available on this OS")
