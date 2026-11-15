from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from typing import Any

from ide_agent_tabs.net.audit import ADMITTED, REFUSED, Audit, AuditError, AuditRecord, audit_path
from ide_agent_tabs.net.private_files import check_private
from support import temp_home

NOW = 1_800_000_000_000


def record(**extra: Any) -> AuditRecord:
    fields: dict[str, Any] = {
        "peer": "PEER-ID",
        "request_id": "r1",
        "op": "open",
        "decision": ADMITTED,
        "outcome": "completed",
        "reason": "ok",
        "generation": 3,
        "tab": "tab-1",
    }
    fields.update(extra)
    return AuditRecord(**fields)


def lines(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f.read().splitlines()]


class AuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home(self)
        self.path = audit_path(self.home)
        self.now = NOW
        self.audit = Audit(self.home, lambda: self.now)

    def test_a_line_holds_the_documented_fields_and_nothing_else(self) -> None:
        self.audit.write(record())
        self.assertEqual(
            lines(self.path),
            [
                {
                    "time": "2027-01-15T08:00:00.000Z",
                    "peer": "PEER-ID",
                    "id": "r1",
                    "op": "open",
                    "decision": "admitted",
                    "outcome": "completed",
                    "reason": "ok",
                    "generation": 3,
                    "tab": "tab-1",
                }
            ],
        )

    def test_unknown_values_are_null(self) -> None:
        self.audit.write(record(request_id=None, op=None, generation=None, tab=None, decision=REFUSED, reason="bad-request"))
        row = lines(self.path)[0]
        self.assertEqual((row["id"], row["op"], row["generation"], row["tab"], row["decision"]), (None, None, None, None, "refused"))

    def test_lines_append_in_order(self) -> None:
        for n in range(3):
            self.audit.write(record(request_id=f"r{n}"))
        self.assertEqual([r["id"] for r in lines(self.path)], ["r0", "r1", "r2"])

    def test_the_file_is_private(self) -> None:
        self.audit.write(record())
        self.assertEqual(check_private(Path(self.path)), [])
        self.assertEqual(check_private(Path(os.path.dirname(self.path))), [])

    def test_rotation_keeps_two_files(self) -> None:
        audit = Audit(self.home, lambda: self.now, max_bytes=600)
        for n in range(12):
            audit.write(record(request_id=f"r{n:02d}"))
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(os.path.exists(self.path + ".1"))
        self.assertFalse(os.path.exists(self.path + ".2"))
        self.assertLessEqual(os.path.getsize(self.path), 600)
        self.assertLessEqual(os.path.getsize(self.path + ".1"), 600)
        kept = [r["id"] for r in lines(self.path + ".1")] + [r["id"] for r in lines(self.path)]
        self.assertEqual(kept[-1], "r11")
        self.assertEqual(kept, sorted(kept))
        self.assertLess(len(kept), 12)

    def test_the_default_limit_is_five_mebibytes(self) -> None:
        self.audit.write(record())
        with open(self.path, "ab") as f:
            f.write(b"x" * (5 * 1024 * 1024))
        self.audit.write(record(request_id="after"))
        self.assertEqual([r["id"] for r in lines(self.path)], ["after"])
        self.assertGreater(os.path.getsize(self.path + ".1"), 5 * 1024 * 1024)

    def test_an_unwritable_log_raises(self) -> None:
        os.makedirs(self.path)
        with self.assertRaises(AuditError):
            self.audit.write(record())
        with self.assertRaises(AuditError):
            self.audit.check()

    def test_check_creates_the_private_file_without_a_line(self) -> None:
        self.audit.check()
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(os.path.getsize(self.path), 0)
        self.assertEqual(check_private(Path(self.path)), [])


if __name__ == "__main__":
    unittest.main()
