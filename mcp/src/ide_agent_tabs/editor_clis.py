from __future__ import annotations

import ntpath
import posixpath
import re
from collections.abc import Mapping
from typing import Callable, NamedTuple

from .installed import exists as path_exists
from .installed import find_on_path
from .jspath import is_absolute

EDITOR_CLIS = ("code", "code-insiders", "cursor", "windsurf", "codium", "antigravity-ide", "kiro", "positron", "trae")


class EditorCli(NamedTuple):
    cli: str
    path: str


class CliInvocation(NamedTuple):
    command: str
    args: list[str]
    windows_verbatim_arguments: bool


_MAC_APPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("code", "Visual Studio Code", ("code",)),
    ("code-insiders", "Visual Studio Code - Insiders", ("code",)),
    ("cursor", "Cursor", ("code", "cursor")),
    ("windsurf", "Windsurf", ("windsurf", "code")),
    ("codium", "VSCodium", ("codium",)),
    ("antigravity-ide", "Antigravity IDE", ("antigravity-ide",)),
    ("antigravity-ide", "Antigravity", ("antigravity-ide",)),
    ("kiro", "Kiro", ("code",)),
    ("positron", "Positron", ("code",)),
    ("trae", "Trae", ("code", "trae")),
)

_LINUX_SNAPS = {"code", "code-insiders", "codium"}
_LINUX_FLATPAKS = {"code": "com.visualstudio.code", "codium": "com.vscodium.codium"}


def env_value(env: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = env.get(name)
        if value is not None:
            return value
    return None


def path_var_of(env: Mapping[str, str]) -> str:
    return env_value(env, "PATH", "Path") or ""


def editor_cli_locations(platform: str, env: Mapping[str, str], user_home: str) -> dict[str, list[str]]:
    locations: dict[str, list[str]] = {cli: [] for cli in EDITOR_CLIS}
    if platform == "win32":
        local = env.get("LOCALAPPDATA")
        programs = ntpath.join(local, "Programs") if local else None
        program_files = env_value(env, "ProgramFiles", "PROGRAMFILES")

        def add(cli: str, root: str | None, *rest: str) -> None:
            if root:
                locations[cli].append(ntpath.join(root, *rest, f"{cli}.cmd"))

        add("code", programs, "Microsoft VS Code", "bin")
        add("code", program_files, "Microsoft VS Code", "bin")
        add("code-insiders", programs, "Microsoft VS Code Insiders", "bin")
        add("code-insiders", program_files, "Microsoft VS Code Insiders", "bin")
        add("cursor", programs, "cursor", "resources", "app", "bin")
        add("windsurf", programs, "Windsurf", "bin")
        add("codium", programs, "VSCodium", "bin")
        add("codium", program_files, "VSCodium", "bin")
        add("antigravity-ide", programs, "Antigravity IDE", "bin")
        add("kiro", programs, "Kiro", "bin")
        add("positron", programs, "Positron", "bin")
        add("positron", program_files, "Positron", "bin")
        add("trae", programs, "Trae", "bin")
    elif platform == "darwin":
        for root in ("/Applications", posixpath.join(user_home, "Applications")):
            for cli, app, shims in _MAC_APPS:
                for shim in shims:
                    locations[cli].append(posixpath.join(root, f"{app}.app", "Contents", "Resources", "app", "bin", shim))
    elif platform == "linux":
        for cli in EDITOR_CLIS:
            flatpak = _LINUX_FLATPAKS.get(cli)
            locations[cli].extend(
                [
                    posixpath.join("/usr/share", cli, "bin", cli),
                    posixpath.join("/opt", cli, "bin", cli),
                    *([posixpath.join("/snap/bin", cli)] if cli in _LINUX_SNAPS else []),
                    posixpath.join(user_home, ".local", "bin", cli),
                    *(
                        [
                            posixpath.join("/var/lib/flatpak/exports/bin", flatpak),
                            posixpath.join(user_home, ".local", "share", "flatpak", "exports", "bin", flatpak),
                        ]
                        if flatpak
                        else []
                    ),
                ]
            )
    return locations


def _cli_file_names(cli: str, platform: str) -> list[str]:
    return [f"{cli}.cmd", f"{cli}.exe"] if platform == "win32" else [cli]


def find_cli_on_path(cli: str, platform: str, env: Mapping[str, str]) -> str | None:
    path_var = path_var_of(env)
    for name in _cli_file_names(cli, platform):
        found = find_on_path(path_var, name)
        if found is not None:
            return found
    return None


def find_editor_clis(platform: str, env: Mapping[str, str], user_home: str, exists: Callable[[str], bool] = path_exists) -> list[EditorCli]:
    locations = editor_cli_locations(platform, env, user_home)
    found: list[EditorCli] = []
    for cli in EDITOR_CLIS:
        file = find_cli_on_path(cli, platform, env) or next((p for p in locations[cli] if exists(p)), None)
        if file:
            found.append(EditorCli(cli, file))
    return found


def resolve_editor_cli(
    name_or_path: str, platform: str, env: Mapping[str, str], user_home: str, exists: Callable[[str], bool] = path_exists
) -> EditorCli | None:
    api = ntpath if platform == "win32" else posixpath
    if is_absolute(name_or_path, platform == "win32"):
        ext = api.splitext(name_or_path)[1]
        candidates = _cli_file_names(name_or_path, platform) if platform == "win32" and ext == "" else [name_or_path]
        file = next((p for p in candidates if exists(p)), None)
        if not file:
            return None
        base = api.basename(file)
        return EditorCli(base[: len(base) - len(api.splitext(base)[1])] if api.splitext(base)[1] else base, file)
    if re.search(r"[\\/]", name_or_path):
        return None
    file = find_cli_on_path(name_or_path, platform, env) or next(
        (p for p in editor_cli_locations(platform, env, user_home).get(name_or_path, []) if exists(p)), None
    )
    return EditorCli(name_or_path, file) if file else None


# Windows refuses to start a .cmd or .bat file without a shell, and cmd.exe gives its own meaning to these
# characters even inside quotes, so text that holds one is refused instead of escaped.
_CMD_SPECIAL = re.compile(r'["%^&|<>!\r\n]')
_CMD_FILE = re.compile(r"\.(cmd|bat)\Z", re.IGNORECASE)


def cli_invocation(cli: str, args: list[str], platform: str, comspec: str | None) -> CliInvocation:
    if platform != "win32" or not _CMD_FILE.search(cli):
        return CliInvocation(cli, list(args), False)
    every = [cli, *args]
    unsafe = next((a for a in every if _CMD_SPECIAL.search(a)), None)
    if unsafe is not None:
        raise ValueError(f"cmd.exe can't safely run with the argument {unsafe}")
    quoted = " ".join(f'"{a}"' for a in every)
    return CliInvocation(comspec or "cmd.exe", ["/d", "/s", "/c", f'"{quoted}"'], True)
