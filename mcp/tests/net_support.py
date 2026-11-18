from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from ide_agent_tabs.net import ingress, wire
from ide_agent_tabs.net.certgen import CERT_FILE
from ide_agent_tabs.net.cli import Services
from ide_agent_tabs.net.cli import main as net_main
from ide_agent_tabs.net.client import local_device_id, peer_cert_path, peers_dir
from ide_agent_tabs.net.identity import der_from_pem
from ide_agent_tabs.net.listener import Listener, Options
from ide_agent_tabs.net.policy import NetworkConfig, PeerGrant, Policy, load_policy, save_policy
from ide_agent_tabs.net.private_files import ensure_private_dir
from ide_agent_tabs.shared.state import ServerState, write_state

OPENSSL = Path(r"C:\Program Files\Git\usr\bin\openssl.exe") if os.name == "nt" else Path(shutil.which("openssl") or "/nonexistent/openssl")
HAS_OPENSSL = OPENSSL.is_file()
LOOPBACK = "127.0.0.1"
TEST_NETWORK = "test-network"


def make_root(test: type[unittest.TestCase] | unittest.TestCase, prefix: str = "iat-net-") -> str:
    root = os.path.realpath(tempfile.mkdtemp(prefix=prefix))
    cleanup = test.addClassCleanup if isinstance(test, type) else test.addCleanup
    cleanup(shutil.rmtree, root, True)
    return root


def create_home(root: str, name: str) -> str:
    home = os.path.join(root, name)
    os.makedirs(home)
    services = Services(home=home, confirm=lambda _lines: True, notify=lambda _what: None, out=lambda _t: None, err=lambda _t: None)
    code = net_main(["lan", "setup", "--openssl", str(OPENSSL)], services)
    assert code == 0, f"lan setup failed with {code}"
    return home


def device_of(home: str) -> str:
    identity = local_device_id(home)
    assert identity is not None
    return identity


def cert_of(home: str) -> Path:
    return Path(home) / "net" / "identity" / CERT_FILE


def reset_peers(home: str) -> None:
    shutil.rmtree(peers_dir(home), ignore_errors=True)
    for name in ("policy.json", "policy.json.lock"):
        try:
            os.remove(os.path.join(home, "net", name))
        except OSError:
            pass


def trust_peer(home: str, other: str, label: str, address: str = LOOPBACK) -> str:
    identity = device_of(other)
    ensure_private_dir(peers_dir(home))
    peer_cert_path(home, identity).write_text(cert_of(other).read_text(encoding="ascii"), encoding="ascii")
    policy = load_policy(home)
    peers = tuple(p for p in policy.peers if p.device_id != identity) + (PeerGrant(device_id=identity, label=label, address=address),)
    save_policy(home, Policy(peers, policy.generation))
    return identity


def forget_peer(home: str, identity: str) -> None:
    policy = load_policy(home)
    save_policy(home, Policy(tuple(p for p in policy.peers if p.device_id != identity), policy.generation))
    try:
        os.remove(peer_cert_path(home, identity))
    except OSError:
        pass


class FakeIngress:
    def __init__(
        self,
        home: str,
        reply: Callable[[str, dict[str, Any]], Any] | None = None,
        *,
        health_pid: int | None = None,
        state_pid: int | None = None,
        serve_health: bool = True,
    ) -> None:
        self.home = home
        self.seen: list[dict[str, Any]] = []
        self.token = ingress.ensure_ingress_token(home)
        self.reply = reply or (lambda peer, request: wire.ok_reply(request["id"], {"peer": peer}))
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _send(self, status: int, payload: Any) -> None:
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path == "/health" and serve_health:
                    self._send(
                        200,
                        {"service": "ide-agent-tabs", "pid": health_pid or os.getpid(), "version": "1", "port": outer.port},
                    )
                else:
                    self._send(404, b"not here")

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                outer.seen.append(
                    {
                        "path": self.path,
                        "host": self.headers.get("Host"),
                        "auth": self.headers.get("Authorization"),
                        "type": self.headers.get("Content-Type"),
                        "body": body,
                    }
                )
                if self.path != ingress.INGRESS_PATH or self.headers.get("Authorization") != f"Bearer {outer.token}":
                    self._send(403, {"error": "forbidden"})
                    return
                self._send(200, outer.reply(body["peer"], body["request"]))

        self.server = ThreadingHTTPServer((LOOPBACK, 0), Handler)
        self.port: int = self.server.server_address[1]
        write_state(home, ServerState(state_pid or os.getpid(), self.port, "1", "now", "0" * 64))
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(5)


class RunningListener:
    def __init__(self, home: str, server_port: int, *, network_check: Callable[[str], bool | None] = lambda _bind: False) -> None:
        self.home = home
        self.ready = threading.Event()
        self.port = 0
        self.options = Options(
            config=NetworkConfig(network="lan", bind=LOOPBACK, port=0, allowed_networks=(TEST_NETWORK,)),
            bind_check=lambda _address: True,
            network_check=network_check,
            network_id=lambda _bind: TEST_NETWORK,
            server_port=server_port,
            ready=self._ready,
        )
        self.listener = Listener(home, self.options)
        self.code: int | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _ready(self, port: int) -> None:
        self.port = port
        self.ready.set()

    def _run(self) -> None:
        self.code = self.listener.run()
        self.ready.set()

    def start(self) -> RunningListener:
        self._thread.start()
        assert self.ready.wait(20), "the listener did not start"
        assert self.code is None, f"the listener refused to start with {self.code}"
        return self

    def stop(self) -> None:
        self.options.stop.set()
        self._thread.join(10)


def wait_until(condition: Callable[[], bool], seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind((LOOPBACK, 0))
        return int(sock.getsockname()[1])


def pem_der(home: str) -> bytes:
    return der_from_pem(cert_of(home).read_text(encoding="ascii"))
