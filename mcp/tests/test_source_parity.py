from __future__ import annotations

import re
import unittest
from typing import Any

from ide_agent_tabs import launch_plan, profiles, reveal, sync
from ide_agent_tabs.terminals import shell
from source_values import Call, Pattern, gradle_text, kotlin, trim_indent, typescript

TS = typescript()
KT = kotlin()

FULL, SEARCH = "full", "search"

REGEXES: list[tuple[str, re.Pattern[str], str]] = [
    ("PROFILE_NAME", profiles._PROFILE_NAME, FULL),
    ("ORI_CMD_UNSAFE", launch_plan._ORI_CMD_SPECIALS, SEARCH),
    ("CODEX_PYTHON_SAFE", profiles._CODEX_PYTHON_SAFE, FULL),
    ("HTTP_SAFE", profiles._HTTP_SAFE, FULL),
    ("HTTP_WORD", profiles._HTTP_WORD, FULL),
    ("COPILOT_COMMAND", profiles._COPILOT_COMMAND, FULL),
    ("CMD_SAFE_PATH", profiles._CMD_SAFE_PATH, FULL),
    ("CLAUDE_COMMAND", profiles._CLAUDE_COMMAND, FULL),
    ("MODEL_PATTERN", launch_plan.MODEL_PATTERN, FULL),
    ("BUNDLE_SEGMENT", reveal.BUNDLE_SEGMENT, SEARCH),
]

NON_ASCII_FOLDS = "Kſİı"
CHARS = [chr(i) for i in range(0x180)] + ["\u2028", "\ufeff", "\u212a", "\u017f", "\u00e9", "\u4e2d", "\U0001f600"]
WORDS = [
    *(f(w) for w in ("claude", "copilot") for f in (str, str.upper, "/a/{}".format, "C:\\a\\{}.cmd".format, "{}.exe".format)),
    *(f(w) for w in ("claude", "copilot") for f in ("{}.PS1".format, "{}.sh".format, "x{}".format, "{}.exe.bak".format)),
    *(f"Foo.{e}" for e in ("app", "APP", "app.bak", "prefPane", "prefpane", "pkg", "framework", "bundle", "plugin", "apps")),
    ".app",
    "app",
    "gpt-5.1",
    "a/b:c@d+e",
    "C:\\Program Files\\x\\python.exe",
    "/usr/bin/python3",
    "-x",
    "a\\",
    *(f"{w}{end}" for w in ("claude", "copilot.exe", "Foo.app", "gpt-5.1", "a") for end in ("\n", "\r\n", "\r", "\u0085", " ")),
    *("claude.pſ1", "copilot.pſ1", "copİlot", "copılot", "Foo.pKg", "Foo.frameworK", "Foo.plugın"),
    *("a\r/copilot", "a /copilot", "a /claude", "x\r/claude", "a\n/claude.exe"),
]
PROBES = [
    *CHARS,
    *(f"a{c}" for c in CHARS),
    *(f"{c}a" for c in CHARS),
    *(f"a{c}a" for c in CHARS),
    *("a" * n for n in (0, 2, 63, 64, 65, 127, 128, 129, 199, 200, 201)),
    *WORDS,
]


NO_UNICODE_CASE = "(?-u)"


def python_source(source: str, kotlin_source: bool) -> str:
    ends = "\n\r\u0085  " if kotlin_source else "\n\r  "
    # Java's $ and \Z also match before one final line terminator; JavaScript's $ matches only at the end.
    end = rf"(?=(?:\r\n|[{ends}])?\Z)" if kotlin_source else r"\Z"
    escapes = {r"\z": r"\Z", r"\Z": end} if kotlin_source else {}
    out: list[str] = []
    in_class = False
    i = 0
    while i < len(source):
        c = source[i]
        if c == "\\":
            pair = source[i : i + 2]
            out.append(pair if in_class else escapes.get(pair, pair))
            i += 2
            continue
        if in_class:
            in_class = c != "]"
            out.append(c)
        elif c == ".":
            out.append(f"[^{ends}]")
        elif c == "$":
            out.append(end)
        else:
            in_class = c == "["
            out.append(c)
        i += 1
    return "".join(out)


def compiled(p: Pattern, kotlin_source: bool, mode: str) -> re.Pattern[str]:
    if p.other_flags:
        raise AssertionError(f"unexpected regex flags {p.other_flags}")
    source = p.source
    ascii_case = not kotlin_source or source.startswith(NO_UNICODE_CASE)
    if kotlin_source and source.startswith(NO_UNICODE_CASE):
        source = source[len(NO_UNICODE_CASE) :]
    if not kotlin_source and mode == FULL:
        if not (source.startswith("^") and source.endswith("$") and not source.endswith("\\$")):
            raise AssertionError(f"/{source}/ is not anchored at both ends")
        source = source[1:-1]
    # Kotlin's Regex adds UNICODE_CASE to IGNORE_CASE, so K (U+212A) matches k unless the source starts with (?-u);
    # JavaScript's i flag without u folds no other character onto an ASCII letter.
    flags = (re.IGNORECASE | (re.ASCII if ascii_case else 0)) if p.ignore_case else 0
    return re.compile(python_source(source, kotlin_source), flags)


def accepted(pattern: re.Pattern[str], mode: str) -> list[str]:
    test = pattern.fullmatch if mode == FULL else pattern.search
    return [s for s in PROBES if test(s)]


def python_profile(p: profiles.AgentProfile) -> dict[str, Any]:
    return {
        "name": p.name,
        "label": p.label,
        "command": p.command,
        "args": list(p.args),
        "promptFlag": p.prompt_flag,
        "modelFlag": p.model_flag,
        "env": dict(p.env),
        "icon": p.icon,
    }


def ts_profile(c: Call) -> dict[str, Any]:
    if c.callee != "profile":
        raise AssertionError(f"BUILTIN_PROFILES entry calls {c.callee}")
    name, label, command, *rest = c.args
    extra: dict[str, Any] = rest[0] if rest else {}
    return {
        "name": name,
        "label": label,
        "command": command,
        "args": extra.get("args", []),
        "promptFlag": extra.get("promptFlag"),
        "modelFlag": extra.get("modelFlag"),
        "env": extra.get("env", {}),
        "icon": extra.get("icon"),
    }


def kotlin_profile(c: Call, params: list[str]) -> dict[str, Any]:
    if c.callee != "AgentProfile":
        raise AssertionError(f"BUILTIN_PROFILES entry calls {c.callee}")
    values: dict[str, Any] = {"args": [], "env": {}, "promptFlag": None, "modelFlag": None, "icon": None}
    values.update(zip(params, c.args))
    values.update(c.kwargs)
    return {k: values[k] for k in ("name", "label", "command", "args", "promptFlag", "modelFlag", "env", "icon")}


def kotlin_variant(c: Call) -> tuple[str, str, str]:
    toggle = re.fullmatch(r"it\.(\w+)", c.block or "")
    if c.callee != "LocalVariant" or toggle is None:
        raise AssertionError(f"cannot read LOCAL_VARIANTS entry {c}")
    name, base = c.args
    return name, base, toggle.group(1)


class SourceParityTest(unittest.TestCase):
    maxDiff = None

    def both(self, name: str, expected: Any) -> None:
        for sources in (TS, KT):
            with self.subTest(name=name, kotlin=sources.kotlin):
                self.assertEqual(sources.value(name), expected)

    def test_codex_tab_arguments_match(self) -> None:
        self.both("CODEX_TAB_ARGS", list(profiles.CODEX_TAB_ARGS))

    def test_limits_match(self) -> None:
        self.both("MAX_PROMPT_CHARS", profiles.MAX_PROMPT_CHARS)
        self.both("MAX_ENTRIES", profiles.MAX_ENTRIES)
        self.both("MAX_INPUT_CHARS", shell.MAX_INPUT_CHARS)
        self.both("COPILOT_HEALTH_TIMEOUT_MS", profiles.COPILOT_HEALTH_TIMEOUT_MS)

    def test_ori_agents_and_aliases_match(self) -> None:
        self.both("ORI_PROFILES", list(launch_plan.ORI_AGENTS))
        self.both("AGENT_ALIASES", profiles.AGENT_ALIASES)

    def test_local_variants_match(self) -> None:
        expected = [tuple(v) for v in profiles.LOCAL_VARIANTS]
        self.assertEqual([(v["name"], v["base"], v["toggle"]) for v in TS.value("LOCAL_VARIANTS")], expected)
        self.assertEqual([kotlin_variant(c) for c in KT.value("LOCAL_VARIANTS")], expected)

    def test_builtin_profiles_match(self) -> None:
        expected = [python_profile(p) for p in profiles.BUILTIN_PROFILES]
        self.assertEqual([ts_profile(c) for c in TS.value("BUILTIN_PROFILES")], expected)
        params = KT.class_params("AgentProfile")
        self.assertEqual([kotlin_profile(c, params) for c in KT.value("BUILTIN_PROFILES")], expected)

    def test_model_pattern_source_matches_the_typescript_literal(self) -> None:
        self.assertEqual(TS.value("MODEL_PATTERN").source, launch_plan.MODEL_SOURCE)

    def test_regexes_accept_the_same_strings(self) -> None:
        for name, python, mode in REGEXES:
            expected = accepted(python, mode)
            for sources in (TS, KT):
                with self.subTest(name=name, kotlin=sources.kotlin):
                    value = sources.value(name)
                    self.assertIsInstance(value, Pattern)
                    self.assertEqual(value.ignore_case, bool(python.flags & re.IGNORECASE))
                    self.assertEqual(accepted(compiled(value, sources.kotlin, mode), mode), expected)

    def test_no_regex_accepts_a_trailing_newline_or_a_non_ascii_case_fold(self) -> None:
        newline = [s for s in PROBES if s.endswith("\n")]
        folded = [s for s in PROBES if any(c in s for c in NON_ASCII_FOLDS)]
        for name, python, mode in REGEXES:
            checked = newline + (folded if python.flags & re.IGNORECASE else [])
            patterns = [
                ("python", python),
                ("typescript", compiled(TS.value(name), False, mode)),
                ("kotlin", compiled(KT.value(name), True, mode)),
            ]
            for language, pattern in patterns:
                with self.subTest(name=name, language=language):
                    test = pattern.fullmatch if mode == FULL else pattern.search
                    self.assertEqual([s for s in checked if test(s)], [])

    def test_cwd_header_keeps_the_characters_the_ide_copies_keep(self) -> None:
        kept = [c for c in CHARS if profiles.cwd_header(c) == c]
        for sources in (TS, KT):
            with self.subTest(kotlin=sources.kotlin):
                pattern = compiled(sources.value("CWD_SAFE"), sources.kotlin, FULL)
                self.assertEqual([c for c in CHARS if pattern.fullmatch(c)], kept)


def _gradle_since_build(text: str) -> str:
    match = re.search(r'\bsinceBuild\s*=\s*"([^"]+)"', text)
    assert match is not None, "build.gradle.kts sets no sinceBuild"
    return match.group(1)


def _gradle_update_plugins_template(text: str) -> str:
    task = text.index('tasks.register("updatePluginsXml")')
    start = text.index('"""', task)
    end = text.index('"""', start + 3)
    return trim_indent(text[start + 3 : end]) + "\n"


class GradleRepositoryXmlTest(unittest.TestCase):
    def test_since_build_matches(self) -> None:
        text = gradle_text()
        self.assertEqual(_gradle_since_build(text), sync.JETBRAINS_SINCE_BUILD)
        since = re.search(r'since-build="([^"]+)"', _gradle_update_plugins_template(text))
        assert since is not None
        self.assertEqual(since.group(1), sync.JETBRAINS_SINCE_BUILD)

    def test_update_plugins_xml_matches_the_gradle_task_for_a_plain_url(self) -> None:
        version, url = "1.2.3", "file:///C:/repo/ide-agent-tabs-1.2.3.zip"
        template = _gradle_update_plugins_template(gradle_text())
        self.assertIn("$zipUrl", template)
        self.assertIn("$pluginVersion", template)
        rendered = template.replace("$zipUrl", url).replace("$pluginVersion", version)
        self.assertEqual(rendered, sync.update_plugins_xml(version, url))


if __name__ == "__main__":
    unittest.main()
