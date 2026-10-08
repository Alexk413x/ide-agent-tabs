from __future__ import annotations

import math
import socket
import sys
import time
import unittest
from email.message import Message

from ide_agent_tabs.jev import client
from ide_agent_tabs.jev.client import JevError, call_jev, js_to_number, retry_delay_ms
from ide_agent_tabs.jsjson import parse
from jev_support import StubTypeSafe

QUESTIONS = {"q": {"type": "noul", "instructions": "Is it?"}}
OK = {"status": 200, "body": '{"model":"jev-1","answers":{"q":{"type":"noul","noul":0.5}},"usage":{"input_tokens":3}}'}


def headers(**values: str) -> Message:
    m = Message()
    for name, value in values.items():
        m[name.replace("_", "-")] = value
    return m


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Retries(unittest.TestCase):
    stub: StubTypeSafe

    @classmethod
    def setUpClass(cls) -> None:
        cls.stub = StubTypeSafe()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.stub.close()

    def call(self, **kwargs: object) -> dict[str, object]:
        return call_jev("tsk-secret", self.stub.url, "state", QUESTIONS, **kwargs)  # type: ignore[arg-type]

    def test_sends_one_post_with_sdk_headers(self) -> None:
        self.stub.set(OK)
        result = self.call()
        self.assertEqual(result["model"], "jev-1")
        [seen] = self.stub.seen
        self.assertEqual((seen["method"], seen["path"]), ("POST", "/v1/systemone"))
        self.assertEqual(seen["headers"]["authorization"], "Bearer tsk-secret")
        self.assertEqual(seen["headers"]["user-agent"], "typesafe-sdk/0.6.0")
        self.assertIsNone(seen["headers"]["x-typesafe-retry-count"])
        self.assertEqual(parse(seen["body"]), {"state": "state", "questions": QUESTIONS, "model": "jev-latest"})

    def test_retries_twice_then_reports_the_status(self) -> None:
        self.stub.set({"status": 500, "body": '{"error":"boom tsk-secret"}'})
        delays: list[float] = []
        with self.assertRaises(JevError) as caught:
            self.call(sleep=delays.append, rand=lambda: 0.0)
        self.assertEqual(caught.exception.message, "TypeSafe answered HTTP 500: boom ***")
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(delays, [0.5, 1.0])
        self.assertEqual([s["headers"]["x-typesafe-retry-count"] for s in self.stub.seen], [None, "1", "2"])

    def test_does_not_retry_a_client_error(self) -> None:
        self.stub.set({"status": 401, "body": ""})
        with self.assertRaises(JevError) as caught:
            self.call(sleep=lambda _: self.fail("no retry expected"))
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(len(self.stub.seen), 1)

    def test_retries_a_slow_attempt_then_times_out(self) -> None:
        self.stub.set(OK, delay_s=0.6)
        with self.assertRaises(JevError) as caught:
            self.call(sleep=lambda _: None, attempt_ms=150, limit_ms=10_000)
        self.assertEqual((caught.exception.message, caught.exception.status), ("Jev did not answer within 30 s.", "timeout"))
        self.assertEqual(len(self.stub.seen), 3)

    def test_the_call_limit_stops_retries(self) -> None:
        self.stub.set(OK, delay_s=1.5)
        started = time.monotonic()
        with self.assertRaises(JevError) as caught:
            self.call(limit_ms=300)
        self.assertEqual(caught.exception.status, "timeout")
        self.assertLess(time.monotonic() - started, 1.2)
        self.assertEqual(len(self.stub.seen), 1)

    def test_a_backoff_past_the_limit_ends_the_call(self) -> None:
        self.stub.set({"status": 503, "body": "", "headers": {"retry-after": "50"}})
        with self.assertRaises(JevError) as caught:
            self.call(limit_ms=2_000, sleep=lambda _: self.fail("no sleep past the limit"))
        self.assertEqual(caught.exception.status, "timeout")

    def test_a_reply_that_is_not_an_object_reads_as_empty(self) -> None:
        self.stub.set({"status": 200, "body": "not json"})
        self.assertEqual(self.call(), {})


class Connection(unittest.TestCase):
    def test_refused_connection_is_retried_then_reported(self) -> None:
        delays: list[float] = []
        with self.assertRaises(JevError) as caught:
            call_jev("k", f"http://127.0.0.1:{free_port()}", "s", QUESTIONS, sleep=delays.append, rand=lambda: 1.0)
        self.assertEqual(caught.exception.message, "Could not reach TypeSafe: Connection error: fetch failed")
        self.assertEqual(caught.exception.status, "connection")
        self.assertEqual(delays, [0.375, 0.75])

    def test_base_url_comes_from_the_environment(self) -> None:
        self.assertEqual(client.base_url_of({}), "https://api.typesafe.ai")
        self.assertEqual(client.base_url_of({"TYPESAFE_BASE_URL": ""}), "https://api.typesafe.ai")
        self.assertEqual(client.base_url_of({"TYPESAFE_BASE_URL": "http://127.0.0.1:9/api//"}), "http://127.0.0.1:9/api")

    def test_runtime_header_names_python(self) -> None:
        v = sys.version_info
        self.assertTrue(client.runtime_header().startswith(f"python/{v.major}.{v.minor}.{v.micro} ({sys.platform}; "))


class RetryDelay(unittest.TestCase):
    def test_backoff_doubles_to_a_cap_with_jitter(self) -> None:
        self.assertEqual([retry_delay_ms(a, None, lambda: 0.0) for a in range(5)], [500, 1000, 2000, 4000, 5000])
        self.assertEqual(retry_delay_ms(0, None, lambda: 1.0), 375)
        self.assertEqual(retry_delay_ms(1, None, lambda: 0.5), 875)

    def test_server_delays_are_honoured_up_to_a_minute(self) -> None:
        self.assertEqual(retry_delay_ms(0, headers(retry_after_ms="1500"), lambda: 0.0), 1500)
        self.assertEqual(retry_delay_ms(0, headers(retry_after_ms="x", retry_after="2"), lambda: 0.0), 2000)
        self.assertEqual(retry_delay_ms(0, headers(retry_after="61"), lambda: 0.0), 500)
        self.assertEqual(retry_delay_ms(0, headers(retry_after="-1"), lambda: 0.0), 500)
        self.assertEqual(retry_delay_ms(0, headers(retry_after="soon"), lambda: 0.0), 500)
        self.assertEqual(retry_delay_ms(0, headers(retry_after="Thu, 01 Jan 1970 00:00:00 GMT"), lambda: 0.0), 0)

    def test_numbers_parse_like_javascript(self) -> None:
        cases = {"": 0.0, " 12 ": 12.0, "1e3": 1000.0, "0x10": 16.0, "0B11": 3.0, ".5": 0.5, "5.": 5.0, "-Infinity": -math.inf}
        for text, want in cases.items():
            self.assertEqual(js_to_number(text), want, text)
        for text in ("1_0", "nan", "inf", "0x", "0x1g", "1e", "abc", "--1"):
            self.assertTrue(math.isnan(js_to_number(text)), text)


if __name__ == "__main__":
    unittest.main()
