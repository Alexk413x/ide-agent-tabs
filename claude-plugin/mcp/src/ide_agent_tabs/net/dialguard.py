from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .pairing import PairingError
from .policy import NetworkConfig, load_network_config
from .tailscale import TailscaleClient, TailscaleError
from .transport import TailscaleTransport, transport_for

TRUST_S = 30.0
OWN_PREFIX_LEN = 10
MAX_TRUSTED = 256


class DialGuard:
    def __init__(
        self,
        load: Callable[[str], tuple[NetworkConfig, list[str]]] = load_network_config,
        client: TailscaleClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        trust_s: float = TRUST_S,
    ) -> None:
        self._load = load
        self._client = client
        self._clock = clock
        self._trust_s = trust_s
        self._lock = threading.Lock()
        self._transport: TailscaleTransport | None = None
        self._trusted: dict[str, float] = {}

    def _current(self, config: NetworkConfig) -> TailscaleTransport | None:
        with self._lock:
            held = self._transport
            if held is None or not held.allowed_owner_ids:
                built = transport_for(config, self._client)
                held = built if isinstance(built, TailscaleTransport) else None
                self._transport = held
            return held

    def check(self, home: str, address: str, port: int) -> None:
        config, _ = self._load(home)
        if config.network != "tailscale":
            return
        now = self._clock()
        with self._lock:
            until = self._trusted.get(address)
        if until is not None and now < until:
            return
        transport = self._current(config)
        if transport is None:
            raise PairingError("Tailscale mode has no transport")
        try:
            own = transport.client.ip4()
        except TailscaleError as error:
            raise PairingError(str(error)) from error
        transport.check_destination(address, port, own, OWN_PREFIX_LEN)
        with self._lock:
            if len(self._trusted) >= MAX_TRUSTED:
                self._trusted.clear()
            self._trusted[address] = now + self._trust_s


GUARD = DialGuard()
