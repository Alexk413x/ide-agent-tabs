from __future__ import annotations

import functools
import ntpath
import os
import posixpath
import re
import sys
from collections.abc import Mapping
from typing import Any, Callable, NamedTuple

from .editor_clis import cli_invocation, env_value, find_editor_clis
from .ide_catalog import IDE_CATALOG, entry_for_product_info
from .jsjson import CONTROL, parse, trim
from .jspath import posix_resolve, win32_resolve
from .terminals.processes import spawn_detached, terminal_environment
from .version import compare_versions


class IdeInstall(NamedTuple):
    key: str
    product: str
    kind: str
    launcher: str
    version: str | None = None


class DiscoveryFs(NamedTuple):
    exists: Callable[[str], bool]
    readdir: Callable[[str], list[str]]
    read_text: Callable[[str], str | None]


class DiscoveryContext(NamedTuple):
    platform: str
    env: Mapping[str, str]
    user_home: str
    arch: str
    fs: DiscoveryFs


def _readdir(folder: str) -> list[str]:
    try:
        return os.listdir(folder)
    except OSError:
        return []


def _read_text(file: str) -> str | None:
    try:
        with open(file, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _exists(file: str) -> bool:
    try:
        return os.path.exists(file)
    except ValueError:
        return False


SYSTEM_DISCOVERY_FS = DiscoveryFs(_exists, _readdir, _read_text)


def node_arch() -> str:
    if sys.platform == "win32":
        machine = os.environ.get("PROCESSOR_ARCHITECTURE", "")
    else:
        machine = os.uname().machine
    wide = sys.maxsize > 2**32
    machine = machine.lower()
    if machine in ("amd64", "x86_64", "x64"):
        return "x64" if wide else "ia32"
    if machine in ("arm64", "aarch64"):
        return "arm64" if wide else "arm"
    if machine in ("x86", "i386", "i686"):
        return "ia32"
    return machine


def node_platform() -> str:
    return "linux" if sys.platform.startswith("linux") else sys.platform


class LaunchEntry(NamedTuple):
    os: str
    launcher_path: str
    arch: str | None


class ProductInfo(NamedTuple):
    launch: list[LaunchEntry]
    name: str | None = None
    version: str | None = None
    build_number: str | None = None
    product_code: str | None = None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and trim(value) != "" else None


def parse_product_info(text: str | None) -> ProductInfo | None:
    try:
        value = parse(text if text is not None else "")
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    launch: list[LaunchEntry] = []
    items = value.get("launch")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        os_name = _text(item.get("os"))
        launcher = _text(item.get("launcherPath"))
        if os_name and launcher:
            launch.append(LaunchEntry(os_name, launcher, _text(item.get("arch"))))
    return ProductInfo(
        launch, _text(value.get("name")), _text(value.get("version")), _text(value.get("buildNumber")), _text(value.get("productCode"))
    )


_OS_NAMES = {"win32": "Windows", "darwin": "macOS", "linux": "Linux"}
_ARCH_NAMES = {"x64": "amd64", "arm64": "aarch64"}


def launcher_path(info: ProductInfo, platform: str, arch: str) -> str | None:
    name = _OS_NAMES.get(platform)
    wanted = name.lower() if name else None
    mine = [entry for entry in info.launch if entry.os.lower() == wanted]
    exact = next((entry for entry in mine if entry.arch == _ARCH_NAMES.get(arch, arch)), None)
    chosen = exact or (mine[0] if mine else None)
    return chosen.launcher_path if chosen else None


class _Root(NamedTuple):
    folder: str
    depth: int
    suffix: str | None = None


def _jetbrains_roots(ctx: DiscoveryContext) -> list[_Root]:
    env, home = ctx.env, ctx.user_home
    if ctx.platform == "win32":
        program_files = env_value(env, "ProgramFiles", "PROGRAMFILES")
        local = env.get("LOCALAPPDATA")
        roots: list[_Root] = []
        if program_files:
            roots += [_Root(ntpath.join(program_files, "JetBrains"), 1), _Root(ntpath.join(program_files, "Android"), 1)]
        if local:
            roots += [_Root(ntpath.join(local, "Programs"), 1), _Root(ntpath.join(local, "JetBrains", "Toolbox", "apps"), 3)]
        return roots
    if ctx.platform == "darwin":
        return [
            _Root("/Applications", 1),
            _Root(posixpath.join(home, "Applications"), 1),
            _Root(posixpath.join(home, "Library", "Application Support", "JetBrains", "Toolbox", "apps"), 4),
        ]
    if ctx.platform == "linux":
        return [
            _Root(posixpath.join(home, ".local", "share", "JetBrains", "Toolbox", "apps"), 3),
            _Root("/opt", 1),
            _Root("/usr/share", 1),
            _Root("/usr/local", 1),
            _Root("/snap", 1, "current"),
        ]
    return []


def _product_info_file(folder: str, ctx: DiscoveryContext) -> str | None:
    if ctx.platform == "darwin":
        if not folder.endswith(".app"):
            return None
        file = posixpath.join(folder, "Contents", "Resources", "product-info.json")
        return file if ctx.fs.exists(file) else None
    file = (ntpath if ctx.platform == "win32" else posixpath).join(folder, "product-info.json")
    return file if ctx.fs.exists(file) else None


def _jetbrains_install(folder: str, file: str, ctx: DiscoveryContext) -> IdeInstall | None:
    info = parse_product_info(ctx.fs.read_text(file))
    if info is None:
        return None
    entry = entry_for_product_info(info.name, info.product_code)
    if entry is None or entry.kind != "jetbrains":
        return None
    version = info.version or info.build_number
    product = info.name if info.name is not None else entry.name
    if ctx.platform == "darwin":
        return IdeInstall(entry.key, product, "jetbrains", folder, version)
    relative = launcher_path(info, ctx.platform, ctx.arch)
    if not relative:
        return None
    windows = ctx.platform == "win32"
    resolve = win32_resolve if windows else posix_resolve
    sep = "\\" if windows else "/"
    launcher = resolve(folder, relative)
    if not launcher.startswith(resolve(folder) + sep) or not ctx.fs.exists(launcher):
        return None
    return IdeInstall(entry.key, product, "jetbrains", launcher, version)


def _scan(root: _Root, ctx: DiscoveryContext, found: list[IdeInstall]) -> None:
    api = ntpath if ctx.platform == "win32" else posixpath

    def walk(folder: str, depth: int) -> None:
        for name in ctx.fs.readdir(folder):
            if name.startswith("."):
                continue
            candidate = api.join(folder, name, root.suffix) if root.suffix else api.join(folder, name)
            file = _product_info_file(candidate, ctx)
            if file:
                install = _jetbrains_install(candidate, file, ctx)
                if install is not None:
                    found.append(install)
            elif depth > 1:
                walk(candidate, depth - 1)

    walk(root.folder, root.depth)


_VENDOR_PREFIX = re.compile(r"^[A-Za-z]+-")


def _sortable(i: IdeInstall) -> str:
    return _VENDOR_PREFIX.sub("", i.version if i.version is not None else "0", count=1)


def _build_order(a: IdeInstall, b: IdeInstall) -> int:
    return compare_versions(_sortable(b), _sortable(a))


def discover_ides(ctx: DiscoveryContext) -> list[IdeInstall]:
    editors: list[IdeInstall] = []
    for cli, file in find_editor_clis(ctx.platform, ctx.env, ctx.user_home, ctx.fs.exists):
        entry = next((e for e in IDE_CATALOG if e.cli == cli), None)
        if entry is not None:
            editors.append(IdeInstall(entry.key, entry.name, "vscode", file))
    jetbrains: list[IdeInstall] = []
    for root in _jetbrains_roots(ctx):
        _scan(root, ctx, jetbrains)
    seen: set[str] = set()
    unique: list[IdeInstall] = []
    for install in sorted(jetbrains, key=functools.cmp_to_key(_build_order)):
        if install.launcher not in seen:
            seen.add(install.launcher)
            unique.append(install)
    return [*editors, *unique]


_DROP_NAMES = {"NO_COLOR", "FORCE_COLOR", "CLAUDECODE", "TERMINAL_EMULATOR", "ELECTRON_RUN_AS_NODE"}
_DROP_PREFIXES = (
    "CLAUDE_",
    "ANTHROPIC_",
    "CODEX_",
    "GEMINI_CLI",
    "COPILOT_",
    "IDE_AGENT_TABS_",
    "JEDITERM_SOURCE",
    "TERM_PROGRAM",
    "VSCODE_",
    "MCP_",
)
_KEEP_NAMES = {"IDE_AGENT_TABS_HOME"}


# An IDE passes its own environment to every terminal tab it opens, so nothing from the calling agent
# session may reach it. IDE_AGENT_TABS_HOME stays: the IDE registers its endpoint under that folder.
def launch_environment(env: Mapping[str, str]) -> dict[str, str]:
    kept: dict[str, str] = {}
    for raw, value in terminal_environment(env).items():
        name = raw.upper()
        if name in _KEEP_NAMES or (name not in _DROP_NAMES and not name.startswith(_DROP_PREFIXES)):
            kept[raw] = value
    return kept


class IdeCommand(NamedTuple):
    command: str
    args: list[str]
    windows_verbatim_arguments: bool
    windows_hide: bool

    def command_line(self) -> str | list[str]:
        if self.windows_verbatim_arguments:
            return " ".join([self.command, *self.args])
        return [self.command, *self.args]


def ide_launch_command(install: IdeInstall, folder: str, platform: str, comspec: str | None) -> IdeCommand:
    if CONTROL.search(folder):
        raise ValueError("the folder path holds a control character")
    if install.kind == "vscode":
        inv = cli_invocation(install.launcher, [folder], platform, comspec)
        # A .cmd editor CLI runs through cmd.exe, whose console window would flash; the editor window it
        # starts is a separate process and shows anyway.
        return IdeCommand(inv.command, inv.args, inv.windows_verbatim_arguments, inv.command != install.launcher)
    if platform == "darwin":
        return IdeCommand("open", ["-na", install.launcher, "--args", folder], False, False)
    return IdeCommand(install.launcher, [folder], False, False)


def spawn_ide(cmd: IdeCommand, env: Mapping[str, str]) -> None:
    spawn_detached(cmd.command_line(), env)


def system_discovery(platform: str, env: Mapping[str, str]) -> DiscoveryContext:
    return DiscoveryContext(platform, env, os.path.expanduser("~"), node_arch(), SYSTEM_DISCOVERY_FS)
