from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class Candidate:
    device_id: str
    device_name: str
    hub_id: str
    hub_name: str
    address: str
    port: int
    kind: Literal["join", "member"]
    verified: bool


class Discovery(Protocol):
    def announce(self, hub_id: str, hub_name: str, device_name: str, port: int) -> None: ...

    def browse(self, timeout: float) -> list[Candidate]: ...


class NullDiscovery:
    def announce(self, hub_id: str, hub_name: str, device_name: str, port: int) -> None:
        return None

    def browse(self, timeout: float) -> list[Candidate]:
        return []
