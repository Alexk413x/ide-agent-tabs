from __future__ import annotations

import hashlib
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from ide_agent_tabs.net import certgen
from ide_agent_tabs.net.certgen import CertgenError, Identity, generate_identity
from ide_agent_tabs.net.identity import der_from_pem, parse_device_id
from ide_agent_tabs.net.private_files import ensure_private_dir


def find_openssl() -> Path | None:
    git = Path(r"C:\Program Files\Git\usr\bin\openssl.exe")
    if sys.platform == "win32":
        return git if git.is_file() else None
    found = shutil.which("openssl")
    return Path(found) if found else None


OPENSSL = find_openssl()
NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)


def x509(openssl: Path, cert: Path, *arguments: str) -> str:
    result = subprocess.run(
        [str(openssl), "x509", "-in", str(cert), "-noout", *arguments], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def parse_date(text: str) -> datetime:
    return datetime.strptime(text.split("=", 1)[1].strip().replace("GMT", "UTC"), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)


@unittest.skipIf(OPENSSL is None, "no openssl at an absolute path")
class GenerateIdentityTest(unittest.TestCase):
    openssl: Path

    @classmethod
    def setUpClass(cls) -> None:
        assert OPENSSL is not None
        cls.openssl = OPENSSL

    def setUp(self) -> None:
        base = Path(os.path.realpath(tempfile.mkdtemp(prefix="iat-cert-")))
        self.addCleanup(shutil.rmtree, str(base), True)
        self.folder = base / "net"
        ensure_private_dir(self.folder)
        self.passphrase = b"correct horse battery staple 0123456789abcdef"

    def generate(self, folder: Path | None = None, passphrase: bytes | None = None, now: datetime = NOW) -> Identity:
        return generate_identity(self.openssl, folder or self.folder, passphrase or self.passphrase, now=now)

    def supports_not_before(self) -> bool:
        return certgen._supports_not_before(self.openssl, self.folder)

    def test_creates_the_key_and_certificate_and_the_device_id(self) -> None:
        identity = self.generate()
        self.assertEqual(identity.cert_path, self.folder / "cert.pem")
        self.assertEqual(identity.key_path, self.folder / "key.pem")
        self.assertEqual(sorted(entry.name for entry in self.folder.iterdir()), ["cert.pem", "key.pem"])
        der = der_from_pem(identity.cert_path.read_text())
        self.assertEqual(parse_device_id(identity.device_id), hashlib.sha256(der).digest())

    def test_the_key_is_an_encrypted_pkcs8_key_for_p256(self) -> None:
        identity = self.generate()
        text = identity.key_path.read_text()
        self.assertTrue(text.startswith("-----BEGIN ENCRYPTED PRIVATE KEY-----"))
        self.assertNotIn("-----BEGIN PRIVATE KEY-----", text)
        self.assertIn("prime256v1", x509(self.openssl, identity.cert_path, "-text"))

    def test_the_certificate_follows_the_profile(self) -> None:
        text = x509(self.openssl, self.generate().cert_path, "-text")
        for expected in (
            "ecdsa-with-SHA256",
            "Issuer: CN=agent-tabs",
            "Subject: CN=agent-tabs",
            "X509v3 Basic Constraints: critical\n                CA:FALSE",
            "X509v3 Key Usage: critical\n                Digital Signature",
            "TLS Web Server Authentication, TLS Web Client Authentication",
            "X509v3 Subject Key Identifier",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text.replace("\r\n", "\n"))
        self.assertNotIn("CA:TRUE", text)

    def test_validity_uses_not_before_when_the_generator_has_it(self) -> None:
        cert = self.generate().cert_path
        start = parse_date(x509(self.openssl, cert, "-startdate"))
        end = parse_date(x509(self.openssl, cert, "-enddate"))
        if self.supports_not_before():
            self.assertEqual(start, NOW - timedelta(days=1))
            self.assertEqual(end, NOW + timedelta(days=10958))
        else:
            self.assertLess(abs(start - datetime.now(timezone.utc)), timedelta(minutes=5))
            self.assertEqual((end - start).days, 10958)

    def test_validity_falls_back_to_days_when_the_generator_lacks_not_before(self) -> None:
        with mock.patch.object(certgen, "_supports_not_before", return_value=False):
            cert = self.generate().cert_path
        start = parse_date(x509(self.openssl, cert, "-startdate"))
        end = parse_date(x509(self.openssl, cert, "-enddate"))
        self.assertLess(abs(start - datetime.now(timezone.utc)), timedelta(minutes=5))
        self.assertEqual((end - start).days, 10958)

    def test_the_serial_is_random_and_at_most_159_bits(self) -> None:
        serials = []
        for name in ("a", "b"):
            folder = self.folder / name
            folder.mkdir()
            serials.append(int(x509(self.openssl, self.generate(folder).cert_path, "-serial").split("=", 1)[1], 16))
        self.assertNotEqual(serials[0], serials[1])
        for serial in serials:
            self.assertGreater(serial, 0)
            self.assertLess(serial, 2**159)

    def test_the_identity_works_for_mutual_tls_with_strict_verification(self) -> None:
        identity = self.generate()

        def password() -> bytes:
            return self.passphrase

        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        for context in (server, client):
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            context.load_cert_chain(str(identity.cert_path), str(identity.key_path), password=password)
            context.load_verify_locations(cafile=str(identity.cert_path))
            context.verify_mode = ssl.CERT_REQUIRED
            context.verify_flags |= ssl.VERIFY_X509_STRICT
        client.check_hostname = False
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        served: list[Any] = []

        def serve() -> None:
            try:
                connection, _ = listener.accept()
                with server.wrap_socket(connection, server_side=True) as secured:
                    served.append(secured.recv(5))
                    secured.sendall(b"pong!")
            except OSError as error:
                served.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        with socket.create_connection(listener.getsockname(), timeout=10) as raw, client.wrap_socket(raw) as secured:
            secured.sendall(b"ping!")
            self.assertEqual(secured.recv(5), b"pong!")
        thread.join(10)
        self.assertEqual(served, [b"ping!"])

    def test_a_wrong_passphrase_cannot_load_the_key(self) -> None:
        identity = self.generate()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        with self.assertRaises(ssl.SSLError):
            context.load_cert_chain(str(identity.cert_path), str(identity.key_path), password=lambda: b"wrong passphrase")
        context.load_cert_chain(str(identity.cert_path), str(identity.key_path), password=lambda: self.passphrase)

    def test_refuses_a_relative_or_bare_openssl_path(self) -> None:
        for name in ("openssl", "openssl.exe", os.path.join("bin", "openssl"), os.path.join("..", self.openssl.name)):
            with self.subTest(name=name), self.assertRaisesRegex(CertgenError, "absolute"):
                generate_identity(Path(name), self.folder, self.passphrase, now=NOW)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_refuses_an_openssl_path_that_does_not_exist(self) -> None:
        with self.assertRaises(CertgenError):
            generate_identity(self.openssl.parent / "no-such-openssl", self.folder, self.passphrase, now=NOW)

    def test_refuses_a_missing_output_folder(self) -> None:
        with self.assertRaises(CertgenError):
            generate_identity(self.openssl, self.folder / "missing", self.passphrase, now=NOW)

    def test_refuses_passphrases_that_do_not_fit_one_stdin_line(self) -> None:
        for bad in (b"", b"two\nlines", b"carriage\rreturn", b"nul\x00byte"):
            with self.subTest(bad=bad), self.assertRaises(CertgenError):
                generate_identity(self.openssl, self.folder, bad, now=NOW)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_refuses_a_time_without_a_zone(self) -> None:
        with self.assertRaises(CertgenError):
            self.generate(now=datetime(2026, 11, 15, 12, 0, 0))  # noqa: DTZ001

    def test_refuses_to_overwrite_an_existing_identity(self) -> None:
        first = self.generate()
        before = (first.key_path.read_bytes(), first.cert_path.read_bytes())
        with self.assertRaisesRegex(CertgenError, "already exists"):
            self.generate()
        self.assertEqual((first.key_path.read_bytes(), first.cert_path.read_bytes()), before)

    def test_a_failing_openssl_raises_and_leaves_nothing_behind(self) -> None:
        with self.assertRaises(CertgenError):
            generate_identity(Path(sys.executable), self.folder, self.passphrase, now=NOW)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_a_failure_after_generation_removes_the_files(self) -> None:
        with mock.patch.object(certgen, "device_id", side_effect=ValueError("boom")), self.assertRaises(CertgenError):
            self.generate()
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_runs_openssl_without_a_shell_with_a_minimal_environment_and_the_passphrase_on_stdin(self) -> None:
        calls: list[dict[str, Any]] = []
        real = subprocess.run

        def record(argv: list[str], **options: Any) -> Any:
            calls.append({"argv": argv, **options})
            return real(argv, **options)

        hostile = {"OPENSSL_CONF": "x", "OPENSSL_MODULES": "y", "RANDOM_VAR": "z"}
        with mock.patch.dict(os.environ, hostile), mock.patch.object(subprocess, "run", side_effect=record):
            self.generate()
        self.assertGreaterEqual(len(calls), 3)
        for call in calls:
            with self.subTest(argv=call["argv"][1:3]):
                self.assertIsInstance(call["argv"], list)
                self.assertEqual(call["argv"][0], str(self.openssl))
                self.assertFalse(call.get("shell", False))
                self.assertEqual(call["cwd"], str(self.folder))
                self.assertGreater(call["timeout"], 0)
                self.assertTrue(set(call["env"]) <= {"SYSTEMROOT", "SYSTEMDRIVE"}, call["env"])
                self.assertFalse(any(self.passphrase.decode() in argument for argument in call["argv"]))
        for call in calls:
            if call["argv"][1] in ("genpkey", "req") and "-help" not in call["argv"]:
                self.assertEqual(call["input"], self.passphrase + b"\n")
                self.assertIn("stdin", call["argv"])
        genpkey = next(call["argv"] for call in calls if call["argv"][1] == "genpkey")
        self.assertIn("ec_paramgen_curve:prime256v1", genpkey)
        self.assertIn("-aes-256-cbc", genpkey)

    def test_the_passphrase_is_never_written_to_the_output_folder(self) -> None:
        self.generate()
        for entry in self.folder.iterdir():
            self.assertNotIn(self.passphrase, entry.read_bytes())


class EnvironmentTest(unittest.TestCase):
    def test_keeps_only_the_system_variables_on_windows_and_nothing_elsewhere(self) -> None:
        with mock.patch.dict(os.environ, {"OPENSSL_CONF": "x", "OPENSSL_MODULES": "y", "PATH": "z"}):
            environment = certgen._environment()
        if sys.platform == "win32":
            self.assertEqual(set(environment), {"SYSTEMROOT", "SYSTEMDRIVE"} & {name.upper() for name in os.environ})
        else:
            self.assertEqual(environment, {})


class CheckInputsTest(unittest.TestCase):
    def test_a_relative_path_is_refused_before_anything_runs(self) -> None:
        with mock.patch.object(subprocess, "run") as run, self.assertRaises(CertgenError):
            generate_identity(Path("openssl"), Path(tempfile.gettempdir()), b"x", now=NOW)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
