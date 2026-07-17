from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from plow_whip_web.config import load_private_env


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    env_file = root / ".env.local"
    try:
        loaded = load_private_env(env_file)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if not loaded:
        print(
            "Worker Pool 提醒：未找到 .env.local；请从 .env.local.example 创建本机配置，"
            "启动 Host Bridge，并在 Provider 页面执行 0 Token 探测。",
            file=sys.stderr,
        )
    web = root / "web"
    if not (web / "dist" / "index.html").is_file():
        pnpm = os.environ.get("PLOW_WHIP_PNPM") or shutil.which("pnpm")
        if not pnpm:
            raise SystemExit("pnpm not found; set PLOW_WHIP_PNPM to its executable path")
        subprocess.run([pnpm, "install", "--frozen-lockfile"], cwd=web, check=True)
        subprocess.run([pnpm, "run", "build"], cwd=web, check=True)
    subprocess.run(
        [sys.executable, "-m", "plow_whip_web", "--data-dir", str(root / "runtime")],
        cwd=root, check=True,
    )


if __name__ == "__main__":
    main()
