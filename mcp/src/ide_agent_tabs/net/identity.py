from __future__ import annotations

import base64
import hashlib
import re

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_GROUP_LENGTH = 13
_GROUPS = 4
_ENCODED_LENGTH = _GROUP_LENGTH * _GROUPS
_CHUNK_LENGTH = 7
_PEM_BLOCK = re.compile(r"-----BEGIN CERTIFICATE-----([A-Za-z0-9+/=\s]*)-----END CERTIFICATE-----")
_PEM_MARKER = re.compile(r"-----(?:BEGIN|END) ")
_ID_CHARACTERS = re.compile(rf"[A-Za-z2-7]{{{_ENCODED_LENGTH + _GROUPS}}}")


# Syncthing's check character, not standard Luhn mod N: doubling starts at the first character.
# Changing it makes every id differ from Syncthing's.
def _check_character(group: str) -> str:
    factor = 1
    total = 0
    for character in group:
        addend = factor * _ALPHABET.index(character)
        factor = 1 if factor == 2 else 2
        total += addend // 32 + addend % 32
    return _ALPHABET[(32 - total % 32) % 32]


def _encode_digest(digest: bytes) -> str:
    return base64.b32encode(digest).decode("ascii").rstrip("=")


def format_digest(digest: bytes) -> str:
    if len(digest) != 32:
        raise ValueError("a device id digest is 32 bytes")
    encoded = _encode_digest(digest)
    checked = "".join(
        encoded[start : start + _GROUP_LENGTH] + _check_character(encoded[start : start + _GROUP_LENGTH])
        for start in range(0, _ENCODED_LENGTH, _GROUP_LENGTH)
    )
    return "-".join(checked[start : start + _CHUNK_LENGTH] for start in range(0, len(checked), _CHUNK_LENGTH))


def device_id(der: bytes) -> str:
    return format_digest(hashlib.sha256(der).digest())


def parse_device_id(text: str) -> bytes:
    compact = text.replace("-", "").replace(" ", "")
    if not _ID_CHARACTERS.fullmatch(compact):
        raise ValueError("a device id is 56 letters and digits 2 to 7 (dashes and spaces are ignored)")
    compact = compact.upper()
    stride = _GROUP_LENGTH + 1
    payload = ""
    for start in range(0, len(compact), stride):
        group = compact[start : start + _GROUP_LENGTH]
        if compact[start + _GROUP_LENGTH] != _check_character(group):
            raise ValueError("a check character of the device id is wrong")
        payload += group
    digest = base64.b32decode(payload + "====")
    if _encode_digest(digest) != payload:
        raise ValueError("the device id has nonzero padding bits")
    return digest


def der_from_pem(pem: str) -> bytes:
    blocks = _PEM_BLOCK.findall(pem)
    if len(blocks) != 1 or len(_PEM_MARKER.findall(pem)) != 2:
        raise ValueError("expected exactly one CERTIFICATE block")
    try:
        der = base64.b64decode("".join(blocks[0].split()), validate=True)
    except ValueError as error:
        raise ValueError("the CERTIFICATE block is not valid base64") from error
    if len(der) < 2 or der[0] != 0x30:
        raise ValueError("the CERTIFICATE block is not a DER certificate")
    return der
