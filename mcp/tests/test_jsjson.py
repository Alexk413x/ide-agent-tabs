from __future__ import annotations

import json
import unittest

from ide_agent_tabs import jsjson
from support import js_fixtures


class StringifyTest(unittest.TestCase):
    def test_matches_json_stringify_for_every_fixture(self) -> None:
        for case in js_fixtures()["stringify"]:
            value = jsjson.parse(case["input"])
            with self.subTest(input=case["input"]):
                self.assertEqual(jsjson.stringify(value), case["compact"])
                self.assertEqual(jsjson.stringify(value, 2), case["pretty"])

    def test_output_encodes_as_utf8_even_with_lone_surrogates(self) -> None:
        text = jsjson.stringify(["\ud800", "\ud83d\ude00", "\U0001f600"])
        self.assertEqual(text, '["\\ud800","\U0001f600","\U0001f600"]')
        text.encode("utf-8")

    def test_refuses_values_json_has_no_form_for(self) -> None:
        with self.assertRaises(TypeError):
            jsjson.stringify({1.5: "x"})
        with self.assertRaises(TypeError):
            jsjson.stringify(object())

    def test_non_finite_numbers_become_null_like_javascript(self) -> None:
        self.assertEqual(jsjson.stringify([float("nan"), float("inf"), -float("inf"), 10**400]), "[null,null,null,null]")

    def test_parse_refuses_what_json_parse_refuses(self) -> None:
        for text in ("NaN", "[Infinity]", "-Infinity", "{'a': 1}", "[1,]", ""):
            with self.subTest(text=text), self.assertRaises(ValueError):
                jsjson.parse(text)

    def test_tuples_write_as_arrays(self) -> None:
        self.assertEqual(jsjson.stringify(("a", 1)), json.dumps(["a", 1], separators=(",", ":")))


class Utf16Test(unittest.TestCase):
    def test_length_counts_utf16_units(self) -> None:
        for case in js_fixtures()["utf16Length"]:
            with self.subTest(text=case["text"]):
                self.assertEqual(jsjson.utf16_len(case["text"]), case["length"])

    def test_slice_cuts_like_string_slice(self) -> None:
        for case in js_fixtures()["utf16Slice"]:
            with self.subTest(case=case):
                self.assertEqual(jsjson.utf16_slice(case["text"], case["start"], case["end"]), case["slice"])

    def test_slice_takes_negative_and_open_ends(self) -> None:
        self.assertEqual(jsjson.utf16_slice("abcdef", -2), "ef")
        self.assertEqual(jsjson.utf16_slice("abcdef", 1, -1), "bcde")
        self.assertEqual(jsjson.utf16_slice("abcdef", 4, 2), "")

    def test_well_formed_replaces_lone_surrogates(self) -> None:
        self.assertEqual(jsjson.well_formed("a\ud800b\ud83d\ude00"), "a\ufffdb\U0001f600")


if __name__ == "__main__":
    unittest.main()
