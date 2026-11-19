from __future__ import annotations

import contextlib
import hmac
import os
import re
import secrets
from pathlib import Path

from .private_files import check_private, ensure_private_dir, make_private_file

INGRESS_PATH = "/net/ingress"
INGRESS_TOKEN_FILE = "ingress.token"
_TOKEN = re.compile("[0-9a-f]{64}")


class IngressTokenError(Exception):
    pass


def net_dir(home: str) -> Path:
    return Path(home) / "net"


def ingress_token_path(home: str) -> Path:
    return net_dir(home) / INGRESS_TOKEN_FILE


def read_ingress_token(home: str) -> str | None:
    path = ingress_token_path(home)
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if _TOKEN.fullmatch(text) is None:
        return None
    problems = check_private(path)
    if problems:
        raise IngressTokenError(f"{path} is not private: {'; '.join(problems)}")
    return text


def ensure_ingress_token(home: str) -> str:
    known = read_ingress_token(home)
    if known is not None:
        return known
    ensure_private_dir(net_dir(home))
    path = ingress_token_path(home)
    with contextlib.suppress(FileExistsError):
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(secrets.token_hex(32))
        make_private_file(path)
    token = read_ingress_token(home)
    if token is None:
        raise IngressTokenError(f"{path} holds no valid token; delete it and run net lan setup again")
    return token


def ingress_allowed(header: str | None, token: str | None) -> bool:
    if not token or _TOKEN.fullmatch(token) is None or header is None or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer ") :].strip(), token)
