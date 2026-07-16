from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
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
