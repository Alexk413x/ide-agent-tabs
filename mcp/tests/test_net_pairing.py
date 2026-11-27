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
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from ide_agent_tabs.net import pairing, tls, wire
from ide_agent_tabs.net.identity import device_id, format_digest
from net_support import lock_recorded, wait_until
from test_net_tls import OPENSSL, Identity, make_identity

NOW = 1_700_000_000
GOOD_ID = format_digest(bytes(range(32)))
INTERFACE = "192.168.1.10"
HUB = pairing.HubRef("0123456789abcdef0123456789abcdef", "home")
HUB_KEY = "ab" * 32
FAST = pairing.Timeouts(
    first_frame_s=2.0,
    handshake_s=10.0,
    consent_s=5.0,
    connect_s=2.0,
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


class HashTest(unittest.TestCase):
    CERT_D = b"dialer certificate bytes"
    NONCE_D = bytes(range(32))

    def test_h_prefixes_each_field_with_its_length(self) -> None:
        expected = hashlib.sha256(b"\x00\x00\x00\x01a\x00\x00\x00\x02bc").digest()
        self.assertEqual(pairing.H(b"a", b"bc"), expected)
        self.assertNotEqual(pairing.H(b"ab", b"c"), expected)
        self.assertEqual(pairing.H().hex(), hashlib.sha256(b"").hexdigest())

    def test_commitment_vector(self) -> None:
        commit = pairing.commitment(self.CERT_D, self.NONCE_D)
        self.assertEqual(commit.hex(), "5e75760c314d95259c42fbc990f524a8f89b56987bb0d6a370bb5a4f131cf01d")

    def test_the_commitment_depends_on_the_certificate_and_the_nonce(self) -> None:
        base = pairing.commitment(self.CERT_D, self.NONCE_D)
        self.assertNotEqual(base, pairing.commitment(b"another certificate", self.NONCE_D))
        self.assertNotEqual(base, pairing.commitment(self.CERT_D, bytes(32)))


class NameAndIdTest(unittest.TestCase):
    def test_device_names(self) -> None:
        for good in ("alpha", "My PC-2.local_x", "a", "x" * 32, "12345", "12345a6", "Room 101"):
            self.assertTrue(pairing.valid_device_name(good), good)
        for bad in ("", "x" * 33, "café", "tab\there", "123456", "a1234567b", "name!", "line\nbreak", "１２３４５６"):
            self.assertFalse(pairing.valid_device_name(bad), repr(bad))

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


X = "192.168.1.9"
Y = "192.168.1.10"


class LimitsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="iat-pair-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.now = [float(NOW)]

    @property
    def path(self) -> Path:
        return Path(self.home) / "net" / pairing.PAIRING_FILE

    def limits(self) -> pairing.Limits:
        return pairing.Limits(self.home, clock=lambda: self.now[0])

    def test_three_failures_lock_that_address_for_an_hour(self) -> None:
        limits = self.limits()
        self.assertFalse(limits.record_failure(X))
        self.assertFalse(limits.record_failure(X))
        limits.check_locked(X)
        self.assertTrue(limits.record_failure(X))
        with self.assertRaisesRegex(pairing.Locked, "net trust"):
            limits.check_locked(X)
        self.now[0] += 3599
        with self.assertRaises(pairing.Locked):
            limits.check_locked(X)
        self.now[0] += 2
        limits.check_locked(X)

    def test_a_locked_address_leaves_other_addresses_free(self) -> None:
        limits = self.limits()
        for _ in range(3):
            limits.record_failure(X)
        with self.assertRaises(pairing.Locked):
            limits.check_locked(X)
        limits.check_locked(Y)
        self.assertEqual(list(limits.locked_addresses()), [X])

    def test_failures_from_different_addresses_do_not_add_up(self) -> None:
        limits = self.limits()
        for address in (X, Y, X, Y):
            self.assertFalse(limits.record_failure(address))
        limits.check_locked(X)
        limits.check_locked(Y)
        self.assertTrue(limits.record_failure(X))
        with self.assertRaises(pairing.Locked):
            limits.check_locked(X)
        limits.check_locked(Y)

    def test_failures_expire_after_an_hour(self) -> None:
        limits = self.limits()
        limits.record_failure(X)
        limits.record_failure(X)
        self.now[0] += 3601
        self.assertFalse(limits.record_failure(X))
        limits.check_locked(X)

    def test_a_lock_clears_the_failure_count(self) -> None:
        limits = self.limits()
        for _ in range(3):
            limits.record_failure(X)
        self.now[0] += 3601
        self.assertFalse(limits.record_failure(X))
        self.assertFalse(limits.record_failure(X))
        self.assertTrue(limits.record_failure(X))

    def test_state_survives_a_new_instance(self) -> None:
        for _ in range(3):
            self.limits().record_failure(X)
        again = self.limits()
        with self.assertRaises(pairing.Locked):
            again.check_locked(X)
        self.now[0] += 3601
        again.check_locked(X)

    def test_the_address_table_stays_bounded(self) -> None:
        limits = self.limits()
        for number in range(pairing.MAX_LIMIT_ADDRESSES + 20):
            limits.record_failure(f"10.1.{number // 250}.{number % 250}")
        state = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(state["addresses"]), pairing.MAX_LIMIT_ADDRESSES)

    def test_a_file_in_the_old_format_still_loads_and_locks_nothing(self) -> None:
        self.limits().record_failure(X)
        self.path.write_text('{"windows":[1],"open":null,"failures":[1],"lockedUntil":0}', encoding="utf-8")
        limits = self.limits()
        self.assertEqual(limits.locked_addresses(), {})
        limits.check_locked(X)

    def test_a_file_in_the_old_format_keeps_the_address_locks(self) -> None:
        self.limits().record_failure(X)
        locked = {X: {"failures": [], "lockedUntil": NOW + 600}}
        self.path.write_text(json.dumps({"windows": [], "open": None, "addresses": locked}), encoding="utf-8")
        with self.assertRaises(pairing.Locked):
            self.limits().check_locked(X)
        self.limits().record_failure(Y)
        self.assertEqual(set(json.loads(self.path.read_text(encoding="utf-8"))), {"addresses"})

    def test_spacing_is_per_address(self) -> None:
        now = [0.0]
        spacing = pairing.Spacing(1.0, lambda: now[0])
        self.assertTrue(spacing.allow(X))
        self.assertFalse(spacing.allow(X))
        self.assertTrue(spacing.allow(Y))
        now[0] += 1.5
        self.assertTrue(spacing.allow(X))

    def test_a_refusal_is_logged_once_per_address_and_interval(self) -> None:
        now = [0.0]
        lines: list[str] = []
        refused = pairing.RefusalLog(lines.append, lambda: now[0], interval_s=60.0)
        for _ in range(5):
            refused(X, "too soon")
        refused(Y, "too soon")
        self.assertEqual(lines, [f"join connection from {X} refused: too soon", f"join connection from {Y} refused: too soon"])
        now[0] += 61.0
        refused(X, "too soon")
        self.assertEqual(lines[-1], f"join connection from {X} refused: too soon; 4 more from that address were not logged")
        now[0] += 61.0
        refused(X, "locked")
        self.assertEqual(lines[-1], f"join connection from {X} refused: locked")

    def test_refusals_from_many_addresses_stay_inside_the_budget_and_are_counted_once_later(self) -> None:
        now = [0.0]
        lines: list[str] = []
        refused = pairing.RefusalLog(lines.append, lambda: now[0], interval_s=60.0, budget=3)
        for index in range(10):
            refused(f"10.0.0.{index}", "not private")
        self.assertEqual(len(lines), 3)
        now[0] += 61.0
        refused("10.0.1.1", "not private")
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[-1].endswith("; 7 refusals from other addresses were not logged"), lines[-1])

    def test_the_refusal_table_stays_bounded(self) -> None:
        lines: list[str] = []
        refused = pairing.RefusalLog(lines.append, lambda: 0.0, budget=10_000, cap=8)
        for index in range(100):
            refused(f"10.0.0.{index}", "not private")
        self.assertEqual(len(lines), 100)
        self.assertLessEqual(len(refused._last), 8)  # pyright: ignore[reportPrivateUsage]

    def test_no_log_function_means_no_output_and_no_error(self) -> None:
        pairing.RefusalLog(None)(X, "too soon")

    def test_a_damaged_file_is_refused_with_a_hint(self) -> None:
        self.limits().record_failure(X)
        for damaged in ("not json", "[]", '{"addresses":[]}', '{"extra":1}', '{"addresses":{"a":1}}'):
            self.path.write_text(damaged, encoding="utf-8")
            with self.assertRaisesRegex(pairing.PairingError, "delete it"):
                self.limits().check_locked(X)

    def test_file_holds_no_secrets(self) -> None:
        self.limits().record_failure(X)
        self.assertEqual(set(json.loads(self.path.read_text(encoding="utf-8"))), {"addresses"})


class Side:
    def __init__(
        self,
        *,
        hub: pairing.HubRef | None = None,
        save_error: Exception | None = None,
        gate: threading.Event | None = None,
    ) -> None:
        self.own_hub = hub
        self.save_error = save_error
        self.gate = gate
        self.saving = threading.Event()
        self.saved: list[pairing.PeerInfo] = []
        self.received: list[pairing.HubMessage | None] = []

    def hub(self) -> pairing.HubRef | None:
        return self.own_hub

    def message(self, peer: pairing.PeerInfo) -> pairing.HubMessage:
        assert self.own_hub is not None
        return pairing.HubMessage(self.own_hub, HUB_KEY)

    def save(self, peer: pairing.PeerInfo, received: pairing.HubMessage | None) -> None:
        self.saving.set()
        if self.gate is not None:
            self.gate.wait(10)
        if self.save_error is not None:
            raise self.save_error
        self.saved.append(peer)
        self.received.append(received)


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
        if not path.exists():
            return 0
        return sum(len(entry["failures"]) for entry in json.loads(path.read_text(encoding="utf-8"))["addresses"].values())


class Listener:
    def __init__(
        self,
        test: unittest.TestCase,
        machine: Machine,
        timeouts: pairing.Timeouts = FAST,
        *,
        allow_source: Callable[[str], bool] = lambda _address: True,
        clock: Callable[[], float] = time.monotonic,
        plugin_version: str = pairing.PACKAGE_VERSION,
    ) -> None:
        self.machine = machine
        self.lines: list[str] = []
        self.timeouts = timeouts
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port: int = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.results: list[pairing.PairResult] = []
        self.thread = threading.Thread(
            target=pairing.serve_joins,
            args=(self.sock, machine.identity, machine.side, machine.limits, self.stop),
            kwargs={
                "timeouts": timeouts,
                "on_result": self.results.append,
                "allow_source": allow_source,
                "log": self.lines.append,
                "clock": clock,
                "plugin_version": plugin_version,
            },
            daemon=True,
        )
        self.thread.start()
        test.addCleanup(self.close)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(5)
        self.sock.close()

    def dial(self, machine: Machine, peer_id: str | None = None, **options: Any) -> pairing.PairResult:
        return pairing.dial_join(
            "127.0.0.1",
            self.port,
            peer_id or self.machine.device_id,
            machine.identity,
            machine.side,
            machine.limits,
            timeouts=self.timeouts,
            **options,
        )

    def wait_result(self) -> pairing.PairResult:
        assert wait_until(lambda: bool(self.results), 30), "the listener reported no result"
        return self.results[0]


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


def dialer_hello(identity: pairing.PairIdentity, nonce: bytes, **changes: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "t": "hello",
        "v": 2,
        "mode": "join",
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
        self,
        listener: Side | None = None,
        dialer: Side | None = None,
        *,
        key_of_listener: Identity | None = None,
        key_of_dialer: Identity | None = None,
    ) -> tuple[Machine, Machine]:
        return (
            Machine(self, pair_identity(self.a, "alpha", key=key_of_listener), listener or Side(hub=HUB)),
            Machine(self, pair_identity(self.b, "bravo", key=key_of_dialer), dialer or Side()),
        )

    def run_pair(
        self, listener: Side | None = None, dialer: Side | None = None
    ) -> tuple[Machine, Machine, pairing.PairResult, pairing.PairResult]:
        lm, dm = self.machines(listener, dialer)
        listening = Listener(self, lm)
        dialed = listening.dial(dm)
        return lm, dm, listening.wait_result(), dialed

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

    def test_the_hub_travels_from_the_side_that_has_one_to_the_side_that_has_none(self) -> None:
        lm, dm, _listened, _dialed = self.run_pair()
        self.assertEqual(lm.side.received, [None])
        (received,) = dm.side.received
        assert received is not None
        self.assertEqual((received.hub, received.key), (HUB, HUB_KEY))

    def test_the_peer_pem_is_the_hello_certificate(self) -> None:
        lm, dm, _listened, _dialed = self.run_pair()
        self.assertEqual(lm.side.saved[0].pem, self.b.pem.replace("\r\n", "\n"))
        self.assertEqual(dm.side.saved[0].pem, self.a.pem.replace("\r\n", "\n"))

    def test_both_sides_learn_the_other_plugin_version(self) -> None:
        lm, dm = self.machines()
        listening = Listener(self, lm, plugin_version="1.2.3")
        dialed = listening.dial(dm, plugin_version="1.3.0")
        listened = listening.wait_result()
        self.assertEqual((listened.outcome, dialed.outcome), ("paired", "paired"))
        assert listened.peer is not None and dialed.peer is not None
        self.assertEqual((listened.peer.plugin_version, dialed.peer.plugin_version), ("1.3.0", "1.2.3"))
        self.assertEqual((lm.side.saved[0].plugin_version, dm.side.saved[0].plugin_version), ("1.3.0", "1.2.3"))
        self.assertEqual((lm.failures(), dm.failures()), (0, 0))

    def test_the_versioned_join_carries_the_hub_like_the_legacy_one(self) -> None:
        lm, dm = self.machines()
        listening = Listener(self, lm, plugin_version="1.2.3")
        self.assertEqual(listening.dial(dm, plugin_version="1.3.0").outcome, "paired")
        listening.wait_result()
        (received,) = dm.side.received
        assert received is not None
        self.assertEqual((received.hub, received.key), (HUB, HUB_KEY))

    def test_a_dialer_falls_back_to_the_legacy_hello_when_the_listener_does_not_know_join_pv(self) -> None:
        lm, dm = self.machines()
        with mock.patch.object(pairing, "_LISTEN_MODES", (pairing.JOIN_MODE, pairing.PROBE_MODE)):
            listening = Listener(self, lm, plugin_version="1.2.3")
            with captured_frames() as sent:
                dialed = listening.dial(dm, plugin_version="1.3.0")
            listened = listening.wait_result()
        self.assertEqual((listened.outcome, dialed.outcome), ("paired", "paired"))
        assert listened.peer is not None and dialed.peer is not None
        self.assertEqual((listened.peer.plugin_version, dialed.peer.plugin_version), (None, None))
        self.assertEqual((lm.failures(), dm.failures()), (0, 0))
        modes = [frame["mode"] for frame in sent.frames if frame["t"] == "hello" and "mode" in frame]
        self.assertEqual(modes, ["join-pv", "join-pv", "join"])
        self.assertTrue(all("pv" not in frame for frame in sent.frames if frame.get("mode") == "join"))

    def test_a_legacy_dialer_still_joins_a_listener_that_knows_join_pv(self) -> None:
        lm, dm = self.machines()
        listening = Listener(self, lm, plugin_version="1.2.3")
        dialed = listening.dial(dm, plugin_version="0")
        listened = listening.wait_result()
        self.assertEqual((listened.outcome, dialed.outcome), ("paired", "paired"))
        assert listened.peer is not None and dialed.peer is not None
        self.assertEqual((listened.peer.plugin_version, dialed.peer.plugin_version), (None, None))
        self.assertEqual(lm.failures(), 0)

    def test_a_listener_without_a_valid_own_version_does_not_offer_join_pv(self) -> None:
        lm, dm = self.machines()
        listening = Listener(self, lm, plugin_version="0")
        dialed = listening.dial(dm, plugin_version="1.3.0")
        listened = listening.wait_result()
        self.assertEqual((listened.outcome, dialed.outcome), ("paired", "paired"))
        assert dialed.peer is not None
        self.assertIsNone(dialed.peer.plugin_version)
        self.assertEqual(lm.failures(), 0)

    def test_a_join_pv_hello_with_a_bad_plugin_version_is_refused(self) -> None:
        for bad in ("1.2", "1.2.3.4", "1.2.3-beta", "v1.2.3", "a.b.c", "1.2.3\n", "12345.1.1", "1.2.3 ", "", 5, None, ["1.2.3"]):
            self.refuse_then_pair(
                lambda rogue, ident, bad=bad: rogue.send(dialer_hello(ident, bytes(32), mode="join-pv", pv=bad)),
            )

    def test_a_join_pv_hello_without_a_plugin_version_is_refused(self) -> None:
        self.refuse_then_pair(lambda rogue, ident: rogue.send(dialer_hello(ident, bytes(32), mode="join-pv")))

    def test_a_plugin_version_in_a_legacy_hello_is_refused(self) -> None:
        for mode in ("join", "probe"):
            self.refuse_then_pair(lambda rogue, ident, mode=mode: rogue.send(dialer_hello(ident, bytes(32), mode=mode, pv="1.2.3")))

    def test_a_listener_reply_with_a_bad_plugin_version_fails_the_dialer(self) -> None:
        lm, dm = self.machines()
        for reply_changes in ({"pv": "1.2"}, {"pv": 3}, {"pv": KeyError}, {"extra": 1}):
            server = socket.socket()
            self.addCleanup(server.close)
            server.bind(("127.0.0.1", 0))
            server.listen(4)
            server.settimeout(10)

            def answer(server: socket.socket = server, changes: dict[str, Any] = reply_changes) -> None:
                conn, _ = server.accept()
                with conn:
                    read_json_frame(conn)
                    frame: dict[str, Any] = {
                        "t": "hello",
                        "v": 2,
                        "name": "alpha",
                        "cert": b64(lm.identity.der),
                        "nonce": "00" * 32,
                        "pv": "1.2.3",
                    }
                    for key, value in changes.items():
                        if value is KeyError:
                            del frame[key]
                        else:
                            frame[key] = value
                    wire.write_frame(conn, wire.encode(frame))
                    conn.settimeout(5)
                    with contextlib.suppress(OSError):
                        conn.recv(1)

            thread = threading.Thread(target=answer, daemon=True)
            thread.start()
            result = pairing.dial_join(
                "127.0.0.1", server.getsockname()[1], lm.device_id, dm.identity, dm.side, dm.limits, timeouts=FAST, plugin_version="1.3.0"
            )
            thread.join(10)
            self.assertEqual(result.outcome, "failed", reply_changes)
            self.assertEqual(dm.side.saved, [])

    def check_commit_order(self, dial_options: dict[str, Any], mode: str, extra_keys: list[str]) -> None:
        lm, dm = self.machines()
        listening = Listener(self, lm, plugin_version="1.2.3")
        with captured_frames() as capture:
            dialed = listening.dial(dm, **dial_options)
            listened = listening.wait_result()
        self.assertEqual((listened.outcome, dialed.outcome), ("paired", "paired"))
        self.assertEqual(
            sorted(capture.kinds), ["hello", "hello", "hub", "ready", "ready", "reveal", "saved", "saved", "state", "state", "tls"]
        )
        self.assertEqual(capture.kinds[:3], ["hello", "hello", "reveal"])
        hello, reply, reveal = capture.frames[:3]
        self.assertEqual(sorted(hello), sorted(["cert", "commit", "mode", "name", "t", "v", *extra_keys]))
        self.assertEqual(hello["mode"], mode)
        self.assertEqual(sorted(reply), sorted(["cert", "name", "nonce", "t", "v", *extra_keys]))
        self.assertEqual(hello["commit"], pairing.commitment(dm.identity.der, bytes.fromhex(reveal["nonce"])).hex())
        self.assertNotIn(reveal["nonce"], json.dumps(hello))

    def test_the_hello_commits_before_the_listener_speaks_and_the_reveal_opens_it(self) -> None:
        self.check_commit_order({"plugin_version": "1.3.0"}, "join-pv", ["pv"])

    def test_the_legacy_hello_keeps_its_keys(self) -> None:
        self.check_commit_order({"plugin_version": "0"}, "join", [])

    def test_a_listener_with_another_certificate_fails_the_dialer_before_the_reveal(self) -> None:
        lm, dm = self.machines()
        stranger = device_id(pair_identity(self.c, "charlie").der)
        listening = Listener(self, lm)
        with captured_frames() as capture:
            dialed = listening.dial(dm, peer_id=stranger)
        self.assertEqual(dialed.outcome, "failed")
        self.assertIn("does not match the device id", dialed.reason)
        self.assertEqual(capture.kinds, ["hello", "hello"])
        self.assertEqual((lm.side.saved, dm.side.saved), ([], []))
        self.assertTrue(wait_until(lambda: lm.failures() == 1))
        self.assertEqual(listening.dial(dm).outcome, "paired")
        self.assertEqual(listening.wait_result().outcome, "paired")

    def test_a_third_failure_locks_the_listener(self) -> None:
        lm, dm = self.machines()
        lm.limits.record_failure("127.0.0.1")
        lm.limits.record_failure("127.0.0.1")
        listening = Listener(self, lm)
        rogue = Rogue(listening.port)
        rogue.send(dialer_hello(dm.identity, bytes(32), extra=1))
        self.assertTrue(rogue.closed())
        self.assertTrue(wait_until(lambda: lock_recorded(lm.home, "127.0.0.1")))
        with self.assertRaises(pairing.Locked):
            lm.limits.check_locked("127.0.0.1")
        self.assertEqual(listening.dial(dm).outcome, "failed")
        self.assertEqual((lm.side.saved, dm.side.saved), ([], []))

    def test_dial_refuses_a_locked_address(self) -> None:
        lm, dm = self.machines()
        for _ in range(3):
            dm.limits.record_failure("127.0.0.1")
        with mock.patch.object(socket, "create_connection") as connect, self.assertRaises(pairing.Locked):
            pairing.dial_join("127.0.0.1", 47830, lm.device_id, dm.identity, dm.side, dm.limits)
        connect.assert_not_called()

    def test_a_save_that_fails_is_not_reported_as_saved(self) -> None:
        _lm, dm, listened, dialed = self.run_pair(listener=Side(hub=HUB, save_error=OSError("disk full")))
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

    def test_a_failed_save_is_not_counted_as_a_failure(self) -> None:
        lm, _dm, _listened, _dialed = self.run_pair(listener=Side(hub=HUB, save_error=OSError("disk full")))
        self.assertEqual(lm.failures(), 0)

    def test_a_first_frame_without_a_known_mode_is_dropped_uncounted(self) -> None:
        lm, dm = self.machines()
        listening = Listener(self, lm)
        for first in ({"t": "reveal", "nonce": "00" * 32}, [], "hello", {"v": 2}, None, {"mode": "pair"}, {"mode": 5}, {"mode": None}):
            rogue = Rogue(listening.port)
            rogue.send(first)
            self.assertTrue(rogue.closed(), repr(first))
        rogue = Rogue(listening.port)
        rogue.send_raw(b"\x00\x00\x00\x00")
        self.assertTrue(rogue.closed())
        self.assertEqual(listening.dial(dm).outcome, "paired")
        self.assertEqual(listening.wait_result().outcome, "paired")
        self.assertEqual(lm.failures(), 0)

    def refuse_then_pair(self, send: Callable[[Rogue, pairing.PairIdentity], None], *, counted: bool = True) -> None:
        lm, dm = self.machines()
        listening = Listener(self, lm)
        rogue = Rogue(listening.port)
        send(rogue, dm.identity)
        self.assertTrue(rogue.closed())
        if counted:
            self.assertTrue(wait_until(lambda: lm.failures() == 1))
        self.assertEqual(lm.side.saved, [])
        self.assertEqual(listening.dial(dm).outcome, "paired")
        self.assertEqual(listening.wait_result().outcome, "paired")
        self.assertEqual(lm.failures(), 1 if counted else 0)

    def test_unknown_key_in_hello_is_refused(self) -> None:
        self.refuse_then_pair(lambda rogue, ident: rogue.send(dialer_hello(ident, bytes(32), extra=1)))

    def test_the_retired_tag_field_is_refused(self) -> None:
        self.refuse_then_pair(lambda rogue, ident: rogue.send(dialer_hello(ident, bytes(32), tag="00" * 32)))

    def test_missing_key_in_hello_is_refused(self) -> None:
        for key in ("v", "name", "cert", "commit"):
            missing: dict[str, Any] = {key: KeyError}
            self.refuse_then_pair(lambda rogue, ident, missing=missing: rogue.send(dialer_hello(ident, bytes(32), **missing)))

    def test_a_hello_without_a_mode_is_dropped_uncounted(self) -> None:
        self.refuse_then_pair(lambda rogue, ident: rogue.send(dialer_hello(ident, bytes(32), mode=KeyError)), counted=False)

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
        ]
        for change in bad:
            self.refuse_then_pair(lambda rogue, ident, change=change: rogue.send(dialer_hello(ident, bytes(32), **change)))

    def test_a_hello_with_an_unknown_mode_is_dropped_uncounted(self) -> None:
        for mode in ("pair", "JOIN", "", 5, None):
            self.refuse_then_pair(lambda rogue, ident, mode=mode: rogue.send(dialer_hello(ident, bytes(32), mode=mode)), counted=False)

    def test_a_version_1_hello_is_refused(self) -> None:
        self.refuse_then_pair(lambda rogue, ident: rogue.send(dialer_hello(ident, bytes(32), v=1)))

    def test_a_certificate_with_trailing_bytes_is_refused(self) -> None:
        def send(rogue: Rogue, ident: pairing.PairIdentity) -> None:
            rogue.send(dialer_hello(ident, bytes(32), cert=b64(ident.der + b"\x00")))

        self.refuse_then_pair(send)

    def test_the_listener_refuses_its_own_certificate(self) -> None:
        lm, dm = self.machines()
        listening = Listener(self, lm)
        rogue = Rogue(listening.port)
        rogue.send(dialer_hello(lm.identity, bytes(32)))
        self.assertTrue(rogue.closed())
        self.assertEqual(listening.dial(dm).outcome, "paired")
        self.assertEqual(listening.wait_result().outcome, "paired")
        self.assertEqual(lm.side.saved[0].device_id, dm.device_id)

    def test_duplicate_keys_are_refused(self) -> None:
        def send(rogue: Rogue, ident: pairing.PairIdentity) -> None:
            body = wire.encode(dialer_hello(ident, bytes(32))).decode("utf-8")
            rogue.send_raw(wire_frame(body[:-1] + ',"v":2}'))

        self.refuse_then_pair(send, counted=False)

    def test_trailing_bytes_inside_a_frame_are_refused(self) -> None:
        def send(rogue: Rogue, ident: pairing.PairIdentity) -> None:
            rogue.send_raw(wire_frame(wire.encode(dialer_hello(ident, bytes(32))).decode("utf-8") + " {}"))

        self.refuse_then_pair(send, counted=False)

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

        self.refuse_then_pair(send, counted=False)

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

    def test_an_abandoned_session_before_the_reveal_counts_and_frees_the_listener(self) -> None:
        def send(rogue: Rogue, ident: pairing.PairIdentity) -> None:
            rogue.send(dialer_hello(ident, bytes(32)))
            rogue.recv()
            rogue.close()

        lm, dm = self.machines()
        listening = Listener(self, lm)
        rogue = Rogue(listening.port)
        send(rogue, dm.identity)
        self.assertTrue(wait_until(lambda: lm.failures() == 1))
        self.assertEqual(listening.dial(dm).outcome, "paired")
        self.assertEqual(listening.wait_result().outcome, "paired")
        self.assertEqual(dm.failures(), 0)

    def test_extra_plaintext_after_the_reveal_ends_the_session(self) -> None:
        lm, dm = self.machines()
        listening = Listener(self, lm)
        rogue = Rogue(listening.port)
        nonce = bytes([9] * 32)
        rogue.send(dialer_hello(dm.identity, nonce))
        rogue.recv()
        rogue.send({"t": "reveal", "nonce": nonce.hex()})
        rogue.send({"t": "junk"})
        self.assertEqual(listening.wait_result().outcome, "failed")
        self.assertEqual(lm.side.saved, [])
        rogue.close()

    def test_a_second_dialer_is_refused_without_affecting_the_first(self) -> None:
        gate = threading.Event()
        lm, dm = self.machines(listener=Side(hub=HUB, gate=gate))
        intruder = Machine(self, pair_identity(self.c, "charlie"), Side())
        listening = Listener(self, lm)
        first: list[pairing.PairResult] = []
        thread = threading.Thread(target=lambda: first.append(listening.dial(dm)), daemon=True)
        thread.start()
        self.assertTrue(lm.side.saving.wait(10))
        second = listening.dial(intruder)
        self.assertEqual(second.outcome, "failed")
        self.assertEqual(intruder.side.saved, [])
        gate.set()
        thread.join(30)
        self.assertEqual(first[0].outcome, "paired")
        self.assertEqual(listening.wait_result().outcome, "paired")
        self.assertEqual((lm.failures(), intruder.failures()), (0, 0))

    def test_connections_faster_than_one_per_second_are_dropped(self) -> None:
        slow = dataclasses.replace(FAST, min_interval_s=1.0)
        lm, dm = self.machines()
        listening = Listener(self, lm, timeouts=slow)
        first = Rogue(listening.port)
        self.addCleanup(first.close)
        second = Rogue(listening.port)
        self.addCleanup(second.close)
        self.assertTrue(second.closed())
        time.sleep(1.2)
        self.assertEqual(listening.dial(dm).outcome, "paired")
        listening.wait_result()

    def test_a_source_that_is_not_private_is_refused_and_logged_with_its_address(self) -> None:
        lm, _dm = self.machines()
        listening = Listener(self, lm, allow_source=pairing.private_source)
        rogue = Rogue(listening.port)
        self.addCleanup(rogue.close)
        self.assertTrue(rogue.closed())
        self.assertTrue(wait_until(lambda: bool(listening.lines)))
        self.assertEqual(listening.lines, ["join connection from 127.0.0.1 refused: the address is not a private address"])
        self.assertEqual(lm.failures(), 0)

    def test_a_connection_that_comes_too_soon_is_logged_once(self) -> None:
        slow = dataclasses.replace(FAST, min_interval_s=30.0)
        lm, _dm = self.machines()
        listening = Listener(self, lm, timeouts=slow)
        first = Rogue(listening.port)
        self.addCleanup(first.close)
        for _ in range(3):
            rogue = Rogue(listening.port)
            self.addCleanup(rogue.close)
            self.assertTrue(rogue.closed())
        self.assertTrue(wait_until(lambda: bool(listening.lines)))
        self.assertEqual(
            listening.lines, ["join connection from 127.0.0.1 refused: it came too soon after the last connection from that address"]
        )

    def test_a_locked_address_is_logged(self) -> None:
        lm, _dm = self.machines()
        for _ in range(3):
            lm.limits.record_failure("127.0.0.1")
        listening = Listener(self, lm)
        rogue = Rogue(listening.port)
        self.addCleanup(rogue.close)
        self.assertTrue(rogue.closed())
        self.assertTrue(wait_until(lambda: bool(listening.lines)))
        self.assertEqual(
            listening.lines, ["join connection from 127.0.0.1 refused: pairing is locked for that address after repeated failed attempts"]
        )

    def test_a_session_dropped_while_another_join_runs_is_logged(self) -> None:
        gate = threading.Event()
        lm, dm = self.machines(listener=Side(hub=HUB, gate=gate))
        intruder = Machine(self, pair_identity(self.c, "charlie"), Side())
        listening = Listener(self, lm)
        first: list[pairing.PairResult] = []
        thread = threading.Thread(target=lambda: first.append(listening.dial(dm)), daemon=True)
        thread.start()
        self.assertTrue(lm.side.saving.wait(10))
        self.assertEqual(listening.dial(intruder).outcome, "failed")
        gate.set()
        thread.join(30)
        self.assertTrue(wait_until(lambda: bool(listening.lines)))
        self.assertEqual(listening.lines[0], "join connection from 127.0.0.1 refused: another join was running")

    def test_a_first_frame_without_a_mode_is_logged_as_dropped(self) -> None:
        lm, _dm = self.machines()
        listening = Listener(self, lm)
        rogue = Rogue(listening.port)
        self.addCleanup(rogue.close)
        rogue.send({"t": "hello"})
        self.assertTrue(rogue.closed())
        self.assertTrue(wait_until(lambda: bool(listening.lines)))
        self.assertEqual(listening.lines, ["join connection from 127.0.0.1 refused: the first frame named no known mode"])

    def test_a_connection_that_sends_nothing_is_logged_when_it_times_out(self) -> None:
        quick = dataclasses.replace(FAST, first_frame_s=0.3)
        lm, _dm = self.machines()
        listening = Listener(self, lm, timeouts=quick)
        rogue = Rogue(listening.port)
        self.addCleanup(rogue.close)
        self.assertTrue(rogue.closed())
        self.assertTrue(wait_until(lambda: bool(listening.lines)))
        self.assertEqual(listening.lines, ["join connection from 127.0.0.1 refused: no usable first frame arrived (timed out)"])

    def test_a_good_join_logs_nothing(self) -> None:
        lm, dm = self.machines()
        listening = Listener(self, lm)
        self.assertEqual(listening.dial(dm).outcome, "paired")
        listening.wait_result()
        self.assertEqual(listening.lines, [])

    def test_a_join_right_after_a_probe_waits_out_the_spacing(self) -> None:
        slow = dataclasses.replace(FAST, min_interval_s=1.0)
        lm, dm = self.machines()
        listening = Listener(self, lm, timeouts=slow)
        found = pairing.probe_join("127.0.0.1", listening.port, lm.device_id, dm.identity, timeouts=slow)
        self.assertEqual(found.device_id, lm.device_id)
        self.assertEqual(listening.dial(dm).outcome, "paired")
        self.assertTrue(wait_until(lambda: [r.outcome for r in listening.results] == ["probed", "paired"], 30))
        self.assertEqual(lm.failures(), 0)

    def test_a_connection_that_closes_before_the_reply_is_tried_again_and_then_explained(self) -> None:
        lm, dm = self.machines()
        refuser = socket.socket()
        self.addCleanup(refuser.close)
        refuser.bind(("127.0.0.1", 0))
        refuser.listen(4)
        accepted: list[int] = []

        def close_every_connection() -> None:
            refuser.settimeout(5)
            while len(accepted) < 3:
                try:
                    conn, _ = refuser.accept()
                except OSError:
                    return
                accepted.append(1)
                conn.close()

        thread = threading.Thread(target=close_every_connection, daemon=True)
        thread.start()
        result = pairing.dial_join("127.0.0.1", refuser.getsockname()[1], lm.device_id, dm.identity, dm.side, dm.limits, timeouts=FAST)
        thread.join(10)
        self.assertEqual((result.outcome, result.reason), ("failed", pairing.CLOSED_EARLY_TEXT))
        self.assertEqual(len(accepted), 3)
        self.assertEqual(dm.side.saved, [])

    def test_the_first_frame_must_arrive_in_time(self) -> None:
        quick = dataclasses.replace(FAST, first_frame_s=0.3)
        lm, dm = self.machines()
        listening = Listener(self, lm, timeouts=quick)
        rogue = Rogue(listening.port)
        self.assertTrue(rogue.closed())
        self.assertEqual(listening.dial(dm).outcome, "paired")
        listening.wait_result()
        self.assertEqual(lm.failures(), 0)

    def test_the_exchange_must_reach_the_reveal_in_time(self) -> None:
        quick = dataclasses.replace(FAST, handshake_s=0.5)
        lm, dm = self.machines()
        listening = Listener(self, lm, timeouts=quick)
        rogue = Rogue(listening.port)
        rogue.send(dialer_hello(dm.identity, bytes(32)))
        rogue.recv()
        self.assertTrue(rogue.closed())
        self.assertEqual(lm.side.saved, [])
        self.assertTrue(wait_until(lambda: lm.failures() == 1))

    def test_dial_refuses_bad_arguments_before_connecting(self) -> None:
        lm, dm = self.machines()
        good = {"address": "127.0.0.1", "port": 47830, "peer_id": lm.device_id}
        bad: list[dict[str, Any]] = [
            {"address": "localhost"},
            {"address": "127.0.0.1."},
            {"address": "::1"},
            {"address": "example.com"},
            {"port": 0},
            {"port": 65536},
            {"peer_id": lm.device_id.split("-")[0]},
            {"peer_id": lm.device_id.lower()},
            {"peer_id": lm.device_id.replace("-", "")},
            {"peer_id": ""},
        ]
        with mock.patch.object(socket, "create_connection") as connect:
            for change in bad:
                args = {**good, **change}
                with self.assertRaises(pairing.PairingError, msg=repr(change)):
                    pairing.dial_join(args["address"], args["port"], args["peer_id"], dm.identity, dm.side, dm.limits)
        connect.assert_not_called()
        self.assertEqual(lm.side.saved, [])

    def test_dial_reports_a_refused_connection(self) -> None:
        lm, dm = self.machines()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        result = pairing.dial_join("127.0.0.1", port, lm.device_id, dm.identity, dm.side, dm.limits, timeouts=FAST)
        self.assertEqual(result.outcome, "failed")
        self.assertIn("cannot connect", result.reason)

    def test_certificate_parser_accepts_a_real_certificate(self) -> None:
        der = ssl.PEM_cert_to_DER_cert(self.a.pem)
        self.assertEqual(pairing.parse_certificate(b64(der)), der)
        for broken in (der + b"\x00", der[:-1], der[:-1] + bytes([der[-1] ^ 1])[:0]):
            with self.assertRaises(pairing.PairingError):
                pairing.parse_certificate(b64(broken))

    def test_wrong_key_on_the_listener_fails_both_sides(self) -> None:
        lm, dm = self.machines(key_of_listener=self.c)
        listening = Listener(self, lm)
        dialed = listening.dial(dm)
        listened = listening.wait_result()
        self.assertEqual((listened.outcome, dialed.outcome), ("failed", "failed"))
        self.assertEqual((lm.side.saved, dm.side.saved), ([], []))

    def test_wrong_key_on_the_dialer_fails_both_sides(self) -> None:
        lm, dm = self.machines(key_of_dialer=self.c)
        listening = Listener(self, lm)
        dialed = listening.dial(dm)
        listened = listening.wait_result()
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
            listening = Listener(self, lm)
            dialed = listening.dial(dm)
            listened = listening.wait_result()
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
