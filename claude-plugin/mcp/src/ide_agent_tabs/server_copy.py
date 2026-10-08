from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sys
from typing import Any, NamedTuple

from .clock import now_ms
from .files import ensure_private_dir, file_lock, mtime_ms, read_bytes, read_text_if_exists, remove_file, remove_tree, write_atomically
from .jsjson import parse, stringify
from .version import PACKAGE_VERSION, compare_versions

COPY_DIR = "py"
LAUNCH_DIR = "launch"
SRC_DIR = "src"
PACKAGE_DIR = "ide_agent_tabs"
CURRENT_FILE = "current.json"
VERSION_FILE = "version.json"
PYTHON_FILE = "python.json"
SERVER_ENTRY = "mcp_server.py"
HOOK_ENTRY = "agent_hook.py"
STUB_ENTRIES = (SERVER_ENTRY, HOOK_ENTRY)
OLD_SERVER_FILE = "mcp-server.mjs"
OLD_HOOK_FILE = "agent-hook.mjs"
OLD_FILES = (OLD_SERVER_FILE, OLD_HOOK_FILE, "THIRD_PARTY_NOTICES.txt")
OLD_VERSION_FILE = "version.json"
TEMP_MAX_AGE_MS = 60 * 60 * 1000
_TERMINAL_SCRIPT = re.compile(r"agent-launch\.[A-Za-z0-9]+\Z")
_VERSION_SAFE = re.compile(r"[^0-9A-Za-z.+-]")

_PACKAGE = os.path.dirname(os.path.abspath(__file__))

# Each entry in the copy's launch folder runs the same script inside the current build, so a registration holds
# one path that outlives every update, and an update never changes a file a running server may still import.
STUB = """from __future__ import annotations

import json
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
root = os.path.dirname(here)
try:
    with open(os.path.join(root, "current.json"), encoding="utf-8") as f:
        build = json.load(f)["build"]
    target = os.path.join(root, build, "launch", os.path.basename(__file__))
    with open(target, "rb") as f:
        code = compile(f.read(), target, "exec", dont_inherit=True)
except (OSError, ValueError, KeyError, TypeError) as e:
    sys.stderr.write(f"Agent Tabs: the server copy in {root} is incomplete ({e}); start Claude Code to refresh it.\\n")
    sys.exit(1)
sys.argv[0] = target
exec(code, {"__name__": "__main__", "__file__": target, "__builtins__": __builtins__})
"""


class Source(NamedTuple):
    mcp_dir: str
    scripts_dir: str
    version: str


def plugin_source() -> Source:
    from .list_ides_cli import scripts_dir

    return Source(os.path.dirname(os.path.dirname(_PACKAGE)), scripts_dir(), PACKAGE_VERSION)


def copy_root(home: str) -> str:
    return os.path.join(home, "mcp")


def copy_dir(home: str) -> str:
    return os.path.join(copy_root(home), COPY_DIR)


def python_file(home: str) -> str:
    return os.path.join(copy_root(home), PYTHON_FILE)


def slashes(path: str, platform: str) -> str:
    return path.replace("\\", "/") if platform == "win32" else path


def server_copy_path(home: str, platform: str) -> str:
    return slashes(os.path.join(copy_dir(home), LAUNCH_DIR, SERVER_ENTRY), platform)


def hook_copy_path(home: str, platform: str) -> str:
    return slashes(os.path.join(copy_dir(home), LAUNCH_DIR, HOOK_ENTRY), platform)


def old_server_path(home: str, platform: str) -> str:
    return slashes(os.path.join(copy_root(home), OLD_SERVER_FILE), platform)


def old_hook_path(home: str, platform: str) -> str:
    return slashes(os.path.join(copy_root(home), OLD_HOOK_FILE), platform)


def old_copy_exists(home: str) -> bool:
    root = copy_root(home)
    return any(os.path.exists(os.path.join(root, name)) for name in (*OLD_FILES, LAUNCH_DIR))


def remove_old_copy(home: str, version: str) -> None:
    root = copy_root(home)
    for name in OLD_FILES:
        remove_file(os.path.join(root, name))
    remove_tree(os.path.join(root, LAUNCH_DIR))
    # A 0.8.0 plugin still installed elsewhere reads this file and leaves a copy newer than itself alone.
    write_atomically(os.path.join(root, OLD_VERSION_FILE), stringify({"version": version}) + "\n")


def source_files(source: Source) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    package = os.path.join(source.mcp_dir, SRC_DIR, PACKAGE_DIR)
    for folder, dirs, names in os.walk(package):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in names:
            if name.endswith((".pyc", ".tmp")):
                continue
            full = os.path.join(folder, name)
            files.append(("/".join([SRC_DIR, PACKAGE_DIR, *os.path.relpath(full, package).split(os.sep)]), full))
    launch = os.path.join(source.mcp_dir, LAUNCH_DIR)
    files.extend((f"{LAUNCH_DIR}/{n}", os.path.join(launch, n)) for n in os.listdir(launch) if n.endswith(".py"))
    if os.path.isdir(source.scripts_dir):
        files.extend(
            (f"{LAUNCH_DIR}/{n}", os.path.join(source.scripts_dir, n)) for n in os.listdir(source.scripts_dir) if _TERMINAL_SCRIPT.match(n)
        )
    return sorted(files)


def source_hash(files: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for rel, full in files:
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(read_bytes(full))
        digest.update(b"\0")
    return digest.hexdigest()[:32]


def build_name(version: str, digest: str) -> str:
    return f"{_VERSION_SAFE.sub('_', version)[:32]}-{digest[:12]}"


def bundled_build(source: Source) -> str:
    return build_name(source.version, source_hash(source_files(source)))


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        value = parse(read_text_if_exists(path) or "")
    except (ValueError, OSError):
        return None
    return value if isinstance(value, dict) else None


def read_current(home: str) -> dict[str, Any] | None:
    current = _read_json(os.path.join(copy_dir(home), CURRENT_FILE))
    return current if current is not None and isinstance(current.get("build"), str) else None


def current_build(home: str) -> str | None:
    current = read_current(home)
    if current is None or not os.path.isfile(os.path.join(copy_dir(home), current["build"], VERSION_FILE)):
        return None
    return current["build"]


def _install_build(files: list[tuple[str, str]], target: str, version: str, build: str) -> None:
    temp = f"{os.path.join(os.path.dirname(target), '.' + build)}.{os.getpid()}.{now_ms()}.tmp"
    try:
        for rel, full in files:
            dest = os.path.join(temp, *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(read_bytes(full))
        with open(os.path.join(temp, VERSION_FILE), "w", encoding="utf-8", newline="\n") as f:
            f.write(stringify({"version": version, "build": build}) + "\n")
        remove_tree(target)
        os.replace(temp, target)
    finally:
        remove_tree(temp)


def _prune(folder: str, keep: set[str]) -> None:
    now = now_ms()
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if name in keep or name == LAUNCH_DIR or not os.path.isdir(path):
            continue
        if name.startswith(".") and name.endswith(".tmp"):
            mtime = mtime_ms(path)
            if mtime is not None and now - mtime < TEMP_MAX_AGE_MS:
                continue
        remove_tree(path)


def refresh_server_copy(source: Source, home: str) -> str | None:
    folder = copy_dir(home)
    ensure_private_dir(folder)
    files = source_files(source)
    build = build_name(source.version, source_hash(files))
    with file_lock(os.path.join(folder, "copy")):
        current = read_current(home)
        installed = current.get("version") if current is not None else None
        # Every Claude Code install on this machine shares the copy, so an older plugin never replaces a newer one.
        if isinstance(installed, str) and compare_versions(installed, source.version) > 0 and current_build(home) is not None:
            return None
        target = os.path.join(folder, build)
        if not os.path.isfile(os.path.join(target, VERSION_FILE)):
            _install_build(files, target, source.version, build)
        for entry in STUB_ENTRIES:
            stub = os.path.join(folder, LAUNCH_DIR, entry)
            if read_text_if_exists(stub) != STUB:
                write_atomically(stub, STUB)
        previous = current.get("build") if current is not None else None
        changed = previous != build
        if changed:
            write_atomically(os.path.join(folder, CURRENT_FILE), stringify({"build": build, "version": source.version}) + "\n")
        keep = {build, *([previous] if isinstance(previous, str) else [])}
        with contextlib.suppress(OSError):
            _prune(folder, keep)
        return build if changed else None


def base_interpreter() -> str:
    exe = sys.executable
    base = getattr(sys, "_base_executable", None)
    if sys.prefix != sys.base_prefix and isinstance(base, str) and base and os.path.isfile(base):
        exe = base
    return os.path.abspath(exe)


def read_python(home: str) -> str | None:
    value = (_read_json(python_file(home)) or {}).get("python")
    return value if isinstance(value, str) and value else None


def write_python(home: str, python: str) -> bool:
    if read_python(home) == python:
        return False
    write_atomically(python_file(home), stringify({"python": python}, 2) + "\n")
    return True
