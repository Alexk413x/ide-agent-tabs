from __future__ import annotations

import base64
import contextlib
import dataclasses
import hashlib
import json
import os
import secrets
import shutil
import socket
import ssl
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from ide_agent_tabs.net import pairing, tls, wire
from ide_agent_tabs.net.identity import device_id, format_digest
from test_net_tls import OPENSSL, Identity, make_identity

NOW = 1_700_000_000
GOOD_ID = format_digest(bytes(range(32)))
INTERFACE = "192.168.1.10"
TOKEN = base64.urlsafe_b64encode(bytes(range(16))).rstrip(b"=").decode("ascii")
OTHER_TOKEN = base64.urlsafe_b64encode(bytes(range(16, 32))).rstrip(b"=").decode("ascii")
FAST = pairing.Timeouts(
    first_frame_s=2.0,
    handshake_s=10.0,
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
        "v": 2,
        "name": "alpha",
        "address": "192.168.1.20",
        "port": 47830,
        "id": GOOD_ID,
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
    SECRET = "bd4bddb4cdd3068d6ed8439c0874ade5045394963cb8299f994e818dd17c1c16"

    def test_h_prefixes_each_field_with_its_length(self) -> None:
        expected = hashlib.sha256(b"\x00\x00\x00\x01a\x00\x00\x00\x02bc").digest()
        self.assertEqual(pairing.H(b"a", b"bc"), expected)
        self.assertNotEqual(pairing.H(b"ab", b"c"), expected)
        self.assertEqual(pairing.H().hex(), hashlib.sha256(b"").hexdigest())

    def test_commitment_vector(self) -> None:
        commit = pairing.commitment(self.CERT_D, self.NONCE_D)
        self.assertEqual(commit.hex(), "5e75760c314d95259c42fbc990f524a8f89b56987bb0d6a370bb5a4f131cf01d")

    def test_session_secret_vector(self) -> None:
        secret = pairing.session_secret(self.CERT_D, self.CERT_L, self.NONCE_D, self.NONCE_L)
        self.assertEqual(secret.hex(), self.SECRET)

    def test_the_secret_depends_on_every_input(self) -> None:
        base = pairing.session_secret(self.CERT_D, self.CERT_L, self.NONCE_D, self.NONCE_L)
        self.assertNotEqual(base, pairing.session_secret(self.CERT_L, self.CERT_D, self.NONCE_D, self.NONCE_L))
        self.assertNotEqual(base, pairing.session_secret(self.CERT_D, self.CERT_L, self.NONCE_L, self.NONCE_D))
        self.assertNotEqual(base, pairing.session_secret(self.CERT_D, self.CERT_L, self.NONCE_D, bytes(32)))

    def test_token_tag_vector(self) -> None:
        self.assertEqual(pairing.token_tag(TOKEN), "b1946bede8d280b6e235b30ea626a253a5d912432565cbf109bb8441003d5b9a")

    def test_token_tag_is_not_the_token_and_depends_on_it(self) -> None:
        tag = pairing.token_tag(TOKEN)
        self.assertRegex(tag, r"^[0-9a-f]{64}$")
        self.assertNotIn(TOKEN, tag)
        self.assertNotEqual(tag, pairing.token_tag(OTHER_TOKEN))

    def test_consent_proof_vectors(self) -> None:
        secret = bytes.fromhex(self.SECRET)
        dialer = pairing.consent_proof("dialer", secret, TOKEN)
        listener = pairing.consent_proof("listener", secret, TOKEN)
        self.assertEqual(dialer.hex(), "2044b4ca837a306a9a21a1135f9ba8fc52b5c1dca62ff07d21fb2a6705f0691a")
        self.assertEqual(listener.hex(), "f09fecb01fba6b904c441423020450210bf2f1bba7baa78040b3d9bdcc02145b")

    def test_consent_proof_binds_the_role_the_secret_and_the_token(self) -> None:
        secret = bytes.fromhex(self.SECRET)
        base = pairing.consent_proof("dialer", secret, TOKEN)
        self.assertNotEqual(base, pairing.consent_proof("listener", secret, TOKEN))
        self.assertNotEqual(base, pairing.consent_proof("dialer", bytes(32), TOKEN))
        self.assertNotEqual(base, pairing.consent_proof("dialer", secret, OTHER_TOKEN))

    def test_the_labels_keep_the_hashes_apart(self) -> None:
        secret = bytes.fromhex(self.SECRET)
        values = {
            pairing.commitment(self.CERT_D, self.NONCE_D),
            pairing.session_secret(self.CERT_D, self.CERT_L, self.NONCE_D, self.NONCE_L),
            bytes.fromhex(pairing.token_tag(TOKEN)),
            pairing.consent_proof("dialer", secret, TOKEN),
        }
        self.assertEqual(len(values), 4)


class NameAndTokenTest(unittest.TestCase):
    def test_device_names(self) -> None:
        for good in ("alpha", "My PC-2.local_x", "a", "x" * 32, "12345", "12345a6", "Room 101"):
            self.assertTrue(pairing.valid_device_name(good), good)
        for bad in ("", "x" * 33, "café", "tab\there", "123456", "a1234567b", "name!", "line\nbreak", "１２３４５６"):
            self.assertFalse(pairing.valid_device_name(bad), repr(bad))

    def test_invitation_token_is_128_bits(self) -> None:
        token = pairing.new_invitation_token()
        self.assertEqual(len(token), 22)
        self.assertEqual(len(base64.urlsafe_b64decode(token + "==")), 16)

    def test_full_device_id_returns_the_canonical_form(self) -> None:
        self.assertEqual(pairing.full_device_id(GOOD_ID), GOOD_ID)
        self.assertEqual(len(GOOD_ID.replace("-", "")), 56)

    def test_full_device_id_refuses_anything_else(self) -> None:
        compact = GOOD_ID.replace("-", "")
        wrong_check = compact[:13] + ("A" if compact[13] != "A" else "B") + compact[14:]
        refused: list[object] = [
            None,
            5,
            b"abc",
            "",
            GOOD_ID.split("-")[0],
            GOOD_ID.lower(),
            compact,
            GOOD_ID.replace("-", " "),
            " " + GOOD_ID,
            GOOD_ID + "\n",
            "-".join(compact[start : start + 14] for start in range(0, 56, 14)),
            GOOD_ID[:-8],
            GOOD_ID + "-AAAAAAA",
            "-".join(wrong_check[start : start + 7] for start in range(0, 56, 7)),
            GOOD_ID[:-1] + "1",
            GOOD_ID[:-1] + "!",
        ]
        for value in refused:
            with self.assertRaises(pairing.PairingError, msg=repr(value)):
                pairing.full_device_id(value)


class InvitationTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        token = pairing.new_invitation_token()
        text = pairing.make_invitation("alpha", "192.168.1.20", 47830, GOOD_ID, token, NOW + 300)
        self.assertTrue(text.startswith("atp1-invite:"))
        self.assertNotIn("=", text)
        invitation = pairing.parse_invitation(text, NOW)
        self.assertEqual(invitation, pairing.Invitation("alpha", "192.168.1.20", 47830, GOOD_ID, token, NOW + 300))

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
        duplicate = f'{{"v":2,"v":2,"name":"a","address":"192.168.1.2","port":1,"id":"{GOOD_ID}","token":"{token}","exp":{NOW + 5}}}'
        for raw in (b"", b"[]", b"1", b"null", b"{", b'{"v":2}', b"\xff\xfe", duplicate.encode(), b"{}"):
            with self.assertRaises(pairing.PairingError, msg=repr(raw)):
                pairing.parse_invitation(wrap(raw), NOW)

    def test_refused_fields(self) -> None:
        token = pairing.new_invitation_token()
        cases: list[dict[str, Any]] = [
            {"v": 1},
            {"v": 3},
            {"v": True},
            {"v": "2"},
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
            {"id": GOOD_ID.split("-")[0]},
            {"id": "abcdefg"},
            {"id": "ABCDEFGH"},
            {"id": GOOD_ID.lower()},
            {"id": GOOD_ID.replace("-", "")},
            {"id": GOOD_ID[:-8]},
            {"id": GOOD_ID + "-AAAAAAA"},
            {"id": GOOD_ID[:-1] + "1"},
            {"id": " " + GOOD_ID},
            {"id": 5},
            {"id": None},
            {"id": KeyError},
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
        for bad_id in (GOOD_ID.split("-")[0], GOOD_ID.lower(), GOOD_ID.replace("-", ""), ""):
            with self.assertRaises(pairing.PairingError, msg=bad_id):
                pairing.make_invitation("alpha", "192.168.1.2", 1, bad_id, token, NOW + 5)

    def test_a_version_1_invitation_is_refused(self) -> None:
        with self.assertRaisesRegex(pairing.PairingError, "version"):
            pairing.parse_invitation(invitation_text(v=1), NOW)


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
    def __init__(self, *, save_error: Exception | None = None, gate: threading.Event | None = None) -> None:
        self.save_error = save_error
        self.gate = gate
        self.saving = threading.Event()
        self.saved: list[pairing.PeerInfo] = []

    def save(self, peer: pairing.PeerInfo) -> None:
        self.saving.set()
        if self.gate is not None:
            self.gate.wait(10)
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

    @property
    def device_id(self) -> str:
        return device_id(self.identity.der)

    def failures(self) -> int:
        path = Path(self.home) / "net" / pairing.PAIRING_FILE
        return len(json.loads(path.read_text(encoding="utf-8"))["failures"]) if path.exists() else 0


class Window:
    def __init__(self, test: unittest.TestCase, machine: Machine, token: str = TOKEN, timeouts: pairing.Timeouts = FAST) -> None:
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
                side.save,
                self.machine.limits,
                timeouts=self.timeouts,
                stop=self.stop,
            )
        )

    def dial(self, machine: Machine, token: str | None = None, peer_id: str | None = None) -> pairing.PairResult:
        return pairing.dial(
            "127.0.0.1",
            self.port,
            token or self.token,
            peer_id or self.machine.device_id,
            machine.identity,
            machine.side.save,
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


def read_json_frame(sock: socket.socket) -> dict[str, Any]:
    value = wire.parse_json(wire.read_frame(sock, max_size=pairing.MAX_FRAME))
    assert isinstance(value, dict)
    return value


class Rogue:
    def __init__(self, port: int) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)

    def send(self, frame: object) -> None:
        wire.write_frame(self.sock, wire.encode(frame))

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def recv(self) -> dict[str, Any]:
        return read_json_frame(self.sock)

    def closed(self) -> bool:
        self.sock.settimeout(5)
        try:
            return self.sock.recv(1) == b""
        except (ConnectionResetError, ConnectionAbortedError):
            return True

    def close(self) -> None:
        self.sock.close()


class Capture:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    @property
    def frames(self) -> list[dict[str, Any]]:
        return [json.loads(payload) for payload in self.payloads]

    @property
    def kinds(self) -> list[str]:
        return [frame["t"] for frame in self.frames]


@contextlib.contextmanager
def captured_frames() -> Iterator[Capture]:
    capture = Capture()
    real = wire.write_frame

    def record(sock: socket.socket, payload: bytes) -> None:
        capture.payloads.append(payload)
        real(sock, payload)

    with mock.patch.object(wire, "write_frame", record):
        yield capture


def dialer_hello(identity: pairing.PairIdentity, nonce: bytes, token: str = TOKEN, **changes: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "t": "hello",
        "v": 2,
        "tag": pairing.token_tag(token),
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


def impersonate_dialer(port: int, attacker: pairing.PairIdentity, listener_pem: str, *, tag_of: str, proof_of: str | None) -> None:
    rogue = Rogue(port)
    nonce = secrets.token_bytes(32)
    try:
        rogue.send(dialer_hello(attacker, nonce, token=tag_of))
        reply = rogue.recv()
        rogue.send({"t": "reveal", "nonce": nonce.hex()})
        assert rogue.recv() == {"t": "tls"}
        context = REAL_CLIENT(attacker.cert, attacker.key, attacker.password, [listener_pem])
        with context.wrap_socket(rogue.sock) as secure:
            secure.settimeout(10)
            if proof_of is not None:
                secret = pairing.session_secret(attacker.der, base64.b64decode(reply["cert"]), nonce, bytes.fromhex(reply["nonce"]))
                proof = pairing.consent_proof("dialer", secret, proof_of).hex()
                wire.write_frame(secure, wire.encode({"t": "consent", "role": "dialer", "c": proof}))
            while secure.recv(4096):
                pass
    except (OSError, wire.WireError):
        pass
    finally:
        rogue.close()


class RogueListener:
    def __init__(self, test: unittest.TestCase, identity: pairing.PairIdentity, dialer_pem: str, proof_of: str) -> None:
        self.identity = identity
        self.dialer_pem = dialer_pem
        self.proof_of = proof_of
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.sock.settimeout(10)
        test.addCleanup(self.sock.close)
        self.port: int = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        test.addCleanup(self.thread.join, 10)

    def _run(self) -> None:
        own = self.identity
        try:
            conn, _ = self.sock.accept()
            with conn:
                conn.settimeout(10)
                hello = read_json_frame(conn)
                nonce = secrets.token_bytes(32)
                reply = {"t": "hello", "v": 2, "name": own.name, "cert": b64(own.der), "nonce": nonce.hex()}
                wire.write_frame(conn, wire.encode(reply))
                reveal = read_json_frame(conn)
                wire.write_frame(conn, wire.encode({"t": "tls"}))
                context = REAL_SERVER(own.cert, own.key, own.password, [self.dialer_pem])
                with context.wrap_socket(conn, server_side=True) as secure:
                    secret = pairing.session_secret(base64.b64decode(hello["cert"]), own.der, bytes.fromhex(reveal["nonce"]), nonce)
                    proof = pairing.consent_proof("listener", secret, self.proof_of).hex()
                    wire.write_frame(secure, wire.encode({"t": "consent", "role": "listener", "c": proof}))
                    read_json_frame(secure)
        except (OSError, wire.WireError):
            pass


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
        self,
        listener: Side | None = None,
        dialer: Side | None = None,
        *,
        key_of_listener: Identity | None = None,
        key_of_dialer: Identity | None = None,
    ) -> tuple[Machine, Machine]:
        return (
            Machine(self, pair_identity(self.a, "alpha", key=key_of_listener), listener or Side()),
            Machine(self, pair_identity(self.b, "bravo", key=key_of_dialer), dialer or Side()),
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
        self.assertEqual(saved_by_listener.device_id, dm.device_id)
        self.assertEqual(saved_by_listener.der, dm.identity.der)
        self.assertEqual(saved_by_listener.address, "127.0.0.1")
        self.assertEqual(saved_by_dialer.name, "alpha")
        self.assertEqual(saved_by_dialer.device_id, lm.device_id)
        self.assertEqual(saved_by_dialer.der, lm.identity.der)
        self.assertEqual(saved_by_dialer.address, "127.0.0.1")
        self.assertEqual((listened.peer, dialed.peer), (saved_by_listener, saved_by_dialer))
        self.assertEqual((lm.failures(), dm.failures()), (0, 0))
        self.assertFalse(listened.locked or dialed.locked)

    def test_the_peer_pem_is_the_hello_certificate(self) -> None:
        lm, dm, _listened, _dialed = self.run_pair()
        self.assertEqual(lm.side.saved[0].pem, self.b.pem.replace("\r\n", "\n"))
        self.assertEqual(dm.side.saved[0].pem, self.a.pem.replace("\r\n", "\n"))

    def test_an_invitation_drives_the_dial(self) -> None:
        lm, dm = self.machines()
        window = Window(self, lm)
        text = pairing.make_invitation("alpha", "127.0.0.1", window.port, lm.device_id, TOKEN, NOW + 300)
        invitation = pairing.parse_invitation(text, NOW)
        dialed = pairing.dial(
            invitation.address,
            invitation.port,
            invitation.token,
            invitation.id,
            dm.identity,
            dm.side.save,
            dm.limits,
            timeouts=FAST,
        )
        self.assertEqual((window.end().outcome, dialed.outcome), ("paired", "paired"))

    def test_the_token_never_appears_in_a_frame_on_the_wire(self) -> None:
        lm, dm = self.machines()
        window = Window(self, lm)
        with captured_frames() as capture:
            dialed = window.dial(dm)
            listened = window.end()
        self.assertEqual((listened.outcome, dialed.outcome), ("paired", "paired"))
        self.assertEqual(sorted(capture.kinds), ["consent", "consent", "hello", "hello", "reveal", "saved", "saved", "tls"])
        raw = base64.urlsafe_b64decode(TOKEN + "==")
        needles = [TOKEN.encode(), raw.hex().encode(), base64.b64encode(raw).rstrip(b"=")]
        for payload in capture.payloads:
            for needle in needles:
                self.assertNotIn(needle, payload)
        dialer_hello_frame = capture.frames[0]
        self.assertEqual(dialer_hello_frame["t"], "hello")
        self.assertEqual(dialer_hello_frame["tag"], pairing.token_tag(TOKEN))
        self.assertNotIn("token", dialer_hello_frame)
        for result in (listened, dialed):
            self.assertNotIn(TOKEN, repr(result))

    def test_a_wrong_token_fails_the_dialer_and_saves_nothing(self) -> None:
        lm, dm = self.machines()
        window = Window(self, lm)
        with captured_frames() as capture:
            wrong = window.dial(dm, token=OTHER_TOKEN)
        self.assertEqual(wrong.outcome, "failed")
        self.assertEqual(capture.kinds, ["hello"])
        self.assertEqual((lm.side.saved, dm.side.saved), ([], []))
        self.assertEqual((lm.failures(), dm.failures()), (0, 0))
        self.assertEqual(window.dial(dm).outcome, "paired")
        self.assertEqual(window.end().outcome, "paired")

    def test_a_listener_with_another_certificate_fails_the_dialer_before_the_reveal(self) -> None:
        lm, dm = self.machines()
        stranger = device_id(pair_identity(self.c, "charlie").der)
        window = Window(self, lm)
        with captured_frames() as capture:
            dialed = window.dial(dm, peer_id=stranger)
        self.assertEqual(dialed.outcome, "failed")
        self.assertIn("not the one that made the invitation", dialed.reason)
        self.assertEqual(capture.kinds, ["hello", "hello"])
        self.assertEqual((lm.side.saved, dm.side.saved), ([], []))
        self.assertEqual((lm.failures(), dm.failures()), (0, 0))
        self.assertEqual(window.dial(dm).outcome, "paired")
        self.assertEqual(window.end().outcome, "paired")

    def test_a_machine_that_replays_the_tag_without_the_token_fails_the_listener(self) -> None:
        lm, _dm = self.machines()
        attacker = pair_identity(self.c, "mallory")
        window = Window(self, lm)
        impersonate_dialer(window.port, attacker, self.a.pem, tag_of=TOKEN, proof_of=OTHER_TOKEN)
        listened = window.end()
        self.assertEqual(listened.outcome, "failed")
        self.assertIn("token", listened.reason)
        self.assertFalse(listened.locked)
        assert listened.peer is not None
        self.assertEqual(listened.peer.device_id, device_id(attacker.der))
        self.assertEqual(lm.side.saved, [])
        self.assertEqual(lm.failures(), 1)

    def test_a_machine_that_never_sends_its_consent_times_out_without_counting(self) -> None:
        short = dataclasses.replace(FAST, consent_s=0.5)
        lm, _dm = self.machines()
        window = Window(self, lm, timeouts=short)
        impersonate_dialer(window.port, pair_identity(self.c, "mallory"), self.a.pem, tag_of=TOKEN, proof_of=None)
        listened = window.end()
        self.assertEqual(listened.outcome, "failed")
        self.assertIn("timed out", listened.reason)
        self.assertEqual(lm.side.saved, [])
        self.assertEqual(lm.failures(), 0)

    def test_a_listener_without_the_token_fails_the_dialer(self) -> None:
        _lm, dm = self.machines()
        rogue = RogueListener(self, pair_identity(self.a, "alpha"), ssl.DER_cert_to_PEM_cert(dm.identity.der), OTHER_TOKEN)
        dialed = pairing.dial(
            "127.0.0.1", rogue.port, TOKEN, device_id(rogue.identity.der), dm.identity, dm.side.save, dm.limits, timeouts=FAST
        )
        self.assertEqual(dialed.outcome, "failed")
        self.assertIn("token", dialed.reason)
        self.assertFalse(dialed.locked)
        self.assertEqual(dm.side.saved, [])
        self.assertEqual(dm.failures(), 1)

    def test_a_third_failure_reports_the_lock(self) -> None:
        _lm, dm = self.machines()
        dm.limits.record_failure()
        dm.limits.record_failure()
        rogue = RogueListener(self, pair_identity(self.a, "alpha"), ssl.DER_cert_to_PEM_cert(dm.identity.der), OTHER_TOKEN)
        peer_id = device_id(rogue.identity.der)
        dialed = pairing.dial("127.0.0.1", rogue.port, TOKEN, peer_id, dm.identity, dm.side.save, dm.limits, timeouts=FAST)
        self.assertTrue(dialed.locked)
        with self.assertRaises(pairing.Locked):
            dm.limits.check()
        with self.assertRaises(pairing.Locked):
            pairing.dial("127.0.0.1", rogue.port, TOKEN, peer_id, dm.identity, dm.side.save, dm.limits)

    def test_a_third_failure_locks_the_listener(self) -> None:
        lm, _dm = self.machines()
        lm.limits.record_failure()
        lm.limits.record_failure()
        window = Window(self, lm)
        impersonate_dialer(window.port, pair_identity(self.c, "mallory"), self.a.pem, tag_of=TOKEN, proof_of=OTHER_TOKEN)
        self.assertTrue(window.end().locked)
        with self.assertRaises(pairing.Locked):
            lm.limits.check()

    def test_a_save_that_fails_is_not_reported_as_saved(self) -> None:
        _lm, dm, listened, dialed = self.run_pair(listener=Side(save_error=OSError("disk full")))
        self.assertEqual(listened.outcome, "failed")
        self.assertIn("disk full", listened.reason)
        self.assertEqual(dialed.outcome, "incomplete")
        self.assertIn("Pairing may be incomplete on the other machine", dialed.reason)
        self.assertEqual(len(dm.side.saved), 1)

    def test_a_save_that_fails_on_the_dialer(self) -> None:
        lm, _dm, listened, dialed = self.run_pair(dialer=Side(save_error=OSError("disk full")))
        self.assertEqual(dialed.outcome, "failed")
        self.assertIn("disk full", dialed.reason)
        self.assertEqual(listened.outcome, "incomplete")
        self.assertEqual(len(lm.side.saved), 1)

    def test_a_non_hello_first_frame_is_dropped(self) -> None:
        lm, dm = self.machines()
        window = Window(self, lm)
        for first in ({"t": "reveal", "nonce": "00" * 32}, [], "hello", {"tag": pairing.token_tag(TOKEN)}, None):
            rogue = Rogue(window.port)
            rogue.send(first)
            self.assertTrue(rogue.closed(), repr(first))
        rogue = Rogue(window.port)
        rogue.send_raw(b"\x00\x00\x00\x00")
        self.assertTrue(rogue.closed())
        self.assertEqual(window.dial(dm).outcome, "paired")
        self.assertEqual(window.end().outcome, "paired")

    def refuse_then_pair(self, send: Callable[[Rogue, pairing.PairIdentity], None]) -> None:
        lm, dm = self.machines()
        window = Window(self, lm)
        rogue = Rogue(window.port)
        send(rogue, dm.identity)
        self.assertTrue(rogue.closed())
        self.assertEqual(lm.side.saved, [])
        self.assertEqual(lm.failures(), 0)
        self.assertEqual(window.dial(dm).outcome, "paired")
        self.assertEqual(window.end().outcome, "paired")

    def test_unknown_key_in_hello_is_refused(self) -> None:
        self.refuse_then_pair(lambda rogue, ident: rogue.send(dialer_hello(ident, bytes(32), extra=1)))

    def test_missing_key_in_hello_is_refused(self) -> None:
        for key in ("v", "tag", "name", "cert", "commit"):
            missing: dict[str, Any] = {key: KeyError}
            self.refuse_then_pair(lambda rogue, ident, missing=missing: rogue.send(dialer_hello(ident, bytes(32), **missing)))

    def test_bad_hello_values_are_refused(self) -> None:
        bad: list[dict[str, Any]] = [
            {"v": 3},
            {"v": True},
            {"v": "2"},
            {"name": "pair123456"},
            {"name": ""},
            {"commit": "00" * 31},
            {"commit": "AB" * 32},
            {"cert": ""},
            {"cert": b64(b"not a certificate")},
            {"t": "reveal"},
            {"tag": pairing.token_tag(OTHER_TOKEN)},
            {"tag": pairing.token_tag(TOKEN).upper()},
            {"tag": pairing.token_tag(TOKEN)[:-1]},
            {"tag": ""},
            {"tag": 5},
            {"tag": None},
        ]
        for change in bad:
            self.refuse_then_pair(lambda rogue, ident, change=change: rogue.send(dialer_hello(ident, bytes(32), **change)))

    def test_a_version_1_hello_is_refused(self) -> None:
        self.refuse_then_pair(lambda rogue, ident: rogue.send(dialer_hello(ident, bytes(32), v=1)))

    def test_a_version_1_hello_with_the_token_in_it_is_refused(self) -> None:
        def send(rogue: Rogue, ident: pairing.PairIdentity) -> None:
            frame = dialer_hello(ident, bytes(32), v=1, tag=KeyError)
            frame["token"] = TOKEN
            rogue.send(frame)

        self.refuse_then_pair(send)

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
            rogue.send_raw(wire_frame(body[:-1] + ',"v":2}'))

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

    def after_the_reply(self, follow: Callable[[Rogue], None]) -> None:
        def send(rogue: Rogue, ident: pairing.PairIdentity) -> None:
            rogue.send(dialer_hello(ident, bytes(32)))
            rogue.recv()
            follow(rogue)

        self.refuse_then_pair(send)

    def test_out_of_order_frame_is_refused(self) -> None:
        self.after_the_reply(lambda rogue: rogue.send({"t": "tls"}))

    def test_a_second_hello_instead_of_the_reveal_is_refused(self) -> None:
        def second(rogue: Rogue) -> None:
            rogue.send(dialer_hello(pair_identity(self.b, "bravo"), bytes(32)))

        self.after_the_reply(second)

    def test_reveal_with_an_unknown_key_is_refused(self) -> None:
        self.after_the_reply(lambda rogue: rogue.send({"t": "reveal", "nonce": "00" * 32, "extra": 1}))

    def test_oversize_reveal_is_refused(self) -> None:
        self.after_the_reply(lambda rogue: rogue.send_raw((pairing.MAX_FRAME + 1).to_bytes(4, "big")))

    def test_reveal_that_does_not_match_the_commitment_is_refused(self) -> None:
        self.after_the_reply(lambda rogue: rogue.send({"t": "reveal", "nonce": bytes([1] * 32).hex()}))

    def test_reveal_for_another_certificate_is_refused(self) -> None:
        other = pair_identity(self.c, "charlie")

        def send(rogue: Rogue, ident: pairing.PairIdentity) -> None:
            nonce = bytes([7] * 32)
            frame = dialer_hello(ident, nonce)
            frame["commit"] = pairing.commitment(other.der, nonce).hex()
            rogue.send(frame)
            rogue.recv()
            rogue.send({"t": "reveal", "nonce": nonce.hex()})

        self.refuse_then_pair(send)

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
        rogue.recv()
        rogue.send({"t": "reveal", "nonce": nonce.hex()})
        rogue.send({"t": "junk"})
        self.assertEqual(window.end().outcome, "failed")
        self.assertEqual(lm.side.saved, [])
        self.assertEqual(lm.failures(), 0)
        rogue.close()

    def test_a_second_dialer_is_refused_without_affecting_the_first(self) -> None:
        gate = threading.Event()
        lm, dm = self.machines(listener=Side(gate=gate))
        intruder = Machine(self, pair_identity(self.c, "charlie"), Side())
        window = Window(self, lm)
        first: list[pairing.PairResult] = []
        thread = threading.Thread(target=lambda: first.append(window.dial(dm)), daemon=True)
        thread.start()
        self.assertTrue(lm.side.saving.wait(10))
        second = window.dial(intruder)
        self.assertEqual(second.outcome, "failed")
        self.assertEqual(intruder.side.saved, [])
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

    def test_the_exchange_must_reach_the_reveal_in_time(self) -> None:
        quick = dataclasses.replace(FAST, handshake_s=0.5)
        lm, dm = self.machines()
        window = Window(self, lm, timeouts=quick)
        rogue = Rogue(window.port)
        rogue.send(dialer_hello(dm.identity, bytes(32)))
        rogue.recv()
        self.assertTrue(rogue.closed())
        self.assertEqual(lm.side.saved, [])
        window.close()

    def test_dial_refuses_bad_arguments_before_connecting(self) -> None:
        lm, dm = self.machines()
        good = {"address": "127.0.0.1", "port": 47830, "token": TOKEN, "peer_id": lm.device_id}
        bad: list[dict[str, Any]] = [
            {"address": "localhost"},
            {"address": "127.0.0.1."},
            {"address": "::1"},
            {"address": "example.com"},
            {"port": 0},
            {"port": 65536},
            {"token": "bad token"},
            {"token": TOKEN[:-1]},
            {"peer_id": lm.device_id.split("-")[0]},
            {"peer_id": lm.device_id.lower()},
            {"peer_id": lm.device_id.replace("-", "")},
            {"peer_id": ""},
        ]
        with mock.patch.object(socket, "create_connection") as connect:
            for change in bad:
                args = {**good, **change}
                with self.assertRaises(pairing.PairingError, msg=repr(change)):
                    pairing.dial(args["address"], args["port"], args["token"], args["peer_id"], dm.identity, dm.side.save, dm.limits)
        connect.assert_not_called()
        self.assertEqual(lm.side.saved, [])

    def test_dial_reports_a_refused_connection(self) -> None:
        lm, dm = self.machines()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        result = pairing.dial("127.0.0.1", port, TOKEN, lm.device_id, dm.identity, dm.side.save, dm.limits, timeouts=FAST)
        self.assertEqual(result.outcome, "failed")
        self.assertIn("cannot connect", result.reason)

    def test_listen_once_refuses_a_malformed_token(self) -> None:
        lm, _dm = self.machines()
        with socket.socket() as unused, self.assertRaises(pairing.PairingError):
            pairing.listen_once(unused, "bad token", lm.identity, lm.side.save, lm.limits)

    def test_certificate_parser_accepts_a_real_certificate(self) -> None:
        der = ssl.PEM_cert_to_DER_cert(self.a.pem)
        self.assertEqual(pairing.parse_certificate(b64(der)), der)
        for broken in (der + b"\x00", der[:-1], der[:-1] + bytes([der[-1] ^ 1])[:0]):
            with self.assertRaises(pairing.PairingError):
                pairing.parse_certificate(b64(broken))

    def test_wrong_key_on_the_listener_fails_both_sides(self) -> None:
        lm, dm = self.machines(key_of_listener=self.c)
        window = Window(self, lm)
        dialed = window.dial(dm)
        listened = window.end()
        self.assertEqual((listened.outcome, dialed.outcome), ("failed", "failed"))
        self.assertEqual((lm.side.saved, dm.side.saved), ([], []))

    def test_wrong_key_on_the_dialer_fails_both_sides(self) -> None:
        lm, dm = self.machines(key_of_dialer=self.c)
        window = Window(self, lm)
        dialed = window.dial(dm)
        listened = window.end()
        self.assertEqual((listened.outcome, dialed.outcome), ("failed", "failed"))
        self.assertEqual((lm.side.saved, dm.side.saved), ([], []))

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

        lm, dm = self.machines()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(tls, context_attr, certless if kind == "absent" else impostor))
            if kind == "substituted":
                stack.enter_context(mock.patch.object(tls, honest_attr, widened))
            window = Window(self, lm)
            dialed = window.dial(dm)
            listened = window.end()
        self.assertEqual((listened.outcome, dialed.outcome), ("failed", "failed"), (attacker, kind, listened, dialed))
        self.assertEqual((lm.side.saved, dm.side.saved), ([], []))
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
