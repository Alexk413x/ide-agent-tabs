from __future__ import annotations

import sys
import unittest
from collections.abc import Callable

from ide_agent_tabs.net import elevation
from ide_agent_tabs.net.elevation import ElevationError

USER_SID = "S-1-5-21-111-222-333-1001"
ADMINISTRATOR_SID = "S-1-5-21-111-222-333-500"


def token(elevated: bool) -> Callable[[], bool]:
    return lambda: elevated


def lua(value: int | None) -> Callable[[], int | None]:
    return lambda: value


def sid(value: str) -> Callable[[], str]:
    return lambda: value


class IsElevatedTest(unittest.TestCase):
    def test_windows_reads_the_token(self) -> None:
        self.assertTrue(elevation.is_elevated("win32", token(True)))
        self.assertFalse(elevation.is_elevated("win32", token(False)))

    def test_other_systems_read_the_effective_user(self) -> None:
        self.assertTrue(elevation.is_elevated("linux", token(False), lambda: 0))
        self.assertFalse(elevation.is_elevated("linux", token(True), lambda: 1000))
        self.assertTrue(elevation.is_elevated("darwin", token(False), lambda: 0))

    @unittest.skipUnless(sys.platform == "win32", "reads the real token")
    def test_the_real_token_answers_with_a_bool(self) -> None:
        self.assertIsInstance(elevation.is_elevated(), bool)


class UnelevatedTokenTest(unittest.TestCase):
    def test_a_normal_account_with_uac_on_has_one(self) -> None:
        for value in (1, None):
            self.assertTrue(elevation.unelevated_token_available("win32", lua(value), sid(USER_SID)), msg=repr(value))

    def test_uac_off_has_none(self) -> None:
        self.assertFalse(elevation.unelevated_token_available("win32", lua(0), sid(USER_SID)))

    def test_the_built_in_administrator_has_none(self) -> None:
        self.assertFalse(elevation.unelevated_token_available("win32", lua(1), sid(ADMINISTRATOR_SID)))

    def test_a_rid_that_only_ends_in_500_is_not_the_built_in_administrator(self) -> None:
        self.assertTrue(elevation.unelevated_token_available("win32", lua(1), sid("S-1-5-21-111-222-333-1500")))
        self.assertTrue(elevation.unelevated_token_available("win32", lua(1), sid("S-1-5-21-500-222-333-1001")))

    def test_other_systems_always_have_one(self) -> None:
        self.assertTrue(elevation.unelevated_token_available("linux", lua(0), sid(ADMINISTRATOR_SID)))


class UnsupportedReasonTest(unittest.TestCase):
    def test_uac_off_says_why(self) -> None:
        reason = elevation.unsupported_reason("win32", lua(0), sid(USER_SID))
        assert reason is not None
        self.assertIn("User Account Control is off", reason)
        self.assertIn("Version 1 doesn't support", reason)

    def test_the_built_in_administrator_says_why(self) -> None:
        reason = elevation.unsupported_reason("win32", lua(1), sid(ADMINISTRATOR_SID))
        assert reason is not None
        self.assertIn("built-in Administrator", reason)
        self.assertIn("Version 1 doesn't support", reason)

    def test_a_supported_account_has_no_reason(self) -> None:
        self.assertIsNone(elevation.unsupported_reason("win32", lua(1), sid(USER_SID)))
        self.assertIsNone(elevation.unsupported_reason("linux", lua(0), sid(ADMINISTRATOR_SID)))


class RefuseIfElevatedTest(unittest.TestCase):
    def refuse(self, elevated: bool, enable_lua: int | None = 1, user_sid: str = USER_SID, platform: str = "win32") -> None:
        elevation.refuse_if_elevated(platform, token(elevated), lua(enable_lua), sid(user_sid), lambda: 0 if elevated else 1000)

    def test_an_unelevated_process_continues(self) -> None:
        self.refuse(False)
        self.refuse(False, enable_lua=0)
        self.refuse(False, platform="linux")

    def test_an_elevated_process_is_told_to_use_a_normal_terminal(self) -> None:
        with self.assertRaises(ElevationError) as caught:
            self.refuse(True)
        message = str(caught.exception)
        self.assertIn("Stop", message)
        self.assertIn("normal terminal", message)
        self.assertIn("never continues under the administrator token", message)
        self.assertNotIn("Version 1", message)

    def test_an_elevated_process_without_an_unelevated_token_is_told_version_1_does_not_support_it(self) -> None:
        with self.assertRaises(ElevationError) as caught:
            self.refuse(True, enable_lua=0)
        self.assertIn("Version 1 doesn't support", str(caught.exception))
        with self.assertRaises(ElevationError) as caught:
            self.refuse(True, user_sid=ADMINISTRATOR_SID)
        self.assertIn("Version 1 doesn't support", str(caught.exception))

    def test_root_on_another_system_is_refused(self) -> None:
        with self.assertRaises(ElevationError):
            self.refuse(True, platform="linux")


if __name__ == "__main__":
    unittest.main()
