from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass

from .interfaces import InterfaceError, interface_for, is_safe_alias
from .login_task import Environment, LoginTaskError

LISTENER_RULE = "Agent Tabs LAN"
LISTENER_PORT = 47829
PAIRING_PORT = 47830
PROFILE_PRIVATE = "Private"
PROFILE_ANY = "Any"
ELEVATION_TIMEOUT_S = 150.0
PROBE_TIMEOUT_S = 30.0
DECLINED = 1223
UNKNOWN = 1225

EXIT_REFUSED = 2
EXIT_NOT_CREATED = 3
EXIT_MISMATCH = 4
EXIT_NOT_REMOVED = 5

_POWERSHELL_ENV = "AGENT_TABS_POWERSHELL"
_HELPER_ENV = "AGENT_TABS_HELPER"
_PARAMS_ENV = "AGENT_TABS_PARAMS"
_RULE_ENV = "AGENT_TABS_RULE"

LISTENER_PORTS = (LISTENER_PORT, PAIRING_PORT)
_MATCH_ENV = "AGENT_TABS_MATCH"
_PROGRAM = re.compile(r"[A-Za-z]:\\(?:[^\\/:*?<>|\x00-\x1f\"]+\\)*python(?:w)?\.exe", re.IGNORECASE)
_DOTS = re.compile(r"(?:^|\\)\.\.?(?:\\|$)")
_PRIVATE_RANGES = tuple(ipaddress.IPv4Network(text) for text in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_TAILSCALE_RANGE = ipaddress.IPv4Network("100.64.0.0/10")
_ALLOWED_RANGES = {PROFILE_PRIVATE: _PRIVATE_RANGES, PROFILE_ANY: (_TAILSCALE_RANGE,)}
_PARAMS_CHARS = re.compile(r"[A-Za-z0-9+/=]{1,4096}")

# Runs unelevated. It asks Windows for one UAC prompt that starts the fixed helper below, and it hands the helper
# its parameters as the one argument after the script. Both values come from the environment, so the helper's text
# never holds a value.
_OUTER_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; try { "
    f"$p = Start-Process -FilePath $env:{_POWERSHELL_ENV} "
    f"-ArgumentList @('-NoProfile', '-NonInteractive', '-Command', ('\"' + $env:{_HELPER_ENV} + '\"'), $env:{_PARAMS_ENV}) "
    "-Verb RunAs -Wait -PassThru -WindowStyle Hidden "
    f"}} catch {{ exit {DECLINED} }}; "
    f"if ($null -eq $p.ExitCode) {{ exit {UNKNOWN} }}; "
    "exit $p.ExitCode"
)

# The elevated helper. It is one line without double quotes, because it travels on a command line. It validates
# every value again, creates or removes the one fixed rule, reads a created rule back, and removes the rule when
# any step fails.
_HELPER_LINES = (
    "& { param([string]$Data)",
    "$ErrorActionPreference = 'Stop';",
    (
        "function Ip([string]$s) { if ($s -notmatch '^(0|[1-9][0-9]{0,2})(\\.(0|[1-9][0-9]{0,2})){3}$') { return $null };"
        " $v = [long]0; foreach ($o in $s.Split('.')) { if ([int]$o -gt 255) { return $null }; $v = $v * 256 + [int]$o }; return $v };"
    ),
    "function Mask([int]$b) { return (4294967295 -shl (32 - $b)) -band 4294967295 };",
    "function Dotted([long]$v) { return ((24, 16, 8, 0 | ForEach-Object { ($v -shr $_) -band 255 }) -join '.') };",
    "function Within($ip, $net, [int]$len) { return (($ip -band (Mask $len)) -eq $net) };",
    "$e = 2; $rule = ''; $created = $false;",
    "try {",
    "if ($Data -notmatch '^[A-Za-z0-9+/=]{1,4096}$') { throw 'data' };",
    "$p = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Data)) | ConvertFrom-Json;",
    "$rule = [string]$p.rule;",
    "if ($rule -cne 'Agent Tabs LAN') { throw 'rule' };",
    "if ($p.action -ceq 'remove') {",
    "$e = 5;",
    "if (@(Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue).Count -gt 0) { Remove-NetFirewallRule -DisplayName $rule -ErrorAction Stop };",
    "if (@(Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue).Count -gt 0) { throw 'left' };",
    "exit 0 };",
    "if ($p.action -cne 'add') { throw 'action' };",
    "$ports = @('47829', '47830');",
    "if ((@($p.ports | ForEach-Object { [string][int]$_ }) -join ',') -cne ($ports -join ',')) { throw 'port' };",
    "$prof = [string]$p.profile;",
    "if ($prof -cne 'Private' -and $prof -cne 'Any') { throw 'profile' };",
    (
        "$ranges = @(); if ($prof -ceq 'Private') { $ranges += ,@(167772160, 8); $ranges += ,@(2886729728, 12); $ranges += ,@(3232235520, 16) }"
        " else { $ranges += ,@(1681915904, 10) };"
    ),
    "$program = [string]$p.program;",
    "if ($program -notmatch '^[A-Za-z]:\\\\([^\\\\/:*?<>|\\x00-\\x1f\\x22]+\\\\)*python(w)?\\.exe$' -or $program -match '(^|\\\\)\\.\\.?(\\\\|$)') { throw 'program' };",
    "if (-not (Test-Path -LiteralPath $program -PathType Leaf)) { throw 'program file' };",
    "$alias = [string]$p.alias;",
    "if ($alias -cnotmatch '^[A-Za-z0-9._()#-]([A-Za-z0-9 ._()#-]{0,62}[A-Za-z0-9._()#-])?$') { throw 'alias' };",
    "$m = [regex]::Match([string]$p.remote, '^([0-9.]+)/([0-9]{1,2})$');",
    "if (-not $m.Success) { throw 'remote' };",
    "$net = Ip $m.Groups[1].Value; $len = [int]$m.Groups[2].Value;",
    "if ($null -eq $net -or $len -gt 32 -or ($net -band (Mask $len)) -ne $net) { throw 'remote' };",
    "$ok = $false; foreach ($r in $ranges) { if ($len -ge $r[1] -and (Within $net $r[0] $r[1])) { $ok = $true } };",
    "if (-not $ok) { throw 'remote range' };",
    "$local = Ip ([string]$p.local);",
    "if ($null -eq $local -or -not (Within $local $net $len)) { throw 'local' };",
    "$e = 3;",
    "if (@(Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue).Count -gt 0) { Remove-NetFirewallRule -DisplayName $rule -ErrorAction Stop };",
    (
        "New-NetFirewallRule -DisplayName $rule -Direction Inbound -Action Allow -Protocol TCP -LocalPort $ports"
        " -LocalAddress $p.local -RemoteAddress $p.remote -Program $program -Profile $prof -InterfaceAlias $alias -Enabled True -ErrorAction Stop | Out-Null;"
    ),
    "$created = $true;",
    "$e = 4;",
    "$r = @(Get-NetFirewallRule -DisplayName $rule);",
    "if ($r.Count -ne 1) { throw 'count' };",
    "$r = $r[0];",
    (
        "if ([string]$r.Enabled -ne 'True' -or [string]$r.Direction -ne 'Inbound' -or [string]$r.Action -ne 'Allow' -or [string]$r.Profile -ne $prof)"
        " { throw 'rule' };"
    ),
    "$pf = $r | Get-NetFirewallPortFilter;",
    (
        "if ([string]$pf.Protocol -ne 'TCP' -or (@($pf.LocalPort | Sort-Object) -join ',') -ne ($ports -join ',')"
        " -or (@($pf.RemotePort) -join ',') -ne 'Any') { throw 'port filter' };"
    ),
    "$af = $r | Get-NetFirewallAddressFilter;",
    "$remote = @($af.RemoteAddress) -join ',';",
    "$wide = (Dotted $net) + '/' + (Dotted (Mask $len));",
    (
        "if (@($af.LocalAddress).Count -ne 1 -or [string]$af.LocalAddress -ne [string]$p.local -or ($remote -ne [string]$p.remote -and $remote -ne $wide))"
        " { throw 'address filter' };"
    ),
    "$ap = $r | Get-NetFirewallApplicationFilter;",
    "if ([string]$ap.Program -ne $program) { throw 'application filter' };",
    "$nf = $r | Get-NetFirewallInterfaceFilter;",
    "if ((@($nf.InterfaceAlias) -join ',') -ne $alias) { throw 'interface filter' };",
    "exit 0",
    "} catch {",
    "if ($created -and $e -lt 5) { try { Remove-NetFirewallRule -DisplayName $rule -ErrorAction Stop } catch { } };",
    "exit $e }",
    "}",
)
HELPER_SCRIPT = " ".join(_HELPER_LINES)

_PROBE_SCRIPT = (
    "try { if (@(Get-NetFirewallRule -DisplayName $env:"
    + _RULE_ENV
    + " -ErrorAction SilentlyContinue).Count -gt 0) { exit 0 } else { exit 1 } } catch { exit 3 }"
)

# Runs unelevated and only reads. It exits 0 when the one listener rule matches every value it was given.
_MATCH_SCRIPT = " ".join(
    (
        "try {",
        f"$p = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:{_MATCH_ENV})) | ConvertFrom-Json;",
        "$r = @(Get-NetFirewallRule -DisplayName ([string]$p.rule) -ErrorAction SilentlyContinue);",
        "if ($r.Count -ne 1) { exit 1 }; $r = $r[0];",
        (
            "if ([string]$r.Enabled -ne 'True' -or [string]$r.Direction -ne 'Inbound' -or [string]$r.Action -ne 'Allow'"
            " -or [string]$r.Profile -ne [string]$p.profile) { exit 1 };"
        ),
        "$pf = $r | Get-NetFirewallPortFilter;",
        (
            "if ([string]$pf.Protocol -ne 'TCP' -or (@($pf.LocalPort | Sort-Object) -join ',') -ne (@($p.ports | ForEach-Object { [string]$_ }) -join ',')"
            " -or (@($pf.RemotePort) -join ',') -ne 'Any') { exit 1 };"
        ),
        "$af = $r | Get-NetFirewallAddressFilter;",
        "if (@($af.LocalAddress).Count -ne 1 -or [string]$af.LocalAddress -ne [string]$p.local) { exit 1 };",
        "if (@($p.remotes) -notcontains (@($af.RemoteAddress) -join ',')) { exit 1 };",
        "if ([string]($r | Get-NetFirewallApplicationFilter).Program -ne [string]$p.program) { exit 1 };",
        "if ((@(($r | Get-NetFirewallInterfaceFilter).InterfaceAlias) -join ',') -ne [string]$p.alias) { exit 1 };",
        "exit 0 } catch { exit 3 }",
    )
)

_EXIT_TEXT = {
    EXIT_REFUSED: "the helper refused a value it was given",
    EXIT_NOT_CREATED: "Windows could not create the rule",
    EXIT_MISMATCH: "the rule Windows stored did not match what was asked, so the helper removed it",
    EXIT_NOT_REMOVED: "Windows could not remove the rule",
}


class FirewallError(Exception):
    pass


@dataclass(frozen=True)
class RuleSpec:
    rule: str
    ports: tuple[int, ...]
    program: str
    profile: str
    alias: str
    remote: str
    local: str


def _program(path: str) -> str:
    if not isinstance(path, str) or not _PROGRAM.fullmatch(path) or _DOTS.search(path):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise FirewallError(f"{path!r} is not an absolute Windows path to python.exe or pythonw.exe without quotes or control characters")
    return path


def _in_ranges(network: ipaddress.IPv4Network, profile: str) -> bool:
    return any(network.subnet_of(allowed) for allowed in _ALLOWED_RANGES[profile])


def _subnet(text: str, profile: str) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.IPv4Network(text, strict=True)
    except (ValueError, TypeError) as error:
        raise FirewallError(f"{text!r} is not an IPv4 network such as 192.168.1.0/24") from error
    if str(network) != text:
        raise FirewallError(f"{text!r} is not an IPv4 network written as address/prefix, such as 192.168.1.0/24")
    if not _in_ranges(network, profile):
        allowed = "RFC 1918" if profile == PROFILE_PRIVATE else "100.64.0.0/10"
        raise FirewallError(f"{network} is outside the {allowed} range the {profile} profile allows")
    return network


def _local(text: str, network: ipaddress.IPv4Network) -> str:
    try:
        address = ipaddress.IPv4Address(text)
    except (ValueError, TypeError) as error:
        raise FirewallError(f"{text!r} is not one IPv4 address") from error
    if address not in network or str(address) != text:
        raise FirewallError(f"{text} is not an address inside {network}")
    return str(address)


def rule_spec(rule: str, bind: str, subnet: str, interface_alias: str, program: str, profile: str = PROFILE_PRIVATE) -> RuleSpec:
    if rule != LISTENER_RULE:
        raise FirewallError(f"{rule!r} is not the rule Agent Tabs creates")
    if profile not in _ALLOWED_RANGES:
        raise FirewallError(f"{profile!r} is not a profile Agent Tabs uses")
    if not isinstance(interface_alias, str) or not is_safe_alias(interface_alias):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise FirewallError(f"{interface_alias!r} is not a network interface name Agent Tabs accepts")
    network = _subnet(subnet, profile)
    return RuleSpec(rule, LISTENER_PORTS, _program(program), profile, interface_alias, str(network), _local(bind, network))


def _document(action: str, rule: str, spec: RuleSpec | None) -> dict[str, object]:
    if spec is None:
        return {"action": action, "rule": rule}
    return {
        "action": action,
        "rule": spec.rule,
        "ports": list(spec.ports),
        "program": spec.program,
        "profile": spec.profile,
        "alias": spec.alias,
        "remote": spec.remote,
        "local": spec.local,
    }


def encode_parameters(action: str, rule: str, spec: RuleSpec | None = None) -> str:
    text = json.dumps(_document(action, rule, spec), ensure_ascii=True, separators=(",", ":"))
    blob = base64.b64encode(text.encode("utf-8")).decode("ascii")
    if not _PARAMS_CHARS.fullmatch(blob):
        raise FirewallError("the firewall parameters are too long")
    return blob


def _powershell(env: Environment) -> str:
    return os.path.join(env.windows_directory(), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def _elevate(env: Environment, parameters: str) -> None:
    powershell = _powershell(env)
    variables = {**os.environ, _POWERSHELL_ENV: powershell, _HELPER_ENV: HELPER_SCRIPT, _PARAMS_ENV: parameters}
    try:
        result = env.run(
            powershell, ["-NoProfile", "-NonInteractive", "-Command", _OUTER_SCRIPT], env=variables, timeout=ELEVATION_TIMEOUT_S
        )
    except (OSError, TimeoutError) as error:
        raise FirewallError(f"the elevated step did not finish: {error}") from error
    if result.code == DECLINED:
        raise FirewallError("the Windows prompt for administrator rights was declined, so the firewall rule did not change")
    if result.code != 0:
        reason = _EXIT_TEXT.get(result.code) if result.code is not None else None
        detail = f": {reason}" if reason else ""
        raise FirewallError(f"the elevated firewall step ended with code {result.code}{detail}; the firewall rule may not have changed")


def rule_exists(rule: str, env: Environment | None = None) -> bool | None:
    env = env or Environment()
    if rule != LISTENER_RULE:
        raise FirewallError(f"{rule!r} is not one of the rules Agent Tabs manages")
    try:
        result = env.run(
            _powershell(env),
            ["-NoProfile", "-NonInteractive", "-Command", _PROBE_SCRIPT],
            env={**os.environ, _RULE_ENV: rule},
            timeout=PROBE_TIMEOUT_S,
        )
    except (OSError, TimeoutError, LoginTaskError) as error:
        raise FirewallError(f"Windows could not list the firewall rules: {error}") from error
    if result.code == 0:
        return True
    return False if result.code == 1 else None


def _ports_text(ports: Sequence[int]) -> str:
    return " and ".join(str(port) for port in ports)


def manual_rule_text(bind: str, ports: Sequence[int], remote: str) -> str:
    return (
        f"This system changes no firewall rule. If a firewall runs here, allow inbound TCP ports {_ports_text(ports)} on {bind} "
        f"from {remote} for the Python that runs Agent Tabs, and nothing wider."
    )


def _match_document(spec: RuleSpec) -> str:
    network = ipaddress.IPv4Network(spec.remote)
    document = {
        "rule": spec.rule,
        "ports": [str(port) for port in sorted(spec.ports)],
        "profile": spec.profile,
        "local": spec.local,
        "remotes": [spec.remote, network.with_netmask],
        "program": spec.program,
        "alias": spec.alias,
    }
    return base64.b64encode(json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("utf-8")).decode("ascii")


def rule_matches(spec: RuleSpec, env: Environment) -> bool:
    try:
        result = env.run(
            _powershell(env),
            ["-NoProfile", "-NonInteractive", "-Command", _MATCH_SCRIPT],
            env={**os.environ, _MATCH_ENV: _match_document(spec)},
            timeout=PROBE_TIMEOUT_S,
        )
    except (OSError, TimeoutError, LoginTaskError):
        return False
    return result.code == 0


def apply_listener_rule(
    bind: str, subnet: str, interface_alias: str, env: Environment | None = None, profile: str = PROFILE_PRIVATE
) -> str:
    env = env or Environment()
    if env.platform != "win32":
        return manual_rule_text(bind, LISTENER_PORTS, subnet)
    try:
        spec = rule_spec(LISTENER_RULE, bind, subnet, interface_alias, env.interpreter, profile)
        if not os.path.isfile(env.interpreter):
            raise FirewallError(f"the interpreter {env.interpreter} is not a file")
        where = f"on {interface_alias}" if profile == PROFILE_ANY else f"on the {profile} profile of {interface_alias}"
        what = f"ports {_ports_text(spec.ports)} on {bind} from {spec.remote} {where}"
        if rule_matches(spec, env):
            return f'The Windows firewall rule "{LISTENER_RULE}" already admits {what}.'
        _elevate(env, encode_parameters("add", LISTENER_RULE, spec))
    except LoginTaskError as error:
        raise FirewallError(str(error)) from error
    return f'Created the Windows firewall rule "{LISTENER_RULE}" for {what}.'


def _remove(rule: str, env: Environment | None, only_if_present: bool, prefix: str = "") -> str:
    env = env or Environment()
    if env.platform != "win32":
        return ""
    try:
        if only_if_present and rule_exists(rule, env) is False:
            return f"{prefix} No firewall rule is present.".strip()
        _elevate(env, encode_parameters("remove", rule))
    except LoginTaskError as error:
        raise FirewallError(str(error)) from error
    return f'{prefix} Removed the Windows firewall rule "{rule}".'.strip()


def remove_listener_rule(env: Environment | None = None) -> str:
    return _remove(LISTENER_RULE, env, only_if_present=True)


def apply_rule(bind: str, port: int, peers: Sequence[str], env: Environment | None = None) -> str:
    del peers
    env = env or Environment()
    if env.platform != "win32":
        return manual_rule_text(bind, LISTENER_PORTS, "your local network")
    if port != LISTENER_PORT:
        raise FirewallError(f"{port!r} is not the listener port {LISTENER_PORT}, so no firewall rule is created")
    try:
        info = interface_for(bind, env)
    except (InterfaceError, LoginTaskError) as error:
        raise FirewallError(str(error)) from error
    return apply_listener_rule(bind, info.subnet, info.alias, env)


def remove_rule(env: Environment | None = None) -> str:
    return remove_listener_rule(env)
