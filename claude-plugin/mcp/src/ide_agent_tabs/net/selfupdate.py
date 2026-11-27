from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..clock import now_ms as current_ms
from ..files import file_lock, read_text_if_exists, write_atomically
from ..jev.settings import CONFIG_FILE
from ..jsjson import parse
from ..processes import child_flags, spawn_detached, terminal_environment
from ..shared.state import PORT_OPTION_ENV
from ..version import compare_versions
from .ingress import net_dir
from .private_files import check_private, ensure_private_dir, make_private_file

PLUGIN_NAME = "ide-agent-tabs"
ATTEMPTS_FILE = "selfupdate.json"
CLAUDE_RECORD = "installed_plugins.json"
HOUR_MS = 3_600_000
MAX_ATTEMPTS_PER_HOUR = 6
UPDATE_TIMEOUT_S = 300
SUMMARY_CHARS = 200
SUMMARY_LINES = 3
NO_COMMAND_TEXT = "Update ide-agent-tabs with the client that installed it."
RESTART_NOTE = "Open sessions keep the old skill text and tool list until they restart."

_MARKETPLACE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_VERSION = re.compile(r"[0-9]{1,4}(?:\.[0-9]{1,6}){0,3}")
_PATH_PARTS = re.compile(r"[\\/]+")

Runner = Callable[..., Any]


@dataclass(frozen=True)
class UpdateCommand:
    argv: tuple[str, ...]
    display: str
    refresh: tuple[str, ...] | None = None


@dataclass(frozen=True)
class UpdateOutcome:
    ok: bool
    summary: str


@dataclass(frozen=True)
class Install:
    root: str
    version: str

    @property
    def cli_script(self) -> str:
        return os.path.join(self.root, "mcp", "launch", "agent_tabs.py")

    @property
    def listener_script(self) -> str:
        return os.path.join(self.root, "mcp", "launch", "net_listener.py")


def plugin_root() -> str:
    return str(Path(__file__).resolve().parents[4])


def _recorded_by_claude(root: str, marketplace: str) -> bool:
    plugins = os.path.normpath(root)
    for _ in range(4):
        plugins = os.path.dirname(plugins)
    try:
        with open(os.path.join(plugins, CLAUDE_RECORD), encoding="utf-8") as handle:
            record: Any = json.load(handle)
    except (OSError, ValueError):
        return False
    if isinstance(record, dict) and isinstance(record.get("plugins"), dict):  # pyright: ignore[reportUnknownMemberType]
        record = record["plugins"]
    return isinstance(record, dict) and f"{PLUGIN_NAME}@{marketplace}" in record


def _layout(root: str) -> tuple[str, str] | None:
    parts = [part for part in _PATH_PARTS.split(root) if part]
    # Codex keeps plugins in the same cache layout, so only Claude Code's own record of the install proves the client.
    if (
        len(parts) >= 5
        and parts[-2] == PLUGIN_NAME
        and parts[-4] == "cache"
        and parts[-5] == "plugins"
        and _recorded_by_claude(root, parts[-3])
    ):
        client, marketplace = "claude", parts[-3]
    elif len(parts) >= 3 and parts[-1] == PLUGIN_NAME and parts[-3] == "installed-plugins":
        client, marketplace = "copilot", parts[-2]
    else:
        return None
    if _MARKETPLACE.fullmatch(marketplace) is None or set(marketplace) <= {"."}:
        return None
    return client, marketplace


def update_command(plugin_root: str) -> UpdateCommand | None:
    found = _layout(plugin_root)
    if found is None:
        return None
    client, marketplace = found
    refresh = (client, "plugin", "marketplace", "update", marketplace)
    argv = (client, "plugin", "update", f"{PLUGIN_NAME}@{marketplace}")
    return UpdateCommand(argv, f"{' '.join(refresh)} && {' '.join(argv)}", refresh)


def manual_text(command: UpdateCommand | None) -> str:
    if command is None:
        return NO_COMMAND_TEXT
    return f"To update ide-agent-tabs, run: {command.display}"


def _tail(*texts: str) -> str:
    lines = [line.strip() for text in texts for line in text.splitlines() if line.strip()]
    joined = " | ".join(lines[-SUMMARY_LINES:])
    return "".join(char if char.isprintable() else "?" for char in joined)[-SUMMARY_CHARS:]


def _run_step(argv: tuple[str, ...], runner: Runner, timeout_s: float) -> UpdateOutcome:
    program = shutil.which(argv[0])
    if program is None:
        return UpdateOutcome(False, f"{argv[0]} was not found on PATH")
    try:
        done = runner(
            [program, *argv[1:]],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            shell=False,
            check=False,
            creationflags=child_flags(),
        )
    except subprocess.TimeoutExpired:
        return UpdateOutcome(False, f"{argv[0]} did not finish within {timeout_s:g} s")
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return UpdateOutcome(False, f"{argv[0]} could not run: {type(error).__name__}")
    summary = _tail(str(done.stdout or ""), str(done.stderr or ""))
    if done.returncode != 0:
        return UpdateOutcome(False, f"exit {done.returncode}: {summary}" if summary else f"exit {done.returncode}")
    return UpdateOutcome(True, summary or "exit 0")


def run_update(command: UpdateCommand | None, runner: Runner = subprocess.run, timeout_s: float = UPDATE_TIMEOUT_S) -> UpdateOutcome:
    if command is None:
        return UpdateOutcome(False, "no update command is known for this install")
    outcome = UpdateOutcome(True, "")
    for argv in (*([command.refresh] if command.refresh else []), command.argv):
        outcome = _run_step(argv, runner, timeout_s)
        if not outcome.ok:
            return outcome
    return outcome


def _install_version(root: str) -> str | None:
    try:
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as handle:
            version = json.load(handle).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return version if isinstance(version, str) and _VERSION.fullmatch(version) is not None else None


def _install_problems(folder: str) -> list[str]:
    from .login_task import install_folder_problems

    return install_folder_problems(folder)


def newest_install(plugin_root: str, problems: Callable[[str], list[str]] = _install_problems) -> Install | None:
    found = _layout(plugin_root)
    if found is None:
        return None
    if found[0] == "claude":
        folder = os.path.dirname(os.path.normpath(plugin_root))
        try:
            roots = [os.path.join(folder, name) for name in sorted(os.listdir(folder))]
        except OSError:
            return None
    else:
        roots = [plugin_root]
    best: Install | None = None
    for root in roots:
        version = _install_version(root)
        candidate = Install(root, version) if version is not None else None
        if candidate is None or not os.path.isfile(candidate.cli_script) or not os.path.isfile(candidate.listener_script):
            continue
        if best is None or compare_versions(candidate.version, best.version) > 0:
            best = candidate
    if best is not None and problems(os.path.dirname(best.cli_script)):
        return None
    return best


def spawn_lan_start(home: str, install: Install, server_port: int | None = None, spawn: Callable[..., object] = spawn_detached) -> bool:
    if not os.path.isabs(sys.executable) or not os.path.isfile(install.cli_script):
        return False
    env = terminal_environment(os.environ)
    env["IDE_AGENT_TABS_HOME"] = home
    if server_port is not None:
        env[PORT_OPTION_ENV] = str(server_port)
    try:
        spawn([sys.executable, "-I", "-S", install.cli_script, "net", "lan", "start"], env, os.path.expanduser("~"))
    except OSError:
        return False
    return True


def auto_update_enabled(home: str) -> bool:
    try:
        text = read_text_if_exists(os.path.join(home, CONFIG_FILE))
        config: Any = parse(text) if text and text.strip() else {}
    except (OSError, ValueError):
        return False
    if not isinstance(config, dict):
        return False
    section: Any = config.get("network")  # pyright: ignore[reportUnknownMemberType]
    if section is None:
        return True
    if not isinstance(section, dict):
        return False
    return section.get("autoUpdate", True) is True  # pyright: ignore[reportUnknownMemberType]


def _recent(value: object, now: int) -> list[tuple[str, int]]:
    if not isinstance(value, dict):
        return []
    entries: Any = value.get("attempts")  # pyright: ignore[reportUnknownMemberType]
    if not isinstance(entries, list):
        return []
    found: list[tuple[str, int]] = []
    for entry in entries:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(entry, dict) or set(entry) != {"deviceId", "atMs"}:  # pyright: ignore[reportUnknownArgumentType]
            continue
        device, at = entry["deviceId"], entry["atMs"]  # pyright: ignore[reportUnknownVariableType]
        if isinstance(device, str) and isinstance(at, int) and not isinstance(at, bool) and 0 <= now - at < HOUR_MS:
            found.append((device, at))
    return found


def attempt_allowed(home: str, peer_device_id: str, now_ms: int) -> bool:
    path = net_dir(home) / ATTEMPTS_FILE
    try:
        ensure_private_dir(path.parent)
        with file_lock(str(path)):
            existed = path.exists()
            if existed and check_private(path):
                return False
            try:
                value: object = json.loads(read_text_if_exists(str(path)) or "")
            except ValueError:
                value = None
            recent = _recent(value, now_ms)
            if any(device == peer_device_id for device, _ in recent) or len(recent) >= MAX_ATTEMPTS_PER_HOUR:
                return False
            recent.append((peer_device_id, now_ms))
            body = {"attempts": [{"deviceId": device, "atMs": at} for device, at in recent]}
            write_atomically(str(path), json.dumps(body, indent=2) + "\n")
            if not existed:
                make_private_file(path)
            return True
    except OSError:
        return False


def _in_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, daemon=True).start()


@dataclass
class JoinUpdater:
    home: str
    own_version: str
    log: Callable[[str], None]
    post: Callable[[str, str], int]
    server_port: int | None = None
    command: Callable[[], UpdateCommand | None] = field(default=lambda: update_command(plugin_root()))
    fresh: Callable[[], Install | None] = field(default=lambda: newest_install(plugin_root()))
    runner: Runner = subprocess.run
    spawn: Callable[[str, Install, int | None], bool] = spawn_lan_start
    clock: Callable[[], int] = current_ms
    background: Callable[[Callable[[], None]], object] = _in_thread

    def on_join(self, device_id: str, version: str | None) -> bool:
        if version is None or compare_versions(version, self.own_version) <= 0:
            return False
        if not attempt_allowed(self.home, device_id, self.clock()):
            return False
        self.background(lambda: self._run(device_id, version))
        return True

    def _run(self, device_id: str, version: str) -> None:
        try:
            self._update(device_id, version)
        except Exception as error:  # noqa: BLE001 - the update runs in a background thread and must not end it with a traceback
            self.log(f"self-update: failed {type(error).__name__}")

    def _manual(self, command: UpdateCommand | None, version: str, why: str) -> None:
        self.post(
            self.home,
            f"A machine on ide-agent-tabs {version} joined this machine, which runs {self.own_version}. {why} {manual_text(command)}",
        )

    def _update(self, device_id: str, version: str) -> None:
        command = self.command()
        if not auto_update_enabled(self.home):
            self._manual(command, version, "The unattended update is off (agent-tabs net auto-update on turns it on).")
            return
        if command is None:
            self._manual(command, version, "This install has no known update command, so no update ran.")
            return
        self.log(f"self-update: attempt peer={device_id} command={command.display}")
        outcome = run_update(command, self.runner)
        if not outcome.ok:
            self.log(f"self-update: failed {outcome.summary}")
            self._manual(command, version, f"The unattended update failed ({outcome.summary}).")
            return
        self.log("self-update: ok")
        install = self.fresh()
        if install is None or compare_versions(install.version, self.own_version) <= 0:
            self.post(
                self.home,
                f"This machine ran the ide-agent-tabs update after a join from a machine on version {version}, "
                f"but it found no newer install than {self.own_version}. Run agent-tabs net lan start once the new version is installed. {RESTART_NOTE}",
            )
            return
        self.post(
            self.home,
            f"This machine updated ide-agent-tabs to {install.version} after a join from a machine on version {version}. "
            f"The listener restarts on the new build. {RESTART_NOTE}",
        )
        if not self.spawn(self.home, install, self.server_port):
            self.log("self-update: the listener restart did not start; run agent-tabs net lan start")
