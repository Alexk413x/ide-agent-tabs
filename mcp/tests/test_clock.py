from __future__ import annotations

import time
import unittest

from ide_agent_tabs import clock
from support import js_fixtures


class ClockTest(unittest.TestCase):
    def test_iso_matches_to_iso_string(self) -> None:
        for case in js_fixtures()["isoTimes"]:
            with self.subTest(ms=case["ms"]):
                self.assertEqual(clock.iso(case["ms"]), case["iso"])

    def test_parse_iso_matches_date_parse_for_utc_and_offset_forms(self) -> None:
        for case in js_fixtures()["dateParse"]:
            with self.subTest(text=case["text"]):
                self.assertEqual(clock.parse_iso(case["text"]), case["ms"])

    def test_parse_iso_reads_a_time_without_offset_as_local_time(self) -> None:
        expected = int(time.mktime((2026, 10, 8, 12, 34, 0, 0, 0, -1))) * 1000
        self.assertEqual(clock.parse_iso("2026-10-08T12:34"), expected)

    def test_round_trip(self) -> None:
        now = clock.now_ms()
        self.assertEqual(clock.parse_iso(clock.iso(now)), now)
        self.assertRegex(clock.now_iso(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


if __name__ == "__main__":
    unittest.main()
