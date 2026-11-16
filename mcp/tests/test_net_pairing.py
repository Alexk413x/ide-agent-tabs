from __future__ import annotations

import base64
import contextlib
import dataclasses
import hashlib
import json
import os
import shutil
import socket
import ssl
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from ide_agent_tabs.net import pairing, tls, wire
from ide_agent_tabs.net.identity import device_id, format_digest
from test_net_tls import OPENSSL, Identity, make_identity

NOW = 1_700_000_000
GOOD_ID = format_digest(bytes(range(32)))
INTERFACE = "192.168.1.10"
HELLO_TOKEN = "ABC234"
FAST = pairing.Timeouts(
    first_frame_s=2.0,
    digits_s=10.0,
    typing_s=10.0,
    review_s=10.0,
    consent_s=5.0,
    connect_s=2.0,
    window_s=20.0,
    min_interval_s=0.0,
)
TLS_READY = os.path.exists(OPENSSL) and tls.runtime_problem() is None

REAL_SERVER = tls.server_context
REAL_CLIENT = tls.client_context


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def wire_frame(text: str) -> bytes:
    data = text.encode("utf-8")
    return len(data).to_bytes(4, "big") + data


def invitation_text(**changes: Any) -> str:
    body: dict[str, Any] = {
        "v": 1,
        "name": "alpha",
        "address": "192.168.1.20",
        "port": 47830,
        "id": GOOD_ID.split("-")[0],
        "token": pairing.new_invitation_token(),
        "exp": NOW + 300,
    }
    for key, value in changes.items():
        if value is KeyError:
            del body[key]
        else:
            body[key] = value
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    return pairing.INVITE_PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class HashTest(unittest.TestCase):
    CERT_D = b"dialer certificate bytes"
    CERT_L = b"listener certificate bytes"
    NONCE_D = bytes(range(32))
    NONCE_L = bytes(range(32, 64))
    SECRET = "6aca80810e5f2993c723c96d9f51dfa32afca40ab57910a4dfc1ae00d1c98ad5"

    def test_h_prefixes_each_field_with_its_length(self) -> None:
        expected = hashlib.sha256(b"\x00\x00\x00\x01a\x00\x00\x00\x02bc").digest()
        self.assertEqual(pairing.H(b"a", b"bc"), expected)
        self.assertNotEqual(pairing.H(b"ab", b"c"), expected)
        self.assertEqual(pairing.H().hex(), hashlib.sha256(b"").hexdigest())

    def test_commitment_vector(self) -> None:
        commit = pairing.commitment(self.CERT_D, self.NONCE_D)
        self.assertEqual(commit.hex(), "805eeeb6b098e24ce1d142a2353cae2c5554ae02d335e17229e120f0ef897b0d")

    def test_sas_secret_and_digits_vector(self) -> None:
        secret = pairing.sas_secret(self.CERT_D, self.CERT_L, self.NONCE_D, self.NONCE_L)
        self.assertEqual(secret.hex(), self.SECRET)
        self.assertEqual(pairing.digits(secret), "656065")

    def test_the_secret_depends_on_every_input(self) -> None:
        base = pairing.sas_secret(self.CERT_D, self.CERT_L, self.NONCE_D, self.NONCE_L)
        self.assertNotEqual(base, pairing.sas_secret(self.CERT_L, self.CERT_D, self.NONCE_D, self.NONCE_L))
        self.assertNotEqual(base, pairing.sas_secret(self.CERT_D, self.CERT_L, self.NONCE_L, self.NONCE_D))
        self.assertNotEqual(base, pairing.sas_secret(self.CERT_D, self.CERT_L, self.NONCE_D, bytes(32)))

    def test_digits_use_the_first_word_below_the_limit(self) -> None:
        low = (4_293_999_999).to_bytes(4, "big")
        high = (4_294_000_000).to_bytes(4, "big")
        self.assertEqual(pairing.digits(high + low + bytes(24)), "999999")
        self.assertEqual(pairing.digits(low + bytes(28)), "999999")
        self.assertEqual(pairing.digits(bytes(32)), "000000")

    def test_digits_fallback_vector(self) -> None:
        self.assertEqual(pairing.digits(b"\xff" * 32), "109558")

    def test_digits_fallback_counts_up_from_the_original_secret(self) -> None:
        secret = b"\xff" * 32
        real = pairing.H
        calls: list[tuple[bytes, ...]] = []

        def second_round(*fields: bytes) -> bytes:
            calls.append(fields)
            return secret if len(calls) == 1 else real(*fields)

        with mock.patch.object(pairing, "H", second_round):
            self.assertEqual(pairing.digits(secret), "204036")
        self.assertEqual(calls, [(b"atp1 sas2", secret, b"\x00\x00\x00\x01"), (b"atp1 sas2", secret, b"\x00\x00\x00\x02")])

    def test_consent_proof_binds_the_role_and_the_secret(self) -> None:
        secret = bytes.fromhex(self.SECRET)
        self.assertNotEqual(pairing.consent_proof("dialer", secret), pairing.consent_proof("listener", secret))
        self.assertNotEqual(pairing.consent_proof("dialer", secret), pairing.consent_proof("dialer", bytes(32)))


class NameAndTokenTest(unittest.TestCase):
    def test_device_names(self) -> None:
        for good in ("alpha", "My PC-2.local_x", "a", "x" * 32, "12345", "12345a6", "Room 101"):
            self.assertTrue(pairing.valid_device_name(good), good)
        for bad in ("", "x" * 33, "café", "tab\there", "123456", "a1234567b", "name!", "line\nbreak", "１２３４５６"):
            self.assertFalse(pairing.valid_device_name(bad), repr(bad))

    def test_manual_token_shape(self) -> None:
        tokens = {pairing.new_manual_token() for _ in range(40)}
        self.assertGreater(len(tokens), 30)
        for token in tokens:
            self.assertRegex(token, r"^[A-Z2-7]{6}$")

    def test_parse_manual_token(self) -> None:
        self.assertEqual(pairing.parse_manual_token(" abc234 \n"), "ABC234")
        for bad in ("", "ABC23", "ABC2345", "ABC018", "ABC-23", "ABC 23"):
            with self.assertRaises(pairing.PairingError):
                pairing.parse_manual_token(bad)

    def test_token_comparison(self) -> None:
        self.assertTrue(pairing.tokens_equal("ABC234", "ABC234"))
        self.assertFalse(pairing.tokens_equal("ABC234", "ABC235"))
        self.assertFalse(pairing.tokens_equal("ABC234", "ABC2345"))
        self.assertFalse(pairing.tokens_equal("ABC23é", "ABC234"))

    def test_invitation_token_is_128_bits(self) -> None:
        token = pairing.new_invitation_token()
        self.assertEqual(len(token), 22)
        self.assertEqual(len(base64.urlsafe_b64decode(token + "==")), 16)


class InvitationTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        token = pairing.new_invitation_token()
        text = pairing.make_invitation("alpha", "192.168.1.20", 47830, GOOD_ID, token, NOW + 300)
        self.assertTrue(text.startswith("atp1-invite:"))
        self.assertNotIn("=", text)
        invitation = pairing.parse_invitation(text, NOW)
        self.assertEqual(invitation, pairing.Invitation("alpha", "192.168.1.20", 47830, GOOD_ID.split("-")[0], token, NOW + 300))

    def test_surrounding_whitespace_is_ignored(self) -> None:
        self.assertEqual(pairing.parse_invitation(f"  {invitation_text()}\n", NOW).name, "alpha")

    def test_expiry_window(self) -> None:
        self.assertEqual(pairing.parse_invitation(invitation_text(exp=NOW + 600), NOW).expires, NOW + 600)
        self.assertEqual(pairing.parse_invitation(invitation_text(exp=NOW + 1), NOW).expires, NOW + 1)
        for exp in (NOW, NOW - 1, NOW + 601, -1, True, "1700000300", 1.5):
            with self.assertRaises(pairing.PairingError, msg=repr(exp)):
                pairing.parse_invitation(invitation_text(exp=exp), NOW)

    def test_refused_text(self) -> None:
        good = invitation_text()
        body = good[len(pairing.INVITE_PREFIX) :]
        refused = [
            "",
            "atp1-invite:",
            body,
            "atp2-invite:" + body,
            "ATP1-INVITE:" + body,
            good + "=",
            good + " x",
            good[:-1] + "+",
            good[:-1] + "/",
            good[:-1] + "é",
            good[: len(good) // 2] + "\n" + good[len(good) // 2 :],
            good + "A" * 2000,
            pairing.INVITE_PREFIX + "A" * 1000,
            pairing.INVITE_PREFIX + "!!!!",
            pairing.INVITE_PREFIX + "A",
        ]
        for text in refused:
            with self.assertRaises(pairing.PairingError, msg=text[:40]):
                pairing.parse_invitation(text, NOW)

    def test_non_canonical_base64_is_refused(self) -> None:
        good = invitation_text()
        last = good[-1]
        swapped = "B" if last != "B" else "C"
        with self.assertRaises(pairing.PairingError):
            pairing.parse_invitation(good[:-1] + swapped, NOW)

    def test_refused_json(self) -> None:
        def wrap(raw: bytes) -> str:
            return pairing.INVITE_PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

        token = pairing.new_invitation_token()
        duplicate = f'{{"v":1,"v":1,"name":"a","address":"192.168.1.2","port":1,"id":"ABCDEFG","token":"{token}","exp":{NOW + 5}}}'
        for raw in (b"", b"[]", b"1", b"null", b"{", b'{"v":1}', b"\xff\xfe", duplicate.encode(), b"{}"):
            with self.assertRaises(pairing.PairingError, msg=repr(raw)):
                pairing.parse_invitation(wrap(raw), NOW)

    def test_refused_fields(self) -> None:
        token = pairing.new_invitation_token()
        cases: list[dict[str, Any]] = [
            {"v": 2},
            {"v": True},
            {"v": "1"},
            {"v": KeyError},
            {"extra": 1},
            {"name": ""},
            {"name": "x" * 33},
            {"name": "pair123456"},
            {"name": "café"},
            {"name": "tab\t"},
            {"name": 5},
            {"name": KeyError},
            {"port": 0},
            {"port": 65536},
            {"port": -1},
            {"port": "47830"},
            {"port": True},
            {"port": 1.5},
            {"port": None},
            {"address": "example.com"},
            {"address": "localhost"},
            {"address": "192.168.1"},
            {"address": "192.168.1.256"},
            {"address": "192.168.01.1"},
            {"address": "192.168.1.1/24"},
            {"address": "::1"},
            {"address": "192.168.1.1\n"},
            {"address": "١٩٢.١٦٨.١.١"},
            {"address": 3232235777},
            {"id": "abcdefg"},
            {"id": "ABCDEF"},
            {"id": "ABCDEFGH"},
            {"id": "ABCDEF1"},
            {"id": GOOD_ID},
            {"token": token[:-1]},
            {"token": token + "A"},
            {"token": "A" * 21 + "B"},
            {"token": "!" * 22},
            {"token": "ABC234"},
            {"token": 5},
        ]
        for change in cases:
            with self.assertRaises(pairing.PairingError, msg=repr(change)):
                pairing.parse_invitation(invitation_text(**change), NOW)

    def test_make_invitation_refuses_bad_fields(self) -> None:
        token = pairing.new_invitation_token()
        with self.assertRaises(pairing.PairingError):
            pairing.make_invitation("pair123456", "192.168.1.2", 1, GOOD_ID, token, NOW + 5)
        with self.assertRaises(pairing.PairingError):
            pairing.make_invitation("alpha", "host.local", 1, GOOD_ID, token, NOW + 5)
        with self.assertRaises(pairing.PairingError):
            pairing.make_invitation("alpha", "192.168.1.2", 1, GOOD_ID, "short", NOW + 5)


class LanDestinationTest(unittest.TestCase):
    def check(self, address: str, port: int = 47830, interface: str = INTERFACE, prefix: int = 24) -> str:
        return pairing.check_lan_destination(address, port, interface, prefix)

    def test_accepts_hosts_on_the_subnet(self) -> None:
        for address in ("192.168.1.20", "192.168.1.1", "192.168.1.254"):
            self.assertEqual(self.check(address), address)
        self.assertEqual(self.check("172.20.9.9", interface="172.20.5.9", prefix=16), "172.20.9.9")
        self.assertEqual(self.check("10.200.1.1", interface="10.1.2.3", prefix=8), "10.200.1.1")
        self.assertEqual(self.check("192.168.77.5", interface=INTERFACE, prefix=16), "192.168.77.5")

    def test_refuses_the_interface_network_and_broadcast_addresses(self) -> None:
        for address in ("192.168.1.10", "192.168.1.0", "192.168.1.255"):
            with self.assertRaises(pairing.PairingError, msg=address):
                self.check(address)
        with self.assertRaises(pairing.PairingError):
            self.check("10.255.255.255", interface="10.1.2.3", prefix=8)
        with self.assertRaises(pairing.PairingError):
            self.check("10.0.0.0", interface="10.1.2.3", prefix=8)

    def test_refuses_other_subnets_and_public_or_special_ranges(self) -> None:
        for address in (
            "192.168.2.20",
            "8.8.8.8",
            "127.0.0.1",
            "169.254.1.1",
            "100.64.0.5",
            "172.32.0.1",
            "172.15.255.1",
            "224.0.0.1",
            "0.0.0.0",
        ):
            with self.assertRaises(pairing.PairingError, msg=address):
                self.check(address)

    def test_refuses_names_and_odd_spellings(self) -> None:
        for address in (
            "pc.local",
            "localhost",
            "192.168.1.20.",
            "192.168.1",
            "::1",
            "192.168.001.020",
            "0xc0a80114",
            "3232235796",
            " 192.168.1.20",
        ):
            with self.assertRaises(pairing.PairingError, msg=address):
                self.check(address)

    def test_refuses_bad_ports(self) -> None:
        for port in (0, -1, 65536, True, "47830", None, 1.5):
            with self.assertRaises(pairing.PairingError, msg=repr(port)):
                self.check("192.168.1.20", port)  # type: ignore[arg-type]

    def test_refuses_bad_interfaces(self) -> None:
        for prefix in (7, 31, 32, 0, True, "24"):
            with self.assertRaises(pairing.PairingError, msg=repr(prefix)):
                self.check("192.168.1.20", prefix=prefix)  # type: ignore[arg-type]
        for interface in ("8.8.8.8", "100.64.1.1", "host", "172.32.0.1"):
            with self.assertRaises(pairing.PairingError, msg=interface):
                self.check("192.168.1.20", interface=interface)


class CertificateShapeTest(unittest.TestCase):
    def test_refuses_what_is_not_one_certificate(self) -> None:
        sequence = b"\x30\x03\x02\x01\x01"
        refused: list[object] = [
            None,
            5,
            "",
            "!!!!",
            b64(b"hello"),
            b64(sequence),
            b64(b"\x30\x80\x00\x00"),
            b64(b"\x30\x81\x05" + bytes(5)),
            b64(b"\x30\x82\x00\x05" + bytes(5)),
            b64(b"\x30\x05" + bytes(4)),
            b64(b"\x30\x05" + bytes(6)),
            b64(b"\x31\x03\x02\x01\x01"),
            b64(sequence) + "\n",
            b64(sequence)[:-1],
        ]
        for text in refused:
            with self.assertRaises(pairing.PairingError, msg=repr(text)):
                pairing.parse_certificate(text)


class LimitsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="iat-pair-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.now = [float(NOW)]

    def limits(self, pid: int = 4001, alive: Callable[[int], bool] = lambda _pid: True) -> pairing.Limits:
        return pairing.Limits(self.home, clock=lambda: self.now[0], pid=pid, alive=alive)

    def test_one_window_at_a_time(self) -> None:
        first, second = self.limits(4001), self.limits(4002)
        first.open_window()
        with self.assertRaisesRegex(pairing.Busy, "pairing busy"):
            second.open_window()
        with self.assertRaises(pairing.Busy):
            second.check()
        second.check_locked()
        first.close_window()
        second.check()
        second.open_window()

    def test_only_the_owner_closes_a_window(self) -> None:
        first, second = self.limits(4001), self.limits(4002)
        first.open_window()
        second.close_window()
        with self.assertRaises(pairing.Busy):
            second.open_window()

    def test_a_window_of_a_dead_process_does_not_block(self) -> None:
        self.limits(4001).open_window()
        self.limits(4002, alive=lambda pid: pid != 4001).open_window()

    def test_an_old_window_does_not_block(self) -> None:
        self.limits(4001).open_window()
        self.now[0] += pairing.OPEN_CAP_S + 1
        self.limits(4002).open_window()

    def test_three_windows_per_hour(self) -> None:
        limits = self.limits()
        for _ in range(3):
            limits.open_window()
            limits.close_window()
            self.now[0] += 60
        with self.assertRaisesRegex(pairing.Busy, "pairing busy"):
            limits.open_window()
        with self.assertRaises(pairing.Busy):
            limits.check()
        self.now[0] += 3600 - 180 - 1
        with self.assertRaises(pairing.Busy):
            limits.open_window()
        self.now[0] += 2
        limits.open_window()

    def test_a_refused_request_opens_nothing(self) -> None:
        limits = self.limits()
        for _ in range(3):
            limits.open_window()
            limits.close_window()
        for _ in range(3):
            with self.assertRaises(pairing.Busy):
                limits.open_window()
        state = json.loads((Path(self.home) / "net" / pairing.PAIRING_FILE).read_text(encoding="utf-8"))
        self.assertEqual(len(state["windows"]), 3)
        self.assertIsNone(state["open"])

    def test_three_failures_lock_for_an_hour(self) -> None:
        limits = self.limits()
        self.assertFalse(limits.record_failure())
        self.assertFalse(limits.record_failure())
        limits.check()
        self.assertTrue(limits.record_failure())
        for action in (limits.check, limits.check_locked, limits.open_window):
            with self.assertRaisesRegex(pairing.Locked, "net trust"):
                action()
        self.now[0] += 3599
        with self.assertRaises(pairing.Locked):
            limits.check_locked()
        self.now[0] += 2
        limits.check()
        limits.open_window()

    def test_failures_expire_after_an_hour(self) -> None:
        limits = self.limits()
        limits.record_failure()
        limits.record_failure()
        self.now[0] += 3601
        self.assertFalse(limits.record_failure())
        limits.check()

    def test_a_lock_clears_the_failure_count(self) -> None:
        limits = self.limits()
        for _ in range(3):
            limits.record_failure()
        self.now[0] += 3601
        self.assertFalse(limits.record_failure())
        self.assertFalse(limits.record_failure())
        self.assertTrue(limits.record_failure())

    def test_state_survives_a_new_instance(self) -> None:
        first = self.limits()
        first.open_window()
        for _ in range(3):
            first.record_failure()
        again = self.limits(4003)
        with self.assertRaises(pairing.Locked):
            again.check()
        self.now[0] += 3601
        again.open_window()

    def test_windows_count_across_instances(self) -> None:
        for pid in (4001, 4002, 4003):
            limits = self.limits(pid)
            limits.open_window()
            limits.close_window()
        with self.assertRaises(pairing.Busy):
            self.limits(4004).open_window()

    def test_a_damaged_file_is_refused_with_a_hint(self) -> None:
        self.limits().open_window()
        path = Path(self.home) / "net" / pairing.PAIRING_FILE
        for damaged in ("not json", "[]", '{"windows":[]}', '{"windows":"x","failures":[],"lockedUntil":0,"open":null}'):
            path.write_text(damaged, encoding="utf-8")
            with self.assertRaisesRegex(pairing.PairingError, "delete it"):
                self.limits().check()

    def test_file_holds_no_secrets(self) -> None:
        limits = self.limits()
        limits.open_window()
        limits.record_failure()
        text = (Path(self.home) / "net" / pairing.PAIRING_FILE).read_text(encoding="utf-8")
        self.assertEqual(set(json.loads(text)), {"windows", "failures", "lockedUntil", "open"})


class Side:
    def __init__(
        self,
        *,
        typing: str = "peer",
        approve: bool = True,
        save_error: Exception | None = None,
        gate: threading.Event | None = None,
    ) -> None:
        self.typing = typing
        self.provider: Callable[[], str] | None = None
        self.approve = approve
        self.save_error = save_error
        self.gate = gate
        self.code: str | None = None
        self.shown = threading.Event()
        self.other: Side | None = None
        self.saved: list[pairing.PeerInfo] = []
        self.reviews = 0

    def show_code(self, peer: pairing.PeerInfo, code: str) -> None:
        self.code = code
        self.shown.set()

    def read_code(self, peer: pairing.PeerInfo, timeout_s: float) -> str | None:
        if self.gate is not None:
            self.gate.wait(10)
        if self.provider is not None:
            return self.provider()
        if self.typing == "no":
            return None
        if self.typing == "timeout":
            time.sleep(timeout_s + 0.2)
            return None
        if self.typing == "wrong":
            return "000000" if self.code != "000000" else "111111"
        other = self.other
        assert other is not None and other.shown.wait(10), "the other window never showed its code"
        return other.code

    def review(self, peer: pairing.PeerInfo, timeout_s: float) -> bool:
        self.reviews += 1
        return self.approve

    def save(self, peer: pairing.PeerInfo) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.saved.append(peer)


class Machine:
    def __init__(self, test: unittest.TestCase, identity: pairing.PairIdentity, side: Side) -> None:
        self.identity = identity
        self.side = side
        self.home = tempfile.mkdtemp(prefix="iat-pair-")
        test.addCleanup(shutil.rmtree, self.home, True)
        self.limits = pairing.Limits(self.home)

    def failures(self) -> int:
        path = Path(self.home) / "net" / pairing.PAIRING_FILE
        return len(json.loads(path.read_text(encoding="utf-8"))["failures"]) if path.exists() else 0


class Window:
    def __init__(self, test: unittest.TestCase, machine: Machine, token: str = HELLO_TOKEN, timeouts: pairing.Timeouts = FAST) -> None:
        self.machine = machine
        self.token = token
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        test.addCleanup(self.sock.close)
        self.port: int = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.results: list[pairing.PairResult] = []
        self.timeouts = timeouts
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        test.addCleanup(self.stop.set)

    def _run(self) -> None:
        side = self.machine.side
        self.results.append(
            pairing.listen_once(
                self.sock,
                self.token,
                self.machine.identity,
                side,
                side.save,
                self.machine.limits,
                timeouts=self.timeouts,
                stop=self.stop,
            )
        )

    def dial(self, machine: Machine, token: str | None = None) -> pairing.PairResult:
        side = machine.side
        return pairing.dial(
            "127.0.0.1",
            self.port,
            token or self.token,
            machine.identity,
            side,
            side.save,
            machine.limits,
            timeouts=self.timeouts,
        )

    def end(self) -> pairing.PairResult:
        self.thread.join(30)
        if not self.results:
            self.stop.set()
            self.thread.join(10)
        self.assert_ended()
        return self.results[0]

    def assert_ended(self) -> None:
        assert not self.thread.is_alive(), "the window never ended"
        assert self.results, "the window returned nothing"

    def close(self) -> pairing.PairResult:
        self.stop.set()
        return self.end()


class Rogue:
    def __init__(self, port: int) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)

    def send(self, frame: object) -> None:
        wire.write_frame(self.sock, wire.encode(frame))

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def recv(self) -> dict[str, Any]:
        value = wire.parse_json(wire.read_frame(self.sock, max_size=pairing.MAX_FRAME))
        assert isinstance(value, dict)
        return value

    def closed(self) -> bool:
        self.sock.settimeout(5)
        try:
            return self.sock.recv(1) == b""
        except (ConnectionResetError, ConnectionAbortedError):
            return True

    def close(self) -> None:
        self.sock.close()


def dialer_hello(identity: pairing.PairIdentity, nonce: bytes, token: str = HELLO_TOKEN, **changes: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "t": "hello",
        "v": 1,
        "token": token,
        "name": identity.name,
        "cert": b64(identity.der),
        "commit": pairing.commitment(identity.der, nonce).hex(),
    }
    for key, value in changes.items():
        if value is KeyError:
            del frame[key]
        else:
            frame[key] = value
    return frame


def pair_identity(identity: Identity, name: str, *, key: Identity | None = None) -> pairing.PairIdentity:
    source = key or identity
    return pairing.PairIdentity(
        name=name,
        der=ssl.PEM_cert_to_DER_cert(identity.pem),
        cert=identity.cert,
        key=source.key,
        password=lambda: source.password,
    )


@unittest.skipUnless(TLS_READY, "needs Git's openssl and a TLS 1.3 Python")
class PairingSessionTest(unittest.TestCase):
    a: Identity
    b: Identity
    c: Identity

    @classmethod
    def setUpClass(cls) -> None:
        folder = tempfile.TemporaryDirectory(prefix="iat-pairing-")
        cls.addClassCleanup(folder.cleanup)
        cls.a, cls.b, cls.c = (make_identity(Path(folder.name), name) for name in ("a", "b", "c"))

    def machines(
        self, listener: Side | None = None, dialer: Side | None = None, *, key_of_listener: Identity | None = None
    ) -> tuple[Machine, Machine]:
        listening, dialing = listener or Side(), dialer or Side()
        listening.other, dialing.other = dialing, listening
        return (
            Machine(self, pair_identity(self.a, "alpha", key=key_of_listener), listening),
            Machine(self, pair_identity(self.b, "bravo"), dialing),
        )

    def run_pair(
        self, listener: Side | None = None, dialer: Side | None = None
    ) -> tuple[Machine, Machine, pairing.PairResult, pairing.PairResult]:
        lm, dm = self.machines(listener, dialer)
        window = Window(self, lm)
        dialed = window.dial(dm)
        return lm, dm, window.end(), dialed

    def test_success_on_both_sides(self) -> None:
        lm, dm, listened, dialed = self.run_pair()
        self.assertEqual((listened.outcome, dialed.outcome), ("paired", "paired"))
        self.assertEqual(len(lm.side.saved), 1)
        self.assertEqual(len(dm.side.saved), 1)
        saved_by_listener, saved_by_dialer = lm.side.saved[0], dm.side.saved[0]
        self.assertEqual(saved_by_listener.name, "bravo")
        self.assertEqual(saved_by_listener.device_id, device_id(dm.identity.der))
        self.assertEqual(saved_by_listener.der, dm.identity.der)
        self.assertEqual(saved_by_listener.address, "127.0.0.1")
        self.assertEqual(saved_by_dialer.name, "alpha")
        self.assertEqual(saved_by_dialer.der, lm.identity.der)
        self.assertEqual(saved_by_dialer.address, "127.0.0.1")
        self.assertEqual((lm.side.reviews, dm.side.reviews), (1, 1))
        self.assertEqual(lm.side.code, dm.side.code)
        self.assertEqual((lm.failures(), dm.failures()), (0, 0))

    def test_the_peer_pem_is_the_hello_certificate(self) -> None:
        lm, dm, _listened, _dialed = self.run_pair()
        self.assertEqual(lm.side.saved[0].pem, self.b.pem.replace("\r\n", "\n"))
        self.assertEqual(dm.side.saved[0].pem, self.a.pem.replace("\r\n", "\n"))

    def test_digits_never_appear_in_results(self) -> None:
        lm, dm, listened, dialed = self.run_pair(dialer=Side(typing="wrong"))
        code = lm.side.code
        assert code is not None and dm.side.code == code
        for result in (listened, dialed):
            self.assertNotIn(code, repr(result))

    def test_wrong_code_on_the_dialer_ends_both_sides(self) -> None:
        lm, dm, listened, dialed = self.run_pair(dialer=Side(typing="wrong"))
        self.assertEqual(dialed.outcome, "failed")
        self.assertEqual(listened.outcome, "failed")
        self.assertEqual((lm.side.saved, dm.side.saved), ([], []))
        self.assertEqual((lm.side.reviews, dm.side.reviews), (0, 0))
        self.assertEqual((lm.failures(), dm.failures()), (0, 1))

    def test_wrong_code_on_the_listener_ends_both_sides(self) -> None:
        lm, dm, listened, dialed = self.run_pair(listener=Side(typing="wrong"))
        self.assertEqual((listened.outcome, dialed.outcome), ("failed", "failed"))
        self.assertEqual((lm.side.saved, dm.side.saved), ([], []))
        self.assertEqual((lm.failures(), dm.failures()), (1, 0))

    def test_no_at_the_code_declines_and_counts(self) -> None:
        lm, dm, listened, dialed = self.run_pair(listener=Side(typing="no"))
        self.assertEqual(listened.outcome, "declined")
        self.assertEqual(dialed.outcome, "failed")
        self.assertEqual((lm.side.saved, dm.side.saved), ([], []))
        self.assertEqual((lm.failures(), dm.failures()), (1, 0))

    def test_a_third_failure_reports_the_lock(self) -> None:
        lm, dm = self.machines(dialer=Side(typing="wrong"))
        dm.limits.record_failure()
        dm.limits.record_failure()
        window = Window(self, lm)
        dialed = window.dial(dm)
        window.end()
        self.assertTrue(dialed.locked)
        with self.assertRaises(pairing.Locked):
            dm.limits.check()
        with self.assertRaises(pairing.Locked):
            pairing.dial("127.0.0.1", window.port, HELLO_TOKEN, dm.identity, dm.side, dm.side.save, dm.limits)

    def test_a_typing_timeout_fails_without_counting(self) -> None:
        short = dataclasses.replace(FAST, typing_s=1.0)
        lm, dm = self.machines(listener=Side(typing="timeout"))
        window = Window(self, lm, timeouts=short)
        dialed = window.dial(dm)
        listened = window.end()
        self.assertEqual((listened.outcome, dialed.outcome), ("failed", "failed"))
        self.assertIn("timed out", listened.reason)
        self.assertEqual((lm.failures(), dm.failures()), (0, 0))
        self.assertEqual((lm.side.saved, dm.side.saved), ([], []))

    def test_no_at_the_review_leaves_that_side_unsaved(self) -> None:
        lm, dm, listened, dialed = self.run_pair(listener=Side(approve=False))
        self.assertEqual(listened.outcome, "declined")
        self.assertEqual(lm.side.saved, [])
        self.assertEqual(lm.failures(), 1)
        self.assertEqual(dialed.outcome, "incomplete")
        self.assertIn("Pairing may be incomplete on the other machine", dialed.reason)
        self.assertEqual(len(dm.side.saved), 1)

    def test_no_at_the_review_on_the_dialer(self) -> None:
        lm, dm, listened, dialed = self.run_pair(dialer=Side(approve=False))
        self.assertEqual(dialed.outcome, "declined")
        self.assertEqual(dm.side.saved, [])
        self.assertEqual(listened.outcome, "incomplete")
        self.assertEqual(len(lm.side.saved), 1)
        self.assertEqual(dm.failures(), 1)

    def test_no_on_both_sides_saves_nothing(self) -> None:
        lm, dm, listened, dialed = self.run_pair(Side(approve=False), Side(approve=False))
        self.assertEqual((listened.outcome, dialed.outcome), ("declined", "declined"))
        self.assertEqual((lm.side.saved, dm.side.saved), ([], []))

    def test_a_save_that_fails_is_not_reported_as_saved(self) -> None:
        _lm, dm, listened, dialed = self.run_pair(listener=Side(save_error=OSError("disk full")))
        self.assertEqual(listened.outcome, "failed")
        self.assertIn("disk full", listened.reason)
        self.assertEqual(dialed.outcome, "incomplete")
        self.assertEqual(len(dm.side.saved), 1)

    def test_wrong_token_is_dropped_without_counting(self) -> None:
        lm, dm = self.machines()
        window = Window(self, lm)
        rogue = Rogue(window.port)
        rogue.send(dialer_hello(dm.identity, bytes(32), token="ZZZZZZ"))
        self.assertTrue(rogue.closed())
        wrong = window.dial(dm, token="ZZZZZ2")
        self.assertEqual(wrong.outcome, "failed")
        self.assertFalse(lm.side.shown.is_set())
        self.assertEqual((lm.failures(), dm.failures()), (0, 0))
        dialed = window.dial(dm)
        self.assertEqual(dialed.outcome, "paired")
        self.assertEqual(window.end().outcome, "paired")

    def test_a_non_hello_first_frame_is_dropped(self) -> None:
        lm, dm = self.machines()
        window = Window(self, lm)
        for first in ({"t": "reveal", "nonce": "00" * 32}, [], "hello", {"token": HELLO_TOKEN}, None):
            rogue = Rogue(window.port)
            rogue.send(first)
            self.assertTrue(rogue.closed(), repr(first))
        rogue = Rogue(window.port)
        rogue.send_raw(b"\x00\x00\x00\x00")
        self.assertTrue(rogue.closed())
        self.assertEqual(window.dial(dm).outcome, "paired")
        self.assertEqual(window.end().outcome, "paired")

    def refuse_then_pair(self, send: Callable[[Rogue, pairing.PairIdentity], None], *, replied: bool = False) -> None:
        lm, dm = self.machines()
        window = Window(self, lm)
        rogue = Rogue(window.port)
        send(rogue, dm.identity)
        if replied:
            self.assertEqual(rogue.recv()["t"], "hello")
        self.assertTrue(rogue.closed())
        self.assertFalse(lm.side.shown.is_set())
        self.assertEqual(lm.failures(), 0)
        self.assertEqual(window.dial(dm).outcome, "paired")
        self.assertEqual(window.end().outcome, "paired")

    def test_unknown_key_in_hello_is_refused(self) -> None:
        self.refuse_then_pair(lambda rogue, ident: rogue.send(dialer_hello(ident, bytes(32), extra=1)))

    def test_missing_key_in_hello_is_refused(self) -> None:
        for key in ("v", "name", "cert", "commit"):
            missing: dict[str, Any] = {key: KeyError}
            self.refuse_then_pair(lambda rogue, ident, missing=missing: rogue.send(dialer_hello(ident, bytes(32), **missing)))

    def test_bad_hello_values_are_refused(self) -> None:
        bad: list[dict[str, Any]] = [
            {"v": 2},
            {"v": True},
            {"name": "pair123456"},
            {"name": ""},
            {"commit": "00" * 31},
            {"commit": "AB" * 32},
            {"cert": ""},
            {"cert": b64(b"not a certificate")},
            {"t": "reveal"},
        ]
        for change in bad:
            self.refuse_then_pair(lambda rogue, ident, change=change: rogue.send(dialer_hello(ident, bytes(32), **change)))

    def test_a_certificate_with_trailing_bytes_is_refused(self) -> None:
        def send(rogue: Rogue, ident: pairing.PairIdentity) -> None:
            rogue.send(dialer_hello(ident, bytes(32), cert=b64(ident.der + b"\x00")))

        self.refuse_then_pair(send)

    def test_the_listener_refuses_its_own_certificate(self) -> None:
        lm, dm = self.machines()
        window = Window(self, lm)
        rogue = Rogue(window.port)
        rogue.send(dialer_hello(lm.identity, bytes(32)))
        self.assertTrue(rogue.closed())
        self.assertEqual(window.dial(dm).outcome, "paired")
        window.end()

    def test_duplicate_keys_are_refused(self) -> None:
        def send(rogue: Rogue, ident: pairing.PairIdentity) -> None:
            body = wire.encode(dialer_hello(ident, bytes(32))).decode("utf-8")
            rogue.send_raw(wire_frame(body[:-1] + ',"v":1}'))

        self.refuse_then_pair(send)

    def test_trailing_bytes_inside_a_frame_are_refused(self) -> None:
        def send(rogue: Rogue, ident: pairing.PairIdentity) -> None:
            rogue.send_raw(wire_frame(wire.encode(dialer_hello(ident, bytes(32))).decode("utf-8") + " {}"))

        self.refuse_then_pair(send)

    def test_a_frame_that_follows_the_hello_at_once_is_refused(self) -> None:
        def send(rogue: Rogue, ident: pairing.PairIdentity) -> None:
            frames = wire_frame(wire.encode(dialer_hello(ident, bytes(32))).decode("utf-8")) + wire_frame(
                '{"t":"reveal","nonce":"' + "00" * 32 + '"}'
            )
            rogue.send_raw(frames)

        self.refuse_then_pair(send)

    def test_oversize_first_frame_is_refused(self) -> None:
        def send(rogue: Rogue, ident: pairing.PairIdentity) -> None:
            rogue.send_raw((pairing.MAX_FRAME + 1).to_bytes(4, "big"))

        self.refuse_then_pair(send)

    def test_out_of_order_frame_is_refused(self) -> None:
        def send(rogue: Rogue, ident: pairing.PairIdentity) -> None:
            rogue.send(dialer_hello(ident, bytes(32)))
            rogue.recv()
            rogue.send({"t": "tls"})

        self.refuse_then_pair(send)

    def test_a_second_hello_instead_of_the_reveal_is_refused(self) -> None:
        def send(rogue: Rogue, ident: pairing.PairIdentity) -> None:
            rogue.send(dialer_hello(ident, bytes(32)))
            rogue.recv()
            rogue.send(dialer_hello(ident, bytes(32)))

        self.refuse_then_pair(send)

    def test_reveal_with_an_unknown_key_is_refused(self) -> None:
        def send(rogue: Rogue, ident: pairing.PairIdentity) -> None:
            rogue.send(dialer_hello(ident, bytes(32)))
            rogue.recv()
            rogue.send({"t": "reveal", "nonce": "00" * 32, "extra": 1})

        self.refuse_then_pair(send)

    def test_oversize_reveal_is_refused(self) -> None:
        def send(rogue: Rogue, ident: pairing.PairIdentity) -> None:
            rogue.send(dialer_hello(ident, bytes(32)))
            rogue.recv()
            rogue.send_raw((pairing.MAX_FRAME + 1).to_bytes(4, "big"))

        self.refuse_then_pair(send)

    def test_reveal_that_does_not_match_the_commitment_is_refused(self) -> None:
        def send(rogue: Rogue, ident: pairing.PairIdentity) -> None:
            rogue.send(dialer_hello(ident, bytes(32)))
            rogue.recv()
            rogue.send({"t": "reveal", "nonce": bytes([1] * 32).hex()})

        self.refuse_then_pair(send)

    def test_reveal_for_another_certificate_is_refused(self) -> None:
        lm, dm = self.machines()
        other = pair_identity(self.c, "charlie")

        def send(rogue: Rogue, ident: pairing.PairIdentity) -> None:
            nonce = bytes([7] * 32)
            frame = dialer_hello(ident, nonce)
            frame["commit"] = pairing.commitment(other.der, nonce).hex()
            rogue.send(frame)
            rogue.recv()
            rogue.send({"t": "reveal", "nonce": nonce.hex()})

        window = Window(self, lm)
        rogue = Rogue(window.port)
        send(rogue, dm.identity)
        self.assertTrue(rogue.closed())
        self.assertEqual(window.dial(dm).outcome, "paired")
        window.end()

    def test_an_abandoned_session_before_the_nonces_keeps_the_window(self) -> None:
        lm, dm = self.machines()
        window = Window(self, lm)
        rogue = Rogue(window.port)
        rogue.send(dialer_hello(dm.identity, bytes(32)))
        rogue.recv()
        rogue.close()
        self.assertEqual(window.dial(dm).outcome, "paired")
        window.end()
        self.assertEqual((lm.failures(), dm.failures()), (0, 0))

    def test_extra_plaintext_after_the_reveal_ends_the_session(self) -> None:
        lm, dm = self.machines()
        window = Window(self, lm)
        rogue = Rogue(window.port)
        nonce = bytes([9] * 32)
        rogue.send(dialer_hello(dm.identity, nonce))
        reply = rogue.recv()
        secret = pairing.sas_secret(dm.identity.der, lm.identity.der, nonce, bytes.fromhex(reply["nonce"]))
        lm.side.provider = lambda: pairing.digits(secret)
        rogue.send({"t": "reveal", "nonce": nonce.hex()})
        rogue.send({"t": "junk"})
        self.assertEqual(window.end().outcome, "failed")
        self.assertEqual(lm.side.code, pairing.digits(secret))
        self.assertEqual(lm.side.saved, [])
        rogue.close()

    def test_a_session_after_the_nonces_uses_up_the_window(self) -> None:
        lm, dm = self.machines(listener=Side(typing="no"))
        window = Window(self, lm)
        dialed = window.dial(dm)
        listened = window.end()
        self.assertEqual(listened.outcome, "declined")
        self.assertEqual(dialed.outcome, "failed")
        self.assertFalse(window.thread.is_alive())

    def test_a_second_dialer_is_refused_without_affecting_the_first(self) -> None:
        gate = threading.Event()
        lm, dm = self.machines(listener=Side(gate=gate))
        intruder = Machine(self, pair_identity(self.c, "charlie"), Side())
        window = Window(self, lm)
        first: list[pairing.PairResult] = []
        thread = threading.Thread(target=lambda: first.append(window.dial(dm)), daemon=True)
        thread.start()
        self.assertTrue(lm.side.shown.wait(10))
        second = window.dial(intruder)
        self.assertEqual(second.outcome, "failed")
        self.assertFalse(intruder.side.shown.is_set())
        gate.set()
        thread.join(30)
        self.assertEqual(first[0].outcome, "paired")
        self.assertEqual(window.end().outcome, "paired")
        self.assertEqual(intruder.failures(), 0)

    def test_connections_faster_than_one_per_second_are_dropped(self) -> None:
        slow = dataclasses.replace(FAST, min_interval_s=1.0)
        lm, dm = self.machines()
        window = Window(self, lm, timeouts=slow)
        Rogue(window.port)
        self.assertEqual(window.dial(dm).outcome, "failed")
        time.sleep(1.2)
        self.assertEqual(window.dial(dm).outcome, "paired")
        window.end()

    def test_the_window_closes_at_its_timeout(self) -> None:
        brief = dataclasses.replace(FAST, window_s=0.5)
        lm, _dm = self.machines()
        window = Window(self, lm, timeouts=brief)
        result = window.end()
        self.assertEqual(result.outcome, "failed")
        self.assertIn("window closed", result.reason)

    def test_the_first_frame_must_arrive_in_time(self) -> None:
        quick = dataclasses.replace(FAST, first_frame_s=0.3)
        lm, dm = self.machines()
        window = Window(self, lm, timeouts=quick)
        rogue = Rogue(window.port)
        self.assertTrue(rogue.closed())
        self.assertEqual(window.dial(dm).outcome, "paired")
        window.end()

    def test_the_exchange_must_reach_the_digits_in_time(self) -> None:
        quick = dataclasses.replace(FAST, digits_s=0.5)
        lm, dm = self.machines()
        window = Window(self, lm, timeouts=quick)
        rogue = Rogue(window.port)
        rogue.send(dialer_hello(dm.identity, bytes(32)))
        rogue.recv()
        self.assertTrue(rogue.closed())
        self.assertFalse(lm.side.shown.is_set())
        window.close()

    def test_dial_refuses_names_and_bad_ports_before_connecting(self) -> None:
        lm, dm = self.machines()
        for address in ("localhost", "127.0.0.1.", "::1", "example.com"):
            with self.assertRaises(pairing.PairingError, msg=address):
                pairing.dial(address, 47830, HELLO_TOKEN, dm.identity, dm.side, dm.side.save, dm.limits)
        with self.assertRaises(pairing.PairingError):
            pairing.dial("127.0.0.1", 0, HELLO_TOKEN, dm.identity, dm.side, dm.side.save, dm.limits)
        self.assertEqual(lm.side.saved, [])

    def test_dial_reports_a_refused_connection(self) -> None:
        _lm, dm = self.machines()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        result = pairing.dial("127.0.0.1", port, HELLO_TOKEN, dm.identity, dm.side, dm.side.save, dm.limits, timeouts=FAST)
        self.assertEqual(result.outcome, "failed")
        self.assertIn("cannot connect", result.reason)

    def test_a_malformed_token_is_refused_locally(self) -> None:
        lm, dm = self.machines()
        with socket.socket() as unused, self.assertRaises(pairing.PairingError):
            pairing.listen_once(unused, "bad token", lm.identity, lm.side, lm.side.save, lm.limits)
        result = Window(self, lm).dial(dm, token="bad token")
        self.assertEqual(result.outcome, "failed")

    def test_certificate_parser_accepts_a_real_certificate(self) -> None:
        der = ssl.PEM_cert_to_DER_cert(self.a.pem)
        self.assertEqual(pairing.parse_certificate(b64(der)), der)
        for broken in (der + b"\x00", der[:-1], der[:-1] + bytes([der[-1] ^ 1])[:0]):
            with self.assertRaises(pairing.PairingError):
                pairing.parse_certificate(b64(broken))

    def test_wrong_key_on_the_listener_fails_both_sides(self) -> None:
        listening, dialing = Side(), Side()
        listening.other, dialing.other = dialing, listening
        lm = Machine(self, pair_identity(self.a, "alpha", key=self.c), listening)
        dm = Machine(self, pair_identity(self.b, "bravo"), dialing)
        window = Window(self, lm)
        dialed = window.dial(dm)
        listened = window.end()
        self.assertEqual((listened.outcome, dialed.outcome), ("failed", "failed"))
        self.assertEqual((listening.saved, dialing.saved, listening.reviews, dialing.reviews), ([], [], 0, 0))

    def test_wrong_key_on_the_dialer_fails_both_sides(self) -> None:
        listening, dialing = Side(), Side()
        listening.other, dialing.other = dialing, listening
        lm = Machine(self, pair_identity(self.a, "alpha"), listening)
        dm = Machine(self, pair_identity(self.b, "bravo", key=self.c), dialing)
        window = Window(self, lm)
        dialed = window.dial(dm)
        listened = window.end()
        self.assertEqual((listened.outcome, dialed.outcome), ("failed", "failed"))
        self.assertEqual((listening.saved, dialing.saved, listening.reviews, dialing.reviews), ([], [], 0, 0))

    def attack(self, attacker: str, kind: str) -> None:
        context_attr = "client_context" if attacker == "dialer" else "server_context"
        honest_attr = "server_context" if attacker == "dialer" else "client_context"
        real_attacker = REAL_CLIENT if attacker == "dialer" else REAL_SERVER
        real_honest = REAL_SERVER if attacker == "dialer" else REAL_CLIENT
        stranger = self.c

        def certless(cert: Path, key: Path, password: Callable[[], bytes], trusted: list[str]) -> ssl.SSLContext:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT if attacker == "dialer" else ssl.PROTOCOL_TLS_SERVER)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_REQUIRED
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            context.load_verify_locations(cadata="\n".join(trusted))
            return context

        def impostor(cert: Path, key: Path, password: Callable[[], bytes], trusted: list[str]) -> ssl.SSLContext:
            return real_attacker(stranger.cert, stranger.key, lambda: stranger.password, trusted)

        def widened(cert: Path, key: Path, password: Callable[[], bytes], trusted: list[str]) -> ssl.SSLContext:
            return real_honest(cert, key, password, [*trusted, stranger.pem])

        listening, dialing = Side(), Side()
        listening.other, dialing.other = dialing, listening
        lm = Machine(self, pair_identity(self.a, "alpha"), listening)
        dm = Machine(self, pair_identity(self.b, "bravo"), dialing)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(tls, context_attr, certless if kind == "absent" else impostor))
            if kind == "substituted":
                stack.enter_context(mock.patch.object(tls, honest_attr, widened))
            window = Window(self, lm)
            dialed = window.dial(dm)
            listened = window.end()
        self.assertEqual((listened.outcome, dialed.outcome), ("failed", "failed"), (attacker, kind, listened, dialed))
        self.assertEqual((listening.saved, dialing.saved), ([], []))
        self.assertEqual((listening.reviews, dialing.reviews), (0, 0))
        if kind == "substituted":
            honest = listened if attacker == "dialer" else dialed
            self.assertIn("announced", honest.reason)

    def test_an_absent_certificate_is_refused_by_both_sides(self) -> None:
        for attacker in ("dialer", "listener"):
            self.attack(attacker, "absent")

    def test_an_untrusted_certificate_is_refused_by_both_sides(self) -> None:
        for attacker in ("dialer", "listener"):
            self.attack(attacker, "untrusted")

    def test_a_substituted_certificate_is_refused_by_both_sides_even_when_tls_accepts_it(self) -> None:
        for attacker in ("dialer", "listener"):
            self.attack(attacker, "substituted")

    def test_the_substituted_certificate_is_accepted_by_tls_alone(self) -> None:
        stranger = self.c
        server = REAL_SERVER(self.a.cert, self.a.key, lambda: self.a.password, [self.b.pem, stranger.pem])
        client = REAL_CLIENT(stranger.cert, stranger.key, lambda: stranger.password, [self.a.pem])
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        served: list[bytes] = []

        def serve() -> None:
            raw, _ = listener.accept()
            with server.wrap_socket(raw, server_side=True) as conn:
                served.append(conn.getpeercert(binary_form=True) or b"")

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        with socket.create_connection(listener.getsockname(), timeout=5) as raw, client.wrap_socket(raw) as conn:
            self.assertEqual(conn.version(), "TLSv1.3")
        thread.join(5)
        self.assertEqual(served, [ssl.PEM_cert_to_DER_cert(stranger.pem)])


if __name__ == "__main__":
    unittest.main()
