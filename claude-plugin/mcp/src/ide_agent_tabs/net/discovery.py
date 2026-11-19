from __future__ import annotations

import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from .beacon import (
    BROADCAST,
    MULTICAST,
    Endpoint,
    Heard,
    HubView,
    Inbox,
    JoinAnnouncement,
    MemberBeacon,
    MemberCheck,
    Wire,
    build_join,
)

REMEMBERED = "remembered"
PROBE = "probe"
SOURCE_ORDER = (REMEMBERED, MULTICAST, BROADCAST, PROBE)


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
    source: str = ""


class Discovery(Protocol):
    def announce(self, hub_id: str, hub_name: str, device_name: str, port: int) -> None: ...

    def browse(self, timeout: float) -> list[Candidate]: ...


class NullDiscovery:
    def announce(self, hub_id: str, hub_name: str, device_name: str, port: int) -> None:
        return None

    def browse(self, timeout: float) -> list[Candidate]:
        return []


def source_label(paths: frozenset[str] | set[str]) -> str:
    return "+".join(name for name in SOURCE_ORDER if name in paths)


class LanDiscovery:
    def __init__(
        self,
        wire: Wire,
        device_id: str,
        *,
        hub_view: Callable[[], HubView | None] | None = None,
        make_endpoint: Callable[[Wire], Endpoint] = Endpoint,
        clock: Callable[[], float] = time.time,
        poll_s: float = 0.25,
    ) -> None:
        self.wire = wire
        self.device_id = device_id
        self.error: str | None = None
        self._hub_view = hub_view
        self._make_endpoint = make_endpoint
        self._clock = clock
        self._poll_s = poll_s
        self._inbox: Inbox | None = None

    def announce(self, hub_id: str, hub_name: str, device_name: str, port: int) -> None:
        endpoint = self._make_endpoint(self.wire)
        try:
            endpoint.send(build_join(device_name, self.device_id, hub_id or None, hub_name or None, port))
        finally:
            endpoint.close()

    def paths_for(self, identifier: str) -> frozenset[str]:
        return self._inbox.paths_for(identifier) if self._inbox is not None else frozenset()

    def listen(self, until: Callable[[], bool]) -> Generator[Heard | None, None, None]:
        try:
            endpoint = self._make_endpoint(self.wire)
        except OSError as error:
            self.error = str(error.strerror or error)
            return
        inbox = Inbox(self.wire, self.device_id)
        self._inbox = inbox
        check = MemberCheck(self._hub_view, self._clock) if self._hub_view is not None else None
        try:
            while not until():
                datagram = endpoint.recv(self._poll_s)
                heard = inbox.accept(datagram) if datagram is not None else None
                if (
                    heard is not None
                    and isinstance(heard.announcement, MemberBeacon)
                    and (check is None or not check.check(heard.announcement).ok)
                ):
                    heard = None
                yield heard
        finally:
            endpoint.close()

    def browse(self, timeout: float) -> list[Candidate]:
        deadline = time.monotonic() + timeout
        found: dict[tuple[str, str], tuple[Candidate, str]] = {}
        for heard in self.listen(lambda: time.monotonic() >= deadline):
            if heard is None:
                continue
            announcement = heard.announcement
            if isinstance(announcement, JoinAnnouncement):
                candidate = Candidate(
                    announcement.device_id,
                    announcement.device_name,
                    announcement.hub_id or "",
                    announcement.hub_name or "",
                    heard.host,
                    announcement.port,
                    "join",
                    False,
                )
            else:
                candidate = Candidate(announcement.device_id, "", announcement.hub_id, "", heard.host, announcement.port, "member", False)
            found.setdefault((candidate.device_id, candidate.address), (candidate, announcement.id))
        rows: list[Candidate] = []
        for candidate, identifier in found.values():
            source = source_label(self.paths_for(identifier))
            rows.append(replace(candidate, source=source))
        return rows
