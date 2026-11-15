from __future__ import annotations

import base64
import hashlib
import unittest

from ide_agent_tabs.net.identity import _ALPHABET, _check_character, der_from_pem, device_id, format_digest, parse_device_id

# lib/protocol/deviceid_test.go in github.com/syncthing/syncthing
SYNCTHING_SOURCE_ID = "P56IOI7-MZJNU2Y-IQGDREY-DM2MGTI-MGL3BXN-PQ6W5BM-TBBZ4TJ-XZWICQ2"
SYNCTHING_SOURCE_DIGEST = "7f7c8723ecca5b4d22061c4981b34c34d865ec376be1eb74330873c9a6f9b205"
# https://docs.syncthing.net/dev/device-ids.html: the digest is b"asdl" * 8
SYNCTHING_DOCS_ID = "MFZWI3D-BONSGYC-YLTMRWG-C43ENR5-QXGZDMM-FZWI3DP-BONSGYY-LTMRWAD"
SYNCTHING_DOCS_DIGEST = b"asdl" * 8

DER = b"\x30\x03\x02\x01\x00"


def pem(der: bytes = DER, label: str = "CERTIFICATE") -> str:
    body = base64.b64encode(der).decode("ascii")
    lines = [body[start : start + 64] for start in range(0, len(body), 64)]
    return "\n".join([f"-----BEGIN {label}-----", *lines, f"-----END {label}-----", ""])


def with_check_characters(payload: str) -> str:
    return "".join(payload[start : start + 13] + _check_character(payload[start : start + 13]) for start in range(0, 52, 13))


class DeviceIdTest(unittest.TestCase):
    def test_formats_the_digest_of_syncthings_test_vector(self) -> None:
        self.assertEqual(format_digest(bytes.fromhex(SYNCTHING_SOURCE_DIGEST)), SYNCTHING_SOURCE_ID)

    def test_formats_the_digest_of_the_syncthing_docs_example(self) -> None:
        self.assertEqual(format_digest(SYNCTHING_DOCS_DIGEST), SYNCTHING_DOCS_ID)

    def test_device_id_hashes_the_der_with_sha256(self) -> None:
        for der in (DER, b"", bytes(range(256)) * 4):
            with self.subTest(length=len(der)):
                self.assertEqual(device_id(der), format_digest(hashlib.sha256(der).digest()))

    def test_layout(self) -> None:
        text = device_id(DER)
        self.assertEqual(len(text), 63)
        self.assertEqual([len(chunk) for chunk in text.split("-")], [7] * 8)
        self.assertEqual(text, text.upper())
        self.assertTrue(set(text.replace("-", "")) <= set(_ALPHABET))

    def test_every_digit_of_the_alphabet_has_a_check_character(self) -> None:
        for digit in _ALPHABET:
            with self.subTest(digit=digit):
                self.assertIn(_check_character(digit * 13), _ALPHABET)

    def test_digest_of_the_wrong_size_is_refused(self) -> None:
        for size in (0, 31, 33):
            with self.subTest(size=size), self.assertRaises(ValueError):
                format_digest(bytes(size))


class ParseDeviceIdTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        for der in (DER, b"", b"x" * 1000):
            with self.subTest(length=len(der)):
                self.assertEqual(parse_device_id(device_id(der)), hashlib.sha256(der).digest())

    def test_syncthing_vectors(self) -> None:
        self.assertEqual(parse_device_id(SYNCTHING_SOURCE_ID), bytes.fromhex(SYNCTHING_SOURCE_DIGEST))
        self.assertEqual(parse_device_id(SYNCTHING_DOCS_ID), SYNCTHING_DOCS_DIGEST)

    def test_accepts_the_forms_syncthing_accepts_when_they_carry_check_characters(self) -> None:
        compact = SYNCTHING_SOURCE_ID.replace("-", "")
        forms = [
            compact,
            compact.lower(),
            SYNCTHING_SOURCE_ID.replace("-", " "),
            "P56IOI7 MZJNU2Y IQGDREY DM2MGTI MGL3BXN PQ6W5BM TBBZ4TJ XZWICQ2",
            "-".join(compact[start : start + 14] for start in range(0, 56, 14)),
        ]
        for form in forms:
            with self.subTest(form=form):
                self.assertEqual(parse_device_id(form).hex(), SYNCTHING_SOURCE_DIGEST)

    def test_refuses_the_form_without_check_characters(self) -> None:
        for form in ("P56IOI7MZJNU2IQGDREYDM2MGTMGL3BXNPQ6W5BTBBZ4TJXZWICQ", "P56IOI7-MZJNU2-IQGDREY-DM2MGT-MGL3BXN-PQ6W5B-TBBZ4TJ-XZWICQ"):
            with self.subTest(form=form), self.assertRaises(ValueError):
                parse_device_id(form)

    def test_refuses_each_wrong_check_character(self) -> None:
        compact = SYNCTHING_SOURCE_ID.replace("-", "")
        for position in range(13, 56, 14):
            right = compact[position]
            for wrong in _ALPHABET:
                if wrong == right:
                    continue
                with self.subTest(position=position, wrong=wrong), self.assertRaisesRegex(ValueError, "check character"):
                    parse_device_id(compact[:position] + wrong + compact[position + 1 :])

    def test_refuses_a_changed_payload_character(self) -> None:
        compact = SYNCTHING_SOURCE_ID.replace("-", "")
        for position in (0, 5, 12, 14, 30, 54):
            changed = "A" if compact[position] != "A" else "B"
            with self.subTest(position=position), self.assertRaises(ValueError):
                parse_device_id(compact[:position] + changed + compact[position + 1 :])

    def test_refuses_wrong_lengths(self) -> None:
        compact = SYNCTHING_SOURCE_ID.replace("-", "")
        for text in ("", "a", compact[:7], compact[:55], compact + "A", compact + compact):
            with self.subTest(length=len(text)), self.assertRaises(ValueError):
                parse_device_id(text)

    def test_refuses_characters_outside_the_alphabet(self) -> None:
        compact = SYNCTHING_SOURCE_ID.replace("-", "")
        for bad in ("0", "1", "8", "9", "=", "\n", "\x00", "ß", "İ", "_", "."):
            with self.subTest(bad=repr(bad)), self.assertRaises(ValueError):
                parse_device_id(compact[:10] + bad + compact[11:])

    def test_refuses_syncthings_typo_correction(self) -> None:
        with self.assertRaises(ValueError):
            parse_device_id("P561017MZJNU2YIQGDREYDM2MGTIMGL3BXNPQ6W5BMT88Z4TJXZWICQ2")

    def test_refuses_surrounding_whitespace_other_than_spaces(self) -> None:
        for text in (SYNCTHING_SOURCE_ID + "\n", "\t" + SYNCTHING_SOURCE_ID):
            with self.subTest(text=repr(text)), self.assertRaises(ValueError):
                parse_device_id(text)

    def test_refuses_nonzero_padding_bits_even_with_correct_check_characters(self) -> None:
        payload = SYNCTHING_SOURCE_ID.replace("-", "")
        payload = "".join(payload[start : start + 13] for start in range(0, 56, 14))
        self.assertEqual(payload[-1], "Q")
        forged = with_check_characters(payload[:-1] + "R")
        with self.assertRaisesRegex(ValueError, "padding"):
            parse_device_id(forged)


class DerFromPemTest(unittest.TestCase):
    def test_reads_one_certificate_block(self) -> None:
        self.assertEqual(der_from_pem(pem()), DER)
        self.assertEqual(der_from_pem(pem().replace("\n", "\r\n")), DER)

    def test_reads_a_long_block(self) -> None:
        der = b"\x30" + bytes(range(256)) * 3
        self.assertEqual(der_from_pem(pem(der)), der)

    def test_refuses_anything_but_exactly_one_certificate(self) -> None:
        cases = {
            "empty": "",
            "no block": "hello",
            "two blocks": pem() + pem(),
            "a private key": pem(DER, "PRIVATE KEY"),
            "a certificate and a key": pem() + pem(DER, "ENCRYPTED PRIVATE KEY"),
            "a certificate request": pem(DER, "CERTIFICATE REQUEST"),
            "unterminated": "-----BEGIN CERTIFICATE-----\nMAMCAQA=\n",
            "not base64": "-----BEGIN CERTIFICATE-----\nMA!MCAQA=\n-----END CERTIFICATE-----\n",
            "not a DER sequence": pem(b"\x31\x03\x02\x01\x00"),
            "empty body": pem(b""),
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                der_from_pem(text)


if __name__ == "__main__":
    unittest.main()
