from __future__ import annotations

import time
from collections.abc import Callable, Generator

from .beacon import (
    BROADCAST,
    MULTICAST,
    Endpoint,
    Heard,
    HubView,
    Inbox,
    MemberBeacon,
    MemberCheck,
    Wire,
)

REMEMBERED = "remembered"
PROBE = "probe"
SOURCE_ORDER = (REMEMBERED, MULTICAST, BROADCAST, PROBE)


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
        self.datagrams = 0
        self.invalid = 0
        self.ignored_beacons = 0
        self.group_joined: bool | None = None
        self.setup_notes: tuple[str, ...] = ()
        self._hub_view = hub_view
        self._make_endpoint = make_endpoint
        self._clock = clock
        self._poll_s = poll_s
        self._inbox: Inbox | None = None

    def paths_for(self, identifier: str) -> frozenset[str]:
        return self._inbox.paths_for(identifier) if self._inbox is not None else frozenset()

    def listen(self, until: Callable[[], bool]) -> Generator[Heard | None, None, None]:
        try:
            endpoint = self._make_endpoint(self.wire)
        except OSError as error:
            self.error = str(error.strerror or error)
            return
        self.group_joined = endpoint.joined
        self.setup_notes = tuple(endpoint.notes)
        inbox = Inbox(self.wire, self.device_id)
        self._inbox = inbox
        check = MemberCheck(self._hub_view, self._clock) if self._hub_view is not None else None
        try:
            while not until():
                datagram = endpoint.recv(self._poll_s)
                if datagram is not None:
                    self.datagrams += 1
                heard = inbox.accept(datagram) if datagram is not None else None
                self.invalid = inbox.rejected
                if (
                    heard is not None
                    and isinstance(heard.announcement, MemberBeacon)
                    and (check is None or not check.check(heard.announcement).ok)
                ):
                    self.ignored_beacons += 1
                    heard = None
                yield heard
        finally:
            endpoint.close()
