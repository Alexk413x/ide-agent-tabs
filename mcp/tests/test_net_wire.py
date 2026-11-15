from __future__ import annotations

import json
import socket
import threading
import unittest
from typing import Any

from ide_agent_tabs.net import wire
from ide_agent_tabs.net.wire import (
    CloseBody,
    InventoryBody,
    OpenBody,
    RemoteSession,
    SendBody,
    WireError,
    parse_inventory,
    parse_json,
    parse_reply,
    parse_request,
)


class FrameTest(unittest.TestCase):
    def setUp(self) -> None:
        self.left, self.right = socket.socketpair()
        self.left.settimeout(2)
        self.right.settimeout(2)
        self.addCleanup(self.left.close)
        self.addCleanup(self.right.close)

    def test_round_trip(self) -> None:
        wire.write_frame(self.left, b'{"a":1}')
        self.assertEqual(wire.read_frame(self.right), b'{"a":1}')

    def test_round_trip_at_the_limit(self) -> None:
        payload = b"x" * wire.MAX_FRAME
        thread = threading.Thread(target=wire.write_frame, args=(self.left, payload))
        thread.start()
        self.assertEqual(wire.read_frame(self.right), payload)
        thread.join()

    def test_header_is_four_bytes_big_endian(self) -> None:
        wire.write_frame(self.left, b"abc")
        self.assertEqual(self.right.recv(7), b"\x00\x00\x00\x03abc")

    def test_write_refuses_an_oversize_payload(self) -> None:
        with self.assertRaises(WireError):
            wire.write_frame(self.left, b"x" * (wire.MAX_FRAME + 1))

    def test_oversize_length_is_refused_before_the_body_is_read(self) -> None:
        self.left.sendall((wire.MAX_FRAME + 1).to_bytes(4, "big"))
        with self.assertRaises(WireError):
            wire.read_frame(self.right)

    def test_max_size_overrides_the_default(self) -> None:
        self.left.sendall((11).to_bytes(4, "big"))
        with self.assertRaises(WireError):
            wire.read_frame(self.right, max_size=10)
        wire.write_frame(self.left, b"0123456789")
        self.assertEqual(wire.read_frame(self.right, max_size=10), b"0123456789")

    def test_zero_length_is_refused(self) -> None:
        self.left.sendall(b"\x00\x00\x00\x00")
        with self.assertRaises(WireError):
            wire.read_frame(self.right)

    def test_eof_inside_the_header_is_refused(self) -> None:
        self.left.sendall(b"\x00\x00")
        self.left.close()
        with self.assertRaises(WireError):
            wire.read_frame(self.right)

    def test_eof_inside_the_body_is_refused(self) -> None:
        self.left.sendall((10).to_bytes(4, "big") + b"abc")
        self.left.close()
        with self.assertRaises(WireError):
            wire.read_frame(self.right)

    def test_eof_before_any_byte_is_refused(self) -> None:
        self.left.close()
        with self.assertRaises(WireError):
            wire.read_frame(self.right)

    def test_two_frames_in_one_write_read_separately(self) -> None:
        self.left.sendall(b"\x00\x00\x00\x01a\x00\x00\x00\x02bc")
        self.assertEqual(wire.read_frame(self.right), b"a")
        self.assertEqual(wire.read_frame(self.right), b"bc")


class ParseJsonTest(unittest.TestCase):
    def assertRejects(self, text: str | bytes) -> None:
        data = text.encode("utf-8") if isinstance(text, str) else text
        with self.assertRaises(WireError):
            parse_json(data)

    def test_parses_an_object(self) -> None:
        self.assertEqual(parse_json(b'{"a":[1,2,{"b":"c"}],"d":null,"e":true}'), {"a": [1, 2, {"b": "c"}], "d": None, "e": True})

    def test_rejects_invalid_utf8(self) -> None:
        self.assertRejects(b'{"a":"\xff"}')

    def test_rejects_invalid_json(self) -> None:
        for text in ("", "{", '{"a":}', "{'a':1}", '{"a":1} x', '{"a":"\n"}'):
            with self.subTest(text=text):
                self.assertRejects(text)

    def test_rejects_a_duplicate_key(self) -> None:
        self.assertRejects('{"a":1,"a":2}')

    def test_rejects_a_nested_duplicate_key(self) -> None:
        self.assertRejects('{"a":{"b":1,"b":2}}')
        self.assertRejects('{"a":[{"x":1},{"b":{"c":1,"c":1}}]}')

    def test_accepts_the_same_key_in_sibling_objects(self) -> None:
        self.assertEqual(parse_json(b'[{"a":1},{"a":2}]'), [{"a": 1}, {"a": 2}])

    def test_rejects_nan_and_infinity(self) -> None:
        for text in ('{"a":NaN}', '{"a":Infinity}', '{"a":-Infinity}', "[NaN]"):
            with self.subTest(text=text):
                self.assertRejects(text)

    def test_rejects_floats(self) -> None:
        for text in ('{"a":1.5}', '{"a":1.0}', '{"a":1e3}', '{"a":1E+2}', '{"a":-0.0}'):
            with self.subTest(text=text):
                self.assertRejects(text)

    def test_accepts_integers(self) -> None:
        self.assertEqual(parse_json(b'{"a":-12,"b":0}'), {"a": -12, "b": 0})

    def test_rejects_a_huge_integer(self) -> None:
        self.assertRejects('{"a":' + "9" * 17 + "}")
        self.assertRejects('{"a":-' + "9" * 17 + "}")
        self.assertEqual(parse_json(('{"a":' + "9" * 16 + "}").encode()), {"a": 10**16 - 1})

    def test_depth_limit(self) -> None:
        ok = "[" * 16 + "]" * 16
        self.assertEqual(parse_json(ok.encode()), json.loads(ok))
        self.assertRejects("[" * 17 + "]" * 17)
        self.assertRejects('{"a":' * 17 + "1" + "}" * 17)

    def test_rejects_extreme_nesting_without_recursing(self) -> None:
        self.assertRejects("[" * wire.MAX_FRAME)

    def test_brackets_inside_strings_do_not_count_as_depth(self) -> None:
        text = json.dumps({"a": "[" * 100 + '\\"[[[[', "b": "{" * 100})
        self.assertEqual(parse_json(text.encode()), json.loads(text))

    def test_rejects_an_unpaired_surrogate(self) -> None:
        self.assertRejects('{"a":"\\ud800"}')
        self.assertRejects('{"\\udc00":1}')
        self.assertEqual(parse_json(b'{"a":"\\ud83d\\ude00"}'), {"a": "\U0001f600"})

    def test_encode_is_compact_utf8(self) -> None:
        self.assertEqual(wire.encode({"a": [1, "é"]}), '{"a":[1,"é"]}'.encode())

    def test_encode_refuses_nan(self) -> None:
        with self.assertRaises(ValueError):
            wire.encode({"a": float("nan")})


class HelloTest(unittest.TestCase):
    def test_hello_frame_is_accepted(self) -> None:
        wire.check_hello(parse_json(wire.hello_frame()))

    def test_hello_frame_content(self) -> None:
        self.assertEqual(json.loads(wire.hello_frame()), {"v": 1, "hello": "agent-tabs-net"})

    def test_rejects_other_hellos(self) -> None:
        bad: list[Any] = [
            {"v": 2, "hello": "agent-tabs-net"},
            {"v": 0, "hello": "agent-tabs-net"},
            {"v": True, "hello": "agent-tabs-net"},
            {"v": "1", "hello": "agent-tabs-net"},
            {"v": 1, "hello": "other"},
            {"v": 1},
            {"hello": "agent-tabs-net"},
            {"v": 1, "hello": "agent-tabs-net", "extra": 1},
            [],
            "hello",
        ]
        for obj in bad:
            with self.subTest(obj=obj), self.assertRaises(WireError):
                wire.check_hello(obj)


def request(op: str, body: dict[str, Any], request_id: Any = "r1") -> dict[str, Any]:
    return {"id": request_id, "op": op, "body": body}


class RequestTest(unittest.TestCase):
    def assertRejects(self, obj: Any) -> None:
        with self.assertRaises(WireError):
            parse_request(obj)

    def test_open_minimal(self) -> None:
        parsed = parse_request(request("open", {"agent": "claude", "folder": "proj-1", "task": "do it"}))
        self.assertEqual(parsed.id, "r1")
        self.assertEqual(parsed.op, "open")
        self.assertEqual(parsed.body, OpenBody(agent="claude", folder="proj-1", task="do it", model=None))

    def test_open_with_model(self) -> None:
        parsed = parse_request(request("open", {"agent": "codex", "folder": "a-b", "task": "t", "model": "gpt-5.4:high/x@y+z"}))
        self.assertEqual(parsed.body, OpenBody(agent="codex", folder="a-b", task="t", model="gpt-5.4:high/x@y+z"))

    def test_open_task_length_limit(self) -> None:
        base = {"agent": "claude", "folder": "f"}
        parse_request(request("open", {**base, "task": "x" * 30000}))
        self.assertRejects(request("open", {**base, "task": "x" * 30001}))
        self.assertRejects(request("open", {**base, "task": ""}))

    def test_open_invalid_fields(self) -> None:
        good: dict[str, Any] = {"agent": "claude", "folder": "f", "task": "t"}
        cases: list[tuple[str, Any]] = [
            ("agent", "agy"),
            ("agent", "copilot"),
            ("agent", "Claude"),
            ("agent", 1),
            ("agent", None),
            ("folder", ""),
            ("folder", "a" * 65),
            ("folder", "a/b"),
            ("folder", "a.b"),
            ("folder", "a b"),
            ("folder", "..\\x"),
            ("folder", "é"),
            ("folder", 5),
            ("task", 5),
            ("task", None),
            ("task", ["t"]),
            ("model", ""),
            ("model", "a" * 201),
            ("model", "a b"),
            ("model", "a;b"),
            ("model", "a\nb"),
            ("model", 1),
            ("model", None),
            ("model", True),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.assertRejects(request("open", {**good, field: value}))

    def test_open_missing_and_unknown_fields(self) -> None:
        good: dict[str, Any] = {"agent": "claude", "folder": "f", "task": "t"}
        for field in good:
            with self.subTest(missing=field):
                self.assertRejects(request("open", {k: v for k, v in good.items() if k != field}))
        self.assertRejects(request("open", {**good, "args": ["--x"]}))
        self.assertRejects(request("open", {**good, "env": {}}))
        self.assertRejects(request("open", {**good, "cwd": "/tmp"}))

    def test_close(self) -> None:
        self.assertEqual(parse_request(request("close", {"tab": "tab-1.a"})).body, CloseBody(tab="tab-1.a"))

    def test_close_invalid(self) -> None:
        for body in ({}, {"tab": ""}, {"tab": 3}, {"tab": True}, {"tab": "a b"}, {"tab": "a" * 129}, {"tab": ".x"}, {"tab": "t", "x": 1}):
            with self.subTest(body=body):
                self.assertRejects(request("close", body))

    def test_send(self) -> None:
        parsed = parse_request(request("send", {"to": "sess_1", "text": "hi"}))
        self.assertEqual(parsed.body, SendBody(to="sess_1", text="hi", reply_to=None))
        parsed = parse_request(request("send", {"to": "s", "text": "hi", "replyTo": "m-0123456789abcdef"}))
        self.assertEqual(parsed.body, SendBody(to="s", text="hi", reply_to="m-0123456789abcdef"))

    def test_send_text_limit(self) -> None:
        parse_request(request("send", {"to": "s", "text": "é" * 32000}))
        self.assertRejects(request("send", {"to": "s", "text": "x" * 32001}))
        self.assertRejects(request("send", {"to": "s", "text": ""}))

    def test_send_invalid_fields(self) -> None:
        good: dict[str, Any] = {"to": "s", "text": "t"}
        cases: list[tuple[str, Any]] = [
            ("to", ""),
            ("to", "a" * 129),
            ("to", ".ab"),
            ("to", 1),
            ("to", None),
            ("text", 1),
            ("text", None),
            ("text", True),
            ("replyTo", "m-xyz"),
            ("replyTo", "M-0123456789abcdef"),
            ("replyTo", "m-0123456789abcdef0"),
            ("replyTo", 7),
            ("replyTo", None),
            ("replyTo", False),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.assertRejects(request("send", {**good, field: value}))
        self.assertRejects(request("send", {**good, "from": "peer"}))
        self.assertRejects(request("send", {"to": "s"}))

    def test_inventory(self) -> None:
        self.assertEqual(parse_request(request("inventory", {})).body, InventoryBody())

    def test_inventory_rejects_fields(self) -> None:
        self.assertRejects(request("inventory", {"x": 1}))

    def test_request_id_rules(self) -> None:
        parse_request(request("inventory", {}, "a" * 64))
        parse_request(request("inventory", {}, "A_b-9"))
        for bad in ("", "a" * 65, "a b", "a.b", "é", 1, None, True, ["a"]):
            with self.subTest(request_id=bad):
                self.assertRejects(request("inventory", {}, bad))

    def test_unknown_op_and_shapes(self) -> None:
        self.assertRejects(request("shell", {}))
        self.assertRejects(request("OPEN", {}))
        self.assertRejects({"id": "r", "op": "inventory"})
        self.assertRejects({"op": "inventory", "body": {}})
        self.assertRejects({"id": "r", "body": {}})
        self.assertRejects({"id": "r", "op": "inventory", "body": {}, "from": "peer"})
        self.assertRejects({"id": "r", "op": "inventory", "body": []})
        self.assertRejects({"id": "r", "op": "inventory", "body": None})
        self.assertRejects([])
        self.assertRejects("x")
        self.assertRejects(None)

    def test_op_must_be_a_string(self) -> None:
        self.assertRejects(request(["open"], {}))  # type: ignore[arg-type]
        self.assertRejects(request(None, {}))  # type: ignore[arg-type]

    def test_parsed_through_json(self) -> None:
        data = wire.encode(request("open", {"agent": "claude", "folder": "f", "task": "hé\nllo"}))
        self.assertEqual(parse_request(parse_json(data)).body, OpenBody(agent="claude", folder="f", task="hé\nllo"))


class ReplyTest(unittest.TestCase):
    def test_ok_reply(self) -> None:
        parsed = parse_reply({"re": "r1", "ok": True, "body": {"tab": "t1"}})
        self.assertEqual((parsed.re, parsed.ok, parsed.body), ("r1", True, {"tab": "t1"}))

    def test_refused_reply(self) -> None:
        parsed = parse_reply({"re": "r1", "ok": False, "refused": True})
        self.assertEqual((parsed.re, parsed.ok, parsed.body), ("r1", False, None))

    def test_builders_round_trip(self) -> None:
        self.assertEqual(parse_reply(wire.ok_reply("r", {"a": 1})).body, {"a": 1})
        self.assertFalse(parse_reply(wire.refused_reply("r")).ok)
        self.assertEqual(wire.refused_reply("r"), {"re": "r", "ok": False, "refused": True})

    def test_invalid_replies(self) -> None:
        bad: list[Any] = [
            {"re": "r1", "ok": True},
            {"re": "r1", "ok": True, "body": []},
            {"re": "r1", "ok": True, "body": {}, "refused": True},
            {"re": "r1", "ok": True, "body": {}, "reason": "x"},
            {"re": "r1", "ok": False},
            {"re": "r1", "ok": False, "refused": False},
            {"re": "r1", "ok": False, "refused": 1},
            {"re": "r1", "ok": False, "refused": True, "reason": "no"},
            {"re": "r1", "ok": False, "refused": True, "body": {}},
            {"re": "r1", "ok": 1, "body": {}},
            {"re": "r1", "ok": "true", "body": {}},
            {"re": "r1", "body": {}},
            {"ok": True, "body": {}},
            {"re": "", "ok": True, "body": {}},
            {"re": 1, "ok": True, "body": {}},
            {"re": "a b", "ok": True, "body": {}},
            [],
            None,
        ]
        for obj in bad:
            with self.subTest(obj=obj), self.assertRaises(WireError):
                parse_reply(obj)


def session(**over: Any) -> dict[str, Any]:
    return {"id": "s1", "name": "Main", "agent": "claude", "status": "idle", **over}


class InventoryTest(unittest.TestCase):
    def assertRejects(self, body: Any) -> None:
        with self.assertRaises(WireError):
            parse_inventory(body)

    def test_valid(self) -> None:
        rows = parse_inventory({"sessions": [session(), session(id="s_2", name="é 日本", agent="codex-ollama", status="permission")]})
        self.assertEqual(
            rows,
            [RemoteSession("s1", "Main", "claude", "idle"), RemoteSession("s_2", "é 日本", "codex-ollama", "permission")],
        )

    def test_empty(self) -> None:
        self.assertEqual(parse_inventory({"sessions": []}), [])

    def test_every_agent_and_status(self) -> None:
        for agent in wire.SESSION_AGENTS:
            with self.subTest(agent=agent):
                parse_inventory({"sessions": [session(agent=agent)]})
        for status in ("idle", "busy", "permission", "waking", "unknown"):
            with self.subTest(status=status):
                parse_inventory({"sessions": [session(status=status)]})

    def test_session_count_limit(self) -> None:
        make = [session(id=f"s{i}") for i in range(256)]
        self.assertEqual(len(parse_inventory({"sessions": make})), 256)
        self.assertRejects({"sessions": make + [session(id="s256")]})

    def test_id_rules(self) -> None:
        parse_inventory({"sessions": [session(id="a" * 128)]})
        for bad in ("a" * 129, "", ".ab", "a b", "a/b", "é", "a\n", 1, None, True):
            with self.subTest(id=bad):
                self.assertRejects({"sessions": [session(id=bad)]})

    def test_name_rules(self) -> None:
        parse_inventory({"sessions": [session(name="n" * 128)]})
        parse_inventory({"sessions": [session(name="")]})
        self.assertRejects({"sessions": [session(name="n" * 129)]})
        for char in ("\x00", "\n", "\r", "\t", "\x1b", "\x7f", "\x85", "\u2028", "\u2029"):
            with self.subTest(char=repr(char)):
                self.assertRejects({"sessions": [session(name="a" + char + "b")]})
        for bad in (1, None, ["x"]):
            with self.subTest(name=bad):
                self.assertRejects({"sessions": [session(name=bad)]})

    def test_name_length_counts_characters(self) -> None:
        parse_inventory({"sessions": [session(name="\U0001f600" * 128)]})
        self.assertRejects({"sessions": [session(name="\U0001f600" * 129)]})

    def test_duplicate_ids(self) -> None:
        self.assertRejects({"sessions": [session(), session(name="other")]})

    def test_bad_agent_and_status(self) -> None:
        for bad in ("gemini", "", "Claude", 1, None):
            with self.subTest(agent=bad):
                self.assertRejects({"sessions": [session(agent=bad)]})
        for bad in ("done", "", "IDLE", 0, None):
            with self.subTest(status=bad):
                self.assertRejects({"sessions": [session(status=bad)]})

    def test_unknown_and_missing_fields(self) -> None:
        self.assertRejects({"sessions": [session(path="/home/x")]})
        self.assertRejects({"sessions": [session(folder="f")]})
        for field in ("id", "name", "agent", "status"):
            with self.subTest(missing=field):
                row = session()
                del row[field]
                self.assertRejects({"sessions": [row]})
        self.assertRejects({"sessions": [], "extra": 1})
        self.assertRejects({})

    def test_wrong_shapes(self) -> None:
        for bad in (None, [], "x", {"sessions": None}, {"sessions": {}}, {"sessions": ["s"]}, {"sessions": [None]}, {"sessions": [[]]}):
            with self.subTest(body=bad):
                self.assertRejects(bad)

    def test_inventory_body_builds_a_valid_body(self) -> None:
        sessions = [RemoteSession("s1", "Main", "claude", "busy")]
        body = wire.inventory_body(sessions)
        self.assertEqual(body, {"sessions": [{"id": "s1", "name": "Main", "agent": "claude", "status": "busy"}]})
        self.assertEqual(parse_inventory(body), sessions)

    def test_inventory_body_refuses_an_invalid_session(self) -> None:
        with self.assertRaises(WireError):
            wire.inventory_body([RemoteSession("s1", "a\nb", "claude", "busy")])

    def test_reply_through_the_wire(self) -> None:
        body = wire.inventory_body([RemoteSession("s1", "Main", "claude", "idle")])
        reply = parse_reply(parse_json(wire.encode(wire.ok_reply("inv-1", body))))
        assert reply.body is not None
        self.assertEqual(parse_inventory(reply.body), [RemoteSession("s1", "Main", "claude", "idle")])


class EscapeDisplayTest(unittest.TestCase):
    def test_plain_text_is_unchanged(self) -> None:
        self.assertEqual(wire.escape_display("Main tab é 日本 \U0001f600"), "Main tab é 日本 \U0001f600")

    def test_named_escapes(self) -> None:
        self.assertEqual(wire.escape_display("a\nb\tc\rd"), "a\\nb\\tc\\rd")

    def test_backslash_is_escaped(self) -> None:
        self.assertEqual(wire.escape_display("a\\nb"), "a\\\\nb")

    def test_control_characters_use_hex(self) -> None:
        self.assertEqual(wire.escape_display("\x1b[31mred"), "\\x1b[31mred")
        self.assertEqual(wire.escape_display("\x00\x7f\x85"), "\\x00\\x7f\\x85")

    def test_line_separators_and_format_characters_are_escaped(self) -> None:
        self.assertEqual(wire.escape_display("a\u2028b\u2029c"), "a\\u2028b\\u2029c")
        self.assertEqual(wire.escape_display("\u202eevil"), "\\u202eevil")

    def test_unpaired_surrogate_is_escaped(self) -> None:
        self.assertEqual(wire.escape_display("\ud800"), "\\ud800")

    def test_result_is_a_single_line(self) -> None:
        shown = wire.escape_display("line one\nline two\r\n# heading\x1b]0;title\x07")
        self.assertEqual(shown.splitlines(), [shown])
        self.assertTrue(shown.isprintable())


if __name__ == "__main__":
    unittest.main()
