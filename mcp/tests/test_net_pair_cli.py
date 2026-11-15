from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ide_agent_tabs.net import cli, elevation, pair_cli, pairing, window
from ide_agent_tabs.net.client import peer_cert_path
from ide_agent_tabs.net.firewall import FirewallError
from ide_agent_tabs.net.identity import format_digest
from ide_agent_tabs.net.interfaces import InterfaceError, InterfaceInfo
from ide_agent_tabs.net.listener import listener_file, log_file
from ide_agent_tabs.net.login_task import Environment
from ide_agent_tabs.net.pair_cli import ConsolePrompter, ConsoleTerminal, PairDeps
from ide_agent_tabs.net.pairing import PairResult, PeerInfo
from ide_agent_tabs.net.policy import FolderGrant, PeerGrant, Policy, load_policy, save_policy
from ide_agent_tabs.net.window import WindowError
from net_support import HAS_OPENSSL, OPENSSL, cert_of, create_home, device_of, make_root, pem_der, reset_peers
from test_net_cli import NEEDS, Harness

GOOD_ID = format_digest(bytes(range(32)))
BIND = "192.168.50.10"
SUBNET = "192.168.50.0/24"
FAST = pairing.Timeouts(
    first_frame_s=5.0,
    digits_s=20.0,
    typing_s=20.0,
    review_s=20.0,
    consent_s=10.0,
    connect_s=5.0,
    window_s=60.0,
    min_interval_s=0.0,
)
CODE_LINE = re.compile(r"^Code: (\d{6})$", re.MULTILINE)


def invitation_line(address: str = "192.168.50.20", port: int = 47830, name: str = "bravo", ahead: int = 300) -> str:
    return pairing.make_invitation(name, address, port, GOOD_ID, pairing.new_invitation_token(), int(time.time()) + ahead)


class ScriptTerminal:
    def __init__(self, rules: Sequence[tuple[str, Any]] = (), typing: Callable[[], str | None] | None = None) -> None:
        self.lines: list[str] = []
        self.prompts: list[str] = []
        self.unmatched: list[str] = []
        self.rules = [(needle, list(value) if isinstance(value, list) else value) for needle, value in rules]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        self.typing = typing
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        with self._lock:
            self.lines.append(text)

    @property
    def text(self) -> str:
        with self._lock:
            return "\n".join(self.lines)

    def code(self) -> str | None:
        found = CODE_LINE.search(self.text)
        return found.group(1) if found else None

    def wait_code(self, seconds: float = 30.0) -> str | None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            code = self.code()
            if code is not None:
                return code
            time.sleep(0.02)
        return None

    def read(self, prompt: str, timeout_s: float) -> str | None:
        with self._lock:
            self.prompts.append(prompt)
        if "code shown on the other machine" in prompt and self.typing is not None:
            return self.typing()
        for needle, value in self.rules:
            if needle in prompt:
                answer: Any = value
                if isinstance(answer, list):
                    if answer:
                        return answer.pop(0)
                    continue
                if callable(answer):
                    answer = answer()
                return answer
        with self._lock:
            self.unmatched.append(prompt)
        return None


class Machine:
    def __init__(
        self,
        test: unittest.TestCase,
        home: str,
        *,
        rules: Sequence[tuple[str, Any]] = (),
        typing: Callable[[], str | None] | None = None,
        bind: str = BIND,
    ) -> None:
        self.test = test
        self.home = home
        self.bind = bind
        self.screen = ScriptTerminal(rules, typing)
        self.events: list[str] = []
        self.windows: list[tuple[list[str], str | None, str]] = []
        self.rules_opened: list[tuple[str, str, str]] = []
        self.bound: list[tuple[str, int]] = []
        self.sockets: list[socket.socket] = []
        self.installed: list[tuple[str, str, str]] = []
        self.appended: list[Path] = []
        self.agents: list[tuple[str, str]] = []
        self.on_path = True
        self.open_agent_result: Callable[[str, str], str | None] | None = None
        self.elevation_error: Exception | None = None
        self.lan_error: Exception | None = None
        self.rule_error: Exception | None = None
        self.close_error: Exception | None = None
        self.bind_error: Exception | None = None
        self.openssl: str | None = str(OPENSSL) if HAS_OPENSSL else None
        self.lan_calls: list[tuple[str, bool]] = []
        self.listen_impl: Callable[..., PairResult] | None = None
        self.dial_impl: Callable[..., PairResult] | None = None
        self.dial_address: str | None = None
        self.destination_error: Exception | None = None
        self.window_hook: Callable[[], None] | None = None
        self.window_message = window.CONTINUE_MESSAGE
        self.deps = PairDeps(
            interactive=lambda: True,
            refuse_if_elevated=self._elevation,
            open_window=self._open_window,
            caller=lambda: "Claude Code",
            terminal=lambda _write: self.screen,
            local_addresses=lambda: [self.bind],
            require_lan_ready=self._lan_ready,
            find_openssl=lambda _configured, _env: self.openssl,
            open_rule=self._open_rule,
            close_rule=self._close_rule,
            bind_socket=self._bind_socket,
            check_destination=self._destination,
            listen=self._listen,
            dial=self._dial,
            timeouts=FAST,
            install_shim=self._install_shim,
            path_contains=lambda _folder: self.on_path,
            append_to_user_path=self._append_path,
            open_agent=self._open_agent,
            interpreter=r"C:\py\python.exe",
            launch_script=r"C:\plugin\launch\agent_tabs.py",
            invitation_wait_s=0.6,
            sleep=lambda _seconds: time.sleep(0.02),
        )
        self.harness = Harness(test, home, out=self.screen.write, pair=self.deps)
        test.addCleanup(self._close_sockets)

    def _close_sockets(self) -> None:
        for sock in self.sockets:
            sock.close()

    def _elevation(self) -> None:
        self.events.append("elevation")
        if self.elevation_error is not None:
            raise self.elevation_error

    def _open_window(self, subcommand: list[str], invitation: str | None, caller: str) -> str:
        self.events.append("window")
        self.windows.append((subcommand, invitation, caller))
        if self.window_hook is not None:
            self.window_hook()
        return self.window_message

    def _lan_ready(self, address: str, allow_virtual: bool) -> InterfaceInfo:
        self.events.append("lan-ready")
        self.lan_calls.append((address, allow_virtual))
        if self.lan_error is not None:
            raise self.lan_error
        prefix = ".".join(address.split(".")[:3])
        return InterfaceInfo("Ethernet", address, 24, f"{prefix}.0/24", "Private", True)

    def _open_rule(self, bind: str, subnet: str, alias: str) -> str:
        self.events.append("open-rule")
        if self.rule_error is not None:
            raise self.rule_error
        self.rules_opened.append((bind, subnet, alias))
        return "Created the pairing rule."

    def _close_rule(self) -> str:
        self.events.append("close-rule")
        if self.close_error is not None:
            raise self.close_error
        return "Removed the pairing rule."

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
        if self.destination_error is not None:
            raise self.destination_error
        return pairing.check_lan_destination(address, port, interface, prefix)

    def _listen(self, *args: Any, **kwargs: Any) -> PairResult:
        self.events.append("listen")
        return (self.listen_impl or pairing.listen_once)(*args, **kwargs)

    def _dial(self, address: str, *args: Any, **kwargs: Any) -> PairResult:
        self.events.append("dial")
        return (self.dial_impl or pairing.dial)(self.dial_address or address, *args, **kwargs)

    def _install_shim(self, home: str, interpreter: str, entry: str) -> Path:
        self.installed.append((home, interpreter, entry))
        return Path(home) / "bin"

    def _append_path(self, folder: Path) -> str:
        self.appended.append(folder)
        return f"Appended {folder} to your user PATH."

    def _open_agent(self, agent: str, folder: str) -> str | None:
        self.agents.append((agent, folder))
        return self.open_agent_result(agent, folder) if self.open_agent_result is not None else None

    def run(self, *args: str) -> int:
        return cli.main(["pair", *args], self.harness.services)

    @property
    def out(self) -> str:
        return self.screen.text

    @property
    def errors(self) -> str:
        return self.harness.errors

    @property
    def notices(self) -> list[str]:
        return self.harness.notices

    def failures(self) -> int:
        path = Path(self.home) / "net" / pairing.PAIRING_FILE
        return len(json.loads(path.read_text(encoding="utf-8"))["failures"]) if path.exists() else 0

    def window_is_open(self) -> bool:
        try:
            pairing.Limits(self.home).check()
        except pairing.Busy as error:
            return "already open" in str(error)
        return False


def peer_of(home: str, name: str = "bravo", address: str = "192.168.50.20") -> PeerInfo:
    return PeerInfo(name, address, device_of(home), pem_der(home))


class Session:
    def __init__(
        self,
        result: PairResult | None = None,
        peer: PeerInfo | None = None,
        *,
        review: bool = True,
        during: Callable[[], None] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.result = result or PairResult("failed", "the pairing window closed before another machine paired")
        self.peer = peer
        self.review = review
        self.during = during
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.typed: str | None = None
        self.reviewed: bool | None = None

    def _drive(self, prompter: Any, save: Callable[[PeerInfo], None]) -> None:
        if self.peer is not None:
            prompter.show_code(self.peer, "123456")
            self.typed = prompter.read_code(self.peer, 5.0)
            if self.review:
                self.reviewed = prompter.review(self.peer, 5.0)
                if self.reviewed:
                    save(self.peer)

    def listen(
        self, sock: socket.socket, token: str, identity: Any, prompter: Any, save: Callable[[PeerInfo], None], limits: Any, **kwargs: Any
    ) -> PairResult:
        self.calls.append({"token": token, "port": sock.getsockname()[1], "name": identity.name})
        if self.during is not None:
            self.during()
        if self.error is not None:
            raise self.error
        self._drive(prompter, save)
        return self.result

    def dial(
        self,
        address: str,
        port: int,
        token: str,
        identity: Any,
        prompter: Any,
        save: Callable[[PeerInfo], None],
        limits: Any,
        **kwargs: Any,
    ) -> PairResult:
        self.calls.append({"address": address, "port": port, "token": token, "name": identity.name})
        if self.during is not None:
            self.during()
        if self.error is not None:
            raise self.error
        self._drive(prompter, save)
        return self.result


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
        for name in ("config.json",):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(self.a, name))
        for name in (pairing.PAIRING_FILE, pairing.PAIRING_FILE + ".lock", pair_cli.INVITATION_FILE):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(self.a, "net", name))
        with contextlib.suppress(OSError):
            os.remove(listener_file(self.a))
        with contextlib.suppress(OSError):
            os.remove(log_file(self.a, "cli.log"))
        self.b_id, self.c_id = device_of(self.b), device_of(self.c)

    def machine(self, **kwargs: Any) -> Machine:
        return Machine(self, self.a, **kwargs)

    def write_config(self, **network: Any) -> None:
        Path(self.a, "config.json").write_text(json.dumps({"network": network}), encoding="utf-8")

    def config(self) -> dict[str, Any]:
        return json.loads(Path(self.a, "config.json").read_text(encoding="utf-8"))

    def folder(self, name: str = "work") -> str:
        path = os.path.realpath(tempfile.mkdtemp(prefix=f"iat-pair-{name}-"))
        self.addCleanup(shutil.rmtree, path, True)
        return path


def review_rules(*extra: tuple[str, Any]) -> list[tuple[str, Any]]:
    return [
        *extra,
        ("Label for this peer", ""),
        ("Folder", ""),
        ("Turn the listener on", "n"),
        ("Start the listener each time", "n"),
        ("Open ", "n"),
        ("Append", "n"),
        ("Type yes", "yes"),
    ]


class HandOffTest(PairTestCase):
    def test_without_a_terminal_every_form_opens_a_window_after_the_elevation_check(self) -> None:
        cases = (
            ([], ["net", "pair"], None),
            (["--invite"], ["net", "pair", "--invite"], None),
            (["--join", invitation_line()], ["net", "pair", "--join"], "invitation"),
            (["--peer", "192.168.50.20", "--token", "abc234"], ["net", "pair", "--peer", "192.168.50.20", "--token", "ABC234"], None),
        )
        for args, expected, invitation in cases:
            machine = self.machine()
            machine.deps.interactive = lambda: False
            if args == ["--invite"]:
                machine.window_hook = lambda: pair_cli.write_invitation(self.a, invitation_line())
            self.assertEqual(machine.run(*args), 0, machine.errors)
            self.assertEqual(machine.events[:2], ["elevation", "window"], args)
            subcommand, carried, caller = machine.windows[0]
            self.assertEqual(subcommand, expected)
            self.assertEqual(carried, args[1] if invitation else None)
            self.assertEqual(caller, "Claude Code")
            self.assertTrue(machine.out.startswith(window.CONTINUE_MESSAGE))
            self.assertEqual(machine.screen.prompts, [])
            self.assertEqual(machine.rules_opened, [])
            self.assertEqual(machine.bound, [])

    def test_the_invitation_comes_back_through_a_private_file_and_stays(self) -> None:
        machine = self.machine()
        machine.deps.interactive = lambda: False
        line = invitation_line()
        machine.window_hook = lambda: threading.Timer(0.2, pair_cli.write_invitation, (self.a, line)).start()
        machine.deps.invitation_wait_s = 5.0
        self.assertEqual(machine.run("--invite"), 0, machine.errors)
        self.assertEqual(machine.screen.lines, [window.CONTINUE_MESSAGE, line])
        self.assertTrue(pair_cli.invitation_path(self.a).is_file())
        self.assertEqual(pair_cli.invitation_path(self.a).read_text(encoding="ascii"), line + "\n")

    def test_the_six_digits_never_reach_the_caller(self) -> None:
        machine = self.machine()
        machine.deps.interactive = lambda: False
        machine.window_hook = lambda: pair_cli.write_invitation(self.a, invitation_line())
        self.assertEqual(machine.run("--invite"), 0)
        self.assertIsNone(CODE_LINE.search(machine.out))
        self.assertEqual(machine.screen.prompts, [])

    def test_a_missing_invitation_is_reported(self) -> None:
        machine = self.machine()
        machine.deps.interactive = lambda: False
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("no invitation", machine.errors)

    def test_a_file_from_an_earlier_window_is_not_returned(self) -> None:
        pair_cli.write_invitation(self.a, invitation_line())
        old = time.time() - 3600
        os.utime(pair_cli.invitation_path(self.a), (old, old))
        machine = self.machine()
        machine.deps.interactive = lambda: False
        self.assertEqual(machine.run("--invite"), 1)
        self.assertNotIn("atp1-invite:", machine.out)

    def test_an_expired_or_damaged_file_is_not_returned(self) -> None:
        machine = self.machine()
        machine.deps.interactive = lambda: False
        for text in ("atp1-invite:abc", "not an invitation", invitation_line() + "\nsecond"):
            machine.window_hook = lambda text=text: pair_cli.write_invitation(self.a, text)
            machine.screen.lines.clear()
            self.assertEqual(machine.run("--invite"), 1, text)
            self.assertNotIn(text, machine.out)

    def test_a_window_that_cannot_open_is_not_waited_for(self) -> None:
        machine = self.machine()
        machine.deps.interactive = lambda: False
        machine.window_message = "No terminal window could be opened from here. Run this in a terminal: agent-tabs net pair --invite"
        started = time.monotonic()
        self.assertEqual(machine.run("--invite"), 1)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn("Run this in a terminal", machine.out)

    def test_a_refused_window_command_is_an_error(self) -> None:
        machine = self.machine()
        machine.deps.interactive = lambda: False

        def refuse(_subcommand: list[str], _invitation: str | None, _caller: str) -> str:
            raise WindowError("the invitation must be atp1-invite: followed by letters")

        machine.deps.open_window = refuse
        self.assertEqual(machine.run("--join", "junk"), 1)
        self.assertIn("atp1-invite", machine.errors)

    def test_an_elevated_command_stops_before_anything_else(self) -> None:
        machine = self.machine()
        machine.elevation_error = elevation.ElevationError("Stop: this command runs with administrator rights")
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("administrator", machine.errors)
        self.assertEqual(machine.events, ["elevation"])
        self.assertEqual(machine.screen.prompts, [])
        machine = self.machine()
        machine.deps.interactive = lambda: False
        machine.elevation_error = elevation.ElevationError("Stop")
        self.assertEqual(machine.run(), 1)
        self.assertEqual(machine.windows, [])

    def test_usage_errors(self) -> None:
        machine = self.machine()
        for args in (["--peer", "192.168.50.20"], ["--token", "ABC234"], ["--invite", "--join", invitation_line()], ["--bogus"]):
            self.assertEqual(machine.run(*args), 2, args)
        self.assertEqual(machine.run("--peer", "192.168.50.20", "--token", "short"), 1)
        self.assertEqual(machine.windows, [])


class WindowPeerTest(unittest.TestCase):
    ENV = Environment(
        platform="win32", interpreter=r"C:\py\python.exe", launch_script=r"C:\plugin\launch\agent_tabs.py", user_home=r"C:\Users\Me"
    )

    def open(self, subcommand: list[str], invitation: str | None = None) -> list[Any]:
        calls: list[Any] = []
        window.open_command_window(
            subcommand,
            invitation,
            "Claude Code",
            self.ENV,
            popen=lambda command, **options: calls.append(command),
            notifier=lambda _t: None,
            environ={},
        )
        return calls

    def test_a_validated_peer_and_token_open_a_window(self) -> None:
        calls = self.open(["net", "pair", "--peer", "192.168.50.20", "--token", "ABC234"])
        self.assertEqual(calls[0][5:], [r"C:\plugin\launch\agent_tabs.py", "net", "pair", "--peer", "192.168.50.20", "--token", "ABC234"])

    def test_bad_addresses_and_tokens_are_refused_before_any_window_opens(self) -> None:
        for address, token in (
            ("host.local", "ABC234"),
            ("192.168.050.20", "ABC234"),
            ("192.168.50.256", "ABC234"),
            ("::1", "ABC234"),
            ("192.168.50.20 ", "ABC234"),
            ("192.168.50.20", "abc234"),
            ("192.168.50.20", "ABC23"),
            ("192.168.50.20", "ABC231"),
            ("192.168.50.20", "ABC234 --x"),
        ):
            with self.assertRaises(WindowError, msg=(address, token)):
                self.open(["net", "pair", "--peer", address, "--token", token])
        for subcommand in (
            ["net", "pair", "--peer", "192.168.50.20"],
            ["net", "pair", "--peer", "192.168.50.20", "--token"],
            ["net", "pair", "--peer", "192.168.50.20", "--other", "ABC234"],
            ["net", "pair", "--peer", "192.168.50.20", "--token", "ABC234", "--invite"],
        ):
            with self.assertRaises(WindowError, msg=subcommand):
                self.open(subcommand)
        with self.assertRaises(WindowError):
            self.open(["net", "pair", "--peer", "192.168.50.20", "--token", "ABC234"], "atp1-invite:abc")


class PrepareTest(PairTestCase):
    def test_a_public_network_stops_the_command_before_anything_listens(self) -> None:
        machine = self.machine()
        machine.lan_error = InterfaceError("Ethernet is a Public network. Open Settings > Network & internet > Ethernet > Private network.")
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("Public network", machine.errors)
        self.assertIn("Private network", machine.errors)
        self.assertEqual(machine.events, ["elevation", "lan-ready"])
        self.assertFalse(machine.window_is_open())
        self.assertFalse(Path(self.a, "net", pairing.PAIRING_FILE).exists())
        self.assertFalse(pair_cli.invitation_path(self.a).exists())

    def test_the_join_side_checks_the_network_too(self) -> None:
        machine = self.machine()
        machine.lan_error = InterfaceError("Ethernet is a Public network.")
        self.assertEqual(machine.run("--join", invitation_line()), 1)
        self.assertEqual(machine.events, ["elevation", "lan-ready"])
        self.assertEqual(machine.failures(), 0)

    def test_a_single_physical_address_is_shown_and_used(self) -> None:
        machine = self.machine()
        machine.listen_impl = Session().listen
        machine.deps.local_addresses = lambda: [BIND, "10.0.0.5", "8.8.8.8", "127.0.0.1", "169.254.1.1"]
        machine.harness.services.interface_for = lambda address: InterfaceInfo(
            "Ethernet" if address == BIND else "vEthernet (WSL)", address, 24, "192.168.50.0/24", "Private", address == BIND
        )
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn(f"Using {BIND} on Ethernet.", machine.out)
        self.assertEqual(machine.lan_calls, [(BIND, False)])
        self.assertEqual(machine.screen.prompts, [])

    def test_several_addresses_are_listed_and_picked_by_number(self) -> None:
        machine = self.machine(rules=[("Type the number", ["x", "2"])])
        machine.listen_impl = Session().listen
        machine.deps.local_addresses = lambda: [BIND, "10.0.0.5"]
        machine.run("--invite")
        self.assertIn("  1. 192.168.50.10", machine.out)
        self.assertIn("  2. 10.0.0.5", machine.out)
        self.assertEqual(machine.lan_calls, [("10.0.0.5", False)])

    def test_no_choice_stops_the_command(self) -> None:
        machine = self.machine(rules=[("Type the number", None)])
        machine.deps.local_addresses = lambda: [BIND, "10.0.0.5"]
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("no address was chosen", machine.errors)
        self.assertEqual(machine.lan_calls, [])

    def test_no_physical_private_address_names_the_fix(self) -> None:
        machine = self.machine()
        machine.deps.local_addresses = lambda: ["8.8.8.8", "127.0.0.1"]
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn('"bind"', machine.errors)
        self.assertEqual(machine.lan_calls, [])

    def test_a_configured_bind_is_used_and_may_be_a_named_adapter(self) -> None:
        self.write_config(bind="10.0.0.5")
        machine = self.machine()
        machine.listen_impl = Session().listen
        machine.deps.local_addresses = lambda: self.fail("the configured bind needs no listing")
        machine.run("--invite")
        self.assertEqual(machine.lan_calls, [("10.0.0.5", True)])
        self.assertEqual(machine.rules_opened, [("10.0.0.5", "10.0.0.0/24", "Ethernet")])

    def test_the_device_name_comes_from_the_config_or_the_host(self) -> None:
        self.write_config(deviceName="Desk 1")
        machine = self.machine()
        session = Session()
        machine.listen_impl = session.listen
        machine.run("--invite")
        self.assertEqual(session.calls[0]["name"], "Desk 1")
        self.assertNotIn("deviceName", machine.errors)
        self.write_config(deviceName="x" * 40)
        machine = self.machine()
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("deviceName", machine.errors)
        self.assertEqual(machine.events, ["elevation"])
        Path(self.a, "config.json").unlink()
        machine = self.machine()
        session = Session()
        machine.listen_impl = session.listen
        machine.run("--invite")
        self.assertTrue(pairing.valid_device_name(session.calls[0]["name"]))

    def test_a_missing_identity_is_created_with_the_found_openssl(self) -> None:
        home = make_root(self)
        machine = Machine(self, home)
        session = Session()
        machine.listen_impl = session.listen
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn(f"identity with openssl at {OPENSSL}", machine.out)
        self.assertIsNotNone(device_of(home))
        self.assertEqual(json.loads(Path(home, "config.json").read_text())["network"]["openssl"], str(OPENSSL))
        self.assertEqual(len(session.calls), 1)

    def test_a_missing_openssl_stops_before_anything_listens(self) -> None:
        home = make_root(self)
        machine = Machine(self, home)
        machine.openssl = None
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("No openssl", machine.errors)
        self.assertIn("lan setup --openssl", machine.errors)
        self.assertEqual(machine.rules_opened, [])
        self.assertFalse(os.path.exists(os.path.join(home, "net", "identity")))

    def test_the_shim_is_installed_and_a_failure_only_warns(self) -> None:
        machine = self.machine()
        machine.listen_impl = Session().listen
        machine.run("--invite")
        self.assertEqual(machine.installed, [(self.a, r"C:\py\python.exe", r"C:\plugin\launch\agent_tabs.py")])

        def broken(_home: str, _interpreter: str, _entry: str) -> Path:
            raise pair_cli.shim.ShimError("the folder holds other files")

        machine = self.machine()
        machine.listen_impl = Session().listen
        machine.deps.install_shim = broken
        machine.run("--invite")
        self.assertIn("holds other files", machine.errors)
        self.assertEqual(len(machine.rules_opened), 1)


class ListenerTest(PairTestCase):
    def test_the_window_opens_the_rule_the_socket_and_the_invitation_then_closes_everything(self) -> None:
        seen: dict[str, Any] = {}
        machine = self.machine()

        def during() -> None:
            seen["open"] = machine.window_is_open()
            seen["file"] = pair_cli.read_invitation(self.a, 0, time.time())
            seen["mode"] = oct(os.stat(pair_cli.invitation_path(self.a)).st_mode & 0o777) if os.name != "nt" else None

        session = Session(during=during)
        machine.listen_impl = session.listen
        self.assertEqual(machine.run("--invite"), 1)
        order = [event for event in machine.events if event in ("open-rule", "bind-socket", "listen", "close-rule")]
        self.assertEqual(order, ["open-rule", "bind-socket", "listen", "close-rule"])
        self.assertEqual(machine.rules_opened, [(BIND, SUBNET, "Ethernet")])
        self.assertEqual(machine.bound, [(BIND, 47830)])
        self.assertTrue(seen["open"])
        line = seen["file"]
        self.assertIsNotNone(line)
        self.assertIn(line, machine.out)
        invitation = pairing.parse_invitation(line, time.time())
        self.assertEqual((invitation.name, invitation.address, invitation.port), (session.calls[0]["name"], BIND, session.calls[0]["port"]))
        self.assertEqual(invitation.id, device_of(self.a).split("-")[0])
        self.assertEqual(invitation.token, session.calls[0]["token"])
        self.assertLessEqual(invitation.expires - time.time(), 300)
        self.assertGreater(invitation.expires - time.time(), 290)
        self.assertIn(f"On the other machine run: agent-tabs net pair --join {line}", machine.out)
        self.assertFalse(machine.window_is_open())
        self.assertFalse(pair_cli.invitation_path(self.a).exists())
        with self.assertRaises(OSError):
            machine.sockets[0].getsockname()

    def test_the_invitation_token_is_fresh_each_time(self) -> None:
        tokens: list[str] = []
        for _ in range(2):
            machine = self.machine()
            session = Session()
            machine.listen_impl = session.listen
            machine.run("--invite")
            tokens.append(session.calls[0]["token"])
            pairing.Limits(self.a).close_window()
        self.assertNotEqual(tokens[0], tokens[1])
        self.assertEqual(len(tokens[0]), 22)

    def test_a_bare_pair_prints_the_peer_command_with_a_six_character_token(self) -> None:
        machine = self.machine()
        session = Session()
        machine.listen_impl = session.listen
        machine.run()
        token = session.calls[0]["token"]
        self.assertRegex(token, r"^[A-Z2-7]{6}$")
        self.assertIn(f"On the other machine run: agent-tabs net pair --peer {BIND} --token {token}", machine.out)
        self.assertFalse(pair_cli.invitation_path(self.a).exists())
        self.assertNotIn("atp1-invite:", machine.out)

    def test_an_error_while_listening_still_closes_the_rule_the_window_and_the_file(self) -> None:
        machine = self.machine()
        machine.listen_impl = Session(error=RuntimeError("boom")).listen
        with self.assertRaises(RuntimeError):
            machine.run("--invite")
        self.assertEqual(machine.events[-1], "close-rule")
        self.assertFalse(machine.window_is_open())
        self.assertFalse(pair_cli.invitation_path(self.a).exists())
        with self.assertRaises(OSError):
            machine.sockets[0].getsockname()

    def test_ctrl_c_closes_everything_and_stops(self) -> None:
        machine = self.machine()
        machine.listen_impl = Session(error=KeyboardInterrupt()).listen
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("Pairing stopped", machine.errors)
        self.assertEqual(machine.events[-1], "close-rule")
        self.assertFalse(machine.window_is_open())

    def test_a_failed_socket_closes_the_rule_and_the_window(self) -> None:
        machine = self.machine()
        machine.bind_error = OSError("address in use")
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("address in use", machine.errors)
        self.assertEqual(machine.events[-1], "close-rule")
        self.assertFalse(machine.window_is_open())

    def test_a_declined_firewall_prompt_opens_nothing_and_does_not_prompt_again(self) -> None:
        machine = self.machine()
        machine.rule_error = FirewallError("the Windows prompt for administrator rights was declined")
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("declined", machine.errors)
        self.assertNotIn("close-rule", machine.events)
        self.assertNotIn("bind-socket", machine.events)
        self.assertFalse(machine.window_is_open())
        self.assertFalse(pair_cli.invitation_path(self.a).exists())

    def test_a_rule_that_cannot_be_removed_is_reported_and_the_window_still_closes(self) -> None:
        machine = self.machine()
        machine.listen_impl = Session().listen
        machine.close_error = FirewallError("Windows could not remove the rule")
        machine.run("--invite")
        self.assertIn("could not remove the rule", machine.errors)
        self.assertIn("Agent Tabs pairing", machine.errors)
        self.assertFalse(machine.window_is_open())

    def test_a_second_window_is_busy_and_touches_nothing(self) -> None:
        first = pairing.Limits(self.a)
        first.open_window()
        self.addCleanup(first.close_window)
        stale: list[str] = []
        machine = self.machine()
        machine.harness.services.remove_stale_pairing_rule = lambda: stale.append("x") or ""
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("pairing busy", machine.errors)
        self.assertEqual(stale, [])
        self.assertNotIn("open-rule", machine.events)

    def test_a_stale_rule_is_removed_before_the_window_opens(self) -> None:
        machine = self.machine()
        machine.listen_impl = Session().listen
        machine.harness.stale_rule = 'Removed the Windows firewall rule "Agent Tabs pairing".'
        machine.run("--invite")
        self.assertEqual(machine.harness.stale_checks, 1)
        self.assertIn("Removed the Windows firewall rule", machine.out)

    def test_a_locked_machine_opens_nothing(self) -> None:
        limits = pairing.Limits(self.a)
        for _ in range(3):
            limits.record_failure()
        machine = self.machine()
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("locked", machine.errors)
        self.assertIn("net trust", machine.errors)
        self.assertEqual(machine.rules_opened, [])

    def test_three_windows_an_hour_is_the_limit(self) -> None:
        for _ in range(3):
            machine = self.machine()
            machine.listen_impl = Session().listen
            machine.run("--invite")
        machine = self.machine()
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("pairing busy", machine.errors)
        self.assertEqual(len(machine.rules_opened), 0)


class JoinTest(PairTestCase):
    def test_declining_counts_as_a_failure_and_sends_nothing(self) -> None:
        for answer in ("n", "", "no", "maybe"):
            pairing.Limits(self.a).check_locked()
            machine = self.machine(rules=[("Pair with", answer)])
            session = Session()
            machine.dial_impl = session.dial
            before = machine.failures()
            self.assertEqual(machine.run("--join", invitation_line()), 1, answer)
            self.assertEqual(machine.failures(), before + 1, answer)
            self.assertEqual(session.calls, [])
            self.assertNotIn("dial", machine.events)
            self.assertIn("Cancelled", machine.errors)
            os.remove(os.path.join(self.a, "net", pairing.PAIRING_FILE))

    def test_the_third_decline_locks_pairing_and_notifies(self) -> None:
        machine = self.machine(rules=[("Pair with", "n")])
        for _ in range(3):
            machine = self.machine(rules=[("Pair with", "n")])
            machine.run("--join", invitation_line())
        self.assertTrue(any("locked" in notice and "net trust" in notice for notice in machine.notices), machine.notices)
        self.assertIn("locked", machine.errors)
        fourth = self.machine(rules=[("Pair with", "y")])
        self.assertEqual(fourth.run("--join", invitation_line()), 1)
        self.assertIn("locked", fourth.errors)
        self.assertNotIn("dial", fourth.events)

    def test_no_answer_is_not_a_failure(self) -> None:
        machine = self.machine(rules=[("Pair with", None)])
        self.assertEqual(machine.run("--join", invitation_line()), 1)
        self.assertEqual(machine.failures(), 0)
        self.assertIn("nothing was sent", machine.errors)

    def test_the_prompt_names_the_other_machine_and_this_one(self) -> None:
        machine = self.machine(rules=[("Pair with", "n")])
        machine.run("--join", invitation_line(address="192.168.50.20", name="bravo"))
        self.assertEqual(machine.screen.prompts, ["Pair with bravo at 192.168.50.20? Type y to continue: "])
        self.assertIn(f"This machine: {machine.screen.text.split('This machine: ')[1].split(chr(10))[0]}", machine.out)
        self.assertRegex(machine.out, rf"This machine: \S.* {re.escape(BIND)}")

    def test_an_address_that_fails_the_check_is_refused_before_any_prompt_or_dial(self) -> None:
        for address in ("8.8.8.8", "10.1.2.3", "192.168.50.10", "192.168.50.0", "192.168.50.255", "192.168.51.20"):
            machine = self.machine()
            session = Session()
            machine.dial_impl = session.dial
            self.assertEqual(machine.run("--join", invitation_line(address=address)), 1, address)
            self.assertEqual(machine.screen.prompts, [], address)
            self.assertEqual(session.calls, [], address)
            self.assertEqual(machine.failures(), 0)
            self.assertTrue(machine.errors, address)

    def test_a_bad_or_expired_invitation_is_refused(self) -> None:
        for text in ("junk", invitation_line(ahead=-5), invitation_line(ahead=100000)):
            machine = self.machine()
            self.assertEqual(machine.run("--join", text), 1, text)
            self.assertEqual(machine.screen.prompts, [])
            self.assertNotIn("dial", machine.events)

    def test_peer_and_token_dial_the_pairing_port_with_the_token(self) -> None:
        machine = self.machine(rules=[("Pair with", "y")])
        session = Session()
        machine.dial_impl = session.dial
        machine.run("--peer", "192.168.50.20", "--token", "abc234")
        self.assertEqual(machine.screen.prompts, ["Pair with the machine at 192.168.50.20? Type y to continue: "])
        self.assertEqual(session.calls, [{"address": "192.168.50.20", "port": 47830, "token": "ABC234", "name": session.calls[0]["name"]}])

    def test_peer_is_checked_like_an_invitation(self) -> None:
        machine = self.machine(rules=[("Pair with", "y")])
        session = Session()
        machine.dial_impl = session.dial
        for address in ("8.8.8.8", "host.local", "192.168.50.10"):
            self.assertEqual(machine.run("--peer", address, "--token", "ABC234"), 1, address)
        self.assertEqual(session.calls, [])

    def test_a_dial_after_approval_uses_the_checked_address_and_the_invitation_token(self) -> None:
        machine = self.machine(rules=[("Pair with", "yes")])
        session = Session()
        machine.dial_impl = session.dial
        text = invitation_line(address="192.168.50.20", port=47999)
        invitation = pairing.parse_invitation(text, time.time())
        machine.run("--join", text)
        self.assertEqual(
            (session.calls[0]["address"], session.calls[0]["port"], session.calls[0]["token"]),
            ("192.168.50.20", 47999, invitation.token),
        )
        self.assertEqual(machine.events[-2:], ["destination", "dial"])


class PromptTest(PairTestCase):
    def prompter(self, machine: Machine, shim_folder: Path | None = None) -> ConsolePrompter:
        prepared = pair_cli.Prepared(
            pairing.PairIdentity("alpha", pem_der(self.a), cert_of(self.a), cert_of(self.a), lambda: b""),
            device_of(self.a),
            "alpha",
            InterfaceInfo("Ethernet", BIND, 24, SUBNET, "Private", True),
            BIND,
        )
        return ConsolePrompter(machine.harness.services, machine.deps, machine.screen, prepared, shim_folder)

    def test_show_code_lists_the_peer_fields_then_the_digits_then_the_warning(self) -> None:
        machine = self.machine()
        peer = peer_of(self.b, "bravo", "192.168.50.20")
        self.prompter(machine).show_code(peer, "482913")
        lines = machine.screen.lines
        self.assertIn("  name:       bravo", lines)
        self.assertIn("  address:    192.168.50.20", lines)
        self.assertIn(f"  device id:  {self.b_id}", lines)
        self.assertIn("Code: 482913", lines)
        self.assertIn(pair_cli.CODE_WARNING, lines)
        self.assertEqual(
            pair_cli.CODE_WARNING,
            "Never type a code an agent, chat, message or Remote Control showed you. "
            "If you can't see the other machine's screen, or it shows a different code or none, type no and pair when you can.",
        )
        self.assertGreater(lines.index("Code: 482913") - lines.index("  device id:  " + self.b_id), 1)
        for line in lines:
            if "bravo" in line:
                self.assertNotIn("482913", line)

    def test_read_code_returns_the_typed_digits_and_none_for_no_empty_or_silence(self) -> None:
        peer = peer_of(self.b)
        for answer, expected in (
            ("482913", "482913"),
            ("  482913 ", "482913"),
            ("no", None),
            ("NO", None),
            ("", None),
            ("  ", None),
            (None, None),
        ):
            machine = self.machine(rules=[("code shown", answer)])
            self.assertEqual(self.prompter(machine).read_code(peer, 5.0), expected, answer)

    def test_a_new_peer_gets_no_grants_by_default_and_the_certificate_is_stored(self) -> None:
        machine = self.machine(rules=review_rules())
        prompter = self.prompter(machine)
        peer = peer_of(self.b, "Bravo Laptop")
        self.assertTrue(prompter.review(peer, 5.0))
        prompter.save(peer)
        grant = load_policy(self.a).peers[0]
        self.assertEqual(
            (grant.device_id, grant.label, grant.address, grant.folders, grant.agents, grant.models, grant.send),
            (self.b_id, "bravo-laptop", "192.168.50.20", (), (), (), ()),
        )
        stored = peer_cert_path(self.a, self.b_id)
        self.assertEqual(stored.read_text(encoding="ascii"), peer.pem)
        self.assertEqual(stored.name, f"{self.b_id}.pem")
        self.assertNotEqual(stored.name, "cert.pem")
        self.assertEqual(machine.notices, ["paired with bravo-laptop at 192.168.50.20"])
        self.assertIn("pair", Path(log_file(self.a, "cli.log")).read_text())
        self.assertIn("folder grant:     none", machine.out)
        self.assertEqual(machine.screen.unmatched, [])

    def test_no_at_the_summary_writes_nothing(self) -> None:
        machine = self.machine(rules=[("Type yes", "no"), *review_rules()])
        prompter = self.prompter(machine)
        self.assertFalse(prompter.review(peer_of(self.b), 5.0))
        self.assertIsNone(prompter.choices)
        self.assertEqual(load_policy(self.a).peers, ())
        self.assertFalse(peer_cert_path(self.a, self.b_id).exists())
        with self.assertRaises(cli.CliError):
            prompter.save(peer_of(self.b))
        self.assertEqual(load_policy(self.a).peers, ())

    def test_anything_but_yes_cancels(self) -> None:
        for answer in ("y", "Yes", "", None):
            machine = self.machine(rules=[("Type yes", answer), *review_rules()])
            self.assertFalse(self.prompter(machine).review(peer_of(self.b), 5.0), answer)
            self.assertEqual(load_policy(self.a).peers, ())

    def test_silence_in_the_middle_of_the_review_cancels(self) -> None:
        machine = self.machine(rules=[("Turn the listener on", None), *review_rules()])
        self.assertFalse(self.prompter(machine).review(peer_of(self.b), 5.0))
        machine = self.machine(rules=review_rules())
        self.assertFalse(self.prompter(machine).review(peer_of(self.b), 0.0))

    def test_the_label_is_editable_and_checked(self) -> None:
        save_policy(self.a, Policy((PeerGrant(self.c_id, "taken", "192.168.50.30"),)))
        machine = self.machine(rules=[("Label for this peer", ["Bad Label", "taken", "my-laptop"]), *review_rules()])
        prompter = self.prompter(machine)
        self.assertTrue(prompter.review(peer_of(self.b), 5.0))
        assert prompter.choices is not None
        self.assertEqual(prompter.choices.label, "my-laptop")
        self.assertIn("1 to 32 characters", machine.out)
        self.assertIn("belongs to another peer", machine.out)

    def test_the_default_label_is_lowercased_and_made_unique(self) -> None:
        save_policy(self.a, Policy((PeerGrant(self.c_id, "work-pc", "192.168.50.30"),)))
        machine = self.machine(rules=review_rules())
        prompter = self.prompter(machine)
        prompter.review(peer_of(self.b, "Work PC.1"), 5.0)
        assert prompter.choices is not None
        self.assertEqual(prompter.choices.label, "work-pc-1")
        self.assertIn("[work-pc-1]", "".join(machine.screen.prompts))

    def test_an_existing_peer_keeps_its_grants_and_only_the_address_changes(self) -> None:
        folder = self.folder()
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
        machine = self.machine(rules=[("Label for this peer", self.fail), ("Folder", self.fail), *review_rules()])
        prompter = self.prompter(machine)
        peer = peer_of(self.b, "bravo", "192.168.50.20")
        self.assertTrue(prompter.review(peer, 5.0))
        self.assertIn("already trusted", machine.out)
        self.assertIn("  label:    old-name", machine.out)
        self.assertIn("  address:  192.168.50.99", machine.out)
        self.assertIn(f"work={folder}", machine.out)
        self.assertIn("keeps these grants and updates the address to 192.168.50.20", machine.out)
        self.assertNotIn("Label for this peer", "".join(machine.screen.prompts))
        prompter.save(peer)
        kept = load_policy(self.a).peers[0]
        self.assertEqual((kept.label, kept.address, kept.agents, kept.max_tabs), ("old-name", "192.168.50.20", ("claude",), 3))
        self.assertEqual([f.path for f in kept.folders], [folder])
        self.assertEqual(len(load_policy(self.a).peers), 1)

    def test_declining_for_an_existing_peer_leaves_the_entry_exactly_as_it_was(self) -> None:
        save_policy(self.a, Policy((PeerGrant(self.b_id, "old-name", "192.168.50.99", agents=("codex",)),)))
        before = Path(self.a, "net", "policy.json").read_bytes()
        machine = self.machine(rules=[("Type yes", "no"), *review_rules()])
        self.assertFalse(self.prompter(machine).review(peer_of(self.b), 5.0))
        self.assertEqual(Path(self.a, "net", "policy.json").read_bytes(), before)
        self.assertFalse(peer_cert_path(self.a, self.b_id).exists())

    def trust_claude(self, machine: Machine, folder: str) -> None:
        (Path(machine.harness.user_home) / ".claude.json").write_text(
            json.dumps({"projects": {folder.replace("\\", "/"): {"hasTrustDialogAccepted": True}}}), encoding="utf-8"
        )

    def test_a_trusted_folder_is_granted_with_the_chosen_agents(self) -> None:
        folder = self.folder("proj")
        machine = self.machine(rules=review_rules(("Folder", folder), ("Agents that may", "1")))
        self.trust_claude(machine, folder)
        prompter = self.prompter(machine)
        peer = peer_of(self.b)
        self.assertTrue(prompter.review(peer, 5.0))
        prompter.save(peer)
        grant = load_policy(self.a).peers[0]
        self.assertEqual(grant.agents, ("claude",))
        self.assertEqual(
            (grant.folders[0].alias, grant.folders[0].path),
            (re.sub(r"[^a-z0-9-]+", "-", os.path.basename(folder).lower()).strip("-")[:32], folder),
        )
        self.assertNotEqual(grant.folders[0].ino, 0)
        self.assertIn(f"{folder}", machine.out)
        self.assertIn("for claude", machine.out)

    def test_both_agents_need_the_folder_trusted_in_each(self) -> None:
        folder = self.folder("proj")
        machine = self.machine(rules=[("Press Enter", "no"), *review_rules(("Folder", folder), ("Agents that may", "3"))])
        self.trust_claude(machine, folder)
        prompter = self.prompter(machine)
        self.assertTrue(prompter.review(peer_of(self.b), 5.0))
        assert prompter.choices is not None
        self.assertEqual((prompter.choices.folders, prompter.choices.agents), ((), ()))
        self.assertIn("codex has not trusted this folder yet.", machine.out)

    def test_an_untrusted_folder_shows_what_trust_activates_and_waits_for_the_agent(self) -> None:
        folder = self.folder("proj")
        os.makedirs(os.path.join(folder, ".claude"))
        for name in (".claude/settings.json", ".claude/settings.local.json", ".mcp.json", "CLAUDE.md"):
            Path(folder, name).write_text("{}", encoding="utf-8")
        machine = self.machine(rules=review_rules(("Folder", folder), ("Agents that may", "1"), ("Open claude", "y"), ("Press Enter", "")))

        def trust_after_the_dialog(agent: str, where: str) -> str | None:
            self.trust_claude(machine, where)
            return None

        machine.open_agent_result = trust_after_the_dialog
        prompter = self.prompter(machine)
        self.assertTrue(prompter.review(peer_of(self.b), 5.0))
        self.assertEqual(machine.agents, [("claude", folder)])
        self.assertIn(f"  folder: {folder}", machine.out)
        self.assertIn("claude has not trusted this folder yet.", machine.out)
        self.assertIn(".claude/settings.json, .claude/settings.local.json, .mcp.json, CLAUDE.md", machine.out)
        self.assertIn(pair_cli.TRUST_WARNING, machine.out)
        self.assertEqual(
            pair_cli.TRUST_WARNING,
            "Sessions the peer opens here run these without asking you. Removing the grant doesn't remove the agent's trust.",
        )
        assert prompter.choices is not None
        self.assertEqual(prompter.choices.folders[0].path, folder)
        self.assertFalse(os.path.exists(os.path.join(machine.harness.user_home, ".codex")))

    def test_a_folder_that_is_still_untrusted_after_enter_asks_again_and_can_be_skipped(self) -> None:
        folder = self.folder("proj")
        machine = self.machine(
            rules=review_rules(("Folder", folder), ("Agents that may", "2"), ("Open codex", "n"), ("Press Enter", ["", "no"]))
        )
        prompter = self.prompter(machine)
        self.assertTrue(prompter.review(peer_of(self.b), 5.0))
        assert prompter.choices is not None
        self.assertEqual(prompter.choices.folders, ())
        self.assertEqual(machine.agents, [])
        self.assertIn("project files that trust activates here: none found", machine.out)
        self.assertEqual(machine.screen.prompts.count("Press Enter after codex trusts the folder, or type no to skip this grant: "), 2)

    def test_agent_window_problems_are_shown(self) -> None:
        folder = self.folder("proj")
        machine = self.machine(
            rules=review_rules(("Folder", folder), ("Agents that may", "1"), ("Open claude", "y"), ("Press Enter", "no"))
        )
        machine.open_agent_result = lambda _agent, _folder: "claude is not on the PATH here, so open it yourself."
        self.prompter(machine).review(peer_of(self.b), 5.0)
        self.assertIn("claude is not on the PATH here", machine.out)

    def test_broad_or_overlapping_folders_are_refused(self) -> None:
        home = os.path.realpath(self.folder("home"))
        parent = self.folder("parent")
        child = os.path.join(parent, "child")
        os.makedirs(child)
        stat = os.stat(child)
        save_policy(
            self.a, Policy((PeerGrant(self.c_id, "other", "192.168.50.30", (FolderGrant("child", child, stat.st_dev, stat.st_ino),)),))
        )
        root = os.path.abspath(os.sep)
        machine = self.machine(rules=review_rules(("Folder", [home, root, parent, "relative/path", os.path.join(parent, "missing"), ""])))
        machine.harness.services.user_home = home
        prompter = self.prompter(machine)
        self.assertTrue(prompter.review(peer_of(self.b), 5.0))
        assert prompter.choices is not None
        self.assertEqual(prompter.choices.folders, ())
        self.assertIn("the home folder is too broad", machine.out)
        self.assertIn("a drive root is too broad", machine.out)
        self.assertIn("holds folders already granted to other", machine.out)
        self.assertEqual(machine.out.count("error:"), 5)

    def test_the_listener_login_and_path_choices_are_asked_in_order(self) -> None:
        machine = self.machine(
            rules=[("Turn the listener on", "y"), ("Start the listener each time", "y"), ("Append", "y"), *review_rules()]
        )
        machine.on_path = False
        prompter = self.prompter(machine, Path(self.a) / "bin")
        self.assertTrue(prompter.review(peer_of(self.b), 5.0))
        assert prompter.choices is not None
        self.assertEqual((prompter.choices.listener, prompter.choices.login, prompter.choices.path), (True, True, True))
        self.assertIn("listener:         on, with its firewall rule", machine.out)
        self.assertIn("start at login:   yes", machine.out)
        self.assertIn(f"append {Path(self.a) / 'bin'}", machine.out)

    def test_login_is_not_offered_without_the_listener_and_path_not_when_present(self) -> None:
        machine = self.machine(rules=[("Start the listener each time", self.fail), ("Append", self.fail), *review_rules()])
        machine.on_path = True
        prompter = self.prompter(machine, Path(self.a) / "bin")
        self.assertTrue(prompter.review(peer_of(self.b), 5.0))
        assert prompter.choices is not None
        self.assertEqual((prompter.choices.listener, prompter.choices.login, prompter.choices.path), (False, False, False))
        self.assertNotIn("Start the listener each time", "".join(machine.screen.prompts))

    def test_defaults_are_listener_on_login_off_path_off(self) -> None:
        machine = self.machine(rules=[("Turn the listener on", ""), ("Start the listener each time", ""), ("Append", ""), *review_rules()])
        machine.on_path = False
        prompter = self.prompter(machine, Path(self.a) / "bin")
        prompter.review(peer_of(self.b), 5.0)
        assert prompter.choices is not None
        self.assertEqual((prompter.choices.listener, prompter.choices.login, prompter.choices.path), (True, False, False))

    def test_a_save_failure_surfaces_to_the_session(self) -> None:
        save_policy(self.a, Policy((PeerGrant(self.c_id, "bravo", "192.168.50.30"),)))
        machine = self.machine(rules=review_rules(("Label for this peer", "bravo")))
        prompter = self.prompter(machine)
        peer = peer_of(self.b)
        prompter.choices = pair_cli.Choices("bravo")
        with self.assertRaises(cli.CliError):
            prompter.save(peer)
        self.assertFalse(peer_cert_path(self.a, self.b_id).exists())


class FinishTest(PairTestCase):
    def listener_session(self, machine: Machine, result: PairResult, rules: bool = True) -> Session:
        peer = peer_of(self.b, "bravo", "192.168.50.20")
        session = Session(result, peer)
        machine.listen_impl = session.listen
        return session

    def paired(self) -> PairResult:
        return PairResult("paired", "paired", peer_of(self.b, "bravo", "192.168.50.20"))

    def test_a_paired_listener_with_the_listener_chosen_turns_it_on_and_records_the_choice(self) -> None:
        machine = self.machine(rules=review_rules(("Turn the listener on", "y"), ("Start the listener each time", "y")))
        self.listener_session(machine, self.paired())
        self.assertEqual(machine.run("--invite"), 0, machine.errors)
        self.assertEqual(self.config()["network"]["network"], "lan")
        self.assertEqual(self.config()["network"]["bind"], BIND)
        self.assertEqual(machine.harness.firewall, [(BIND, SUBNET, "Ethernet")])
        self.assertEqual(machine.harness.logins, [self.a])
        self.assertEqual(len(machine.harness.spawned), 1)
        self.assertIn("Saved bravo", machine.out)
        self.assertEqual(load_policy(self.a).peers[0].label, "bravo")
        self.assertTrue(Path(self.a, "net", "ingress.token").is_file())

    def test_without_the_listener_choice_only_the_bind_is_recorded(self) -> None:
        machine = self.machine(rules=review_rules())
        self.listener_session(machine, self.paired())
        self.assertEqual(machine.run("--invite"), 0, machine.errors)
        self.assertEqual(self.config()["network"], {"bind": BIND})
        self.assertEqual(machine.harness.firewall, [])
        self.assertEqual(machine.harness.spawned, [])
        self.assertEqual(machine.harness.logins, [])

    def test_an_incomplete_pairing_still_applies_the_choices_and_says_what_to_do(self) -> None:
        machine = self.machine(rules=review_rules(("Turn the listener on", "y")))
        self.listener_session(machine, PairResult("incomplete", pairing.INCOMPLETE_TEXT, peer_of(self.b, "bravo", "192.168.50.20")))
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("net revoke bravo", machine.errors)
        self.assertNotIn("<label>", machine.errors)
        self.assertEqual(self.config()["network"]["network"], "lan")
        self.assertEqual(len(machine.harness.spawned), 1)

    def test_a_failed_or_declined_pairing_applies_nothing(self) -> None:
        for result in (
            PairResult("failed", "the code you typed does not match", peer_of(self.b)),
            PairResult("declined", "you answered no", peer_of(self.b)),
            PairResult("failed", "the pairing window closed before another machine paired"),
        ):
            machine = self.machine(rules=review_rules(("Turn the listener on", "y")))
            session = Session(result, None)
            machine.listen_impl = session.listen
            pairing.Limits(self.a).close_window()
            self.assertEqual(machine.run("--invite"), 1, result)
            self.assertIn(result.reason, machine.errors)
            self.assertFalse(os.path.exists(os.path.join(self.a, "config.json")))
            self.assertEqual(load_policy(self.a).peers, ())
            self.assertEqual(machine.harness.spawned, [])
            for name in (pairing.PAIRING_FILE,):
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(self.a, "net", name))

    def test_a_lockout_notifies_that_net_trust_still_works(self) -> None:
        machine = self.machine()
        machine.listen_impl = Session(PairResult("declined", "you answered no", peer_of(self.b), locked=True)).listen
        self.assertEqual(machine.run("--invite"), 1)
        self.assertEqual(len(machine.notices), 1)
        self.assertIn("locked for an hour", machine.notices[0])
        self.assertIn("net trust", machine.notices[0])
        self.assertIn("locked", machine.errors)

    def test_a_declined_attempt_notifies_without_claiming_a_lock(self) -> None:
        machine = self.machine()
        machine.listen_impl = Session(PairResult("declined", "you answered no", peer_of(self.b))).listen
        machine.run("--invite")
        self.assertEqual(len(machine.notices), 1)
        self.assertNotIn("locked", machine.notices[0])
        self.assertIn("net trust", machine.notices[0])

    def test_the_dialer_path_saves_and_applies_the_same_way(self) -> None:
        machine = self.machine(rules=review_rules(("Pair with", "y"), ("Turn the listener on", "y")))
        peer = peer_of(self.b, "bravo", "192.168.50.20")
        machine.dial_impl = Session(PairResult("paired", "paired", peer), peer).dial
        self.assertEqual(machine.run("--join", invitation_line()), 0, machine.errors)
        self.assertEqual(self.config()["network"]["network"], "lan")
        self.assertEqual(load_policy(self.a).peers[0].device_id, self.b_id)
        self.assertEqual(machine.harness.firewall, [(BIND, SUBNET, "Ethernet")])

    def test_a_listener_that_fails_to_start_is_reported_but_the_pairing_stays(self) -> None:
        machine = self.machine(rules=review_rules(("Turn the listener on", "y")))
        self.listener_session(machine, self.paired())
        machine.harness.services.spawn = lambda _home, _port: None
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("could not be started", machine.errors)
        self.assertEqual(len(load_policy(self.a).peers), 1)

    def test_the_path_is_appended_when_chosen_and_a_failure_only_warns(self) -> None:
        machine = self.machine(rules=review_rules(("Append", "y")))
        machine.on_path = False
        self.listener_session(machine, self.paired())
        self.assertEqual(machine.run("--invite"), 0, machine.errors)
        self.assertEqual(machine.appended, [Path(self.a) / "bin"])
        self.assertIn("Appended", machine.out)

        def broken(_folder: Path) -> str:
            raise OSError("registry is locked")

        machine = self.machine(rules=review_rules(("Append", "y")))
        machine.on_path = False
        machine.deps.append_to_user_path = broken
        pairing.Limits(self.a).close_window()
        self.listener_session(machine, self.paired())
        self.assertEqual(machine.run("--invite"), 1)
        self.assertIn("registry is locked", machine.errors)
        self.assertEqual(len(load_policy(self.a).peers), 1)


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

    def test_open_agent_window_builds_argument_lists_without_a_shell(self) -> None:
        calls: list[tuple[list[str], dict[str, Any]]] = []

        def popen(command: list[str], **options: Any) -> None:
            calls.append((command, options))

        folder = "/work/it's"
        which = {"claude": "/bin/claude", "xterm": "/usr/bin/xterm"}.get
        self.assertIsNone(pair_cli.open_agent_window("claude", folder, "win32", which, popen, {"CLAUDECODE": "1", "PATH": "p"}))
        command, options = calls[-1]
        self.assertEqual(command, ["/bin/claude"])
        self.assertEqual((options["cwd"], options["creationflags"], options["shell"]), (folder, pair_cli.CREATE_NEW_CONSOLE, False))
        self.assertNotIn("CLAUDECODE", options["env"])
        self.assertIsNone(pair_cli.open_agent_window("claude", folder, "linux", which, popen, {"DISPLAY": ":0"}))
        self.assertEqual(calls[-1][0], ["/usr/bin/xterm", "-e", "/bin/claude"])
        self.assertIsNone(pair_cli.open_agent_window("claude", folder, "darwin", which, popen, {}))
        self.assertEqual(calls[-1][0][-1], "cd '/work/it'\"'\"'s' && /bin/claude")

    def test_open_agent_window_reports_what_it_cannot_do(self) -> None:
        def popen(command: list[str], **options: Any) -> None:
            raise OSError("denied")

        which = {"claude": "/bin/claude"}.get
        self.assertIn("not on the PATH", pair_cli.open_agent_window("codex", "/w", "linux", which, popen, {"DISPLAY": ":0"}) or "")
        self.assertIn("No terminal window", pair_cli.open_agent_window("claude", "/w", "linux", which, popen, {}) or "")
        self.assertIn("No terminal program", pair_cli.open_agent_window("claude", "/w", "linux", which, popen, {"DISPLAY": ":0"}) or "")
        self.assertIn("denied", pair_cli.open_agent_window("claude", "/w", "win32", which, popen, {}) or "")
        self.assertIn("not an agent", pair_cli.open_agent_window("bash", "/w", "linux", which, popen, {}) or "")

    def test_the_caller_is_named_from_the_environment(self) -> None:
        self.assertEqual(pair_cli._caller_name({"CLAUDECODE": "1"}), "Claude Code")  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(pair_cli._caller_name({"CODEX_HOME": "x"}), "Codex")  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(pair_cli._caller_name({}), "")  # pyright: ignore[reportPrivateUsage]

    def test_the_console_terminal_reads_a_line_times_out_and_ends(self) -> None:
        lines = ["first\n", "second\r\n"]
        gate = threading.Event()

        def readline() -> str:
            if lines:
                return lines.pop(0)
            gate.wait(5)
            return ""

        written: list[str] = []
        terminal = ConsoleTerminal(written.append, readline)
        with contextlib.redirect_stdout(io.StringIO()) as shown:
            self.assertEqual(terminal.read("> ", 2.0), "first")
            self.assertEqual(terminal.read("> ", 2.0), "second")
            self.assertIsNone(terminal.read("> ", 0.1))
            terminal.write("shown")
            self.assertEqual(written, ["shown"])
            gate.set()
            self.assertIsNone(terminal.read("> ", 2.0))
            self.assertIsNone(terminal.read("> ", 2.0))
        self.assertIn("> ", shown.getvalue())

    def test_a_late_line_answers_the_next_prompt_instead_of_starting_a_second_reader(self) -> None:
        gate = threading.Event()
        count = 0

        def readline() -> str:
            nonlocal count
            count += 1
            gate.wait(5)
            return "late\n"

        terminal = ConsoleTerminal(lambda _t: None, readline)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(terminal.read("> ", 0.05))
            gate.set()
            self.assertEqual(terminal.read("> ", 2.0), "late")
        self.assertEqual(count, 1)


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
            for leftover in (pairing.PAIRING_FILE, pairing.PAIRING_FILE + ".lock", pair_cli.INVITATION_FILE):
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(home, "net", leftover))
            Path(home, "config.json").write_text(json.dumps({"network": {"deviceName": name}}), encoding="utf-8")

    def machines(self, listener_rules: Sequence[tuple[str, Any]], dialer_rules: Sequence[tuple[str, Any]]) -> tuple[Machine, Machine]:
        a = Machine(self, self.a, rules=listener_rules)
        b = Machine(self, self.b, rules=dialer_rules, bind="192.168.50.20")
        a.screen.typing = b.screen.wait_code
        b.screen.typing = a.screen.wait_code
        b.dial_address = "127.0.0.1"
        b.destination_error = None
        b.deps.check_destination = lambda address, port, _interface, _prefix: address
        return a, b

    def pair(self, a: Machine, b: Machine) -> tuple[int, int]:
        codes: list[int] = []
        thread = threading.Thread(target=lambda: codes.append(a.run("--invite")), daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        line = None
        while time.monotonic() < deadline and line is None:
            line = next((entry for entry in a.screen.lines if entry.startswith("atp1-invite:")), None)
            time.sleep(0.02)
        assert line is not None, a.errors
        a_port = pairing.parse_invitation(line, time.time()).port
        b.dial_address = "127.0.0.1"
        b.deps.dial = lambda address, port, *rest, **kw: b._dial(address, a_port, *rest, **kw)  # pyright: ignore[reportPrivateUsage]
        code_b = b.run("--join", line)
        thread.join(40)
        assert codes, "the listening side never finished"
        return codes[0], code_b

    def test_two_machines_pair_through_the_real_protocol_with_scripted_input(self) -> None:
        a, b = self.machines(
            review_rules(("Label for this peer", "bravo-box")),
            review_rules(("Pair with", "y"), ("Label for this peer", "alpha-box")),
        )
        code_a, code_b = self.pair(a, b)
        self.assertEqual((code_a, code_b), (0, 0), f"{a.errors}\n{b.errors}")
        self.assertEqual(a.screen.unmatched, [])
        self.assertEqual(b.screen.unmatched, [])
        self.assertEqual(a.screen.code(), b.screen.code())
        a_peer, b_peer = load_policy(self.a).peers[0], load_policy(self.b).peers[0]
        self.assertEqual((a_peer.device_id, a_peer.label, a_peer.folders, a_peer.agents), (device_of(self.b), "bravo-box", (), ()))
        self.assertEqual((b_peer.device_id, b_peer.label, b_peer.folders, b_peer.agents), (device_of(self.a), "alpha-box", (), ()))
        self.assertEqual(
            peer_cert_path(self.a, device_of(self.b)).read_text(encoding="ascii").strip(),
            cert_of(self.b).read_text(encoding="ascii").strip(),
        )
        self.assertEqual(
            peer_cert_path(self.b, device_of(self.a)).read_text(encoding="ascii").strip(),
            cert_of(self.a).read_text(encoding="ascii").strip(),
        )
        self.assertEqual(a.events[-1], "close-rule")
        self.assertFalse(a.window_is_open())
        self.assertFalse(pair_cli.invitation_path(self.a).exists())
        self.assertEqual((a.failures(), b.failures()), (0, 0))
        self.assertEqual((a.harness.spawned, b.harness.spawned), ([], []))
        for machine in (a, b):
            self.assertNotIn(machine.screen.code() or "x", machine.errors)
            self.assertNotIn(machine.screen.code() or "x", "".join(machine.notices))

    def test_a_wrong_code_on_one_side_saves_nothing_anywhere_and_counts_a_failure(self) -> None:
        a, b = self.machines(review_rules(), review_rules(("Pair with", "y")))
        a.screen.typing = lambda: "000000" if b.screen.wait_code() != "000000" else "111111"
        code_a, code_b = self.pair(a, b)
        self.assertEqual((code_a, code_b), (1, 1))
        self.assertEqual((load_policy(self.a).peers, load_policy(self.b).peers), ((), ()))
        self.assertEqual(a.failures(), 1)
        self.assertIn("does not match", a.errors)
        self.assertFalse(a.window_is_open())
        self.assertEqual(a.events[-1], "close-rule")

    def test_no_at_the_summary_on_one_side_saves_nothing_on_that_side(self) -> None:
        a, b = self.machines(review_rules(("Type yes", "no")), review_rules(("Pair with", "y")))
        code_a, code_b = self.pair(a, b)
        self.assertEqual(code_a, 1)
        self.assertEqual(load_policy(self.a).peers, ())
        self.assertIn(code_b, (0, 1))


if __name__ == "__main__":
    unittest.main()
