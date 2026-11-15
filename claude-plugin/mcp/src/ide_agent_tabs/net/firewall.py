from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
from collections.abc import Callable, Sequence

from ..processes import RunResult
from .login_task import Environment, LoginTaskError
from .policy import is_allowed_bind

RULE_NAME = "Agent Tabs LAN"
ELEVATION_TIMEOUT_S = 150.0
PROBE_TIMEOUT_S = 30.0
DECLINED = 1223
UNKNOWN = 1225

_POWERSHELL_ENV = "AGENT_TABS_POWERSHELL"
_ELEVATED_ENV = "AGENT_TABS_ELEVATED"
_UNSAFE_PROGRAM = re.compile(r"[\x00-\x1f\"]")
_DRIVE_PATH = re.compile(r"[A-Za-z]:\\")

_OUTER_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "try { "
    f"$p = Start-Process -FilePath $env:{_POWERSHELL_ENV} "
    f"-ArgumentList @('-NoProfile', '-NonInteractive', '-EncodedCommand', $env:{_ELEVATED_ENV}) "
    "-Verb RunAs -Wait -PassThru -WindowStyle Hidden "
    f"}} catch {{ exit {DECLINED} }}; "
    f"if ($null -eq $p.ExitCode) {{ exit {UNKNOWN} }}; "
    "exit $p.ExitCode"
)

_INNER_SCRIPT = (
    "$plan = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{plan}')) | ConvertFrom-Json\n"
    "& $plan.netsh @($plan.delete) | Out-Null\n"
    "$code = $LASTEXITCODE\n"
    "if (@($plan.add).Count -gt 0) { & $plan.netsh @($plan.add) | Out-Null; $code = $LASTEXITCODE }\n"
    "exit $code\n"
)


class FirewallError(Exception):
    pass


def _address(text: str) -> str:
    try:
        ip = ipaddress.ip_address(text) if "%" not in text else None
    except ValueError:
        ip = None
    if ip is None or ip.is_unspecified or ip.is_multicast:
        raise FirewallError(f"{text!r} is not the IP address of one machine")
    return str(ip)


def _port(port: int) -> str:
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port <= 65535:
        raise FirewallError(f"{port!r} is not a port number")
    return str(port)


def _program(path: str) -> str:
    if not _DRIVE_PATH.match(path) or _UNSAFE_PROGRAM.search(path):
        raise FirewallError(f"{path!r} is not an absolute Windows path without quotes or control characters")
    return path


def _bind(bind: str) -> str:
    address = _address(bind)
    if not is_allowed_bind(address):
        raise FirewallError(f"{bind} is not a private address")
    return address


def rule_arguments(bind: str, port: int, peers: Sequence[str], program: str) -> list[str]:
    remote = sorted({_address(peer) for peer in peers}, key=lambda text: (":" in text, text))
    if not remote:
        raise FirewallError("no trusted peer has an address to allow")
    # PowerShell hands an argument that holds a space to netsh in quotes, so these values carry none of their own.
    return [
        "advfirewall",
        "firewall",
        "add",
        "rule",
        f"name={RULE_NAME}",
        "dir=in",
        "action=allow",
        "protocol=TCP",
        f"localport={_port(port)}",
        f"localip={_bind(bind)}",
        f"remoteip={','.join(remote)}",
        "profile=private",
        f"program={_program(program)}",
    ]


def delete_arguments() -> list[str]:
    return ["advfirewall", "firewall", "delete", "rule", f"name={RULE_NAME}"]


def show_arguments() -> list[str]:
    return ["advfirewall", "firewall", "show", "rule", f"name={RULE_NAME}"]


def elevated_command(netsh: str, delete: Sequence[str], add: Sequence[str]) -> str:
    plan = json.dumps({"netsh": netsh, "delete": list(delete), "add": list(add)}, ensure_ascii=True)
    blob = base64.b64encode(plan.encode("utf-8")).decode("ascii")
    return base64.b64encode(_INNER_SCRIPT.replace("{plan}", blob).encode("utf-16-le")).decode("ascii")


def _tools(env: Environment) -> tuple[str, str]:
    root = env.windows_directory()
    return (
        os.path.join(root, "System32", "netsh.exe"),
        os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
    )


def _elevate(env: Environment, delete: Sequence[str], add: Sequence[str]) -> None:
    netsh, powershell = _tools(env)
    variables = {**os.environ, _POWERSHELL_ENV: powershell, _ELEVATED_ENV: elevated_command(netsh, delete, add)}
    try:
        result = env.run(
            powershell, ["-NoProfile", "-NonInteractive", "-Command", _OUTER_SCRIPT], env=variables, timeout=ELEVATION_TIMEOUT_S
        )
    except (OSError, TimeoutError) as error:
        raise FirewallError(f"the elevated step did not finish: {error}") from error
    if result.code == DECLINED:
        raise FirewallError("the Windows prompt for administrator rights was declined, so the firewall rule did not change")
    if result.code != 0:
        raise FirewallError(f"the elevated netsh step ended with code {result.code}; the firewall rule may not have changed")


def rule_exists(env: Environment) -> bool:
    netsh, _ = _tools(env)
    try:
        result: RunResult = env.run(netsh, show_arguments(), timeout=PROBE_TIMEOUT_S)
    except (OSError, TimeoutError) as error:
        raise FirewallError(f"netsh could not list the firewall rules: {error}") from error
    return result.code == 0


def manual_rule_text(bind: str, port: int, peers: Sequence[str]) -> str:
    remote = ", ".join(sorted(set(peers))) or "the addresses of your trusted peers"
    return (
        f"This system changes no firewall rule. If a firewall runs here, allow inbound TCP port {port} on {bind} "
        f"from {remote} for the Python that runs the listener, and nothing wider."
    )


def apply_rule(
    bind: str,
    port: int,
    peers: Sequence[str],
    env: Environment | None = None,
    exists: Callable[[str], bool] = os.path.isfile,
) -> str:
    env = env or Environment()
    if env.platform != "win32":
        return manual_rule_text(bind, port, peers)
    try:
        if not peers:
            return remove_rule(env, "No trusted peer has an address, so the firewall rule is removed and none is created.")
        if not exists(env.interpreter):
            raise FirewallError(f"the interpreter {env.interpreter} is not a file")
        add = rule_arguments(bind, port, peers, env.interpreter)
        _elevate(env, delete_arguments(), add)
    except LoginTaskError as error:
        raise FirewallError(str(error)) from error
    return f'Created the Windows firewall rule "{RULE_NAME}" for port {port} on {bind} from {", ".join(sorted(set(peers)))}.'


def remove_rule(env: Environment | None = None, prefix: str = "") -> str:
    env = env or Environment()
    if env.platform != "win32":
        return ""
    try:
        if not rule_exists(env):
            return f"{prefix} No firewall rule is present.".strip()
        _elevate(env, delete_arguments(), [])
    except LoginTaskError as error:
        raise FirewallError(str(error)) from error
    return f'{prefix} Removed the Windows firewall rule "{RULE_NAME}".'.strip()
