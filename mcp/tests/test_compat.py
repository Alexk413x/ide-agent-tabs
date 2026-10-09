from __future__ import annotations

import ast
import os
import unittest

from support import ROOT, SRC, TESTS

LAUNCH = os.path.join(ROOT, "claude-plugin", "mcp", "launch")
BANNED_MODULES = {"tomllib"}
BANNED_ATTRIBUTES = {
    ("datetime", "UTC"),
    ("itertools", "pairwise"),
    ("asyncio", "timeout"),
    ("asyncio", "TaskGroup"),
}
BANNED_NAMES = {"sqlite_errorcode", "sqlite_errorname", "pairwise", "TaskGroup", "ExceptionGroup", "fromisoformat"}


def python_files() -> list[str]:
    found: list[str] = []
    for top in (SRC, LAUNCH, TESTS):
        for folder, dirs, names in os.walk(top):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            found.extend(os.path.join(folder, n) for n in names if n.endswith(".py"))
    return sorted(found)


def problems(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, path)
    found: list[str] = []
    where = os.path.relpath(path, ROOT)
    if source.strip() and not any(
        isinstance(node, ast.ImportFrom) and node.module == "__future__" and any(a.name == "annotations" for a in node.names)
        for node in tree.body
    ):
        found.append(f"{where}: no 'from __future__ import annotations'")
    match_node = getattr(ast, "Match", None)
    try_star = getattr(ast, "TryStar", None)
    for node in ast.walk(tree):
        line = f"{where}:{getattr(node, 'lineno', 0)}"
        if match_node is not None and isinstance(node, match_node):
            found.append(f"{line}: match statement")
        if try_star is not None and isinstance(node, try_star):
            found.append(f"{line}: except*")
        if isinstance(node, ast.Import):
            found.extend(f"{line}: import {a.name}" for a in node.names if a.name in BANNED_MODULES)
        if isinstance(node, ast.ImportFrom):
            if node.module in BANNED_MODULES:
                found.append(f"{line}: from {node.module}")
            found.extend(f"{line}: from {node.module} import {a.name}" for a in node.names if (node.module, a.name) in BANNED_ATTRIBUTES)
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and (node.value.id, node.attr) in BANNED_ATTRIBUTES:
                found.append(f"{line}: {node.value.id}.{node.attr}")
            if node.attr in BANNED_NAMES:
                found.append(f"{line}: .{node.attr}")
        if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            found.append(f"{line}: {node.id}")
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            keys = {k.arg for k in node.keywords}
            if name == "zip" and "strict" in keys:
                found.append(f"{line}: zip(strict=)")
            if name == "dataclass" and keys & {"slots", "kw_only"}:
                found.append(f"{line}: dataclass(slots= or kw_only=)")
    return found


class Python39Test(unittest.TestCase):
    def test_sources_avoid_features_newer_than_python_3_9(self) -> None:
        files = python_files()
        self.assertTrue(files)
        found = [p for f in files for p in problems(f)]
        self.assertEqual(found, [])

    def test_the_checker_catches_what_it_bans(self) -> None:
        sample = os.path.join(TESTS, "fixtures", "banned_sample.txt")
        self.assertGreaterEqual(len(problems(sample)), 6)


if __name__ == "__main__":
    unittest.main()
