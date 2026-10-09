from __future__ import annotations

import os
import re
import sys
from typing import Callable, NamedTuple

MAX_DEPTH = 16
LOOKUP_TIMEOUT_S = 5.0
# A child can't start before its parent; process start times on macOS have one-second steps.
START_SLACK_MS = 1_500
WINDOWS_EPOCH_OFFSET_MS = 11_644_473_600_000
_SKIPPED = re.compile(
    r"(cmd|bash|sh|dash|zsh|fish|ksh|mksh|tcsh|csh|pwsh|powershell|nu|busybox|env|wsl|conhost|py|pyw)(\.exe)?", re.IGNORECASE
)
_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MAX_PATH = 260


class ProcessInfo(NamedTuple):
    pid: int
    ppid: int
    name: str
    start_ms: float


Lookup = Callable[[int], "ProcessInfo | None"]


def _base_name(name: str) -> str:
    base = name.strip().replace("\\", "/").rsplit("/", 1)[-1]
    return base.removeprefix("-")


def is_skipped_name(name: str) -> bool:
    return _SKIPPED.fullmatch(_base_name(name)) is not None


# Claude Code runs a headersHelper through a shell, and `py -3` starts Python as its own child, so the agent is
# the nearest ancestor that is neither. A name check would pick an outer session for a nested one that runs as
# `node cli.js`.
def find_agent(lookup: Lookup, self_pid: int) -> ProcessInfo | None:
    child = lookup(self_pid)
    seen = {self_pid}
    for _ in range(MAX_DEPTH):
        if child is None or child.ppid in seen:
            return None
        seen.add(child.ppid)
        parent = lookup(child.ppid)
        if parent is None or parent.start_ms > child.start_ms + START_SLACK_MS:
            return None
        if not is_skipped_name(parent.name):
            return parent
        child = parent
    return None


def _windows_lookup() -> Lookup:
    if sys.platform != "win32":
        return lambda _pid: None
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * _MAX_PATH),
        )

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    k32.Process32NextW.restype = wintypes.BOOL
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
    k32.GetProcessTimes.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.CloseHandle.restype = wintypes.BOOL

    table: dict[int, tuple[int, str]] = {}
    snapshot = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot is None or snapshot == wintypes.HANDLE(-1).value:
        return lambda _pid: None
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        more = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
            more = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)

    def start_ms(pid: int) -> float | None:
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return created / 10_000 - WINDOWS_EPOCH_OFFSET_MS if created else None
        finally:
            k32.CloseHandle(handle)

    def lookup(pid: int) -> ProcessInfo | None:
        known = table.get(pid)
        if known is None or pid <= 0:
            return None
        started = start_ms(pid)
        return None if started is None else ProcessInfo(pid, known[0], known[1], started)

    return lookup


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def parse_linux_stat(pid: int, stat: str, boot_ms: float, tick_ms: float) -> ProcessInfo | None:
    open_at = stat.find("(")
    close_at = stat.rfind(")")
    if open_at == -1 or close_at < open_at:
        return None
    fields = stat[close_at + 2 :].split(" ")
    try:
        return ProcessInfo(pid, int(fields[1]), stat[open_at + 1 : close_at], boot_ms + int(fields[19]) * tick_ms)
    except (IndexError, ValueError):
        return None


def _linux_lookup() -> Lookup:
    match = re.search(r"^btime (\d+)$", _read("/proc/stat") or "", re.MULTILINE)
    boot_ms = int(match.group(1)) * 1000 if match else 0
    tick_ms = 1000 / ((os.sysconf("SC_CLK_TCK") if sys.platform != "win32" else 0) or 100)

    def lookup(pid: int) -> ProcessInfo | None:
        stat = _read(f"/proc/{pid}/stat") if pid > 0 else None
        return None if stat is None or not match else parse_linux_stat(pid, stat, boot_ms, tick_ms)

    return lookup


def parse_ps_table(text: str) -> dict[int, ProcessInfo]:
    import time

    table: dict[int, ProcessInfo] = {}
    for line in text.split("\n"):
        parts = line.split()
        if len(parts) < 8:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
            local = time.strptime(" ".join(parts[2:7]), "%a %b %d %H:%M:%S %Y")
        except ValueError:
            continue
        if pid > 0:
            table[pid] = ProcessInfo(pid, ppid, " ".join(parts[7:]), time.mktime(local) * 1000)
    return table


def _ps_lookup(timeout_s: float) -> Lookup:
    import subprocess

    try:
        done = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,lstart=,comm="],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return lambda _pid: None
    table = parse_ps_table(done.stdout or "")
    return table.get


def process_lookup(platform: str = sys.platform, timeout_s: float = LOOKUP_TIMEOUT_S) -> Lookup:
    if platform == "win32":
        return _windows_lookup()
    if platform.startswith("linux"):
        return _linux_lookup()
    return _ps_lookup(timeout_s)


def find_agent_process(self_pid: int | None = None, timeout_s: float = LOOKUP_TIMEOUT_S) -> ProcessInfo | None:
    try:
        return find_agent(process_lookup(sys.platform, timeout_s), os.getpid() if self_pid is None else self_pid)
    except (OSError, ValueError, AttributeError):
        return None
