from __future__ import annotations

import re
import threading
from collections.abc import Mapping, Sequence
from typing import Callable, NamedTuple, Optional

from ..jsjson import trim
from ..processes import RunResult, run

KEY_ENV = "TYPESAFE_API_KEY"
KEY_SERVICE = "typesafe"
KEY_ACCOUNT = "api_key"
COMPOUND_TARGET = f"{KEY_ACCOUNT}@{KEY_SERVICE}"
WINDOWS_TARGETS = (KEY_SERVICE, COMPOUND_TARGET)
STORE_TIMEOUT_S = 15.0
_CRED_TYPE_GENERIC = 1
_ERROR_NOT_FOUND = 1168
_PRINTABLE = re.compile("[ -~]+")


class StoredCredential(NamedTuple):
    target: str
    user: str | None
    blob: bytes


CommandRunner = Callable[[str, Sequence[str], Mapping[str, str], float], RunResult]
CredentialReader = Callable[[str], Optional[StoredCredential]]


class FoundKey(NamedTuple):
    key: str
    source: str


class KeyLookup(NamedTuple):
    found: FoundKey | None = None
    missing: str | None = None


def store_description(platform: str) -> str:
    if platform == "win32":
        return f"the Windows Credential Manager (generic credential {KEY_SERVICE} with user name {KEY_ACCOUNT}, then {COMPOUND_TARGET})"
    if platform == "darwin":
        return f"the macOS keychain (service {KEY_SERVICE}, account {KEY_ACCOUNT})"
    return f"the Secret Service keyring (service {KEY_SERVICE}, username {KEY_ACCOUNT})"


def _decode(encoding: str, data: bytes) -> str | None:
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        return None
    return text[1:] if text.startswith(chr(0xFEFF)) else text


# An API key is printable ASCII, so a UTF-8 blob read as UTF-16LE never passes this test.
def decode_blob(data: bytes) -> str | None:
    wide = _decode("utf-16-le", data) if len(data) % 2 == 0 else None
    if wide is not None and _PRINTABLE.fullmatch(wide):
        return wide
    narrow = _decode("utf-8", data)
    return (trim(narrow) or None) if narrow is not None else None


def pick_windows_credential(entries: Sequence[StoredCredential]) -> str | None:
    chosen = next((e for e in entries if e.target == KEY_SERVICE and e.user == KEY_ACCOUNT), None)
    if chosen is None:
        chosen = next((e for e in entries if e.target == COMPOUND_TARGET), None)
    return decode_blob(chosen.blob) if chosen is not None else None


def read_windows_credential(target: str) -> StoredCredential | None:
    import ctypes
    from ctypes import wintypes

    class CREDENTIALW(ctypes.Structure):
        _fields_ = (
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        )

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.CredReadW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW)))
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = (ctypes.c_void_p,)
    advapi32.CredFree.restype = None
    pointer = ctypes.POINTER(CREDENTIALW)()
    if not advapi32.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        code = ctypes.get_last_error()
        if code == _ERROR_NOT_FOUND:
            return None
        raise ctypes.WinError(code)
    try:
        cred = pointer.contents
        size = cred.CredentialBlobSize
        blob = ctypes.string_at(cred.CredentialBlob, size) if size > 0 and cred.CredentialBlob else b""
        return StoredCredential(target, cred.UserName, blob)
    finally:
        advapi32.CredFree(pointer)


def _read_windows(read: CredentialReader, problems: list[str]) -> str | None:
    found: list[StoredCredential] = []
    for target in WINDOWS_TARGETS:
        try:
            entry = read(target)
        except OSError as e:
            problems.append(f"CredReadW failed for {target}: {e}")
            continue
        if entry is not None:
            found.append(entry)
    return pick_windows_credential(found)


def _run_command(command: str, args: Sequence[str], env: Mapping[str, str], timeout: float) -> RunResult:
    return run(command, args, env=env, timeout=timeout)


def _failure(command: str, e: BaseException) -> str:
    if isinstance(e, FileNotFoundError):
        return f"{command} is not installed"
    message = str(e)
    return message if message.startswith(command) else f"{command} failed: {message}"


def _read_command(runner: CommandRunner, env: Mapping[str, str], command: str, args: Sequence[str], problems: list[str]) -> str | None:
    try:
        result = runner(command, args, env, STORE_TIMEOUT_S)
    except (OSError, TimeoutError) as e:
        problems.append(_failure(command, e))
        return None
    key = trim(result.stdout)
    return key if result.code == 0 and key != "" else None


class KeyDeps:
    def __init__(
        self,
        env: Mapping[str, str],
        platform: str,
        run_command: CommandRunner | None = None,
        read_credential: CredentialReader | None = None,
    ) -> None:
        self.env = env
        self.platform = platform
        self.run_command = run_command
        self.read_credential = read_credential


def _read_store(deps: KeyDeps, problems: list[str]) -> str | None:
    if deps.platform == "win32":
        return _read_windows(deps.read_credential or read_windows_credential, problems)
    runner = deps.run_command or _run_command
    if deps.platform == "darwin":
        return _read_command(runner, deps.env, "security", ["find-generic-password", "-s", KEY_SERVICE, "-a", KEY_ACCOUNT, "-w"], problems)
    return _read_command(runner, deps.env, "secret-tool", ["lookup", "service", KEY_SERVICE, "username", KEY_ACCOUNT], problems)


def look_up_key(deps: KeyDeps) -> KeyLookup:
    from_env = trim(deps.env.get(KEY_ENV) or "")
    if from_env:
        return KeyLookup(found=FoundKey(from_env, "env"))
    problems: list[str] = []
    stored = _read_store(deps, problems)
    if stored:
        return KeyLookup(found=FoundKey(stored, "credential-store"))
    detail = f" ({'; '.join(problems)})" if problems else ""
    return KeyLookup(
        missing=(
            f"No TypeSafe API key found. The server looked in the {KEY_ENV} environment variable and in "
            f"{store_description(deps.platform)}{detail}. "
            f"Set {KEY_ENV}, or store the key in the credential store under service {KEY_SERVICE}, account {KEY_ACCOUNT}."
        )
    )


class KeyStore:
    def __init__(self, deps: KeyDeps) -> None:
        self._deps = deps
        self._cached: FoundKey | None = None
        self._lock = threading.Lock()

    def look_up(self) -> KeyLookup:
        with self._lock:
            if self._cached is not None:
                return KeyLookup(found=self._cached)
            result = look_up_key(self._deps)
            if result.found is not None:
                self._cached = result.found
            return result
