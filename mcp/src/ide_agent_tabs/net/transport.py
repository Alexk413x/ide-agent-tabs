from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Protocol

from .interfaces import InterfaceError, InterfaceInfo, private_interfaces, require_lan_ready
from .pairing import check_lan_destination
from .policy import NetworkConfig, is_allowed_bind

ENABLED_MODES = frozenset({"lan"})


class Transport(Protocol):
    def allows_bind(self, address: str) -> bool: ...

    def bind_address(self, config: NetworkConfig) -> str | None: ...

    def candidates(self, addresses: Iterable[str], describe: Callable[[str], InterfaceInfo]) -> list[InterfaceInfo]: ...

    def check_destination(self, address: str, port: int, interface_address: str, prefix_len: int) -> str: ...

    def check_connection(self, address: str) -> None: ...

    def firewall_scope(self, info: InterfaceInfo) -> str: ...

    def require_ready(self, address: str, allow_virtual: bool = False) -> InterfaceInfo: ...

    def wait_ready(
        self,
        address: str,
        timeout_s: float,
        allow_virtual: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> InterfaceInfo: ...


class LanTransport:
    def allows_bind(self, address: str) -> bool:
        return is_allowed_bind(address)

    def bind_address(self, config: NetworkConfig) -> str | None:
        return config.bind if config.bind is not None and self.allows_bind(config.bind) else None

    def candidates(self, addresses: Iterable[str], describe: Callable[[str], InterfaceInfo]) -> list[InterfaceInfo]:
        return private_interfaces(addresses, describe)

    def check_destination(self, address: str, port: int, interface_address: str, prefix_len: int) -> str:
        return check_lan_destination(address, port, interface_address, prefix_len)

    def check_connection(self, address: str) -> None:
        return None

    def firewall_scope(self, info: InterfaceInfo) -> str:
        return info.subnet

    def require_ready(self, address: str, allow_virtual: bool = False) -> InterfaceInfo:
        return require_lan_ready(address, allow_virtual=allow_virtual)

    def wait_ready(
        self,
        address: str,
        timeout_s: float,
        allow_virtual: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> InterfaceInfo:
        deadline = clock() + timeout_s
        while True:
            try:
                return self.require_ready(address, allow_virtual)
            except InterfaceError:
                if clock() >= deadline:
                    raise
                sleep(min(1.0, max(0.0, deadline - clock())))


def network_enabled(config: NetworkConfig) -> bool:
    return config.network in ENABLED_MODES


def transport_for(config: NetworkConfig) -> Transport | None:
    return LanTransport() if config.network == "lan" else None
