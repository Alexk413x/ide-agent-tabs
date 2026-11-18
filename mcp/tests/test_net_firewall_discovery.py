from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ide_agent_tabs.net import firewall
from ide_agent_tabs.processes import RunResult
from test_net_firewall import ALIAS, BIND, PYTHON, SUBNET, FakeRun, environment, interpreter_exists, parameters_of

MISMATCH = RunResult(1, "", "")
REAL_POWERSHELL = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
PROGRAM_PATTERN = re.compile(r"[A-Za-z]:\\(?:[^\\/:*?<>|\x00-\x1f\"]+\\)*python(?:w)?\.exe", re.IGNORECASE)

# Stand-ins for the NetSecurity cmdlets, so the helper's logic runs without administrator rights or a real rule.
STUBS = r"""
$global:store = @{}
$global:failOn = ''
$global:corrupt = ''
function Get-NetFirewallRule { param([string]$DisplayName, $ErrorAction) if ($global:store.ContainsKey($DisplayName)) { return [pscustomobject]$global:store[$DisplayName] } }
function New-NetFirewallRule { param([string]$DisplayName, $Direction, $Action, $Protocol, $LocalPort, $LocalAddress, $RemoteAddress, $Program, $Profile, $InterfaceAlias, $Enabled, $ErrorAction)
  if ($global:failOn -eq $DisplayName) { throw 'simulated failure' }
  $la = if ($PSBoundParameters.ContainsKey('LocalAddress')) { $LocalAddress } else { 'Any' }
  $proto = if ($global:corrupt -eq $DisplayName) { 'TCP' } else { $Protocol }
  $global:store[$DisplayName] = @{ DisplayName = $DisplayName; Enabled = 'True'; Direction = 'Inbound'; Action = 'Allow'; Profile = $Profile; Protocol = $proto; LocalPort = @($LocalPort); LocalAddress = $la; RemoteAddress = $RemoteAddress; Program = $Program; Alias = $InterfaceAlias } }
function Remove-NetFirewallRule { param([string]$DisplayName, $ErrorAction) $global:store.Remove($DisplayName) }
function Get-NetFirewallPortFilter { process { [pscustomobject]@{ Protocol = $_.Protocol; LocalPort = $_.LocalPort; RemotePort = 'Any' } } }
function Get-NetFirewallAddressFilter { process { [pscustomobject]@{ LocalAddress = $_.LocalAddress; RemoteAddress = $_.RemoteAddress } } }
function Get-NetFirewallApplicationFilter { process { [pscustomobject]@{ Program = $_.Program } } }
function Get-NetFirewallInterfaceFilter { process { [pscustomobject]@{ InterfaceAlias = $_.Alias } } }
function Dump { ($global:store.Keys | Sort-Object | ForEach-Object { $r = $global:store[$_]; $_ + '|' + $r.Protocol + '|' + ($r.LocalPort -join ',') + '|' + $r.LocalAddress + '|' + $r.RemoteAddress }) -join ';' }
Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action { Dump | Set-Content $global:dumpPath } | Out-Null
"""


def encoded(action: str, rule: str = firewall.LISTENER_RULE, program: str = PYTHON) -> str:
    spec = firewall.rule_spec(firewall.LISTENER_RULE, BIND, SUBNET, ALIAS, program)
    return firewall.encode_parameters(action, rule, spec if action == "add" else None)


class ConstantsTest(unittest.TestCase):
    def test_the_discovery_rule_covers_the_udp_beacon_port(self) -> None:
        self.assertEqual(firewall.DISCOVERY_PORT, 47831)
        self.assertEqual(firewall.DISCOVERY_PORTS, (47831,))
        self.assertEqual(firewall.DISCOVERY_RULE, "Agent Tabs LAN discovery")
        self.assertEqual(firewall.LISTENER_PORTS, (47829, 47830))


class HelperTextTest(unittest.TestCase):
    def test_the_helper_names_both_fixed_rules_and_the_udp_port(self) -> None:
        script = firewall.HELPER_SCRIPT
        for part in ("'Agent Tabs LAN'", "'Agent Tabs LAN discovery'", "'UDP'", "'TCP'", "'47831'"):
            self.assertIn(part, script)
        self.assertNotIn('"', script)
        self.assertNotIn("\n", script)

    def test_the_probe_and_match_scripts_hold_no_value_and_cover_the_sibling_rule(self) -> None:
        self.assertIn("' discovery'", firewall._MATCH_SCRIPT)  # pyright: ignore[reportPrivateUsage]
        self.assertIn("' discovery'", firewall._PROBE_SCRIPT)  # pyright: ignore[reportPrivateUsage]
        self.assertIn("'UDP'", firewall._MATCH_SCRIPT)  # pyright: ignore[reportPrivateUsage]
        for verb in ("New-NetFirewallRule", "Remove-NetFirewallRule", "Set-NetFirewallRule", "RunAs"):
            self.assertNotIn(verb, firewall._MATCH_SCRIPT)  # pyright: ignore[reportPrivateUsage]
            self.assertNotIn(verb, firewall._PROBE_SCRIPT)  # pyright: ignore[reportPrivateUsage]


class OnePromptTest(unittest.TestCase):
    def test_a_fresh_machine_gets_one_probe_and_one_elevation_for_both_rules(self) -> None:
        run = FakeRun(MISMATCH)
        with interpreter_exists():
            message = firewall.apply_listener_rule(BIND, SUBNET, ALIAS, environment(run))
        self.assertEqual(len(run.calls), 2)
        elevations = [call for call in run.calls if firewall._PARAMS_ENV in call[2]]  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(len(elevations), 1)
        self.assertEqual(parameters_of(elevations[0])["action"], "add")
        self.assertIn("Agent Tabs LAN discovery", message)
        self.assertIn("UDP port 47831", message)
        self.assertIn("47829 and 47830", message)

    def test_a_machine_with_the_tcp_rule_but_no_udp_rule_gets_one_elevation(self) -> None:
        # The one probe answers for both rules, so a missing UDP rule is a mismatch that leads to one prompt.
        run = FakeRun(MISMATCH)
        with interpreter_exists():
            firewall.apply_listener_rule(BIND, SUBNET, ALIAS, environment(run))
        self.assertEqual(sum(firewall._PARAMS_ENV in call[2] for call in run.calls), 1)  # pyright: ignore[reportPrivateUsage]

    def test_a_machine_whose_two_rules_match_needs_no_elevation(self) -> None:
        run = FakeRun(RunResult(0, "", ""))
        with interpreter_exists():
            message = firewall.apply_listener_rule(BIND, SUBNET, ALIAS, environment(run))
        self.assertEqual(len(run.calls), 1)
        self.assertIn("already admits", message)
        self.assertIn("Agent Tabs LAN discovery", message)

    def test_the_match_document_is_the_listener_rule_document(self) -> None:
        run = FakeRun(MISMATCH)
        with interpreter_exists():
            firewall.apply_listener_rule(BIND, SUBNET, ALIAS, environment(run))
        document = json.loads(base64.b64decode(run.calls[0][2][firewall._MATCH_ENV]))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(document["rule"], "Agent Tabs LAN")
        self.assertEqual(document["ports"], ["47829", "47830"])

    def test_removal_is_one_elevation_for_both_rules(self) -> None:
        run = FakeRun(RunResult(0, "", ""), RunResult(0, "", ""))
        message = firewall.remove_listener_rule(environment(run))
        self.assertEqual(len(run.calls), 2)
        self.assertEqual(parameters_of(run.calls[1]), {"action": "remove", "rule": "Agent Tabs LAN"})
        self.assertIn("Removed", message)

    def test_other_systems_are_told_about_the_udp_port(self) -> None:
        text = firewall.apply_listener_rule(BIND, SUBNET, ALIAS, environment(FakeRun(), "linux"))
        self.assertIn("UDP port 47831", text)
        self.assertIn("47829 and 47830", text)

    def test_a_declined_prompt_changes_neither_rule(self) -> None:
        run = FakeRun(MISMATCH, RunResult(firewall.DECLINED, "", ""))
        with interpreter_exists(), self.assertRaises(firewall.FirewallError) as caught:
            firewall.apply_listener_rule(BIND, SUBNET, ALIAS, environment(run))
        self.assertIn("did not change", str(caught.exception))


def powershell_ready() -> bool:
    return sys.platform == "win32" and os.path.isfile(REAL_POWERSHELL) and PROGRAM_PATTERN.fullmatch(sys.executable) is not None


@unittest.skipUnless(powershell_ready(), "needs Windows PowerShell and a python.exe path the helper accepts")
class HelperDryRunTest(unittest.TestCase):
    def run_script(self, body: str, env: dict[str, str] | None = None) -> tuple[int, str]:
        with tempfile.TemporaryDirectory(prefix="iat-fw-") as folder:
            dump = os.path.join(folder, "dump.txt")
            script = os.path.join(folder, "run.ps1")
            with open(script, "w", encoding="utf-8") as handle:
                handle.write(f"$global:dumpPath = '{dump}'\n{STUBS}\n{body}\n")
            done = subprocess.run(
                [REAL_POWERSHELL, "-NoProfile", "-NonInteractive", "-File", script],
                capture_output=True,
                timeout=60,
                env={**os.environ, **(env or {})},
                check=False,
            )
            text = Path(dump).read_text(encoding="utf-8").strip() if os.path.exists(dump) else ""
            return done.returncode, text

    def helper(self, action: str, before: str = "") -> tuple[int, str]:
        return self.run_script(f"{before}\n{firewall.HELPER_SCRIPT} '{encoded(action, program=sys.executable)}'")

    def test_add_creates_both_rules_with_the_listener_scope(self) -> None:
        code, dump = self.helper("add")
        self.assertEqual(code, 0)
        rules = dump.split(";")
        self.assertEqual(
            rules,
            [f"Agent Tabs LAN|TCP|47829,47830|{BIND}|{SUBNET}", f"Agent Tabs LAN discovery|UDP|47831|Any|{SUBNET}"],
        )

    def test_a_failure_creating_the_udp_rule_removes_the_tcp_rule_too(self) -> None:
        code, dump = self.helper("add", "$global:failOn = 'Agent Tabs LAN discovery'")
        self.assertEqual((code, dump), (firewall.EXIT_NOT_CREATED, ""))

    def test_a_failure_creating_the_tcp_rule_creates_nothing(self) -> None:
        code, dump = self.helper("add", "$global:failOn = 'Agent Tabs LAN'")
        self.assertEqual((code, dump), (firewall.EXIT_NOT_CREATED, ""))

    def test_a_read_back_mismatch_on_the_udp_rule_removes_both_rules(self) -> None:
        code, dump = self.helper("add", "$global:corrupt = 'Agent Tabs LAN discovery'")
        self.assertEqual((code, dump), (firewall.EXIT_MISMATCH, ""))

    def test_remove_deletes_both_rules(self) -> None:
        seed = (
            "$null = New-NetFirewallRule -DisplayName 'Agent Tabs LAN' -Protocol TCP -LocalPort 1 -Program 'x' -Profile 'Private' -InterfaceAlias 'e' -RemoteAddress 'r'\n"
            "$null = New-NetFirewallRule -DisplayName 'Agent Tabs LAN discovery' -Protocol UDP -LocalPort 1 -Program 'x' -Profile 'Private' -InterfaceAlias 'e' -RemoteAddress 'r'"
        )
        code, dump = self.helper("remove", seed)
        self.assertEqual((code, dump), (0, ""))

    def test_an_existing_rule_of_either_name_is_replaced(self) -> None:
        seed = "$null = New-NetFirewallRule -DisplayName 'Agent Tabs LAN discovery' -Protocol TCP -LocalPort 9 -Program 'x' -Profile 'Private' -InterfaceAlias 'e' -RemoteAddress 'r'"
        code, dump = self.helper("add", seed)
        self.assertEqual(code, 0)
        self.assertIn(f"Agent Tabs LAN discovery|UDP|47831|Any|{SUBNET}", dump)

    def match(self, seed: str) -> int:
        spec = firewall.rule_spec(firewall.LISTENER_RULE, BIND, SUBNET, ALIAS, sys.executable)
        document = firewall._match_document(spec)  # pyright: ignore[reportPrivateUsage]
        code, _ = self.run_script(f"{seed}\n{firewall._MATCH_SCRIPT}", {firewall._MATCH_ENV: document})  # pyright: ignore[reportPrivateUsage]
        return code

    def rule(self, name: str, protocol: str, ports: str, local: str | None) -> str:
        where = f" -LocalAddress '{local}'" if local else ""
        return (
            f"$null = New-NetFirewallRule -DisplayName '{name}' -Protocol {protocol} -LocalPort @({ports}){where} -RemoteAddress '{SUBNET}' "
            f"-Program '{sys.executable}' -Profile 'Private' -InterfaceAlias '{ALIAS}'\n"
        )

    def test_the_match_probe_needs_both_rules(self) -> None:
        tcp = self.rule("Agent Tabs LAN", "TCP", "'47829','47830'", BIND)
        udp = self.rule("Agent Tabs LAN discovery", "UDP", "'47831'", None)
        self.assertEqual(self.match(tcp + udp), 0)
        self.assertEqual(self.match(tcp), 1)
        self.assertEqual(self.match(udp), 1)

    def test_the_match_probe_refuses_a_udp_rule_bound_to_one_local_address(self) -> None:
        tcp = self.rule("Agent Tabs LAN", "TCP", "'47829','47830'", BIND)
        udp = self.rule("Agent Tabs LAN discovery", "UDP", "'47831'", BIND)
        self.assertEqual(self.match(tcp + udp), 1)

    def test_the_match_probe_refuses_a_udp_rule_on_another_port(self) -> None:
        tcp = self.rule("Agent Tabs LAN", "TCP", "'47829','47830'", BIND)
        udp = self.rule("Agent Tabs LAN discovery", "UDP", "'47999'", None)
        self.assertEqual(self.match(tcp + udp), 1)


if __name__ == "__main__":
    unittest.main()
