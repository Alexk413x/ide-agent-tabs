from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from ide_agent_tabs.net import cli, elevation, pair_cli, pairing
from ide_agent_tabs.net.client import peer_cert_path
from ide_agent_tabs.net.firewall import FirewallError
from ide_agent_tabs.net.identity import format_digest
from ide_agent_tabs.net.interfaces import InterfaceError, InterfaceInfo
from ide_agent_tabs.net.listener import listener_file, log_file
from ide_agent_tabs.net.login_task import LoginTaskError
from ide_agent_tabs.net.pair_cli import PairDeps
from ide_agent_tabs.net.pairing import PairResult, PeerInfo
from ide_agent_tabs.net.policy import FolderGrant, PeerGrant, Policy, load_policy, save_policy
from ide_agent_tabs.net.private_files import check_private
from net_support import HAS_OPENSSL, OPENSSL, cert_of, create_home, device_of, make_root, pem_der, reset_peers
from test_net_cli import NEEDS, Harness

GOOD_ID = format_digest(bytes(range(32)))
BIND = "192.168.50.10"
PEER_ADDRESS = "192.168.50.20"
SUBNET = "192.168.50.0/24"
WINDOW_CLOSED = "the pairing window closed before another machine paired"
FAST = pairing.Timeouts(
    first_frame_s=5.0,
    handshake_s=20.0,
    consent_s=10.0,
    connect_s=5.0,
    window_s=30.0,
    min_interval_s=0.0,
)


def invitation_line(address: str = PEER_ADDRESS, port: int = 47830, name: str = "bravo", ahead: int = 300, device: str = GOOD_ID) -> str:
    return pairing.make_invitation(name, address, port, device, pairing.new_invitation_token(), int(time.time()) + ahead)


def peer_of(home: str, name: str = "bravo", address: str = PEER_ADDRESS) -> PeerInfo:
    return PeerInfo(name, address, device_of(home), pem_der(home))


class Machine:
    def __init__(self, test: unittest.TestCase, home: str, *, bind: str = BIND) -> None:
        self.home = home
        self.bind = bind
        self.now = time.time()
        self.events: list[str] = []
        self.servers: list[tuple[str, str, str, str]] = []
        self.rules_opened: list[tuple[str, str, str]] = []
        self.bound: list[tuple[str, int]] = []
        self.sockets: list[socket.socket] = []
        self.lan_calls: list[tuple[str, bool]] = []
        self.openssl: str | None = str(OPENSSL) if HAS_OPENSSL else None
        self.elevation_error: Exception | None = None
        self.lan_error: Exception | None = None
        self.rule_error: Exception | None = None
        self.bind_error: Exception | None = None
        self.spawn_pid: int | None = 4242
        self.invitation_text: Callable[[], str | None] = self._own_invitation
        self.listen_impl: Callable[..., PairResult] | None = None
        self.dial_impl: Callable[..., PairResult] | None = None
        self.dial_address: str | None = None
        self.deps = PairDeps(
            refuse_if_elevated=self._elevation,
            local_addresses=lambda: [self.bind],
            require_lan_ready=self._lan_ready,
            find_openssl=lambda _configured, _env: self.openssl,
            ensure_rule=self._ensure_rule,
            spawn_server=self._spawn_server,
            bind_socket=self._bind_socket,
            check_destination=self._destination,
            listen=self._listen,
            dial=self._dial,
            timeouts=FAST,
            interpreter=r"C:\py\python.exe",
            launch_script=r"C:\plugin\launch\agent_tabs.py",
            clock=lambda: self.now,
            sleep=self._sleep,
            invitation_wait_s=5.0,
        )
        self.harness = Harness(test, home, pair=self.deps)
        test.addCleanup(self._close_sockets)

    def _close_sockets(self) -> None:
        for sock in self.sockets:
            sock.close()

    def _sleep(self, seconds: float) -> None:
        self.now += seconds

    def _own_invitation(self) -> str:
        token = pairing.new_invitation_token()
        return pairing.make_invitation("alpha", self.bind, pairing.PAIRING_PORT, device_of(self.home), token, int(self.now) + 300)

    def _elevation(self) -> None:
        self.events.append("elevation")
        if self.elevation_error is not None:
            raise self.elevation_error

    def _lan_ready(self, address: str, allow_virtual: bool) -> InterfaceInfo:
        self.events.append("lan-ready")
        self.lan_calls.append((address, allow_virtual))
        if self.lan_error is not None:
            raise self.lan_error
        prefix = ".".join(address.split(".")[:3])
        return InterfaceInfo("Ethernet", address, 24, f"{prefix}.0/24", "Private", True)

    def _ensure_rule(self, bind: str, subnet: str, alias: str) -> str:
        self.events.append("ensure-rule")
        if self.rule_error is not None:
            raise self.rule_error
        self.rules_opened.append((bind, subnet, alias))
        return "Created the listener rule."

    def _spawn_server(self, home: str, bind: str, interpreter: str, launch_script: str) -> int | None:
        self.events.append("spawn")
        self.servers.append((home, bind, interpreter, launch_script))
        if self.spawn_pid is None:
            return None
        text = self.invitation_text()
        if text is not None:
            pair_cli.write_invitation(home, text)
        return self.spawn_pid

    def _bind_socket(self, address: str, port: int) -> socket.socket:
        self.events.append("bind-socket")
        self.bound.append((address, port))
        if self.bind_error is not None:
            raise self.bind_error
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(4)
        self.sockets.append(sock)
        return sock

    def _destination(self, address: str, port: int, interface: str, prefix: int) -> str:
        self.events.append("destination")
        return pairing.check_lan_destination(address, port, interface, prefix)

    def _listen(self, *args: Any, **kwargs: Any) -> PairResult:
        self.events.append("listen")
        return (self.listen_impl or pairing.listen_once)(*args, **kwargs)

    def _dial(self, address: str, *args: Any, **kwargs: Any) -> PairResult:
        self.events.append("dial")
        return (self.dial_impl or pairing.dial)(self.dial_address or address, *args, **kwargs)

    def run(self, *args: str) -> int:
        return cli.main(["pair", *args], self.harness.services)

    def serve(self) -> int:
        return self.run("--serve", "--bind", self.bind)

    @property
    def out(self) -> str:
        return self.harness.text

    @property
    def errors(self) -> str:
        return self.harness.errors

    @property
    def notices(self) -> list[str]:
        return self.harness.notices

    def window_is_open(self) -> bool:
        try:
            pairing.Limits(self.home).check()
        except pairing.Busy as error:
            return "already open" in str(error)
        return False

    def result(self) -> dict[str, Any] | None:
        return pair_cli.read_result(self.home)


class Session:
    def __init__(
        self,
        result: PairResult | None = None,
        peer: PeerInfo | None = None,
        *,
        save: bool = True,
        during: Callable[[], None] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.result = result
        self.peer = peer
        self.save = save
        self.during = during
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def _outcome(self, saver: Callable[[PeerInfo], None]) -> PairResult:
        if self.during is not None:
            self.during()
        if self.error is not None:
            raise self.error
        if self.peer is not None and self.save:
            saver(self.peer)
        if self.result is not None:
            return self.result
        if self.peer is not None:
            return PairResult("paired", "paired", self.peer)
        return PairResult("failed", WINDOW_CLOSED)

    def listen(
        self, sock: socket.socket, token: str, identity: Any, saver: Callable[[PeerInfo], None], limits: Any, **kwargs: Any
    ) -> PairResult:
        self.calls.append({"port": sock.getsockname()[1], "token": token, "name": identity.name, "kwargs": kwargs, "limits": limits})
        return self._outcome(saver)

    def dial(
        self,
        address: str,
        port: int,
        token: str,
        peer_id: str,
        identity: Any,
        saver: Callable[[PeerInfo], None],
        limits: Any,
        **kwargs: Any,
    ) -> PairResult:
        self.calls.append(
            {
                "address": address,
                "port": port,
                "token": token,
                "peer_id": peer_id,
                "name": identity.name,
                "kwargs": kwargs,
                "limits": limits,
            }
        )
        return self._outcome(saver)


@NEEDS
class PairTestCase(unittest.TestCase):
    a: str
    b: str
    c: str

    @classmethod
    def setUpClass(cls) -> None:
        root = make_root(cls)
        cls.a, cls.b, cls.c = (create_home(root, name) for name in ("a", "b", "c"))

    def setUp(self) -> None:
        reset_peers(self.a)
        with contextlib.suppress(OSError):
            os.remove(os.path.join(self.a, "config.json"))
        for name in (pairing.PAIRING_FILE, pairing.PAIRING_FILE + ".lock", pair_cli.INVITATION_FILE, pair_cli.RESULT_FILE):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(self.a, "net", name))
        with contextlib.suppress(OSError):
            os.remove(listener_file(self.a))
        with contextlib.suppress(OSError):
            os.remove(log_file(self.a, "cli.log"))
        self.b_id, self.c_id = device_of(self.b), device_of(self.c)

    def machine(self) -> Machine:
        return Machine(self, self.a)

    def write_config(self, **network: Any) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": network}), encoding="utf-8")

    def config(self) -> dict[str, Any]:
        return json.loads(Path(self.a, "config.json").read_text(encoding="utf-8"))

    def assert_nothing_started(self, machine: Machine) -> None:
        self.assertEqual(machine.harness.spawned, [])
        self.assertEqual(machine.harness.logins, [])
        self.assertEqual(machine.harness.firewall, [])
        self.assertFalse(os.path.exists(os.path.join(self.a, "config.json")))
        self.assertEqual(load_policy(self.a).peers, ())


class ElevationAndUsageTest(PairTestCase):
    def test_an_elevated_command_stops_before_anything_else(self) -> None:
        for args in (["--invite"], ["--serve", "--bind", BIND], ["--join", invitation_line()]):
            machine = self.machine()
            machine.elevation_error = elevation.ElevationError("Stop: this command runs with administrator rights")
            self.assertEqual(machine.run(*args), 1, args)
            self.assertIn("administrator", machine.errors)
            self.assertEqual(machine.events, ["elevation"])
            self.assertFalse(os.path.exists(os.path.join(self.a, "net", pairing.PAIRING_FILE)))

    def test_usage_errors(self) -> None:
        machine = self.machine()
        for args in (
            [],
            ["--serve"],
            ["--invite", "--join", invitation_line()],
            ["--invite", "--serve", "--bind", BIND],
            ["--peer", PEER_ADDRESS, "--token", "ABC234"],
            ["--bogus"],
        ):
            self.assertEqual(machine.run(*args), 2, args)
        self.assertNotIn("spawn", machine.events)
        self.assertNotIn("bind-socket", machine.events)
        self.assertNotIn("dial", machine.events)


class PrepareTest(PairTestCase):
    def test_a_public_network_stops_every_mode_before_anything_starts(self) -> None:
        for args in (["--invite"], ["--serve", "--bind", BIND], ["--join", invitation_line()]):
            machine = self.machine()
            machine.lan_error = InterfaceError("Ethernet is a Public network. Open Settings > Network & internet > Private network.")
            self.assertEqual(machine.run(*args), 1, args)
            self.assertIn("Public network", machine.errors)
            self.assertEqual(machine.events, ["elevation", "lan-ready"])
            self.assertEqual(machine.servers, [])
            self.assertFalse(os.path.exists(os.path.join(self.a, "net", pairing.PAIRING_FILE)))
            self.assertFalse(pair_cli.invitation_path(self.a).exists())

    def test_a_single_physical_private_address_is_shown_and_used(self) -> None:
        machine = self.machine()
        machine.deps.local_addresses = lambda: [BIND, "10.0.0.5", "8.8.8.8", "127.0.0.1", "169.254.1.1"]
        machine.harness.services.interface_for = lambda address: InterfaceInfo(
            "Ethernet" if address == BIND else "vEthernet (WSL)", address, 24, SUBNET, "Private", address == BIND
        )
        self.assertEqual(machine.run("--invite"), 0, machine.errors)
        self.assertIn(f"Using {BIND} on Ethernet.", machine.harness.out)
        self.assertEqual(machine.lan_calls, [(BIND, False)])
        self.assertEqual(machine.servers[0][1], BIND)

    def test_several_private_addresses_are_listed_and_nothing_is_chosen(self) -> None:
        machine = self.machine()
        machine.deps.local_addresses = lambda: [BIND, "10.0.0.5"]
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn(f"{BIND} (Ethernet)", machine.errors)
        self.assertIn("10.0.0.5 (Ethernet)", machine.errors)
        self.assertIn("--bind", machine.errors)
        self.assertEqual(machine.lan_calls, [])
        self.assertEqual(machine.servers, [])

    def test_no_physical_private_address_names_the_fix(self) -> None:
        machine = self.machine()
        machine.deps.local_addresses = lambda: ["8.8.8.8", "127.0.0.1"]
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("--bind", machine.errors)
        self.assertEqual(machine.lan_calls, [])

    def test_a_configured_bind_is_used_without_listing_addresses(self) -> None:
        self.write_config(bind="10.0.0.5")
        machine = self.machine()
        machine.deps.local_addresses = lambda: self.fail("the configured bind needs no listing")
        self.assertEqual(machine.run("--invite"), 0, machine.errors)
        self.assertEqual(machine.lan_calls, [("10.0.0.5", True)])
        self.assertEqual(machine.rules_opened, [("10.0.0.5", "10.0.0.0/24", "Ethernet")])
        self.assertEqual(machine.servers[0][1], "10.0.0.5")

    def test_the_bind_option_wins_over_the_configured_bind(self) -> None:
        self.write_config(bind="10.0.0.5")
        machine = self.machine()
        self.assertEqual(machine.run("--invite", "--bind", BIND), 0, machine.errors)
        self.assertEqual(machine.lan_calls, [(BIND, True)])
        self.assertEqual(machine.servers[0][1], BIND)

    def test_the_device_name_comes_from_the_config_or_the_host(self) -> None:
        self.write_config(deviceName="Desk 1")
        machine = self.machine()
        session = Session()
        machine.listen_impl = session.listen
        machine.serve()
        self.assertEqual(session.calls[0]["name"], "Desk 1")
        self.assertNotIn("deviceName", machine.errors)

        self.write_config(deviceName="x" * 40)
        machine = self.machine()
        self.assertEqual(machine.serve(), 1)
        self.assertIn("deviceName", machine.errors)
        self.assertEqual(machine.events, ["elevation"])

        Path(self.a, "config.json").unlink()
        machine = self.machine()
        session = Session()
        machine.listen_impl = session.listen
        machine.serve()
        self.assertTrue(pairing.valid_device_name(session.calls[0]["name"]))

    def test_a_missing_identity_is_created_with_the_found_openssl(self) -> None:
        home = make_root(self)
        machine = Machine(self, home)
        self.assertEqual(machine.run("--invite"), 0, machine.errors)
        self.assertIn(f"identity with openssl at {OPENSSL}", machine.harness.text)
        self.assertIsNotNone(device_of(home))
        self.assertEqual(json.loads(Path(home, "config.json").read_text())["network"]["openssl"], str(OPENSSL))
        self.assertEqual(len(machine.servers), 1)

    def test_a_missing_openssl_stops_before_anything_starts(self) -> None:
        home = make_root(self)
        machine = Machine(self, home)
        machine.openssl = None
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("No openssl", machine.errors)
        self.assertIn("lan setup --openssl", machine.errors)
        self.assertEqual(machine.rules_opened, [])
        self.assertEqual(machine.servers, [])
        self.assertFalse(os.path.exists(os.path.join(home, "net", "identity")))


class InviteTest(PairTestCase):
    def test_the_invitation_comes_from_the_detached_server_and_the_command_binds_nothing(self) -> None:
        machine = self.machine()
        self.assertEqual(machine.run("--invite"), 0, machine.errors)
        self.assertEqual(machine.events, ["elevation", "lan-ready", "ensure-rule", "spawn"])
        self.assertEqual(machine.bound, [])
        self.assertEqual(machine.rules_opened, [(BIND, SUBNET, "Ethernet")])
        self.assertEqual(machine.servers, [(self.a, BIND, r"C:\py\python.exe", r"C:\plugin\launch\agent_tabs.py")])
        line = pair_cli.read_invitation(self.a, 0, machine.now)
        assert line is not None
        self.assertIn(line, machine.harness.out)
        self.assertIn(f"agent-tabs net pair --join {line}", machine.out)
        self.assertIn("net status", machine.out)
        self.assertFalse(os.path.exists(os.path.join(self.a, "net", pairing.PAIRING_FILE)))
        self.assertEqual(machine.harness.spawned, [])

    def test_the_invitation_is_waited_for(self) -> None:
        machine = self.machine()
        machine.invitation_text = lambda: None
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            machine.now += seconds
            if len(sleeps) == 3:
                pair_cli.write_invitation(self.a, invitation_line(address=BIND))

        machine.deps.sleep = sleep
        self.assertEqual(machine.run("--invite"), 0, machine.errors)
        self.assertEqual(len(sleeps), 3)

    def test_a_server_that_cannot_start_is_an_error(self) -> None:
        machine = self.machine()
        machine.spawn_pid = None
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("could not be started", machine.errors)

    def test_no_invitation_is_an_error_after_the_wait(self) -> None:
        machine = self.machine()
        machine.invitation_text = lambda: None
        started = machine.now
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("showed no invitation", machine.errors)
        self.assertGreaterEqual(machine.now - started, machine.deps.invitation_wait_s)

    def test_the_last_result_is_added_to_the_error(self) -> None:
        pair_cli.write_result(self.a, PairResult("failed", "pairing busy: a pairing window is already open"), None, time.time())
        machine = self.machine()
        machine.invitation_text = lambda: None
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("showed no invitation: failed: pairing busy", machine.errors)

    def test_a_file_from_an_earlier_run_is_removed_and_never_returned(self) -> None:
        stale = invitation_line()
        pair_cli.write_invitation(self.a, stale)
        machine = self.machine()
        machine.invitation_text = lambda: None
        self.assertEqual(machine.run("--invite"), 1)
        self.assertFalse(pair_cli.invitation_path(self.a).exists())
        self.assertNotIn(stale, machine.harness.out)

    def test_an_expired_or_damaged_file_is_not_returned(self) -> None:
        for text in ("atp1-invite:abc", "not an invitation", invitation_line() + "\nsecond", invitation_line(ahead=-5)):
            machine = self.machine()
            machine.invitation_text = lambda text=text: text
            self.assertEqual(machine.run("--invite"), 1, text)
            self.assertNotIn(text, machine.harness.out)
            self.assertNotIn("atp1-invite:", machine.out)

    def test_a_declined_firewall_prompt_starts_no_server(self) -> None:
        machine = self.machine()
        machine.rule_error = FirewallError("the Windows prompt for administrator rights was declined")
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("declined", machine.errors)
        self.assertIn("Agent Tabs LAN", machine.errors)
        self.assertEqual(machine.servers, [])
        self.assertFalse(pair_cli.invitation_path(self.a).exists())

    def test_an_open_window_starts_nothing(self) -> None:
        first = pairing.Limits(self.a)
        first.open_window()
        self.addCleanup(first.close_window)
        machine = self.machine()
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("pairing busy", machine.errors)
        self.assertEqual(machine.rules_opened, [])
        self.assertEqual(machine.servers, [])


class ServeTest(PairTestCase):
    def test_the_window_the_socket_and_the_invitation_exist_during_listen_and_vanish_after(self) -> None:
        seen: dict[str, Any] = {}
        machine = self.machine()

        def during() -> None:
            seen["open"] = machine.window_is_open()
            seen["line"] = pair_cli.read_invitation(self.a, 0, time.time())
            seen["problems"] = check_private(pair_cli.invitation_path(self.a))

        session = Session(during=during)
        machine.listen_impl = session.listen
        self.assertEqual(machine.serve(), 1)
        self.assertEqual(machine.events, ["elevation", "lan-ready", "bind-socket", "listen"])
        self.assertEqual(machine.bound, [(BIND, pairing.PAIRING_PORT)])
        self.assertTrue(seen["open"])
        self.assertFalse(seen["problems"])
        invitation = pairing.parse_invitation(seen["line"], time.time())
        call = session.calls[0]
        self.assertEqual((invitation.address, invitation.port), (BIND, call["port"]))
        self.assertEqual(invitation.id, device_of(self.a))
        self.assertEqual(invitation.name, call["name"])
        self.assertEqual(invitation.token, call["token"])
        self.assertAlmostEqual(invitation.expires - time.time(), 300, delta=5)
        self.assertIs(call["kwargs"]["timeouts"], FAST)
        self.assertFalse(machine.window_is_open())
        self.assertFalse(pair_cli.invitation_path(self.a).exists())
        with self.assertRaises(OSError):
            machine.sockets[0].getsockname()

    def test_the_token_is_fresh_each_time(self) -> None:
        tokens: list[str] = []
        for _ in range(2):
            machine = self.machine()
            session = Session()
            machine.listen_impl = session.listen
            machine.serve()
            tokens.append(session.calls[0]["token"])
        self.assertNotEqual(tokens[0], tokens[1])
        self.assertEqual(len(tokens[0]), 22)

    def test_serve_opens_no_firewall_rule_itself(self) -> None:
        machine = self.machine()
        machine.listen_impl = Session().listen
        machine.serve()
        self.assertNotIn("ensure-rule", machine.events)

    def test_an_unexpected_error_while_listening_still_closes_everything(self) -> None:
        machine = self.machine()
        machine.listen_impl = Session(error=RuntimeError("boom")).listen
        with self.assertRaises(RuntimeError):
            machine.serve()
        self.assertFalse(machine.window_is_open())
        self.assertFalse(pair_cli.invitation_path(self.a).exists())
        with self.assertRaises(OSError):
            machine.sockets[0].getsockname()

    def test_a_network_error_while_listening_fails_the_pairing_and_closes_everything(self) -> None:
        machine = self.machine()
        machine.listen_impl = Session(error=OSError("socket lost")).listen
        self.assertEqual(machine.serve(), 1)
        self.assertIn("socket lost", machine.errors)
        self.assertIn("Nothing was saved", machine.errors)
        self.assertFalse(machine.window_is_open())
        self.assertFalse(pair_cli.invitation_path(self.a).exists())
        with self.assertRaises(OSError):
            machine.sockets[0].getsockname()
        result = machine.result()
        assert result is not None
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("socket lost", result["reason"])
        self.assert_nothing_started(machine)

    def test_a_failed_socket_closes_the_window_and_records_the_reason(self) -> None:
        machine = self.machine()
        machine.bind_error = OSError("address in use")
        self.assertEqual(machine.serve(), 1)
        self.assertIn("address in use", machine.errors)
        self.assertFalse(machine.window_is_open())
        self.assertFalse(pair_cli.invitation_path(self.a).exists())
        result = machine.result()
        assert result is not None
        self.assertIn("address in use", result["reason"])

    def test_a_window_that_is_already_open_fails_and_records_why(self) -> None:
        first = pairing.Limits(self.a)
        first.open_window()
        self.addCleanup(first.close_window)
        machine = self.machine()
        self.assertEqual(machine.serve(), 1)
        self.assertIn("pairing busy", machine.errors)
        self.assertNotIn("bind-socket", machine.events)
        result = machine.result()
        assert result is not None
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("pairing busy", result["reason"])
        self.assertTrue(machine.window_is_open())

    def test_a_window_that_ends_without_a_peer_changes_nothing(self) -> None:
        machine = self.machine()
        machine.listen_impl = Session().listen
        self.assertEqual(machine.serve(), 1)
        self.assertIn(WINDOW_CLOSED, machine.errors)
        result = machine.result()
        assert result is not None
        self.assertEqual((result["outcome"], result["label"], result["device"]), ("failed", None, None))
        self.assert_nothing_started(machine)

    def test_a_pairing_turns_the_lan_on_starts_the_listener_and_registers_the_login_task(self) -> None:
        machine = self.machine()
        machine.listen_impl = Session(peer=peer_of(self.b)).listen
        self.assertEqual(machine.serve(), 0, machine.errors)
        config = self.config()["network"]
        self.assertEqual((config["network"], config["bind"]), ("lan", BIND))
        self.assertEqual(machine.harness.firewall, [(BIND, SUBNET, "Ethernet")])
        self.assertEqual(machine.harness.logins, [self.a])
        self.assertEqual(machine.harness.events, ["firewall", "register-login"])
        self.assertEqual(len(machine.harness.spawned), 1)
        self.assertIn("Paired with bravo at 192.168.50.20. Grants: none.", machine.out)
        self.assertEqual(machine.notices[0], "paired with bravo at 192.168.50.20")
        result = machine.result()
        assert result is not None
        self.assertEqual(
            (result["outcome"], result["label"], result["device"], result["address"]), ("paired", "bravo", self.b_id, PEER_ADDRESS)
        )
        self.assertFalse(check_private(pair_cli.result_path(self.a)))

    def test_an_incomplete_pairing_keeps_the_peer_turns_the_lan_on_and_fails(self) -> None:
        machine = self.machine()
        peer = peer_of(self.b)
        machine.listen_impl = Session(PairResult("incomplete", pairing.INCOMPLETE_TEXT, peer), peer).listen
        self.assertEqual(machine.serve(), 1)
        self.assertIn("net revoke bravo", machine.errors)
        self.assertEqual(load_policy(self.a).peers[0].device_id, self.b_id)
        self.assertEqual(self.config()["network"]["network"], "lan")
        self.assertEqual(len(machine.harness.spawned), 1)
        self.assertEqual(machine.harness.logins, [self.a])
        result = machine.result()
        assert result is not None
        self.assertEqual(result["outcome"], "incomplete")

    def test_a_failed_pairing_that_locked_the_machine_says_so_and_saves_nothing(self) -> None:
        machine = self.machine()
        machine.listen_impl = Session(PairResult("failed", "the codes do not match", None, True)).listen
        self.assertEqual(machine.serve(), 1)
        self.assertIn(pair_cli.LOCK_NOTICE, machine.notices)
        self.assertIn(pair_cli.LOCK_NOTICE, machine.errors)
        self.assertIn("the codes do not match", machine.errors)
        self.assertIn("Nothing was saved", machine.errors)
        self.assert_nothing_started(machine)

    def test_a_login_task_that_is_refused_is_reported_and_the_pairing_stays(self) -> None:
        machine = self.machine()
        machine.harness.login_error = LoginTaskError("Task Scheduler refused")
        machine.listen_impl = Session(peer=peer_of(self.b)).listen
        self.assertEqual(machine.serve(), 1)
        self.assertIn("login task was not registered", machine.errors)
        self.assertEqual(len(load_policy(self.a).peers), 1)
        self.assertEqual(len(machine.harness.spawned), 1)

    def test_a_listener_that_does_not_start_is_reported_and_the_pairing_stays(self) -> None:
        machine = self.machine()
        machine.harness.services.spawn = lambda _home, _port: None
        machine.listen_impl = Session(peer=peer_of(self.b)).listen
        self.assertEqual(machine.serve(), 1)
        self.assertIn("could not be started", machine.errors)
        self.assertEqual(len(load_policy(self.a).peers), 1)


class JoinTest(PairTestCase):
    def test_a_join_dials_the_checked_address_with_the_invitation_values(self) -> None:
        machine = self.machine()
        session = Session(peer=peer_of(self.b, "bravo", "192.168.50.20"))
        machine.dial_impl = session.dial
        text = invitation_line(address="192.168.50.20", port=47999, name="bravo")
        invitation = pairing.parse_invitation(text, time.time())
        self.assertEqual(machine.run("--join", f"  {text}  "), 0, machine.errors)
        self.assertEqual(machine.events, ["elevation", "lan-ready", "destination", "dial"])
        call = session.calls[0]
        self.assertEqual(
            (call["address"], call["port"], call["token"], call["peer_id"]), ("192.168.50.20", 47999, invitation.token, GOOD_ID)
        )
        self.assertIs(call["kwargs"]["timeouts"], FAST)
        self.assertRegex(machine.out, rf"Pairing \S.* {BIND} with bravo at 192\.168\.50\.20\.")
        self.assertEqual(machine.bound, [])
        self.assertEqual(machine.servers, [])

    def test_a_pairing_turns_the_lan_on_starts_the_listener_and_registers_the_login_task(self) -> None:
        machine = self.machine()
        machine.dial_impl = Session(peer=peer_of(self.b)).dial
        self.assertEqual(machine.run("--join", invitation_line()), 0, machine.errors)
        self.assertEqual(self.config()["network"]["network"], "lan")
        self.assertEqual(self.config()["network"]["bind"], BIND)
        self.assertEqual(load_policy(self.a).peers[0].device_id, self.b_id)
        self.assertEqual(machine.harness.firewall, [(BIND, SUBNET, "Ethernet")])
        self.assertEqual(machine.harness.logins, [self.a])
        self.assertEqual(len(machine.harness.spawned), 1)
        self.assertIn("Grants: none.", machine.out)
        self.assertEqual(machine.result()["outcome"], "paired")  # pyright: ignore[reportOptionalSubscript]

    def test_the_bind_option_picks_the_interface(self) -> None:
        machine = self.machine()
        machine.dial_impl = Session().dial
        machine.run("--join", invitation_line(), "--bind", BIND)
        self.assertEqual(machine.lan_calls, [(BIND, True)])

    def test_a_malformed_expired_or_distant_invitation_fails_before_dialing(self) -> None:
        for text in ("junk", "", invitation_line(ahead=-5), invitation_line(ahead=100000), invitation_line() + "x", "atp1-invite:abc"):
            machine = self.machine()
            session = Session()
            machine.dial_impl = session.dial
            self.assertEqual(machine.run("--join", text), 1, text)
            self.assertTrue(machine.errors, text)
            self.assertNotIn("destination", machine.events)
            self.assertNotIn("dial", machine.events)
            self.assertEqual(session.calls, [])
            self.assertIsNone(machine.result())
        self.assert_nothing_started(machine)

    def test_an_address_that_fails_the_check_is_refused_before_dialing(self) -> None:
        for address in ("8.8.8.8", "10.1.2.3", BIND, "192.168.50.0", "192.168.50.255", "192.168.51.20"):
            machine = self.machine()
            session = Session()
            machine.dial_impl = session.dial
            self.assertEqual(machine.run("--join", invitation_line(address=address)), 1, address)
            self.assertTrue(machine.errors, address)
            self.assertNotIn("dial", machine.events)
            self.assertEqual(session.calls, [])

    def test_a_locked_address_is_not_dialed(self) -> None:
        limits = pairing.Limits(self.a)
        for _ in range(3):
            limits.record_failure(PEER_ADDRESS)
        machine = self.machine()
        session = Session()
        machine.dial_impl = session.dial
        self.assertEqual(machine.run("--join", invitation_line()), 1)
        self.assertIn("locked", machine.errors)
        self.assertNotIn("dial", machine.events)
        self.assertEqual(session.calls, [])

    def test_a_refused_pairing_says_nothing_was_saved(self) -> None:
        machine = self.machine()
        machine.dial_impl = Session(PairResult("failed", "the other machine declined")).dial
        self.assertEqual(machine.run("--join", invitation_line()), 1)
        self.assertIn("the other machine declined", machine.errors)
        self.assertIn("Nothing was saved", machine.errors)
        result = machine.result()
        assert result is not None
        self.assertEqual((result["outcome"], result["reason"]), ("failed", "the other machine declined"))
        self.assert_nothing_started(machine)

    def test_a_dial_that_locks_the_machine_notifies(self) -> None:
        machine = self.machine()
        machine.dial_impl = Session(PairResult("failed", "the token was refused", None, True)).dial
        self.assertEqual(machine.run("--join", invitation_line()), 1)
        self.assertIn(pair_cli.LOCK_NOTICE, machine.notices)
        self.assertIn("net trust", machine.errors)


class SaverTest(PairTestCase):
    def saver(self) -> tuple[pair_cli.Saver, Machine]:
        machine = self.machine()
        return pair_cli.Saver(machine.harness.services), machine

    def test_a_new_peer_gets_no_grants_and_a_label_from_its_name(self) -> None:
        saver, machine = self.saver()
        peer = peer_of(self.b, "Bravo Laptop")
        saver(peer)
        grant = load_policy(self.a).peers[0]
        self.assertEqual(
            (grant.device_id, grant.label, grant.address, grant.folders, grant.agents, grant.models, grant.send),
            (self.b_id, "bravo-laptop", PEER_ADDRESS, (), (), (), ()),
        )
        self.assertEqual(saver.label, "bravo-laptop")
        stored = peer_cert_path(self.a, self.b_id)
        self.assertEqual(stored.read_text(encoding="ascii"), peer.pem)
        self.assertEqual(machine.notices, ["paired with bravo-laptop at 192.168.50.20"])
        self.assertIn("pair", Path(log_file(self.a, "cli.log")).read_text())

    def test_a_new_peer_never_takes_the_label_of_another_peer(self) -> None:
        save_policy(
            self.a,
            Policy(
                (PeerGrant(self.c_id, "bravo", "192.168.50.30"), PeerGrant(format_digest(bytes(range(1, 33))), "bravo-2", "192.168.50.31"))
            ),
        )
        saver, _ = self.saver()
        saver(peer_of(self.b, "Bravo"))
        self.assertEqual(saver.label, "bravo-3")
        self.assertEqual([peer.label for peer in load_policy(self.a).peers], ["bravo", "bravo-2", "bravo-3"])

    def test_a_known_peer_keeps_its_label_and_grants_and_only_the_address_changes(self) -> None:
        folder = os.path.realpath(tempfile.mkdtemp(prefix="iat-pair-work-"))
        self.addCleanup(shutil.rmtree, folder, True)
        stat = os.stat(folder)
        save_policy(
            self.a,
            Policy(
                (
                    PeerGrant(
                        self.b_id,
                        "old-name",
                        "192.168.50.99",
                        (FolderGrant("work", folder, stat.st_dev, stat.st_ino),),
                        ("claude",),
                        max_tabs=3,
                    ),
                )
            ),
        )
        saver, machine = self.saver()
        saver(peer_of(self.b, "bravo", PEER_ADDRESS))
        peers = load_policy(self.a).peers
        self.assertEqual(len(peers), 1)
        kept = peers[0]
        self.assertEqual((kept.label, kept.address, kept.agents, kept.max_tabs), ("old-name", PEER_ADDRESS, ("claude",), 3))
        self.assertEqual([grant.path for grant in kept.folders], [folder])
        self.assertEqual(saver.label, "old-name")
        self.assertEqual(machine.notices, ["paired with old-name at 192.168.50.20"])

    def test_a_known_peer_through_a_join_reports_its_grants(self) -> None:
        save_policy(self.a, Policy((PeerGrant(self.b_id, "old-name", "192.168.50.99", agents=("claude",)),)))
        machine = self.machine()
        machine.dial_impl = Session(peer=peer_of(self.b)).dial
        self.assertEqual(machine.run("--join", invitation_line()), 0, machine.errors)
        self.assertIn("Paired with old-name at 192.168.50.20. Grants: agents claude.", machine.out)
        self.assertEqual(len(load_policy(self.a).peers), 1)

    def test_the_default_label_is_lowercased_made_safe_and_unique(self) -> None:
        def policy(*labels: str) -> Policy:
            return Policy(tuple(PeerGrant(format_digest(bytes([index]) * 32), label, "192.168.50.5") for index, label in enumerate(labels)))

        self.assertEqual(pair_cli.default_label("Work PC.1", policy()), "work-pc-1")
        self.assertEqual(pair_cli.default_label("Work PC", policy("work-pc")), "work-pc-2")
        self.assertEqual(pair_cli.default_label("Work PC", policy("work-pc", "work-pc-2")), "work-pc-3")
        self.assertEqual(pair_cli.default_label("...", policy()), "peer")
        long = "a" * 32
        label = pair_cli.default_label(long, policy(long))
        self.assertEqual(label, "a" * 30 + "-2")
        self.assertEqual(len(label), 32)


class ResultTest(PairTestCase):
    def test_a_result_round_trips_privately(self) -> None:
        peer = peer_of(self.b)
        pair_cli.write_result(self.a, PairResult("paired", "paired", peer), "bravo", 1_700_000_000.9)
        self.assertEqual(
            pair_cli.read_result(self.a),
            {"outcome": "paired", "reason": "paired", "label": "bravo", "device": self.b_id, "address": PEER_ADDRESS, "at": 1_700_000_000},
        )
        self.assertFalse(check_private(pair_cli.result_path(self.a)))

    def test_result_text_describes_success_and_failure(self) -> None:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(1_700_000_000))
        self.assertIsNone(pair_cli.result_text(self.a))
        pair_cli.write_result(self.a, PairResult("paired", "paired", peer_of(self.b)), "bravo", 1_700_000_000)
        self.assertEqual(pair_cli.result_text(self.a), f"paired with bravo at {PEER_ADDRESS} ({when})")
        pair_cli.write_result(self.a, PairResult("failed", WINDOW_CLOSED), None, 1_700_000_000)
        self.assertEqual(pair_cli.result_text(self.a), f"failed: {WINDOW_CLOSED} ({when})")

    def test_an_unreadable_or_odd_result_is_ignored(self) -> None:
        path = pair_cli.result_path(self.a)
        pair_cli.write_result(self.a, PairResult("failed", "x"), None, 1)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(pair_cli.read_result(self.a))
        path.write_text("[1, 2]", encoding="utf-8")
        self.assertIsNone(pair_cli.read_result(self.a))
        self.assertIsNone(pair_cli.result_text(self.a))

    def test_status_shows_the_last_pairing_after_a_pairing(self) -> None:
        machine = self.machine()
        self.assertEqual(machine.harness.run("status"), 0)
        self.assertNotIn("last pairing", machine.harness.text)
        machine.listen_impl = Session(peer=peer_of(self.b)).listen
        self.assertEqual(machine.serve(), 0, machine.errors)
        machine.harness.out.clear()
        self.assertEqual(machine.harness.run("status"), 0)
        self.assertRegex(machine.harness.text, r"last pairing: paired with bravo at 192\.168\.50\.20 \(\d{4}-\d\d-\d\d \d\d:\d\d\)")

    def test_status_shows_a_failed_pairing(self) -> None:
        machine = self.machine()
        machine.listen_impl = Session().listen
        machine.serve()
        machine.harness.out.clear()
        self.assertEqual(machine.harness.run("status"), 0)
        self.assertRegex(machine.harness.text, rf"last pairing: failed: {WINDOW_CLOSED} \(\d{{4}}-\d\d-\d\d \d\d:\d\d\)")


class InvitationFileTest(PairTestCase):
    def test_a_good_file_is_returned_without_its_newline(self) -> None:
        line = invitation_line()
        pair_cli.write_invitation(self.a, line)
        self.assertEqual(pair_cli.read_invitation(self.a, 0, time.time()), line)
        self.assertEqual(pair_cli.invitation_path(self.a).read_text(encoding="ascii"), line + "\n")
        self.assertFalse(check_private(pair_cli.invitation_path(self.a)))

    def test_a_file_older_than_the_start_is_ignored(self) -> None:
        pair_cli.write_invitation(self.a, invitation_line())
        mtime = pair_cli.invitation_path(self.a).stat().st_mtime
        self.assertIsNone(pair_cli.read_invitation(self.a, mtime + 10, time.time()))
        self.assertIsNotNone(pair_cli.read_invitation(self.a, mtime - 10, time.time()))

    def test_a_missing_expired_or_damaged_file_is_ignored(self) -> None:
        self.assertIsNone(pair_cli.read_invitation(self.a, 0, time.time()))
        for text in ("", "junk", invitation_line() + "\n" + invitation_line(), invitation_line(ahead=-5)):
            pair_cli.write_invitation(self.a, text)
            self.assertIsNone(pair_cli.read_invitation(self.a, 0, time.time()), text)

    @unittest.skipIf(os.name == "nt", "mode bits are POSIX")
    def test_a_file_other_users_can_read_is_ignored(self) -> None:
        pair_cli.write_invitation(self.a, invitation_line())
        os.chmod(pair_cli.invitation_path(self.a), 0o644)
        self.assertIsNone(pair_cli.read_invitation(self.a, 0, time.time()))


class HelpersTest(unittest.TestCase):
    def test_find_openssl_prefers_git_for_windows_then_the_path(self) -> None:
        root = make_root(self)
        git = os.path.join(root, "Git", "usr", "bin")
        os.makedirs(git)
        exe = os.path.join(git, "openssl.exe")
        Path(exe).write_text("x")
        other = os.path.join(root, "path-openssl")
        Path(other).write_text("x")
        self.assertEqual(pair_cli.find_openssl(None, {"ProgramFiles": root}, "win32", lambda _n: other), exe)
        self.assertIsNone(pair_cli.find_openssl(None, {"ProgramFiles": os.path.join(root, "none")}, "win32", lambda _n: other))
        self.assertEqual(pair_cli.find_openssl(None, {}, "linux", lambda _n: other), other)
        self.assertIsNone(pair_cli.find_openssl(None, {}, "linux", lambda _n: None))
        self.assertEqual(pair_cli.find_openssl(other, {}, "linux", lambda _n: None), other)
        self.assertIsNone(pair_cli.find_openssl("relative/openssl", {}, "linux", lambda _n: None))

    def test_the_default_server_spawn_builds_the_serve_command_in_the_given_home(self) -> None:
        child = mock.Mock(pid=321)
        with mock.patch.object(pair_cli, "spawn_detached", return_value=child) as spawn:
            pid = PairDeps().spawn_server("/home/x", "192.168.50.10", "/py", "/launch/agent_tabs.py")
        self.assertEqual(pid, 321)
        command, env, _cwd = spawn.call_args.args
        self.assertEqual(command, ["/py", "-I", "-S", "/launch/agent_tabs.py", "net", "pair", "--serve", "--bind", "192.168.50.10"])
        self.assertEqual(env["IDE_AGENT_TABS_HOME"], "/home/x")

    def test_the_default_server_spawn_reports_a_failure_as_none(self) -> None:
        with mock.patch.object(pair_cli, "spawn_detached", side_effect=OSError("denied")):
            self.assertIsNone(PairDeps().spawn_server("/home/x", "192.168.50.10", "/py", "/launch/agent_tabs.py"))

    def test_the_pairing_socket_listens_on_the_given_address(self) -> None:
        sock = pair_cli.bind_pairing_socket("127.0.0.1", 0)
        self.addCleanup(sock.close)
        host, port = sock.getsockname()
        self.assertEqual(host, "127.0.0.1")
        with socket.create_connection((host, port), timeout=2):
            pass

    def test_the_pairing_socket_does_not_share_its_port(self) -> None:
        sock = pair_cli.bind_pairing_socket("127.0.0.1", 0)
        self.addCleanup(sock.close)
        with self.assertRaises(OSError):
            pair_cli.bind_pairing_socket("127.0.0.1", sock.getsockname()[1])


@NEEDS
class LoopbackTest(unittest.TestCase):
    a: str
    b: str

    @classmethod
    def setUpClass(cls) -> None:
        root = make_root(cls)
        cls.a, cls.b = (create_home(root, name) for name in ("a", "b"))

    def setUp(self) -> None:
        for home, name in ((self.a, "alpha"), (self.b, "bravo")):
            reset_peers(home)
            for leftover in (pairing.PAIRING_FILE, pairing.PAIRING_FILE + ".lock", pair_cli.INVITATION_FILE, pair_cli.RESULT_FILE):
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(home, "net", leftover))
            for leftover_path in (Path(home, "config.json"), listener_file(home)):
                with contextlib.suppress(OSError):
                    os.remove(leftover_path)
            Path(home, "config.json").write_text(json.dumps({"network": {"deviceName": name}}), encoding="utf-8")

    def machines(self) -> tuple[Machine, Machine]:
        serving = Machine(self, self.a)
        joining = Machine(self, self.b, bind=PEER_ADDRESS)
        joining.dial_address = "127.0.0.1"
        return serving, joining

    def pair(self, serving: Machine, joining: Machine) -> tuple[int, int]:
        codes: list[int] = []
        thread = threading.Thread(target=lambda: codes.append(serving.serve()), daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        line = None
        while line is None and time.monotonic() < deadline:
            line = pair_cli.read_invitation(self.a, 0, time.time())
            time.sleep(0.02)
        assert line is not None, serving.errors
        code_joining = joining.run("--join", line)
        thread.join(40)
        assert codes, "the serving side never finished"
        return codes[0], code_joining

    def test_two_machines_pair_through_the_real_protocol(self) -> None:
        serving, joining = self.machines()
        code_a, code_b = self.pair(serving, joining)
        self.assertEqual((code_a, code_b), (0, 0), f"{serving.errors}\n{joining.errors}")
        a_peer, b_peer = load_policy(self.a).peers[0], load_policy(self.b).peers[0]
        self.assertEqual((a_peer.device_id, a_peer.label, a_peer.folders, a_peer.agents), (device_of(self.b), "bravo", (), ()))
        self.assertEqual((b_peer.device_id, b_peer.label, b_peer.folders, b_peer.agents), (device_of(self.a), "alpha", (), ()))
        self.assertEqual(
            peer_cert_path(self.a, device_of(self.b)).read_text(encoding="ascii").strip(),
            cert_of(self.b).read_text(encoding="ascii").strip(),
        )
        self.assertEqual(
            peer_cert_path(self.b, device_of(self.a)).read_text(encoding="ascii").strip(),
            cert_of(self.a).read_text(encoding="ascii").strip(),
        )
        for machine in (serving, joining):
            self.assertEqual(machine.harness.logins, [machine.home])
            self.assertEqual(len(machine.harness.spawned), 1)
            self.assertEqual(json.loads(Path(machine.home, "config.json").read_text())["network"]["network"], "lan")
            result = machine.result()
            assert result is not None
            self.assertEqual(result["outcome"], "paired")
        self.assertEqual(serving.bound, [(BIND, pairing.PAIRING_PORT)])
        self.assertEqual(serving.events.count("ensure-rule"), 0)
        self.assertFalse(serving.window_is_open())
        self.assertFalse(pair_cli.invitation_path(self.a).exists())
        self.assertFalse(Path(self.b, "net", pair_cli.INVITATION_FILE).exists())

    def test_pairing_again_keeps_the_label_and_the_grants(self) -> None:
        serving, joining = self.machines()
        self.assertEqual(self.pair(serving, joining), (0, 0), f"{serving.errors}\n{joining.errors}")
        known = load_policy(self.a)
        edited = replace(known.peers[0], label="bravo-desk", agents=("claude",), max_tabs=3)
        save_policy(self.a, replace(known, peers=(edited,)))
        serving, joining = self.machines()
        self.assertEqual(self.pair(serving, joining), (0, 0), f"{serving.errors}\n{joining.errors}")
        peers = load_policy(self.a).peers
        self.assertEqual(len(peers), 1)
        self.assertEqual(
            (peers[0].label, peers[0].agents, peers[0].max_tabs, peers[0].address), ("bravo-desk", ("claude",), 3, "127.0.0.1")
        )
        self.assertEqual(len(load_policy(self.b).peers), 1)
        self.assertIn("Grants: agents claude.", serving.out)


if __name__ == "__main__":
    unittest.main()
