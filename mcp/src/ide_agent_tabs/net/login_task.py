from __future__ import annotations

import contextlib
import os
import plistlib
import re
import stat
import struct
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from .. import processes
from ..files import read_text_if_exists, remove_file, write_atomically
from ..processes import RunResult
from . import private_files
from .private_files import PrivateFileError, check_private, ensure_private_dir, make_private_file

TASK_NAME = "Agent Tabs LAN listener"
LAUNCHER_FILE = "launcher.py"
LAUNCHER_ARGUMENT = "lan-start"
AGENT_LABEL = "com.ide-agent-tabs.lan-listener"
UNIT_NAME = "agent-tabs-lan.service"
LOGON_DELAY = "PT30S"
TOOL_TIMEOUT_S = 30.0

_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_BROAD_SIDS = {"S-1-5-32-545": "Users", "S-1-5-11": "Authenticated Users", "S-1-1-0": "Everyone"}
_ACCESS_ALLOWED_ACE_TYPE = 0
_INHERIT_ONLY_ACE = 0x8
_WRITE_RIGHTS = 0x2 | 0x4 | 0x40 | 0x10000 | 0x40000 | 0x80000 | 0x10000000 | 0x40000000
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x4
_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>")
_UNSAFE_PATH = re.compile(r"[\x00-\x1f\"]")

Run = Callable[..., RunResult]


class LoginTaskError(Exception):
    pass


class AclEntry(NamedTuple):
    ace_type: int
    flags: int
    mask: int
    sid: str


def _windows_directory() -> str:
    import ctypes

    kernel32, _ = private_files._api()
    buffer = ctypes.create_unicode_buffer(520)
    if not kernel32.GetSystemWindowsDirectoryW(buffer, 520):
        raise LoginTaskError("cannot locate the Windows folder")
    return str(buffer.value)


def _current_sid() -> str:
    return private_files._user_sid()


def _acl_entries(path: str) -> list[AclEntry] | None:
    assert sys.platform == "win32"
    import ctypes

    kernel32, advapi32 = private_files._api()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    error = advapi32.GetNamedSecurityInfoW(
        path, _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
    )
    if error:
        raise ctypes.WinError(error)
    try:
        if dacl.value is None:
            return None
        from ctypes import wintypes

        counts = (wintypes.DWORD * 3)()
        if not advapi32.GetAclInformation(dacl, counts, ctypes.sizeof(counts), 2):
            raise ctypes.WinError(ctypes.get_last_error())
        entries: list[AclEntry] = []
        for index in range(counts[0]):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            assert ace.value is not None
            ace_type, flags, _size, mask = struct.unpack("<BBHI", ctypes.string_at(ace.value, 8))
            sid = private_files._sid_string(ace.value + 8) if ace_type == _ACCESS_ALLOWED_ACE_TYPE else ""
            entries.append(AclEntry(ace_type, flags, mask, sid))
        return entries
    finally:
        kernel32.LocalFree(descriptor)


def default_launch_script() -> str:
    return str(Path(__file__).resolve().parents[3] / "launch" / "agent_tabs.py")


@dataclass
class Environment:
    platform: str = field(default_factory=lambda: sys.platform)
    interpreter: str = field(default_factory=lambda: sys.executable)
    launch_script: str = field(default_factory=default_launch_script)
    user_home: str = field(default_factory=lambda: os.path.expanduser("~"))
    run: Run = processes.run
    windows_directory: Callable[[], str] = _windows_directory
    user_sid: Callable[[], str] = _current_sid
    acl_entries: Callable[[str], Sequence[AclEntry] | None] = _acl_entries
    uid: Callable[[], int] = field(default=lambda: os.getuid() if sys.platform != "win32" else 0)
    stat: Callable[[str], os.stat_result] = os.stat


def tool_environment() -> dict[str, str]:
    return {name: os.environ[name] for name in ("SYSTEMROOT", "SYSTEMDRIVE") if name in os.environ}


def net_folder(home: str) -> Path:
    return Path(home) / "net"


def launcher_path(home: str) -> Path:
    return net_folder(home) / LAUNCHER_FILE


def launcher_source(home: str, launch_script: str) -> str:
    return (
        "from __future__ import annotations\n"
        "\n"
        "import os\n"
        "import runpy\n"
        "import sys\n"
        "\n"
        f"HOME = {home!r}\n"
        f"LAUNCH_SCRIPT = {launch_script!r}\n"
        "\n"
        f"if sys.argv[1:] != [{LAUNCHER_ARGUMENT!r}]:\n"
        "    sys.exit(2)\n"
        'os.environ["IDE_AGENT_TABS_HOME"] = HOME\n'
        'sys.argv = [LAUNCH_SCRIPT, "net", "lan", "start"]\n'
        'runpy.run_path(LAUNCH_SCRIPT, run_name="__main__")\n'
    )


def _checked_path(path: str, what: str) -> str:
    if not os.path.isabs(path) or _UNSAFE_PATH.search(path):
        raise LoginTaskError(f"{what} must be an absolute path without quotes or control characters: {path!r}")
    return path


def refresh_launcher(home: str, launch_script: str | None = None) -> Path:
    script = _checked_path(launch_script or default_launch_script(), "the launch script")
    if not os.path.isfile(script):
        raise LoginTaskError(f"the launch script {script} does not exist")
    folder = net_folder(home)
    ensure_private_dir(folder)
    path = launcher_path(home)
    write_atomically(str(path), launcher_source(home, script))
    make_private_file(path)
    return path


def task_xml(*, sid: str, interpreter: str, launcher: str, working_directory: str) -> str:
    arguments = f'-I -S "{launcher}" {LAUNCHER_ARGUMENT}'
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        f'<Task version="1.4" xmlns="{_NAMESPACE}">\n'
        "  <RegistrationInfo>\n"
        "    <Description>Starts the Agent Tabs LAN listener at login. It runs agent-tabs net lan start, which refuses while the network is off."
        "</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        f"      <UserId>{escape(sid)}</UserId>\n"
        f"      <Delay>{LOGON_DELAY}</Delay>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"      <UserId>{escape(sid)}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>false</StartWhenAvailable>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{escape(interpreter)}</Command>\n"
        f"      <Arguments>{escape(arguments)}</Arguments>\n"
        f"      <WorkingDirectory>{escape(working_directory)}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def launch_agent_plist(*, interpreter: str, launcher: str, working_directory: str) -> bytes:
    return plistlib.dumps(
        {
            "Label": AGENT_LABEL,
            "ProgramArguments": [interpreter, "-I", "-S", launcher, LAUNCHER_ARGUMENT],
            "WorkingDirectory": working_directory,
            "RunAtLoad": True,
            "KeepAlive": False,
            "AbandonProcessGroup": True,
            "ProcessType": "Background",
        }
    )


def _systemd_word(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def systemd_unit(*, interpreter: str, launcher: str, working_directory: str) -> str:
    command = " ".join(_systemd_word(word) for word in (interpreter, "-I", "-S", launcher, LAUNCHER_ARGUMENT))
    return (
        "[Unit]\n"
        "Description=Agent Tabs LAN listener\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        "KillMode=process\n"
        f"WorkingDirectory={working_directory.replace('%', '%%')}\n"
        f"ExecStart={command}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def writable_by_others(entries: Sequence[AclEntry] | None) -> list[str]:
    if entries is None:
        return ["it has no access list, so everyone can change it"]
    names: list[str] = []
    for entry in entries:
        if entry.ace_type != _ACCESS_ALLOWED_ACE_TYPE or entry.flags & _INHERIT_ONLY_ACE:
            continue
        group = _BROAD_SIDS.get(entry.sid)
        if group is not None and entry.mask & _WRITE_RIGHTS and group not in names:
            names.append(group)
    return [f"{name} can change it" for name in names]


def _folder_problems(folder: str, env: Environment) -> list[str]:
    problems: list[str] = []
    try:
        if env.platform == "win32":
            problems += writable_by_others(env.acl_entries(folder))
        else:
            status = env.stat(folder)
            if status.st_mode & stat.S_IWOTH:
                problems.append("every user can change it")
            if status.st_uid not in (0, env.uid()):
                problems.append(f"it belongs to uid {status.st_uid}")
    except OSError as error:
        return [f"{folder}: cannot read its access list: {error}"]
    return [f"{folder}: {problem}" for problem in problems]


def verify(home: str, launcher: Path, env: Environment) -> None:
    interpreter = _checked_path(env.interpreter, "the interpreter")
    if not os.path.isfile(interpreter):
        raise LoginTaskError(f"the interpreter {interpreter} is not a file")
    _checked_path(str(launcher), "the launcher")
    problems = [*check_private(net_folder(home)), *check_private(launcher)]
    for folder in dict.fromkeys((os.path.dirname(interpreter), os.path.dirname(env.launch_script))):
        problems += _folder_problems(folder, env)
    if problems:
        raise LoginTaskError("refused because other users could change what the task runs: " + "; ".join(problems))


def _tool(env: Environment, *parts: str) -> str:
    return os.path.join(env.windows_directory(), *parts)


def _single_line(text: str, limit: int = 300) -> str:
    return "".join(char if char.isprintable() else " " for char in text).strip()[:limit]


def _run(env: Environment, command: str, arguments: Sequence[str], what: str) -> RunResult:
    try:
        return env.run(command, arguments, env=tool_environment(), timeout=TOOL_TIMEOUT_S)
    except (OSError, TimeoutError) as error:
        raise LoginTaskError(f"{what} failed: {error}") from error


def _output(result: RunResult) -> str:
    return _single_line(f"{result.stderr} {result.stdout}")


def _register_windows(home: str, launcher: Path, env: Environment) -> str:
    schtasks = _tool(env, "System32", "schtasks.exe")
    definition = task_xml(
        sid=env.user_sid(),
        interpreter=env.interpreter,
        launcher=str(launcher),
        working_directory=str(net_folder(home)),
    )
    handle, name = tempfile.mkstemp(prefix="login-task-", suffix=".xml", dir=str(net_folder(home)))
    try:
        # schtasks reads the definition as UTF-16 with a byte order mark.
        with os.fdopen(handle, "wb") as stream:
            stream.write(definition.encode("utf-16"))
        make_private_file(Path(name))
        result = _run(env, schtasks, ["/Create", "/XML", name, "/TN", TASK_NAME, "/F"], "schtasks")
    finally:
        with contextlib.suppress(OSError):
            os.remove(name)
    if result.code != 0:
        raise LoginTaskError(f"schtasks exited {result.code}: {_output(result)}")
    return f'Registered the Scheduled Task "{TASK_NAME}": at login it runs {env.interpreter} -I -S {launcher} {LAUNCHER_ARGUMENT}.'


def _macos_paths(env: Environment) -> Path:
    return Path(env.user_home) / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def _linux_paths(env: Environment) -> Path:
    return Path(env.user_home) / ".config" / "systemd" / "user" / UNIT_NAME


def _write_user_file(path: Path, data: bytes) -> None:
    os.makedirs(path.parent, exist_ok=True)
    write_atomically(str(path), data)
    os.chmod(path, 0o600)
    problems = check_private(path) if sys.platform != "win32" else []
    if problems:
        raise LoginTaskError("; ".join(problems))


def _register_macos(home: str, launcher: Path, env: Environment) -> str:
    path = _macos_paths(env)
    data = launch_agent_plist(interpreter=env.interpreter, launcher=str(launcher), working_directory=str(net_folder(home)))
    _write_user_file(path, data)
    domain = f"gui/{env.uid()}"
    _run(env, "/bin/launchctl", ["bootout", f"{domain}/{AGENT_LABEL}"], "launchctl")
    result = _run(env, "/bin/launchctl", ["bootstrap", domain, str(path)], "launchctl")
    if result.code != 0:
        raise LoginTaskError(f"launchctl exited {result.code}: {_output(result)}")
    return f"Registered the LaunchAgent {path}."


def _register_linux(home: str, launcher: Path, env: Environment) -> str:
    path = _linux_paths(env)
    text = systemd_unit(interpreter=env.interpreter, launcher=str(launcher), working_directory=str(net_folder(home)))
    _write_user_file(path, text.encode("utf-8"))
    result = _run(env, "/usr/bin/systemctl", ["--user", "enable", UNIT_NAME], "systemctl")
    if result.code != 0:
        raise LoginTaskError(f"systemctl exited {result.code}: {_output(result)}")
    return f"Registered the systemd user unit {path}."


def register(home: str, env: Environment | None = None) -> str:
    env = env or Environment()
    try:
        launcher = refresh_launcher(home, env.launch_script)
        verify(home, launcher, env)
    except PrivateFileError as error:
        raise LoginTaskError(str(error)) from error
    if env.platform == "win32":
        return _register_windows(home, launcher, env)
    if env.platform == "darwin":
        return _register_macos(home, launcher, env)
    return _register_linux(home, launcher, env)


def _remove_launcher(home: str) -> None:
    with contextlib.suppress(OSError):
        remove_file(str(launcher_path(home)))


def _remove_windows(env: Environment) -> str:
    schtasks = _tool(env, "System32", "schtasks.exe")
    deleted = _run(env, schtasks, ["/Delete", "/TN", TASK_NAME, "/F"], "schtasks")
    if deleted.code == 0:
        return f'Removed the Scheduled Task "{TASK_NAME}".'
    if _run(env, schtasks, ["/Query", "/TN", TASK_NAME], "schtasks").code != 0:
        return "No login task is registered."
    raise LoginTaskError(f'schtasks exited {deleted.code} and the task "{TASK_NAME}" remains: {_output(deleted)}')


def _remove_macos(env: Environment) -> str:
    path = _macos_paths(env)
    if not path.is_file():
        return "No login task is registered."
    _run(env, "/bin/launchctl", ["bootout", f"gui/{env.uid()}/{AGENT_LABEL}"], "launchctl")
    try:
        os.remove(path)
    except OSError as error:
        raise LoginTaskError(f"cannot remove {path}: {error}") from error
    return f"Removed the LaunchAgent {path}."


def _remove_linux(env: Environment) -> str:
    path = _linux_paths(env)
    if not path.is_file():
        return "No login task is registered."
    result = _run(env, "/usr/bin/systemctl", ["--user", "disable", UNIT_NAME], "systemctl")
    try:
        os.remove(path)
    except OSError as error:
        raise LoginTaskError(f"cannot remove {path}: {error}") from error
    if result.code != 0:
        raise LoginTaskError(f"removed {path}, but systemctl exited {result.code}: {_output(result)}")
    return f"Removed the systemd user unit {path}."


def remove(home: str, env: Environment | None = None) -> str:
    env = env or Environment()
    if env.platform == "win32":
        message = _remove_windows(env)
    elif env.platform == "darwin":
        message = _remove_macos(env)
    else:
        message = _remove_linux(env)
    _remove_launcher(home)
    return message


def _windows_target(text: str) -> str | None:
    if "<!" in text:
        return None
    try:
        root = ElementTree.fromstring(_XML_DECLARATION.sub("", text.strip()))
    except ElementTree.ParseError:
        return None
    exec_node: Any = root.find(f".//{{{_NAMESPACE}}}Exec")
    if exec_node is None:
        return None
    command = exec_node.findtext(f"{{{_NAMESPACE}}}Command") or ""
    arguments = exec_node.findtext(f"{{{_NAMESPACE}}}Arguments") or ""
    return _single_line(f"{command} {arguments}")


def _status_windows(env: Environment) -> str:
    result = _run(env, _tool(env, "System32", "schtasks.exe"), ["/Query", "/TN", TASK_NAME, "/XML"], "schtasks")
    if result.code != 0:
        return "none"
    target = _windows_target(result.stdout)
    return f'registered as "{TASK_NAME}", runs {target}' if target else f'registered as "{TASK_NAME}", target unreadable'


def _status_file(path: Path, kind: str, target: Callable[[str], str | None]) -> str:
    text = read_text_if_exists(str(path))
    if text is None:
        return "none"
    shown = target(text)
    return f"{kind} {path}, runs {shown}" if shown else f"{kind} {path}, target unreadable"


def _plist_target(text: str) -> str | None:
    try:
        arguments = plistlib.loads(text.encode("utf-8")).get("ProgramArguments")
    except (plistlib.InvalidFileException, ValueError, AttributeError):
        return None
    return _single_line(" ".join(str(word) for word in arguments)) if isinstance(arguments, list) else None


def _unit_target(text: str) -> str | None:
    line = next((row for row in text.splitlines() if row.startswith("ExecStart=")), None)
    return None if line is None else _single_line(line[len("ExecStart=") :])


def status(home: str, env: Environment | None = None) -> str:
    env = env or Environment()
    try:
        if env.platform == "win32":
            return _status_windows(env)
        if env.platform == "darwin":
            return _status_file(_macos_paths(env), "LaunchAgent", _plist_target)
        return _status_file(_linux_paths(env), "systemd user unit", _unit_target)
    except LoginTaskError as error:
        return f"unknown ({error})"
