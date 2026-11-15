from __future__ import annotations

import json
import os
import threading
import unittest
from pathlib import Path
from typing import Any

from ide_agent_tabs.net import ingress
from ide_agent_tabs.net.ingress import (
    INGRESS_PATH,
    IngressTokenError,
    ensure_ingress_token,
    ingress_allowed,
    ingress_token_path,
    read_ingress_token,
)
from ide_agent_tabs.shared.engine import Engine
from ide_agent_tabs.shared.host import Binding, BoundSession
from shared_support import Reply, request, reset_store_mode, serve
from support import temp_home
from test_net_dispatch import PEER, FakeService, folder_of, write_config, write_policy

TOKEN_A = "a" * 64
TOKEN_B = "b" * 64


class TokenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home(self)

    def test_no_file_means_no_token(self) -> None:
        self.assertIsNone(read_ingress_token(self.home))

    def test_ensure_creates_a_private_64_digit_token_once(self) -> None:
        token = ensure_ingress_token(self.home)
        self.assertRegex(token, "^[0-9a-f]{64}$")
        self.assertEqual(ensure_ingress_token(self.home), token)
        self.assertEqual(read_ingress_token(self.home), token)

    def test_a_malformed_token_file_reads_as_none(self) -> None:
        ensure_ingress_token(self.home)
        for text in ("", "short", "G" * 64, TOKEN_A + "00"):
            Path(ingress_token_path(self.home)).write_text(text, encoding="ascii")
            self.assertIsNone(read_ingress_token(self.home))

    def test_a_token_file_that_is_not_private_raises(self) -> None:
        if os.name == "nt":
            self.skipTest("file modes are not the Windows access check")
        ensure_ingress_token(self.home)
        os.chmod(ingress_token_path(self.home), 0o644)
        with self.assertRaises(IngressTokenError):
            read_ingress_token(self.home)

    def test_allowed_compares_the_bearer_value_exactly(self) -> None:
        self.assertTrue(ingress_allowed(f"Bearer {TOKEN_A}", TOKEN_A))
        self.assertFalse(ingress_allowed(f"Bearer {TOKEN_B}", TOKEN_A))
        self.assertFalse(ingress_allowed(f"bearer {TOKEN_A}", TOKEN_A))
        self.assertFalse(ingress_allowed(TOKEN_A, TOKEN_A))
        self.assertFalse(ingress_allowed(None, TOKEN_A))
        self.assertFalse(ingress_allowed(f"Bearer {TOKEN_A}", None))

    def test_the_route_constant(self) -> None:
        self.assertEqual(INGRESS_PATH, "/net/ingress")
        self.assertIs(ingress.INGRESS_PATH, INGRESS_PATH)


class RecordingHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.answer: Any = None
        self.fail = False

    def tools_for(self, agent: str) -> list[dict[str, Any]]:
        return []

    def instructions(self) -> str:
        return ""

    def bind(self, binding: Binding) -> BoundSession:
        raise AssertionError("no session binds in this test")

    def close(self) -> None:
        return None

    def net_ingress(self, peer: str, request: object) -> dict[str, Any]:
        self.calls.append((peer, request))
        if self.fail:
            raise RuntimeError("secret detail C:/Users/someone")
        return self.answer if self.answer is not None else {"re": "r1", "ok": True, "body": {"sessions": []}}


class NoIngressHost(RecordingHost):
    net_ingress = None  # type: ignore[assignment]


def envelope(peer: str = PEER, req: object = None) -> str:
    return json.dumps({"peer": peer, "request": req if req is not None else {"id": "r1", "op": "inventory", "body": {}}})


class RouteCase(unittest.TestCase):
    host_type: type[RecordingHost] = RecordingHost

    def setUp(self) -> None:
        self.home = temp_home(self)
        self.host = self.host_type()
        self.server = serve(self, self.home, self.host)  # pyright: ignore[reportArgumentType]
        self.ingress_token = ensure_ingress_token(self.home)

    def post(
        self, body: str | None = None, token: str | None = None, headers: dict[str, str] | None = None, route: str = INGRESS_PATH
    ) -> Reply:
        sent = {"Content-Type": "application/json"}
        sent["Authorization"] = f"Bearer {token if token is not None else self.ingress_token}"
        sent.update(headers or {})
        return request(self.server.port, route, "POST", sent, envelope() if body is None else body)


class RouteTest(RouteCase):
    def test_the_ingress_token_reaches_the_dispatcher(self) -> None:
        reply = self.post()
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.json, {"re": "r1", "ok": True, "body": {"sessions": []}})
        self.assertEqual(self.host.calls, [(PEER, {"id": "r1", "op": "inventory", "body": {}})])

    def test_the_user_token_is_refused_on_the_ingress_route(self) -> None:
        reply = self.post(token=self.server.token)
        self.assertEqual(reply.status, 403)
        self.assertEqual(self.host.calls, [])

    def test_the_ingress_token_is_refused_on_mcp_and_end(self) -> None:
        headers = {"Accept": "application/json"}
        self.assertEqual(self.post("{}", headers=headers, route="/mcp").status, 403)
        self.assertEqual(self.post(json.dumps({"pid": 4242}), route="/end").status, 403)
        self.assertEqual(self.post("{}", route="/shutdown").status, 403)
        self.assertEqual(self.host.calls, [])

    def test_a_missing_or_wrong_token_is_refused(self) -> None:
        self.assertEqual(self.post(token="0" * 64).status, 403)
        self.assertEqual(self.post(token="").status, 403)
        reply = request(self.server.port, INGRESS_PATH, "POST", {"Content-Type": "application/json"}, envelope())
        self.assertEqual(reply.status, 403)
        self.assertEqual(self.host.calls, [])

    def test_without_a_token_file_every_request_is_refused(self) -> None:
        os.remove(ingress_token_path(self.home))
        self.assertEqual(self.post(token=TOKEN_A).status, 403)
        self.assertEqual(self.post(token=self.server.token).status, 403)

    def test_the_token_is_read_for_each_request(self) -> None:
        Path(ingress_token_path(self.home)).write_text(TOKEN_B, encoding="ascii")
        self.assertEqual(self.post().status, 403)
        self.assertEqual(self.post(token=TOKEN_B).status, 200)

    def test_host_and_origin_checks_apply(self) -> None:
        self.assertEqual(self.post(headers={"Host": "example.test"}).status, 403)
        self.assertEqual(self.post(headers={"Origin": "http://evil.test"}).status, 403)
        self.assertEqual(self.post(headers={"Origin": f"http://127.0.0.1:{self.server.port}"}).status, 200)

    def test_only_post_with_json_is_accepted(self) -> None:
        get = request(self.server.port, INGRESS_PATH, "GET", {"Authorization": f"Bearer {self.ingress_token}"})
        self.assertEqual(get.status, 405)
        self.assertEqual(self.post(headers={"Content-Type": "text/plain"}).status, 400)
        self.assertEqual(self.post(headers={"Content-Type": "application/json; charset=utf-8"}).status, 200)

    def test_a_body_over_64_kib_is_refused_unread(self) -> None:
        big = json.dumps({"peer": PEER, "request": {"id": "r1", "op": "inventory", "body": {}, "pad": "x" * 70_000}})
        self.assertEqual(self.post(big).status, 413)
        self.assertEqual(self.host.calls, [])

    def test_a_body_just_under_the_cap_is_passed_on(self) -> None:
        pad = 64 * 1024 - len(envelope(req={"id": "r1", "pad": ""})) - 16
        self.assertEqual(self.post(envelope(req={"id": "r1", "pad": "x" * pad})).status, 200)

    def test_a_malformed_envelope_is_a_400(self) -> None:
        for body in (
            "not json",
            "[]",
            "{}",
            json.dumps({"peer": PEER}),
            json.dumps({"request": {}}),
            json.dumps({"peer": 5, "request": {}}),
        ):
            self.assertEqual(self.post(body).status, 400, body)
        extra = json.dumps({"peer": PEER, "request": {}, "extra": 1})
        self.assertEqual(self.post(extra).status, 400)
        self.assertEqual(self.host.calls, [])

    def test_a_dispatcher_failure_is_the_generic_refusal(self) -> None:
        self.host.fail = True
        reply = self.post(envelope(req={"id": "r7", "op": "inventory", "body": {}}))
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.json, {"re": "r7", "ok": False, "refused": True})
        self.assertNotIn("secret", reply.text)
        self.assertNotIn("Traceback", reply.text)

    def test_a_request_without_a_usable_id_is_refused_under_a_placeholder_id(self) -> None:
        self.host.fail = True
        self.assertEqual(self.post(envelope(req={"op": "x"})).json, {"re": "invalid", "ok": False, "refused": True})

    def test_the_peer_is_passed_as_given(self) -> None:
        self.post(envelope(peer="SOME-OTHER-PEER"))
        self.assertEqual(self.host.calls[0][0], "SOME-OTHER-PEER")


class NetworkOffRouteTest(RouteCase):
    host_type = NoIngressHost

    def test_a_host_without_networking_refuses(self) -> None:
        reply = self.post(envelope(req={"id": "r3", "op": "inventory", "body": {}}))
        self.assertEqual((reply.status, reply.json), (200, {"re": "r3", "ok": False, "refused": True}))


class EngineWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home(self, "iat-ingress-")
        env = {k: v for k, v in os.environ.items() if not k.startswith("IDE_AGENT_TABS")}
        self.engine = Engine(self.home, lambda _m: None, env=env, detect=False)
        self.addCleanup(self.engine.close)
        self.server = serve(self, self.home, self.engine)
        self.token = ensure_ingress_token(self.home)

    def post(self, req: object, peer: str = PEER) -> Reply:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}
        return request(self.server.port, INGRESS_PATH, "POST", headers, envelope(peer, req))

    def test_the_engine_exposes_one_dispatcher(self) -> None:
        self.assertIs(self.engine.net_dispatcher(), self.engine.net_dispatcher())

    def test_the_engine_refuses_while_the_network_is_off(self) -> None:
        reply = self.post({"id": "r1", "op": "inventory", "body": {}})
        self.assertEqual((reply.status, reply.json), (200, {"re": "r1", "ok": False, "refused": True}))
        self.assertFalse(os.path.exists(os.path.join(self.home, "net", "net.db")))

    def test_the_engine_answers_an_inventory_for_a_granted_peer(self) -> None:
        folder = os.path.join(self.home, "proj")
        os.makedirs(folder)
        from ide_agent_tabs.net.policy import PeerGrant

        write_config(self.home, network="lan", bind="192.168.1.10")
        write_policy(self.home, [PeerGrant(PEER, "laptop", "192.168.1.20", (folder_of(folder),), ("claude",))])
        reply = self.post({"id": "r1", "op": "inventory", "body": {}})
        self.assertEqual((reply.status, reply.json), (200, {"re": "r1", "ok": True, "body": {"sessions": []}}))
        self.assertEqual(
            self.post({"id": "r2", "op": "inventory", "body": {}}, peer="UNKNOWN").json, {"re": "r2", "ok": False, "refused": True}
        )

    def test_the_engine_dispatcher_builds_its_service_from_the_engine(self) -> None:
        built: list[int] = []
        self.engine.service = lambda env: built.append(1) or FakeService()  # type: ignore[method-assign]
        dispatcher = self.engine.net_dispatcher()
        dispatcher._tabs()  # pyright: ignore[reportPrivateUsage]
        dispatcher._tabs()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(built, [1])


class ConcurrencyTest(RouteCase):
    def test_parallel_requests_each_get_their_own_reply(self) -> None:
        replies: list[Reply] = []
        lock = threading.Lock()

        def one(n: int) -> None:
            got = self.post(envelope(req={"id": f"r{n}", "op": "inventory", "body": {}}))
            with lock:
                replies.append(got)

        self.host.answer = None
        threads = [threading.Thread(target=one, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual([r.status for r in replies], [200] * 8)
        self.assertEqual(len(self.host.calls), 8)


def tearDownModule() -> None:
    reset_store_mode()


if __name__ == "__main__":
    unittest.main()
