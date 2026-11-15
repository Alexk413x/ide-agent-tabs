from __future__ import annotations

import contextlib
import os
import secrets
import ssl
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .identity import der_from_pem, device_id

KEY_FILE = "key.pem"
CERT_FILE = "cert.pem"
_CONFIG_FILE = "openssl.cnf"
_ENCRYPTED_KEY_HEADER = "-----BEGIN ENCRYPTED PRIVATE KEY-----"
_TIMEOUT_S = 60
_VALID_DAYS = 10958
_SUBJECT = "/CN=agent-tabs"
_CONFIG = "[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = agent-tabs\n"
_EXTENSIONS = (
    "basicConstraints=critical,CA:FALSE",
    "keyUsage=critical,digitalSignature",
    "extendedKeyUsage=serverAuth,clientAuth",
    "subjectKeyIdentifier=hash",
)


class CertgenError(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    cert_path: Path
    key_path: Path
    device_id: str


def _environment() -> dict[str, str]:
    if sys.platform != "win32":
        return {}
    return {name: os.environ[name] for name in ("SYSTEMROOT", "SYSTEMDRIVE") if name in os.environ}


def _run(openssl: Path, out_dir: Path, arguments: list[str], stdin: bytes) -> str:
    try:
        result = subprocess.run(
            [str(openssl), *arguments],
            input=stdin,
            capture_output=True,
            env=_environment(),
            cwd=str(out_dir),
            timeout=_TIMEOUT_S,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CertgenError(f"openssl {arguments[0]} could not run: {error}") from error
    output = (result.stdout + result.stderr).decode("utf-8", "replace")
    if result.returncode:
        raise CertgenError(f"openssl {arguments[0]} exited {result.returncode}: {output.strip()}")
    return output


def _supports_not_before(openssl: Path, out_dir: Path) -> bool:
    try:
        result = subprocess.run(
            [str(openssl), "req", "-help"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=_environment(),
            cwd=str(out_dir),
            timeout=_TIMEOUT_S,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CertgenError(f"openssl req -help could not run: {error}") from error
    text = (result.stdout + result.stderr).decode("utf-8", "replace")
    return "-not_before" in text and "-not_after" in text


def _validity_arguments(supported: bool, now: datetime) -> list[str]:
    if not supported:
        return ["-days", str(_VALID_DAYS)]
    utc = now.astimezone(timezone.utc)
    stamp = "%Y%m%d%H%M%SZ"
    return ["-not_before", (utc - timedelta(days=1)).strftime(stamp), "-not_after", (utc + timedelta(days=_VALID_DAYS)).strftime(stamp)]


def _check_inputs(openssl: Path, out_dir: Path, passphrase: bytes, now: datetime) -> None:
    if not openssl.is_absolute():
        raise CertgenError(f"the openssl path must be absolute: {openssl}")
    if not openssl.is_file():
        raise CertgenError(f"openssl not found at {openssl}")
    if not out_dir.is_dir():
        raise CertgenError(f"the output folder does not exist: {out_dir}")
    if not passphrase or b"\n" in passphrase or b"\r" in passphrase or b"\0" in passphrase:
        raise CertgenError("the passphrase must be one non-empty line")
    if now.tzinfo is None:
        raise CertgenError("now must carry a time zone")
    for name in (KEY_FILE, CERT_FILE):
        if os.path.lexists(out_dir / name):
            raise CertgenError(f"{out_dir / name} already exists")


def generate_identity(openssl: Path, out_dir: Path, passphrase: bytes, *, now: datetime) -> Identity:
    _check_inputs(openssl, out_dir, passphrase, now)
    key_path = out_dir / KEY_FILE
    cert_path = out_dir / CERT_FILE
    config_path = out_dir / _CONFIG_FILE
    stdin = passphrase + b"\n"
    try:
        _run(
            openssl,
            out_dir,
            ["genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-aes-256-cbc", "-pass", "stdin", "-out", KEY_FILE],
            stdin,
        )
        config_path.write_text(_CONFIG, encoding="ascii")
        serial = secrets.randbits(159) or 1
        arguments = ["req", "-x509", "-config", _CONFIG_FILE, "-key", KEY_FILE, "-passin", "stdin", "-sha256", "-subj", _SUBJECT]
        for extension in _EXTENSIONS:
            arguments += ["-addext", extension]
        arguments += ["-set_serial", f"0x{serial:x}", *_validity_arguments(_supports_not_before(openssl, out_dir), now), "-out", CERT_FILE]
        _run(openssl, out_dir, arguments, stdin)
        return _verify(key_path, cert_path, passphrase)
    except BaseException:
        for leftover in (key_path, cert_path):
            with contextlib.suppress(OSError):
                os.remove(leftover)
        raise
    finally:
        with contextlib.suppress(OSError):
            os.remove(config_path)


def _verify(key_path: Path, cert_path: Path, passphrase: bytes) -> Identity:
    try:
        header = key_path.read_text(encoding="ascii").splitlines()[0]
        if header != _ENCRYPTED_KEY_HEADER:
            raise CertgenError(f"the key file starts with {header!r}, not an encrypted PKCS#8 key")
        identity = device_id(der_from_pem(cert_path.read_text(encoding="ascii")))
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(str(cert_path), str(key_path), password=lambda: passphrase)
    except (OSError, ValueError, IndexError, ssl.SSLError) as error:
        raise CertgenError(f"the generated identity does not verify: {error}") from error
    return Identity(cert_path=cert_path, key_path=key_path, device_id=identity)
