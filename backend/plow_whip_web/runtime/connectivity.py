from __future__ import annotations

import socket
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConnectivityResult:
    state: str
    domestic_ok: bool
    overseas_ok: bool
    model_invoked: bool = False


class ConnectivityProbe:
    model_invoked = False

    def __init__(
        self,
        *,
        domestic_host: str = "223.5.5.5",
        overseas_host: str = "1.1.1.1",
        port: int = 53,
        timeout_seconds: float = 0.2,
    ) -> None:
        self.domestic_host = domestic_host
        self.overseas_host = overseas_host
        self.port = port
        self.timeout_seconds = timeout_seconds

    def check(self) -> ConnectivityResult:
        domestic = self._reachable(self.domestic_host)
        overseas = self._reachable(self.overseas_host)
        return classify_connectivity(domestic, overseas)

    def _reachable(self, host: str) -> bool:
        try:
            with socket.create_connection((host, self.port), timeout=self.timeout_seconds):
                return True
        except OSError:
            return False


def classify_connectivity(domestic_ok: bool, overseas_ok: bool) -> ConnectivityResult:
    if domestic_ok and overseas_ok:
        state = "online"
    elif domestic_ok:
        state = "domestic_only"
    elif overseas_ok:
        state = "overseas_only"
    else:
        state = "offline"
    return ConnectivityResult(state, domestic_ok, overseas_ok)


def network_available(requirement: str, connectivity: str) -> bool:
    if requirement == "none":
        return True
    if requirement == "any":
        return connectivity != "offline"
    if requirement == "domestic":
        return connectivity in {"online", "domestic_only"}
    if requirement == "overseas":
        return connectivity in {"online", "overseas_only"}
    return False
