from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    database_name: str = "plow-whip-web.sqlite3"
    bind_host: str = "127.0.0.1"
    api_token: str | None = None
    embedded_cron: bool = False
    container_loopback: bool = False
    host_bridge_url: str = "http://host.docker.internal:8765"
    host_bridge_token: str | None = None

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.database_name

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.is_loopback and not self.api_token:
            raise ValueError("non-loopback binding requires PLOW_WHIP_API_TOKEN")

    @property
    def is_loopback(self) -> bool:
        if self.container_loopback:
            return True
        if self.bind_host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.bind_host).is_loopback
        except ValueError:
            return False
