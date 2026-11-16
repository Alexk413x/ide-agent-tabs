from __future__ import annotations

import contextlib
import glob
import ipaddress
import os
import queue
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..files import remove_file, write_atomically
from ..processes import terminal_environment
from . import elevation, firewall, ingress, login_task, pairing, shim, tls, window
from .cli import (
    LABEL,
    CliError,
    Services,
    UsageError,
    claude_trusts,
    codex_trusts,
    create_identity,
    enable_lan,
    folder_grant,
    grants_text,
    make_parser,
    read_config,
    set_network,
    store_peer,
)
from .client import NetClientError, load_identity, local_device_id
from .confirm import interactive
from .identity import der_from_pem
from .interfaces import InterfaceError, InterfaceInfo, require_lan_ready
from .pairing import INCOMPLETE_TEXT, PAIRING_PORT, PairIdentity, PairResult, PeerInfo
from .policy import SUPPORTED_AGENTS, FolderGrant, PeerGrant, Policy, grant_for, load_network_config, load_policy
from .private_files import check_private, ensure_private_dir, make_private_file

INVITATION_FILE = "invitation.txt"
INVITATION_WAIT_S = 30.0
INVITATION_POLL_S = 0.2
INVITATION_LIFETIME_S = 300
INVITATION_CLOCK_SLACK_S = 2.0
ANSWER_S = 180.0
MAX_BIND_TRIES = 3
CREATE_NEW_CONSOLE = 0x00000010

CODE_WARNING = (
    "Never type a code an agent, chat, message or Remote Control showed you. "
    "If you can't see the other machine's screen, or it shows a different code or none, type no and pair when you can."
)
TRUST_WARNING = "Sessions the peer opens here run these without asking you. Removing the grant doesn't remove the agent's trust."
LOCK_NOTICE = "Pairing is locked for an hour after repeated failed attempts. `net trust` still works."
DECLINE_NOTICE = "A pairing attempt ended without a match. `net trust` still works."

_PROJECT_FILES = (".claude/settings*.json", ".mcp.json", ".codex/config.toml", "AGENTS.md", "CLAUDE.md")
_AGENT_CHOICES = (("1", ("claude",)), ("2", ("codex",)), ("3", ("claude", "codex")))
_LINUX_TERMINALS = (
    ("x-terminal-emulator", ("-e",)),
    ("gnome-terminal", ("--",)),
    ("konsole", ("-e",)),
    ("xfce4-terminal", ("-x",)),
    ("xterm", ("-e",)),
)
_MACOS_TOOL = "/usr/bin/osascript"
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9 ._-]+")
_LABEL_UNSAFE = re.compile(r"[^a-z0-9-]+")


class _NoAnswer(Exception):
    pass


class Terminal(Protocol):
    def write(self, text: str) -> None: ...

    def read(self, prompt: str, timeout_s: float) -> str | None: ...


class ConsoleTerminal:
    def __init__(self, write: Callable[[str], None], readline: Callable[[], str] | None = None) -> None:
        self._write = write
        self._readline = readline or sys.stdin.readline
        self._lines: queue.Queue[str] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._ended = False

    def write(self, text: str) -> None:
        self._write(text)

    def _pump(self) -> None:
        try:
            line = self._readline()
        except (OSError, ValueError):
            line = ""
        if not line:
            self._ended = True
        self._lines.put(line)

    def read(self, prompt: str, timeout_s: float) -> str | None:
        if self._ended:
            return None
        sys.stdout.write(prompt)
        sys.stdout.flush()
        if self._lines.empty() and (self._reader is None or not self._reader.is_alive()):
            self._reader = threading.Thread(target=self._pump, daemon=True)
            self._reader.start()
        try:
            line = self._lines.get(timeout=max(0.0, timeout_s))
        except queue.Empty:
            sys.stdout.write("\n")
            return None
        return line.rstrip("\r\n") if line else None


@dataclass
class Choices:
    label: str
    folders: tuple[FolderGrant, ...] = ()
    agents: tuple[str, ...] = ()
    listener: bool = False
    login: bool = False
    path: bool = False


@dataclass(frozen=True)
class Prepared:
    identity: PairIdentity
    device_id: str
    name: str
    iface: InterfaceInfo
    bind: str


def _caller_name(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    if env.get("CLAUDECODE"):
        return "Claude Code"
    if any(key.startswith("CODEX_") for key in env):
        return "Codex"
    return ""


def find_openssl(
    configured: str | None,
    env: Mapping[str, str],
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
) -> str | None:
    candidates: list[str] = [configured] if configured else []
    if platform == "win32":
        for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            root = env.get(key)
            if root:
                candidates.append(os.path.join(root, "Git", "usr", "bin", "openssl.exe"))
        local = env.get("LOCALAPPDATA")
        if local:
            candidates.append(os.path.join(local, "Programs", "Git", "usr", "bin", "openssl.exe"))
    else:
        found = which("openssl")
        if found:
            candidates.append(found)
    return next((path for path in candidates if os.path.isabs(path) and os.path.isfile(path)), None)


def local_addresses() -> list[str]:
    found: set[str] = set()
    host = socket.gethostname()
    with contextlib.suppress(OSError):
        found.update(socket.gethostbyname_ex(host)[2])
    with contextlib.suppress(OSError):
        found.update(str(info[4][0]) for info in socket.getaddrinfo(host, None, socket.AF_INET))
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("10.255.255.255", 9))
        found.add(str(probe.getsockname()[0]))
    return sorted(found)


def bind_pairing_socket(address: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((address, port))
        sock.listen(4)
    except OSError:
        sock.close()
        raise
    return sock


def open_agent_window(
    agent: str,
    folder: str,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
    popen: Callable[..., Any] = subprocess.Popen,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    if agent not in SUPPORTED_AGENTS:
        return f"{agent} is not an agent that grants cover."
    exe = which(agent)
    if exe is None:
        return f"{agent} is not on the PATH here, so open it in {folder} yourself."
    env = terminal_environment(os.environ if environ is None else environ)
    try:
        if platform == "win32":
            popen([exe], cwd=folder, env=env, creationflags=CREATE_NEW_CONSOLE, close_fds=True, shell=False)
        elif platform == "darwin":
            line = f"cd {shlex.quote(folder)} && {shlex.quote(exe)}"
            script = ["-e", "on run argv", "-e", 'tell application "Terminal"', "-e", "activate", "-e", "do script (item 1 of argv)"]
            popen([_MACOS_TOOL, *script, "-e", "end tell", "-e", "end run", line], cwd=folder, env=env, close_fds=True, shell=False)
        else:
            if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
                return f"No terminal window can open here, so open {agent} in {folder} yourself."
            for name, lead in _LINUX_TERMINALS:
                found = which(name)
                if found:
                    popen([found, *lead, exe], cwd=folder, env=env, close_fds=True, start_new_session=True, shell=False)
                    return None
            return f"No terminal program was found, so open {agent} in {folder} yourself."
    except OSError as error:
        return f"{agent} could not be opened: {error}. Open it in {folder} yourself."
    return None


def _require_lan_ready(address: str, allow_virtual: bool) -> InterfaceInfo:
    return require_lan_ready(address, allow_virtual=allow_virtual)


@dataclass
class PairDeps:
    interactive: Callable[[], bool] = interactive
    refuse_if_elevated: Callable[[], None] = elevation.refuse_if_elevated
    open_window: Callable[[list[str], str | None, str], str] = window.open_command_window
    caller: Callable[[], str] = _caller_name
    terminal: Callable[[Callable[[str], None]], Terminal] = ConsoleTerminal
    local_addresses: Callable[[], list[str]] = local_addresses
    require_lan_ready: Callable[[str, bool], InterfaceInfo] = _require_lan_ready
    find_openssl: Callable[[str | None, Mapping[str, str]], str | None] = find_openssl
    ensure_rule: Callable[[str, str, str], str] = firewall.apply_listener_rule
    bind_socket: Callable[[str, int], socket.socket] = bind_pairing_socket
    check_destination: Callable[[str, int, str, int], str] = pairing.check_lan_destination
    listen: Callable[..., PairResult] = pairing.listen_once
    dial: Callable[..., PairResult] = pairing.dial
    timeouts: pairing.Timeouts = pairing.DEFAULT_TIMEOUTS
    install_shim: Callable[[str, str, str], Path] = shim.install_shim
    path_contains: Callable[[Path], bool] = shim.path_contains
    append_to_user_path: Callable[[Path], str] = shim.append_to_user_path
    open_agent: Callable[[str, str], str | None] = open_agent_window
    interpreter: str = field(default_factory=lambda: window.console_interpreter(sys.executable))
    launch_script: str = field(default_factory=login_task.default_launch_script)
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    invitation_wait_s: float = INVITATION_WAIT_S


def default_deps() -> PairDeps:
    return PairDeps()


def invitation_path(home: str) -> Path:
    return ingress.net_dir(home) / INVITATION_FILE


def write_invitation(home: str, line: str) -> None:
    path = invitation_path(home)
    ensure_private_dir(path.parent)
    write_atomically(str(path), line + "\n")
    make_private_file(path)


def read_invitation(home: str, not_before: float, now: float) -> str | None:
    path = invitation_path(home)
    try:
        if path.stat().st_mtime < not_before or check_private(path):
            return None
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if len(lines) != 1:
        return None
    try:
        pairing.parse_invitation(lines[0], now)
    except pairing.PairingError:
        return None
    return lines[0].strip()


def _remove_invitation(home: str) -> None:
    with contextlib.suppress(OSError):
        remove_file(str(invitation_path(home)))


@dataclass(frozen=True)
class Mode:
    kind: str
    invitation: str = ""
    peer: str = ""
    token: str = ""

    def window_command(self) -> tuple[list[str], str | None]:
        if self.kind == "join":
            return ["net", "pair", "--join"], self.invitation
        if self.kind == "peer":
            return ["net", "pair", "--peer", self.peer, "--token", self.token], None
        if self.kind == "invite":
            return ["net", "pair", "--invite"], None
        return ["net", "pair"], None


def _mode(args: Sequence[str]) -> Mode:
    parser = make_parser("pair")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--invite", action="store_true")
    group.add_argument("--join")
    group.add_argument("--peer")
    parser.add_argument("--token")
    options = parser.parse_args(args)
    if (options.peer is None) != (options.token is None):
        raise UsageError("agent-tabs net pair: --peer and --token go together")
    if options.invite:
        return Mode("invite")
    if options.join is not None:
        return Mode("join", invitation=options.join.strip())
    if options.peer is not None:
        return Mode("peer", peer=options.peer.strip(), token=pairing.parse_manual_token(options.token))
    return Mode("listen")


def _device_name(svc: Services) -> str:
    configured: object = None
    with contextlib.suppress(CliError):
        section = read_config(svc.home).get("network")
        if isinstance(section, dict):
            configured = section.get("deviceName")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if configured is not None:
        if not isinstance(configured, str) or not pairing.valid_device_name(configured):
            raise CliError("network.deviceName in config.json must be 1 to 32 letters, digits, spaces, - _ or ., with no run of six digits")
        return configured
    cleaned = _NAME_UNSAFE.sub("-", socket.gethostname()).strip(" -.")[:32].strip(" -.")
    return cleaned if cleaned and pairing.valid_device_name(cleaned) else "this-machine"


def _default_label(name: str, policy: Policy) -> str:
    base = _LABEL_UNSAFE.sub("-", name.lower()).strip("-")[:32].strip("-") or "peer"
    taken = {peer.label for peer in policy.peers}
    label, number = base, 2
    while label in taken:
        suffix = f"-{number}"
        label, number = base[: 32 - len(suffix)] + suffix, number + 1
    return label


def _default_alias(raw: str) -> str:
    base = os.path.basename(os.path.normpath(raw))
    return _LABEL_UNSAFE.sub("-", base.lower()).strip("-")[:32].strip("-") or "work"


def _normal(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _inside(child: str, parent: str) -> bool:
    left, right = _normal(child), _normal(parent)
    return left != right and left.startswith(right.rstrip(os.sep) + os.sep)


def _project_files(folder: str) -> list[str]:
    found: list[str] = []
    for pattern in _PROJECT_FILES:
        found.extend(
            os.path.relpath(path, folder).replace(os.sep, "/") for path in sorted(glob.glob(os.path.join(folder, *pattern.split("/"))))
        )
    return found


class ConsolePrompter:
    def __init__(self, svc: Services, deps: PairDeps, terminal: Terminal, own: Prepared, shim_folder: Path | None) -> None:
        self.svc = svc
        self.deps = deps
        self.terminal = terminal
        self.own = own
        self.shim_folder = shim_folder
        self.choices: Choices | None = None
        self.saved: PeerGrant | None = None

    def _ask(self, prompt: str, deadline: float) -> str:
        remaining = deadline - time.monotonic()
        answer = self.terminal.read(prompt, remaining) if remaining > 0 else None
        if answer is None:
            raise _NoAnswer
        return answer.strip()

    def _yes(self, question: str, deadline: float, default: bool) -> bool:
        suffix = " [Y/n]: " if default else " [y/N]: "
        while True:
            answer = self._ask(question + suffix, deadline).lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False

    def show_code(self, peer: PeerInfo, code: str) -> None:
        write = self.terminal.write
        write("")
        write("The other machine")
        write(f"  name:       {peer.name}")
        write(f"  address:    {peer.address}")
        write(f"  device id:  {peer.device_id}")
        write("")
        write(f"Code: {code}")
        write("")
        write(CODE_WARNING)
        write("")

    def read_code(self, peer: PeerInfo, timeout_s: float) -> str | None:
        answer = self.terminal.read("Type the code shown on the other machine's screen, or type no: ", timeout_s)
        if answer is None:
            return None
        text = answer.strip()
        return None if not text or text.lower() == "no" else text

    def review(self, peer: PeerInfo, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        try:
            choices = self._collect(peer, deadline)
            self._summary(peer, choices)
            if self._ask("Type yes to pair and apply this summary, or anything else to cancel: ", deadline) != "yes":
                return False
        except _NoAnswer:
            return False
        except (CliError, ValueError) as error:
            self.terminal.write(f"error: {error}")
            return False
        self.choices = choices
        return True

    def _collect(self, peer: PeerInfo, deadline: float) -> Choices:
        policy = load_policy(self.svc.home)
        known = grant_for(policy, peer.device_id)
        write = self.terminal.write
        write("")
        write(f"Review pairing with {peer.name} at {peer.address}")
        if known is not None:
            write("This device id is already trusted here. Nothing changes until you answer yes.")
            write(f"  label:    {known.label}")
            write(f"  address:  {known.address}")
            write(f"  grants:   {grants_text(known)}")
            write(f"  Typing yes keeps these grants and updates the address to {peer.address}.")
            choices = Choices(known.label, known.folders, known.agents)
        else:
            choices = Choices(self._label(peer, policy, deadline))
            choices.folders, choices.agents = self._grants(choices.label, policy, deadline)
        choices.listener = self._yes(
            "Turn the listener on, with its firewall rule, so the other machine can reach this one?", deadline, True
        )
        if choices.listener:
            choices.login = self._yes("Start the listener each time you sign in?", deadline, True)
        if self.shim_folder is not None and not self.deps.path_contains(self.shim_folder):
            choices.path = self._yes(f"Append {self.shim_folder} to your user PATH so agent-tabs works in any terminal?", deadline, False)
        return choices

    def _label(self, peer: PeerInfo, policy: Policy, deadline: float) -> str:
        default = _default_label(peer.name, policy)
        while True:
            label = self._ask(f"Label for this peer [{default}]: ", deadline) or default
            if LABEL.fullmatch(label) is None:
                self.terminal.write("A label is 1 to 32 characters of a-z, 0-9 and -.")
            elif any(p.label == label for p in policy.peers):
                self.terminal.write(f"The label {label} belongs to another peer.")
            else:
                return label

    def _grants(self, label: str, policy: Policy, deadline: float) -> tuple[tuple[FolderGrant, ...], tuple[str, ...]]:
        while True:
            raw = self._ask(f"Folder {label} may open agent tabs in (absolute path), or press Enter for no grants: ", deadline)
            if not raw:
                return (), ()
            try:
                folder = self._folder(raw, policy)
            except CliError as error:
                self.terminal.write(f"error: {error}")
                continue
            agents = self._agents(deadline)
            return ((folder,), agents) if self._trusted(folder, agents, deadline) else ((), ())

    def _folder(self, raw: str, policy: Policy) -> FolderGrant:
        grant = folder_grant(f"{_default_alias(raw)}={raw}")
        if _normal(grant.path) in (_normal(self.svc.user_home), _normal(os.path.realpath(self.svc.user_home))):
            raise CliError("the home folder is too broad to grant")
        if os.path.dirname(os.path.normpath(grant.path)) == os.path.normpath(grant.path):
            raise CliError("a drive root is too broad to grant")
        for peer in policy.peers:
            if any(_inside(held.path, grant.path) for held in peer.folders):
                raise CliError(f"{grant.path} holds folders already granted to {peer.label}")
        return grant

    def _agents(self, deadline: float) -> tuple[str, ...]:
        while True:
            answer = self._ask("Agents that may open tabs there: 1 claude, 2 codex, 3 both [1]: ", deadline) or "1"
            for number, agents in _AGENT_CHOICES:
                if answer == number:
                    return agents

    def _trusted(self, folder: FolderGrant, agents: Sequence[str], deadline: float) -> bool:
        checks: dict[str, Callable[[Services, str], bool]] = {"claude": claude_trusts, "codex": codex_trusts}
        write = self.terminal.write
        for agent in agents:
            first = True
            while not checks[agent](self.svc, folder.path):
                if first:
                    write(f"{agent} has not trusted this folder yet.")
                    write(f"  folder: {folder.path}")
                    files = _project_files(folder.path)
                    write("  project files that trust activates here: " + (", ".join(files) if files else "none found"))
                    write(f"  {TRUST_WARNING}")
                    if self._yes(f"Open {agent} in that folder in a new window so you can answer its trust dialog?", deadline, True):
                        problem = self.deps.open_agent(agent, folder.path)
                        if problem:
                            write(problem)
                    first = False
                answer = self._ask(f"Press Enter after {agent} trusts the folder, or type no to skip this grant: ", deadline)
                if answer.lower() == "no":
                    return False
        return True

    def _summary(self, peer: PeerInfo, choices: Choices) -> None:
        write = self.terminal.write
        write("")
        write("Summary")
        write(f"  peer:             {peer.name} at {peer.address}")
        write(f"  device id:        {peer.device_id}")
        write(f"  label:            {choices.label}")
        folders = ", ".join(f"{f.alias}={f.path}" for f in choices.folders)
        write(f"  folder grant:     {folders or 'none'}" + (f" for {', '.join(choices.agents)}" if choices.folders else ""))
        write(f"  listener:         {'on, with its firewall rule' if choices.listener else 'off'}")
        write(f"  start at login:   {'yes' if choices.login else 'no'}")
        if self.shim_folder is not None:
            write(f"  user PATH:        {'append ' + str(self.shim_folder) if choices.path else 'unchanged'}")

    def save(self, peer: PeerInfo) -> None:
        choices = self.choices
        if choices is None:
            raise CliError("the review did not finish")
        result, is_new = store_peer(self.svc, peer.device_id, peer.pem, peer.address, choices.label, choices.folders, choices.agents)
        self.saved = result
        self.svc.record(
            "pair", device=peer.device_id, label=result.label, address=peer.address, new=int(is_new), folders=len(result.folders)
        )
        self.svc.notify(f"paired with {result.label} at {peer.address}")


def _prepare(svc: Services, deps: PairDeps, terminal: Terminal) -> Prepared:
    problem = tls.runtime_problem()
    if problem is not None:
        raise CliError(problem)
    config, warnings = load_network_config(svc.home)
    for warning in warnings:
        svc.err(f"warning: {warning}")
    name = _device_name(svc)
    named = config.bind is not None
    bind = config.bind if config.bind is not None else _choose_bind(svc, deps, terminal)
    iface = deps.require_lan_ready(bind, named)
    if local_device_id(svc.home) is None:
        openssl = deps.find_openssl(config.openssl, svc.env)
        if openssl is None:
            raise CliError(
                "No openssl program was found. Install Git for Windows (or openssl elsewhere), "
                "or run agent-tabs net lan setup --openssl <absolute path of openssl>, then pair again."
            )
        svc.out(f"Creating this machine's network identity with openssl at {openssl}.")
        create_identity(svc, openssl)
    try:
        local = load_identity(svc.home)
        der = der_from_pem(local.cert.read_text(encoding="ascii"))
    except (NetClientError, OSError, ValueError) as error:
        raise CliError(f"this machine's network identity cannot be read: {error}") from error
    identity = PairIdentity(name, der, local.cert, local.key, lambda: local.passphrase)
    device = local_device_id(svc.home)
    if device is None:
        raise CliError("this machine's network identity cannot be read")
    return Prepared(identity, device, name, iface, bind)


def _choose_bind(svc: Services, deps: PairDeps, terminal: Terminal) -> str:
    found: list[InterfaceInfo] = []
    for address in deps.local_addresses():
        try:
            ip = ipaddress.IPv4Address(address)
            info = svc.interface_for(address)
        except (ValueError, InterfaceError):
            continue
        if ip.is_private and not ip.is_loopback and not ip.is_link_local and info.physical:
            found.append(info)
    if not found:
        raise CliError(
            "No private address (10.x.x.x, 172.16-31.x.x or 192.168.x.x) of a physical network interface was found. "
            'Set "bind" in the "network" section of config.json to this machine\'s address, then pair again.'
        )
    if len(found) == 1:
        svc.out(f"Using {found[0].address} on {found[0].alias or 'the network'}.")
        return found[0].address
    for _ in range(MAX_BIND_TRIES):
        terminal.write("This machine's addresses:")
        for number, info in enumerate(found, 1):
            terminal.write(f"  {number}. {info.address}  {info.alias}")
        answer = terminal.read("Type the number of the address to use: ", ANSWER_S)
        if answer is None:
            raise CliError("no address was chosen")
        if answer.strip().isdigit() and 1 <= int(answer.strip()) <= len(found):
            return found[int(answer.strip()) - 1].address
    raise CliError("no address was chosen")


def _wait_for_invitation(svc: Services, deps: PairDeps, started: float) -> str | None:
    deadline = deps.clock() + deps.invitation_wait_s
    while True:
        now = deps.clock()
        line = read_invitation(svc.home, started - INVITATION_CLOCK_SLACK_S, now)
        if line is not None:
            return line
        if now >= deadline:
            return None
        deps.sleep(INVITATION_POLL_S)


def _hand_off(svc: Services, deps: PairDeps, mode: Mode) -> int:
    started = deps.clock()
    subcommand, invitation = mode.window_command()
    message = deps.open_window(subcommand, invitation, deps.caller())
    svc.out(message)
    if message != window.CONTINUE_MESSAGE:
        return 1
    if mode.kind != "invite":
        return 0
    line = _wait_for_invitation(svc, deps, started)
    if line is None:
        svc.err(f"The window showed no invitation within {deps.invitation_wait_s:g} s. Read it in the window.")
        return 1
    svc.out(line)
    return 0


def _notify_lock(svc: Services, result: PairResult) -> None:
    if result.locked:
        svc.notify(LOCK_NOTICE)
        svc.err(LOCK_NOTICE)
    elif result.outcome == "declined":
        svc.notify(DECLINE_NOTICE)


def _listen(svc: Services, deps: PairDeps, prepared: Prepared, prompter: ConsolePrompter, mode: Mode, limits: pairing.Limits) -> PairResult:
    limits.check()
    try:
        text = deps.ensure_rule(prepared.bind, prepared.iface.subnet, prepared.iface.alias)
    except firewall.FirewallError as error:
        raise CliError(f"{error}. The other machine can reach this one only through the firewall rule {firewall.LISTENER_RULE}") from error
    if text:
        svc.out(text)
    limits.open_window()
    sock: socket.socket | None = None
    try:
        sock = deps.bind_socket(prepared.bind, PAIRING_PORT)
        port = int(sock.getsockname()[1])
        if mode.kind == "invite":
            token = pairing.new_invitation_token()
            line = pairing.make_invitation(
                prepared.name, prepared.bind, port, prepared.device_id, token, int(deps.clock()) + INVITATION_LIFETIME_S
            )
            write_invitation(svc.home, line)
            svc.out("Invitation, valid for 5 minutes:")
            svc.out(line)
            svc.out(f"On the other machine run: agent-tabs net pair --join {line}")
        else:
            token = pairing.new_manual_token()
            svc.out(f"On the other machine run: agent-tabs net pair --peer {prepared.bind} --token {token}")
        svc.out("Waiting for the other machine. This window closes after 5 minutes.")
        return deps.listen(sock, token, prepared.identity, prompter, prompter.save, limits, timeouts=deps.timeouts)
    finally:
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()
        limits.close_window()
        _remove_invitation(svc.home)


def _dial(
    svc: Services, deps: PairDeps, prepared: Prepared, prompter: ConsolePrompter, mode: Mode, limits: pairing.Limits
) -> PairResult | None:
    limits.check_locked()
    if mode.kind == "join":
        invitation = pairing.parse_invitation(mode.invitation, deps.clock())
        address, port, token, name = invitation.address, invitation.port, invitation.token, invitation.name
    else:
        address, port, token, name = mode.peer, PAIRING_PORT, mode.token, None
    checked = deps.check_destination(address, port, prepared.iface.address, prepared.iface.prefix_len)
    prompter.terminal.write(f"This machine: {prepared.name} {prepared.bind}")
    target = f"{name} at {checked}" if name else f"the machine at {checked}"
    answer = prompter.terminal.read(f"Pair with {target}? Type y to continue: ", ANSWER_S)
    if answer is None:
        svc.err("No answer, so nothing was sent.")
        return None
    if answer.strip().lower() not in ("y", "yes"):
        locked = limits.record_failure()
        svc.err("Cancelled. Nothing was sent.")
        _notify_lock(svc, PairResult("declined", "declined", None, locked))
        return None
    return deps.dial(checked, port, token, prepared.identity, prompter, prompter.save, limits, timeouts=deps.timeouts)


def _apply(svc: Services, deps: PairDeps, prepared: Prepared, choices: Choices) -> bool:
    set_network(svc.home, {"bind": prepared.bind})
    ok = True
    if choices.listener:
        ingress.ensure_ingress_token(svc.home)
        config, _ = load_network_config(svc.home)
        ok = enable_lan(svc, prepared.bind, config.port, choices.login)
    if choices.path:
        try:
            svc.out(deps.append_to_user_path(shim.shim_folder(svc.home)))
        except (shim.ShimError, OSError) as error:
            svc.err(f"warning: the user PATH was not changed: {error}")
            ok = False
    return ok


def _interactive(svc: Services, deps: PairDeps, mode: Mode) -> int:
    terminal = deps.terminal(svc.out)
    prepared = _prepare(svc, deps, terminal)
    shim_folder: Path | None = None
    try:
        shim_folder = deps.install_shim(svc.home, deps.interpreter, deps.launch_script)
    except shim.ShimError as error:
        svc.err(f"warning: {error}")
    prompter = ConsolePrompter(svc, deps, terminal, prepared, shim_folder)
    limits = pairing.Limits(svc.home)
    if mode.kind in ("invite", "listen"):
        result: PairResult | None = _listen(svc, deps, prepared, prompter, mode, limits)
    else:
        result = _dial(svc, deps, prepared, prompter, mode, limits)
    if result is None:
        return 1
    _notify_lock(svc, result)
    saved, choices = prompter.saved, prompter.choices
    if result.outcome not in ("paired", "incomplete") or saved is None or choices is None:
        svc.err(f"Pairing did not finish: {result.reason}. Nothing was saved on this machine.")
        return 1
    svc.out(f"Saved {saved.label} ({saved.device_id}) at {saved.address}. Grants: {grants_text(saved)}.")
    applied = _apply(svc, deps, prepared, choices)
    if result.outcome == "incomplete":
        svc.err(INCOMPLETE_TEXT.replace("<label>", saved.label))
        return 1
    return 0 if applied else 1


def run(svc: Services, deps: PairDeps, args: Sequence[str]) -> int:
    deps.refuse_if_elevated()
    mode = _mode(args)
    if not deps.interactive():
        return _hand_off(svc, deps, mode)
    try:
        return _interactive(svc, deps, mode)
    except KeyboardInterrupt:
        svc.err("Pairing stopped.")
        return 1
