from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import re


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_private_env(path: Path) -> bool:
    """Load a local key file without overriding an existing process environment."""
    path = path.expanduser()
    if not path.is_file():
        return False
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ValueError(f"{path} permissions are too open; run: chmod 600 {path}")
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{line_number}: expected NAME=value")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not _ENV_NAME.fullmatch(name):
            raise ValueError(f"{path}:{line_number}: invalid environment variable name")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(name, value)
    return True


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
