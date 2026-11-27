from __future__ import annotations

import time
from collections.abc import Callable, Collection, Iterable
from typing import Protocol

from .interfaces import InterfaceError, InterfaceInfo, interface_for, private_interfaces, require_lan_ready
from .pairing import PairingError, check_lan_destination
from .policy import NetworkConfig, is_allowed_bind
from .tailscale import TAILSCALE_SCOPE, TailscaleClient, TailscaleError, check_owner, is_tailscale_address

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
        return _wait_until_ready(self.require_ready, address, timeout_s, allow_virtual, sleep, clock)


class TailscaleTransport:
    def __init__(
        self,
        client: TailscaleClient,
        allowed_owner_ids: Collection[int],
        describe: Callable[[str], InterfaceInfo] = interface_for,
    ) -> None:
        self.client = client
        self.allowed_owner_ids = frozenset(allowed_owner_ids)
        self._describe = describe

    def allows_bind(self, address: str) -> bool:
        return is_tailscale_address(address)

    def bind_address(self, config: NetworkConfig) -> str | None:
        return config.bind if config.bind is not None and self.allows_bind(config.bind) else None

    def candidates(self, addresses: Iterable[str], describe: Callable[[str], InterfaceInfo]) -> list[InterfaceInfo]:
        in_range = [address for address in addresses if is_tailscale_address(address)]
        if not in_range:
            return []
        own = self.client.ip4()
        found: list[InterfaceInfo] = []
        for address in in_range:
            if address != own:
                continue
            try:
                found.append(describe(address))
            except (ValueError, InterfaceError):
                continue
        return found

    def check_destination(self, address: str, port: int, interface_address: str, prefix_len: int) -> str:
        if not is_tailscale_address(address):
            raise PairingError(f"the address is not a Tailscale address ({TAILSCALE_SCOPE})")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise PairingError("the port must be a whole number from 1 to 65535")
        if not is_tailscale_address(interface_address):
            raise PairingError(f"the selected interface is not a Tailscale address ({TAILSCALE_SCOPE})")
        if address == interface_address:
            raise PairingError("the address is this machine's own address")
        try:
            check_owner(self.client.whois(address), self.allowed_owner_ids)
        except TailscaleError as error:
            raise PairingError(str(error)) from error
        return address

    def check_connection(self, address: str) -> None:
        check_owner(self.client.whois(address), self.allowed_owner_ids)

    def firewall_scope(self, info: InterfaceInfo) -> str:
        return TAILSCALE_SCOPE

    def require_ready(self, address: str, allow_virtual: bool = False) -> InterfaceInfo:
        if not is_tailscale_address(address):
            raise InterfaceError(f"{address} is not a Tailscale address ({TAILSCALE_SCOPE}), so Tailscale mode refuses it")
        try:
            own = self.client.ip4()
        except TailscaleError as error:
            raise InterfaceError(str(error)) from error
        if address != own:
            raise InterfaceError(f"{address} is not this machine's Tailscale address ({own})")
        info = self._describe(address)
        if not info.alias:
            raise InterfaceError(f"no network interface is known to carry {address}")
        return info

    def wait_ready(
        self,
        address: str,
        timeout_s: float,
        allow_virtual: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> InterfaceInfo:
        return _wait_until_ready(self.require_ready, address, timeout_s, allow_virtual, sleep, clock)


def _wait_until_ready(
    require_ready: Callable[[str, bool], InterfaceInfo],
    address: str,
    timeout_s: float,
    allow_virtual: bool,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> InterfaceInfo:
    deadline = clock() + timeout_s
    while True:
        try:
            return require_ready(address, allow_virtual)
        except InterfaceError:
            if clock() >= deadline:
                raise
            sleep(min(1.0, max(0.0, deadline - clock())))


def network_enabled(config: NetworkConfig) -> bool:
    return config.network in ENABLED_MODES


def transport_for(config: NetworkConfig) -> Transport | None:
    return LanTransport() if config.network == "lan" else None
