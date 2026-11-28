from __future__ import annotations

import json
import unittest
from typing import Any
from unittest import mock

from ide_agent_tabs.net import tailscale, transport
from ide_agent_tabs.net.interfaces import InterfaceError, InterfaceInfo
from ide_agent_tabs.net.pairing import PairingError
from ide_agent_tabs.net.policy import NetworkConfig
from ide_agent_tabs.net.tailscale import (
    OwnDevice,
    TailscaleClient,
    TailscaleDiscovery,
    TailscaleError,
    TailscaleNotInstalled,
    check_owner,
    find_binary,
    is_tailscale_address,
    own_devices,
    own_user_id,
    parse_ip4,
    parse_status,
    parse_whois,
)
from ide_agent_tabs.net.transport import LanTransport, TailscaleTransport, Transport, transport_for

BINARY = "/usr/bin/tailscale"
ME = 11
OTHER = 22
SELF_IP = "100.64.0.1"
PEER_IP = "100.64.0.2"
INFO = InterfaceInfo("Tailscale", SELF_IP, 32, "100.64.0.1/32", "Private", False)


def node(
    host: str = "laptop", user: int = ME, ips: Any = None, online: Any = True, tags: Any = None, sharee: Any = None, **extra: Any
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "ID": f"n{host}",
        "UserID": user,
        "HostName": host,
        "DNSName": f"{host}.tail1234.ts.net.",
        "TailscaleIPs": ips if ips is not None else [PEER_IP, "fd7a:115c:a1e0::1"],
        "Online": online,
    }
    if tags is not None:
        data["Tags"] = tags
    if sharee is not None:
        data["ShareeNode"] = sharee
    data.update(extra)
    return data


def status_text(peers: dict[str, Any] | None = None, **self_extra: Any) -> str:
    data: dict[str, Any] = {"Self": node("desktop", ME, [SELF_IP], **self_extra)}
    if peers is not None:
        data["Peer"] = peers
    return json.dumps(data)


def whois_data(user: int = ME, address: str = PEER_IP, **node_extra: Any) -> dict[str, Any]:
    return {
        "Node": {"ID": 5, "Name": "laptop.tail1234.ts.net.", "User": user, "Addresses": [f"{address}/32"], **node_extra},
        "UserProfile": {"ID": user, "LoginName": "alex@example.com"},
    }


def whois_text(**kwargs: Any) -> str:
    return json.dumps(whois_data(**kwargs))


class FakeRunner:
    def __init__(self, answers: dict[tuple[str, ...], Any] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv: list[str], timeout_s: float) -> str:
        self.calls.append((argv, timeout_s))
        answer = self.answers[tuple(argv[1:])]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def client_for(answers: dict[tuple[str, ...], Any]) -> tuple[TailscaleClient, FakeRunner]:
    runner = FakeRunner(answers)
    return TailscaleClient(runner, lambda: BINARY), runner


IP4 = ("ip", "-4")
SELF = ("status", "--self=true", "--peers=false", "--json")
STATUS = ("status", "--json")


def whois_key(address: str) -> tuple[str, ...]:
    return ("whois", "--json", address)


class FindBinaryTest(unittest.TestCase):
    def test_windows_uses_program_files(self) -> None:
        seen: list[str] = []

        def is_file(path: str) -> bool:
            seen.append(path)
            return True

        self.assertEqual(find_binary("win32", {"ProgramFiles": r"C:\Program Files"}, is_file), r"C:\Program Files\Tailscale\tailscale.exe")
        self.assertEqual(len(seen), 1)

    def test_windows_without_an_absolute_program_files_finds_nothing(self) -> None:
        for environ in ({}, {"ProgramFiles": r"Program Files"}):
            with self.assertRaises(TailscaleNotInstalled):
                find_binary("win32", environ, lambda _path: True)

    def test_macos_tries_the_app_then_the_command_line_paths(self) -> None:
        self.assertEqual(tailscale.binary_paths("darwin", {})[0], "/Applications/Tailscale.app/Contents/MacOS/Tailscale")
        self.assertEqual(find_binary("darwin", {}, lambda path: path == "/opt/homebrew/bin/tailscale"), "/opt/homebrew/bin/tailscale")

    def test_linux_tries_usr_bin_then_usr_sbin(self) -> None:
        self.assertEqual(find_binary("linux", {}, lambda path: path == "/usr/sbin/tailscale"), "/usr/sbin/tailscale")
        self.assertEqual(find_binary("linux", {}, lambda _path: True), "/usr/bin/tailscale")

    def test_a_missing_binary_says_tailscale_is_not_installed(self) -> None:
        for platform in ("linux", "darwin", "win32", "freebsd"):
            with self.assertRaises(TailscaleNotInstalled) as caught:
                find_binary(platform, {"ProgramFiles": r"C:\Program Files"}, lambda _path: False)
            self.assertIn("Tailscale isn't installed", str(caught.exception))

    def test_the_search_ignores_the_path(self) -> None:
        for platform in ("linux", "darwin", "win32"):
            for path in tailscale.binary_paths(platform, {"ProgramFiles": r"C:\Program Files", "PATH": "/tmp/evil"}):
                self.assertTrue(path.startswith(("/", "C:\\")))

    def test_a_missing_binary_stops_every_client_method(self) -> None:
        def locate() -> str:
            return find_binary("linux", {}, lambda _path: False)

        client = TailscaleClient(FakeRunner(), locate)
        for call in (client.ip4, client.self_status, client.status, lambda: client.whois(PEER_IP)):
            with self.assertRaises(TailscaleNotInstalled):
                call()


class IsTailscaleAddressTest(unittest.TestCase):
    def test_range_edges(self) -> None:
        self.assertFalse(is_tailscale_address("100.63.255.255"))
        self.assertTrue(is_tailscale_address("100.64.0.0"))
        self.assertTrue(is_tailscale_address("100.127.255.255"))
        self.assertFalse(is_tailscale_address("100.128.0.0"))

    def test_refuses_other_text(self) -> None:
        for value in (
            "fd7a:115c:a1e0::1",
            "::ffff:100.64.0.1",
            "100.64.0.1/32",
            " 100.64.0.1",
            "100.64.0.01",
            "100.64.0",
            "100.64.0.1\n",
            "",
            "tailscale",
        ):
            self.assertFalse(is_tailscale_address(value), value)
        for value in (None, 1684, b"100.64.0.1", ["100.64.0.1"]):
            self.assertFalse(is_tailscale_address(value))


class ClientTest(unittest.TestCase):
    def test_ip4_runs_the_command_with_the_binary_and_a_timeout(self) -> None:
        client, runner = client_for({IP4: f"{SELF_IP}\n"})
        self.assertEqual(client.ip4(), SELF_IP)
        self.assertEqual(runner.calls, [([BINARY, "ip", "-4"], tailscale.TIMEOUT_S)])

    def test_self_status_asks_for_the_self_entry_only(self) -> None:
        client, runner = client_for({SELF: status_text()})
        me = client.self_status()
        self.assertEqual((me.user_id, me.host_name, me.addresses), (ME, "desktop", (SELF_IP,)))
        self.assertEqual(runner.calls[0][0], [BINARY, "status", "--self=true", "--peers=false", "--json"])

    def test_status_returns_self_and_peers(self) -> None:
        client, _ = client_for({STATUS: status_text({"k1": node("laptop")})})
        result = client.status()
        self.assertEqual([peer.host_name for peer in result.peers], ["laptop"])
        self.assertEqual(result.self_node.host_name, "desktop")

    def test_whois_passes_the_address_and_checks_the_node_carries_it(self) -> None:
        client, runner = client_for({whois_key(PEER_IP): whois_text()})
        result = client.whois(PEER_IP)
        self.assertEqual((result.user_id, result.addresses, result.tags, result.shared_in), (ME, (PEER_IP,), (), False))
        self.assertEqual(runner.calls[0][0], [BINARY, "whois", "--json", PEER_IP])

    def test_whois_refuses_a_node_that_does_not_carry_the_address(self) -> None:
        client, _ = client_for({whois_key(PEER_IP): whois_text(address="100.64.0.9")})
        with self.assertRaises(TailscaleError):
            client.whois(PEER_IP)

    def test_whois_refuses_a_non_tailscale_address_without_running_anything(self) -> None:
        client, runner = client_for({})
        for address in ("192.168.1.2", "--help", "100.128.0.0", ""):
            with self.assertRaises(TailscaleError):
                client.whois(address)
        self.assertEqual(runner.calls, [])

    def test_a_runner_that_raises_an_os_error_or_times_out_fails(self) -> None:
        for error in (OSError("no such file"), TimeoutError("slow"), TailscaleError("exit 1")):
            client, _ = client_for({IP4: error, SELF: error, STATUS: error, whois_key(PEER_IP): error})
            for call in (client.ip4, client.self_status, client.status, lambda client=client: client.whois(PEER_IP)):
                with self.assertRaises(TailscaleError):
                    call()

    def test_a_runner_that_returns_non_text_fails(self) -> None:
        client, _ = client_for({IP4: None})
        with self.assertRaises(TailscaleError):
            client.ip4()


class RunCommandTest(unittest.TestCase):
    def test_failures_become_tailscale_errors(self) -> None:
        from unittest import mock

        from ide_agent_tabs import processes

        with mock.patch.object(processes, "run", side_effect=TimeoutError("slow")), self.assertRaises(TailscaleError):
            tailscale.run_command([BINARY, "ip"], 1.0)
        with mock.patch.object(processes, "run", side_effect=FileNotFoundError("gone")), self.assertRaises(TailscaleError):
            tailscale.run_command([BINARY, "ip"], 1.0)
        failed = processes.RunResult(1, "", "not logged in")
        with mock.patch.object(processes, "run", return_value=failed), self.assertRaises(TailscaleError) as caught:
            tailscale.run_command([BINARY, "ip"], 1.0)
        self.assertIn("not logged in", str(caught.exception))
        with mock.patch.object(processes, "run", return_value=processes.RunResult(None, "", "")), self.assertRaises(TailscaleError):
            tailscale.run_command([BINARY, "ip"], 1.0)
        with mock.patch.object(processes, "run", return_value=processes.RunResult(0, "ok", "")) as run:
            self.assertEqual(tailscale.run_command([BINARY, "ip", "-4"], 3.0), "ok")
        run.assert_called_once_with(BINARY, ["ip", "-4"], timeout=3.0)


class ParseIp4Test(unittest.TestCase):
    def test_accepts_one_address(self) -> None:
        self.assertEqual(parse_ip4("100.100.100.100\r\n"), "100.100.100.100")

    def test_fails_closed(self) -> None:
        for text in ("", "\n", "not an address", "192.168.1.5", "fd7a:115c:a1e0::1", f"{SELF_IP}\n{PEER_IP}\n", "100.64.0.1 extra"):
            with self.assertRaises(TailscaleError, msg=text):
                parse_ip4(text)


class ParseStatusTest(unittest.TestCase):
    def test_parses_a_node(self) -> None:
        text = status_text({"k": node("laptop", tags=[], sharee=False)})
        result = parse_status(text)
        peer = result.peers[0]
        self.assertEqual(
            (peer.id, peer.user_id, peer.host_name, peer.tags, peer.shared_in, peer.online), ("nlaptop", ME, "laptop", (), False, True)
        )
        self.assertEqual(peer.addresses, (PEER_IP, "fd7a:115c:a1e0::1"))
        self.assertEqual(peer.dns_name, "laptop.tail1234.ts.net.")

    def test_tags_and_sharee_are_optional(self) -> None:
        peer = parse_status(status_text({"k": node(tags=["tag:server"], sharee=True)})).peers[0]
        self.assertEqual((peer.tags, peer.shared_in), (("tag:server",), True))
        plain = parse_status(status_text({"k": node()})).peers[0]
        self.assertEqual((plain.tags, plain.shared_in), ((), False))

    def test_absent_peer_map_means_no_peers(self) -> None:
        self.assertEqual(parse_status(status_text()).peers, ())
        self.assertEqual(parse_status(status_text({})).peers, ())

    def test_non_json_and_wrong_top_level_fail(self) -> None:
        for text in ("", "Tailscale is stopped.", "{", "[]", "null", "1", '"x"'):
            with self.assertRaises(TailscaleError, msg=text):
                parse_status(text)

    def test_missing_self_fails(self) -> None:
        for data in ({}, {"Peer": {}}, {"Self": None}, {"Self": []}, {"Self": "x"}):
            with self.assertRaises(TailscaleError, msg=str(data)):
                parse_status(json.dumps(data))

    def test_wrong_peer_container_fails(self) -> None:
        for value in ([], "x", 1, False):
            text = json.dumps({"Self": node(), "Peer": value})
            with self.assertRaises(TailscaleError, msg=str(value)):
                parse_status(text)

    def test_null_peer_container_means_no_peers(self) -> None:
        self.assertEqual(parse_status(json.dumps({"Self": node(), "Peer": None})).peers, ())

    def test_every_required_node_field_fails_when_missing_or_wrong_typed(self) -> None:
        wrong = {
            "ID": [[1], None, True, ""],
            "UserID": ["11", None, True, 1.5],
            "HostName": [None, 5, ""],
            "DNSName": [None, 5, ""],
            "TailscaleIPs": [None, "100.64.0.2", [5], ["not-an-ip"], ["100.64.0.2/32"]],
            "Online": [None, "true", 1],
            "Tags": [None, "tag:x", [5], {}],
            "ShareeNode": [None, "false", 0],
        }
        for field, values in wrong.items():
            if field not in ("Tags", "ShareeNode"):
                broken = node()
                del broken[field]
                with self.assertRaises(TailscaleError, msg=f"missing {field}"):
                    parse_status(status_text({"k": broken}))
            for value in values:
                broken = node()
                broken[field] = value
                with self.assertRaises(TailscaleError, msg=f"{field}={value!r}"):
                    parse_status(status_text({"k": broken}))

    def test_a_bad_self_entry_fails(self) -> None:
        broken = node()
        del broken["UserID"]
        with self.assertRaises(TailscaleError):
            parse_status(json.dumps({"Self": broken}))

    def test_one_bad_peer_fails_the_whole_status(self) -> None:
        with self.assertRaises(TailscaleError):
            parse_status(status_text({"a": node("a"), "b": node("b", online="yes")}))


class ParseWhoisTest(unittest.TestCase):
    def test_parses_a_whois_answer(self) -> None:
        result = parse_whois(whois_text(Tags=[]), PEER_IP)
        self.assertEqual(
            (result.node_id, result.name, result.user_id, result.login_name, result.addresses, result.tags, result.shared_in),
            ("5", "laptop.tail1234.ts.net.", ME, "alex@example.com", (PEER_IP,), (), False),
        )

    def test_a_present_sharer_means_shared_in(self) -> None:
        for sharer in (7, 0, None, "x"):
            self.assertTrue(parse_whois(whois_text(Sharer=sharer)).shared_in, repr(sharer))

    def test_tags_are_read(self) -> None:
        self.assertEqual(parse_whois(whois_text(Tags=["tag:a", "tag:b"])).tags, ("tag:a", "tag:b"))

    def test_non_json_fails(self) -> None:
        for text in ("", "no node found", "{", "[]", "null"):
            with self.assertRaises(TailscaleError, msg=text):
                parse_whois(text)

    def test_empty_missing_or_ambiguous_node_fails(self) -> None:
        good = whois_data()
        for node_value in ({}, None, [], [good["Node"]], [good["Node"], good["Node"]], "x"):
            with self.assertRaises(TailscaleError, msg=repr(node_value)):
                parse_whois(json.dumps({**good, "Node": node_value}))
        missing = dict(good)
        del missing["Node"]
        with self.assertRaises(TailscaleError):
            parse_whois(json.dumps(missing))

    def test_empty_missing_or_wrong_profile_fails(self) -> None:
        good = whois_data()
        for profile in ({}, None, [], "x"):
            with self.assertRaises(TailscaleError, msg=repr(profile)):
                parse_whois(json.dumps({**good, "UserProfile": profile}))
        missing = dict(good)
        del missing["UserProfile"]
        with self.assertRaises(TailscaleError):
            parse_whois(json.dumps(missing))

    def test_every_required_field_fails_when_missing_or_wrong_typed(self) -> None:
        wrong = {
            ("Node", "ID"): [None, [], True, ""],
            ("Node", "Name"): [None, 5, ""],
            ("Node", "User"): [None, "11", True, 1.5],
            ("Node", "Addresses"): [None, "100.64.0.2/32", [5], ["bad"], ["100.64.0.2/x"], ["100.64.0.2/32/1"]],
            ("Node", "Tags"): [None, "tag:x", [5]],
            ("UserProfile", "ID"): [None, "11", True],
            ("UserProfile", "LoginName"): [None, 5, ""],
        }
        for (section, field), values in wrong.items():
            if field != "Tags":
                data = whois_data()
                del data[section][field]
                with self.assertRaises(TailscaleError, msg=f"missing {section}.{field}"):
                    parse_whois(json.dumps(data))
            for value in values:
                data = whois_data()
                data[section][field] = value
                with self.assertRaises(TailscaleError, msg=f"{section}.{field}={value!r}"):
                    parse_whois(json.dumps(data))

    def test_node_and_profile_naming_different_users_fails(self) -> None:
        data = whois_data()
        data["UserProfile"]["ID"] = OTHER
        with self.assertRaises(TailscaleError):
            parse_whois(json.dumps(data))

    def test_an_address_the_node_does_not_carry_fails(self) -> None:
        with self.assertRaises(TailscaleError):
            parse_whois(whois_text(), "100.64.0.99")


class CheckOwnerTest(unittest.TestCase):
    def whois(self, **kwargs: Any) -> tailscale.Whois:
        return parse_whois(whois_text(**kwargs))

    def test_same_owner_passes(self) -> None:
        self.assertIsNone(check_owner(self.whois(), {ME}))

    def test_other_owner_refuses(self) -> None:
        with self.assertRaises(TailscaleError):
            check_owner(self.whois(user=OTHER), {ME})

    def test_tagged_refuses_even_for_an_allowed_user(self) -> None:
        with self.assertRaises(TailscaleError):
            check_owner(self.whois(Tags=["tag:server"]), {ME})

    def test_shared_in_refuses_even_for_an_allowed_user(self) -> None:
        with self.assertRaises(TailscaleError):
            check_owner(self.whois(Sharer=OTHER), {ME})

    def test_an_empty_allowed_set_refuses(self) -> None:
        for allowed in (set(), frozenset(), []):
            with self.assertRaises(TailscaleError):
                check_owner(self.whois(), allowed)

    def test_a_second_allowed_owner_passes(self) -> None:
        self.assertIsNone(check_owner(self.whois(user=OTHER), {ME, OTHER}))
        self.assertIsNone(check_owner(self.whois(user=ME), {ME, OTHER}))
        with self.assertRaises(TailscaleError):
            check_owner(self.whois(user=33), {ME, OTHER})


class OwnUserIdTest(unittest.TestCase):
    def test_returns_the_user_of_an_untagged_machine(self) -> None:
        self.assertEqual(own_user_id(parse_status(status_text()).self_node), ME)

    def test_a_tagged_or_shared_machine_has_no_owner(self) -> None:
        extras: list[dict[str, Any]] = [{"tags": ["tag:x"]}, {"sharee": True}]
        for extra in extras:
            with self.assertRaises(TailscaleError):
                own_user_id(parse_status(status_text(**extra)).self_node)


class OwnDevicesTest(unittest.TestCase):
    def peers(self) -> tailscale.Status:
        return parse_status(
            status_text(
                {
                    "a": node("mine-online", ME, [PEER_IP]),
                    "b": node("mine-offline", ME, ["100.64.0.3"], online=False),
                    "c": node("theirs", OTHER, ["100.64.0.4"]),
                    "d": node("tagged", ME, ["100.64.0.5"], tags=["tag:server"]),
                    "e": node("shared", ME, ["100.64.0.6"], sharee=True),
                    "f": node("no-v4", ME, ["fd7a:115c:a1e0::9"]),
                    "g": node("outside", ME, ["192.168.1.9", "100.64.0.7"]),
                    "h": node("empty-tags", ME, ["100.64.0.8"], tags=[]),
                }
            )
        )

    def test_keeps_only_untagged_unshared_peers_of_an_allowed_owner(self) -> None:
        found = own_devices(self.peers(), {ME})
        self.assertEqual(
            found,
            [
                OwnDevice("mine-online", PEER_IP, True),
                OwnDevice("mine-offline", "100.64.0.3", False),
                OwnDevice("outside", "100.64.0.7", True),
                OwnDevice("empty-tags", "100.64.0.8", True),
            ],
        )

    def test_a_second_owner_adds_that_owners_devices(self) -> None:
        names = [device.host_name for device in own_devices(self.peers(), {ME, OTHER})]
        self.assertIn("theirs", names)
        self.assertEqual(len(names), 5)

    def test_an_empty_allowed_set_finds_nothing(self) -> None:
        self.assertEqual(own_devices(self.peers(), set()), [])

    def test_the_self_node_is_not_a_peer(self) -> None:
        self.assertEqual(own_devices(parse_status(status_text()), {ME}), [])


def transport_for_test(answers: dict[tuple[str, ...], Any], allowed: Any = None) -> tuple[TailscaleTransport, FakeRunner]:
    client, runner = client_for(answers)
    return TailscaleTransport(client, {ME} if allowed is None else allowed, lambda address: INFO), runner


class TailscaleTransportTest(unittest.TestCase):
    def test_satisfies_the_transport_protocol(self) -> None:
        shape: Transport = TailscaleTransport(client_for({})[0], {ME})
        self.assertIsNotNone(shape)

    def test_transport_for_builds_a_transport_per_mode(self) -> None:
        client, _ = client_for({("status", "--self=true", "--peers=false", "--json"): status_text()})
        built = transport_for(NetworkConfig(network="tailscale"), client)
        assert isinstance(built, TailscaleTransport)
        self.assertEqual(built.allowed_owner_ids, frozenset({ME}))
        self.assertIsInstance(transport_for(NetworkConfig(network="lan")), LanTransport)
        self.assertIsNone(transport_for(NetworkConfig()))

    def test_a_tailscale_transport_with_an_unreadable_owner_refuses_every_connection(self) -> None:
        client, _ = client_for({("status", "--self=true", "--peers=false", "--json"): "not json"})
        built = transport_for(NetworkConfig(network="tailscale"), client)
        assert isinstance(built, TailscaleTransport)
        self.assertEqual(built.allowed_owner_ids, frozenset())

    def test_lan_mode_never_builds_a_tailscale_client(self) -> None:
        with mock.patch.object(transport, "TailscaleClient", side_effect=AssertionError("built a client")):
            self.assertIsInstance(transport_for(NetworkConfig(network="lan")), LanTransport)

    def test_allows_bind_follows_the_range(self) -> None:
        subject, _ = transport_for_test({})
        self.assertTrue(subject.allows_bind("100.64.0.0"))
        self.assertTrue(subject.allows_bind("100.127.255.255"))
        for address in ("100.63.255.255", "100.128.0.0", "192.168.1.2", "127.0.0.1", "fd7a:115c:a1e0::1"):
            self.assertFalse(subject.allows_bind(address))

    def test_bind_address_returns_an_allowed_bind_only(self) -> None:
        subject, _ = transport_for_test({})
        self.assertEqual(subject.bind_address(NetworkConfig(network="tailscale", bind=SELF_IP)), SELF_IP)
        self.assertIsNone(subject.bind_address(NetworkConfig(network="tailscale")))
        self.assertIsNone(subject.bind_address(NetworkConfig(network="tailscale", bind="192.168.1.10")))

    def test_candidates_keep_only_the_own_tailscale_address(self) -> None:
        subject, runner = transport_for_test({IP4: SELF_IP})
        described: list[str] = []

        def describe(address: str) -> InterfaceInfo:
            described.append(address)
            return INFO

        found = subject.candidates(["192.168.1.5", PEER_IP, SELF_IP, "100.128.0.1", "127.0.0.1"], describe)
        self.assertEqual((found, described), ([INFO], [SELF_IP]))
        self.assertEqual(len(runner.calls), 1)

    def test_candidates_skip_an_address_that_cannot_be_described(self) -> None:
        subject, _ = transport_for_test({IP4: SELF_IP})

        def describe(address: str) -> InterfaceInfo:
            raise InterfaceError("gone")

        self.assertEqual(subject.candidates([SELF_IP], describe), [])

    def test_candidates_without_a_tailscale_address_do_not_run_tailscale(self) -> None:
        subject, runner = transport_for_test({})
        self.assertEqual(subject.candidates(["192.168.1.5", "10.0.0.2"], lambda address: INFO), [])
        self.assertEqual(runner.calls, [])

    def test_candidates_pass_a_tailscale_failure_up(self) -> None:
        subject, _ = transport_for_test({IP4: TailscaleError("stopped")})
        with self.assertRaises(TailscaleError):
            subject.candidates([SELF_IP], lambda address: INFO)

    def test_check_destination_returns_the_address_for_an_own_device(self) -> None:
        subject, runner = transport_for_test({whois_key(PEER_IP): whois_text()})
        self.assertEqual(subject.check_destination(PEER_IP, 47830, SELF_IP, 32), PEER_IP)
        self.assertEqual(runner.calls[0][0], [BINARY, "whois", "--json", PEER_IP])

    def test_check_destination_refuses_before_running_tailscale(self) -> None:
        subject, runner = transport_for_test({})
        cases: list[tuple[Any, Any, Any]] = [
            ("100.63.255.255", 47830, SELF_IP),
            ("192.168.1.20", 47830, SELF_IP),
            ("100.128.0.0", 47830, SELF_IP),
            ("fd7a:115c:a1e0::1", 47830, SELF_IP),
            (PEER_IP, 0, SELF_IP),
            (PEER_IP, 65536, SELF_IP),
            (PEER_IP, True, SELF_IP),
            (PEER_IP, "47830", SELF_IP),
            (PEER_IP, 47830, "192.168.1.10"),
            (SELF_IP, 47830, SELF_IP),
        ]
        for address, port, interface in cases:
            with self.assertRaises(PairingError, msg=str((address, port, interface))):
                subject.check_destination(address, port, interface, 32)
        self.assertEqual(runner.calls, [])

    def test_check_destination_refuses_a_foreign_tagged_or_shared_device(self) -> None:
        for kwargs in ({"user": OTHER}, {"Tags": ["tag:x"]}, {"Sharer": OTHER}):
            subject, _ = transport_for_test({whois_key(PEER_IP): whois_text(**kwargs)})
            with self.assertRaises(PairingError, msg=str(kwargs)):
                subject.check_destination(PEER_IP, 47830, SELF_IP, 32)

    def test_check_destination_refuses_with_no_allowed_owner(self) -> None:
        subject, _ = transport_for_test({whois_key(PEER_IP): whois_text()}, allowed=set())
        with self.assertRaises(PairingError):
            subject.check_destination(PEER_IP, 47830, SELF_IP, 32)

    def test_check_destination_turns_a_tailscale_failure_into_a_refusal(self) -> None:
        subject, _ = transport_for_test({whois_key(PEER_IP): TimeoutError("slow")})
        with self.assertRaises(PairingError):
            subject.check_destination(PEER_IP, 47830, SELF_IP, 32)
        subject, _ = transport_for_test({whois_key(PEER_IP): "garbage"})
        with self.assertRaises(PairingError):
            subject.check_destination(PEER_IP, 47830, SELF_IP, 32)

    def test_check_connection_passes_an_own_device(self) -> None:
        subject, _ = transport_for_test({whois_key(PEER_IP): whois_text()})
        self.assertIsNone(subject.check_connection(PEER_IP))

    def test_check_connection_raises_on_refusal_and_on_failure(self) -> None:
        for answer in (whois_text(user=OTHER), whois_text(Tags=["tag:x"]), whois_text(Sharer=1), "garbage", OSError("down")):
            subject, _ = transport_for_test({whois_key(PEER_IP): answer})
            with self.assertRaises(TailscaleError, msg=str(answer)):
                subject.check_connection(PEER_IP)
        subject, runner = transport_for_test({})
        with self.assertRaises(TailscaleError):
            subject.check_connection("192.168.1.5")
        self.assertEqual(runner.calls, [])

    def test_a_second_allowed_owner_passes_check_connection(self) -> None:
        subject, _ = transport_for_test({whois_key(PEER_IP): whois_text(user=OTHER)}, allowed={ME, OTHER})
        self.assertIsNone(subject.check_connection(PEER_IP))

    def test_firewall_scope_is_the_tailscale_range(self) -> None:
        subject, _ = transport_for_test({})
        self.assertEqual(subject.firewall_scope(INFO), "100.64.0.0/10")

    def test_require_ready_returns_the_interface_of_the_own_address(self) -> None:
        subject, _ = transport_for_test({IP4: SELF_IP})
        self.assertIs(subject.require_ready(SELF_IP), INFO)
        self.assertIs(subject.require_ready(SELF_IP, allow_virtual=True), INFO)

    def test_require_ready_refuses_other_addresses(self) -> None:
        subject, _ = transport_for_test({IP4: SELF_IP})
        for address in (PEER_IP, "100.63.255.255", "100.128.0.0", "192.168.1.10", "fd7a:115c:a1e0::1"):
            with self.assertRaises(InterfaceError, msg=address):
                subject.require_ready(address)

    def test_require_ready_turns_a_tailscale_failure_into_an_interface_error(self) -> None:
        for answer in (OSError("down"), "garbage", TailscaleError("stopped")):
            subject, _ = transport_for_test({IP4: answer})
            with self.assertRaises(InterfaceError):
                subject.require_ready(SELF_IP)

    def test_require_ready_refuses_when_no_interface_is_known_to_carry_the_address(self) -> None:
        client, _ = client_for({IP4: SELF_IP})
        unverified = InterfaceInfo("", SELF_IP, 32, "100.64.0.1/32", "unknown", True)
        with self.assertRaises(InterfaceError):
            TailscaleTransport(client, {ME}, lambda address: unverified).require_ready(SELF_IP)

        def missing(address: str) -> InterfaceInfo:
            raise InterfaceError("no interface")

        with self.assertRaises(InterfaceError):
            TailscaleTransport(client, {ME}, missing).require_ready(SELF_IP)


class WaitReadyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def test_returns_at_once_when_ready(self) -> None:
        subject, runner = transport_for_test({IP4: SELF_IP})
        self.assertIs(subject.wait_ready(SELF_IP, 5.0, sleep=self.sleep, clock=self.clock), INFO)
        self.assertEqual((len(runner.calls), self.sleeps), (1, []))

    def test_retries_until_tailscale_has_the_address(self) -> None:
        answers = [OSError("down"), "", SELF_IP]
        calls: list[int] = []

        def runner(argv: list[str], timeout_s: float) -> str:
            calls.append(1)
            answer = answers[len(calls) - 1]
            if isinstance(answer, BaseException):
                raise answer
            return answer

        subject = TailscaleTransport(TailscaleClient(runner, lambda: BINARY), {ME}, lambda address: INFO)
        self.assertIs(subject.wait_ready(SELF_IP, 5.0, sleep=self.sleep, clock=self.clock), INFO)
        self.assertEqual((len(calls), self.sleeps), (3, [1.0, 1.0]))

    def test_raises_after_the_timeout(self) -> None:
        subject, runner = transport_for_test({IP4: OSError("down")})
        with self.assertRaises(InterfaceError):
            subject.wait_ready(SELF_IP, 2.0, sleep=self.sleep, clock=self.clock)
        self.assertEqual((len(runner.calls), self.sleeps), (3, [1.0, 1.0]))

    def test_a_zero_timeout_tries_once(self) -> None:
        subject, runner = transport_for_test({IP4: OSError("down")})
        with self.assertRaises(InterfaceError):
            subject.wait_ready(SELF_IP, 0.0, sleep=self.sleep, clock=self.clock)
        self.assertEqual((len(runner.calls), self.sleeps), (1, []))


class TailscaleDiscoveryTest(unittest.TestCase):
    def discovery(self, answer: Any, allowed: Any = None) -> TailscaleDiscovery:
        client, _ = client_for({STATUS: answer})
        return TailscaleDiscovery(client, {ME} if allowed is None else allowed)

    def test_yields_own_online_devices_only(self) -> None:
        peers = {
            "a": node("mine-online", ME, [PEER_IP]),
            "b": node("mine-offline", ME, ["100.64.0.3"], online=False),
            "c": node("theirs", OTHER, ["100.64.0.4"]),
            "d": node("tagged", ME, ["100.64.0.5"], tags=["tag:x"]),
            "e": node("shared", ME, ["100.64.0.6"], sharee=True),
        }
        discovery = self.discovery(status_text(peers))
        self.assertEqual(list(discovery.scan()), [OwnDevice("mine-online", PEER_IP, True)])
        self.assertIsNone(discovery.error)

    def test_a_second_owner_widens_the_result(self) -> None:
        peers = {"a": node("mine", ME, [PEER_IP]), "c": node("theirs", OTHER, ["100.64.0.4"])}
        discovery = self.discovery(status_text(peers), allowed={ME, OTHER})
        self.assertEqual([device.host_name for device in discovery.scan()], ["mine", "theirs"])

    def test_an_empty_allowed_set_yields_nothing(self) -> None:
        discovery = self.discovery(status_text({"a": node("mine", ME, [PEER_IP])}), allowed=set())
        self.assertEqual(list(discovery.scan()), [])

    def test_a_failure_ends_the_scan_and_sets_the_error(self) -> None:
        for answer in (OSError("down"), TimeoutError("slow"), "garbage", status_text({"a": node("x", online="yes")})):
            discovery = self.discovery(answer)
            self.assertEqual(list(discovery.scan()), [])
            self.assertIsNotNone(discovery.error)

    def test_a_missing_binary_sets_the_error(self) -> None:
        def locate() -> str:
            return find_binary("linux", {}, lambda _path: False)

        discovery = TailscaleDiscovery(TailscaleClient(FakeRunner(), locate), {ME})
        self.assertEqual(list(discovery.scan()), [])
        self.assertIn("isn't installed", discovery.error or "")

    def test_a_later_scan_clears_the_error(self) -> None:
        answers = ["garbage", status_text({"a": node("mine", ME, [PEER_IP])})]
        client = TailscaleClient(lambda argv, timeout_s: answers.pop(0), lambda: BINARY)
        discovery = TailscaleDiscovery(client, {ME})
        self.assertEqual(list(discovery.scan()), [])
        self.assertIsNotNone(discovery.error)
        self.assertEqual(len(list(discovery.scan())), 1)
        self.assertIsNone(discovery.error)


if __name__ == "__main__":
    unittest.main()
