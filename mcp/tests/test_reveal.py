from __future__ import annotations

import json
import os
import sys
import unittest
from typing import Any

from ide_agent_tabs.ide_client import IdeError
from ide_agent_tabs.registry import Endpoint
from ide_agent_tabs.reveal import RevealDeps, check_reveal_target, system_reveal
from ide_agent_tabs.service import Service, ServiceDeps
from support import temp_home

PLATFORM = "linux" if sys.platform.startswith("linux") else sys.platform


class Setup:
    def __init__(self, test: unittest.TestCase, failing: tuple[str, ...] = (), open_fails: bool = False) -> None:
        self.home = temp_home(test, "iat-reveal-")
        self.project = temp_home(test, "iat-reveal-p-")
        self.session = temp_home(test, "iat-reveal-s-")
        self.calls: list[tuple[str, str, Any]] = []
        self.opened: list[str] = []
        self.failing = failing
        self.open_fails = open_fails
        endpoints = os.path.join(self.home, "endpoints")
        os.mkdir(endpoints)
        base = {"protocol": 1, "version": "1.0", "token": "t", "pid": os.getpid()}
        for name, ide, product, port in (("jetbrains-1", "jetbrains", "Android Studio", 1), ("vscode-2", "vscode", "Antigravity", 2)):
            with open(os.path.join(endpoints, f"{name}.json"), "w", encoding="utf-8") as f:
                json.dump({**base, "ide": ide, "product": product, "url": f"http://127.0.0.1:{port}/ide-agent-tabs"}, f)
        reveal = system_reveal(PLATFORM)._replace(open=self.open)
        self.service = Service(
            ServiceDeps(
                home=self.home,
                scripts_dir=self.home,
                platform=PLATFORM,
                env={"PATH": ""},
                call_ide=self.call_ide,
                drivers=[],
                is_alive=lambda pid: True,
                reveal=reveal,
            )
        )

    def open(self, folder: str) -> None:
        if self.open_fails:
            raise OSError("spawn explorer.exe ENOENT")
        self.opened.append(folder)

    def call_ide(self, endpoint: Endpoint, route: str, body: Any = None) -> dict[str, Any]:
        self.calls.append((endpoint.id, route, body))
        if route == "info":
            projects = [{"name": "p", "path": self.project, "focused": True}] if endpoint.id.startswith("jetbrains") else []
            return {"ok": True, "projects": projects}
        if endpoint.id in self.failing:
            raise IdeError(f"{endpoint.id} reveal answered HTTP 404: no such route", 404)
        return {"ok": True}

    def reveals(self) -> list[str]:
        return [c[0] for c in self.calls if c[1] == "reveal"]


class RevealTest(unittest.TestCase):
    def test_reveal_goes_to_the_sessions_own_ide_first_then_any_other_then_the_file_manager(self) -> None:
        s = Setup(self)
        self.assertIs(s.service.reveal(s.session, None, [s.session])["ok"], True)
        s.calls.clear()
        own = s.service.reveal(s.session, "vscode-2", [s.session])
        self.assertEqual((own["ok"], s.reveals()), (True, ["vscode-2"]))

        f = Setup(self, ("vscode-2",))
        fallback = f.service.reveal(f.project, "vscode-2", [])
        self.assertIs(fallback["ok"], True)
        self.assertEqual(f.reveals(), ["vscode-2", "jetbrains-1"], "an IDE without the route is skipped")
        self.assertEqual((s.opened, f.opened), ([], []), "the file manager runs only when no IDE can reveal")

        system = Setup(self, ("vscode-2", "jetbrains-1"))
        result = system.service.reveal(system.session, None, [system.session])
        self.assertEqual((result["ok"], result["ide"], len(system.opened)), (True, "system", 1))

        none = Setup(self, ("vscode-2", "jetbrains-1"), open_fails=True)
        failed = none.service.reveal(none.session, None, [none.session])
        self.assertIs(failed["ok"], False)
        self.assertRegex(failed["reason"], r"no such route.*ENOENT")

    def test_reveal_refuses_a_file_a_folder_no_session_or_project_has_and_never_asks_an_ide_then(self) -> None:
        s = Setup(self)
        file = os.path.join(s.session, "note.txt")
        with open(file, "w", encoding="utf-8") as f:
            f.write("x")
        outside = temp_home(self, "iat-reveal-o-")
        for target, reason in (
            (file, "not a folder on this machine"),
            (outside, "is not the folder of a live session or an open IDE project"),
            ("relative/dir", "not an absolute path"),
        ):
            with self.subTest(target=target):
                refused = s.service.reveal(target, None, [s.session])
                self.assertIs(refused["ok"], False)
                self.assertIn(reason, refused["reason"])
        self.assertEqual(s.reveals(), [])
        self.assertIs(s.service.reveal(s.project, None, [])["ok"], True, "an open IDE project's folder is known")

    def test_reveal_resolves_links_before_it_compares(self) -> None:
        root = "C:\\" if sys.platform == "win32" else "/"
        repo, link = f"{root}work{os.sep}repo", f"{root}work{os.sep}link"
        links = {repo: repo, link: f"{root}elsewhere{os.sep}secret"}
        deps = RevealDeps(links.get, lambda _p: True, lambda _p: None, PLATFORM)
        self.assertEqual(check_reveal_target(repo, [repo], deps), repo)
        with self.assertRaisesRegex(ValueError, "is not the folder of a live session"):
            check_reveal_target(link, [repo], deps)

    @unittest.skipIf(sys.platform == "win32", "a macOS path is not absolute on Windows")
    def test_reveal_refuses_a_macos_bundle(self) -> None:
        contents = "/Applications/Foo.app/Contents"
        deps = RevealDeps({contents: contents}.get, lambda _p: True, lambda _p: None, "darwin")
        with self.assertRaisesRegex(ValueError, "inside a macOS bundle"):
            check_reveal_target(contents, [contents], deps)

    @unittest.skipIf(sys.platform == "win32", "creating a symlink needs a privilege on Windows")
    def test_a_real_link_to_another_folder_is_refused(self) -> None:
        known = temp_home(self, "iat-known-")
        linked = os.path.join(known, "away")
        os.symlink(temp_home(self, "iat-away-"), linked)
        with self.assertRaisesRegex(ValueError, "is not the folder of a live session"):
            check_reveal_target(linked, [known], system_reveal(PLATFORM))


if __name__ == "__main__":
    unittest.main()
