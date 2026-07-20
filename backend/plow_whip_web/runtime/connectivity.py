from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class ConnectivityResult:
    state: str
    domestic_ok: bool
    overseas_ok: bool
    domestic_checks: tuple[dict[str, object], ...] = ()
    overseas_checks: tuple[dict[str, object], ...] = ()
    model_invoked: bool = False


class ConnectivityProbe:
    """Bounded multi-signal probe; one endpoint never decides global outage."""

    model_invoked = False

    def __init__(
        self,
        *,
        domestic_host: str = "223.5.5.5",
        overseas_host: str = "1.1.1.1",
        port: int = 53,
        timeout_seconds: float = 0.8,
        domestic_endpoints: tuple[str, ...] = (
            "https://www.baidu.com/",
            "https://www.qq.com/",
        ),
        overseas_endpoints: tuple[str, ...] = (
            "https://api.openai.com/v1/models",
            "https://www.google.com/generate_204",
        ),
    ) -> None:
        self.domestic_host = domestic_host
        self.overseas_host = overseas_host
        self.port = port
        self.timeout_seconds = timeout_seconds
        self.domestic_endpoints = domestic_endpoints
        self.overseas_endpoints = overseas_endpoints

    def check(self) -> ConnectivityResult:
        with ThreadPoolExecutor(max_workers=2) as pool:
            domestic_future = pool.submit(
                self._zone,
                "domestic",
                self.domestic_host,
                self.domestic_endpoints,
            )
            overseas_future = pool.submit(
                self._zone,
                "overseas",
                self.overseas_host,
                self.overseas_endpoints,
            )
            domestic_ok, domestic_checks = domestic_future.result()
            overseas_ok, overseas_checks = overseas_future.result()
        return classify_connectivity(
            domestic_ok,
            overseas_ok,
            domestic_checks=domestic_checks,
            overseas_checks=overseas_checks,
        )

    def _zone(
        self, zone: str, dns_host: str, endpoints: tuple[str, ...]
    ) -> tuple[bool, tuple[dict[str, object], ...]]:
        dns_ok = self._reachable(dns_host)
        checks: list[dict[str, object]] = [
            {"kind": "dns_tcp", "target": dns_host, "ok": dns_ok}
        ]
        with ThreadPoolExecutor(max_workers=max(1, len(endpoints))) as pool:
            endpoint_results = list(pool.map(self._http_reachable, endpoints))
        checks.extend(
            {
                "kind": "public_http",
                "zone": zone,
                "target": endpoint,
                "ok": ok,
                "status": status,
            }
            for endpoint, (ok, status) in zip(
                endpoints, endpoint_results, strict=True
            )
        )
        # DNS plus at least one independent public endpoint is sufficient for
        # the zone. Both endpoint results are still persisted as evidence.
        return dns_ok and any(ok for ok, _ in endpoint_results), tuple(checks)

    def _reachable(self, host: str) -> bool:
        try:
            with socket.create_connection(
                (host, self.port), timeout=self.timeout_seconds
            ):
                return True
        except OSError:
            return False

    def _http_reachable(self, url: str) -> tuple[bool, int | None]:
        request = Request(
            url,
            method="HEAD",
            headers={"User-Agent": "plow-whip-connectivity/2"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return True, int(response.status)
        except HTTPError as error:
            # Authentication/rate-limit/application errors prove transport.
            return True, int(error.code)
        except (URLError, TimeoutError, OSError):
            return False, None


def classify_connectivity(
    domestic_ok: bool,
    overseas_ok: bool,
    *,
    domestic_checks: tuple[dict[str, object], ...] = (),
    overseas_checks: tuple[dict[str, object], ...] = (),
) -> ConnectivityResult:
    if domestic_ok and overseas_ok:
        state = "online"
    elif domestic_ok:
        state = "domestic_only"
    elif overseas_ok:
        state = "overseas_only"
    else:
        state = "offline"
    return ConnectivityResult(
        state,
        domestic_ok,
        overseas_ok,
        domestic_checks,
        overseas_checks,
    )


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
