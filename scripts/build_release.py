from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a self-contained local wheel")
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    web = root / "web"
    pnpm = os.environ.get("PLOW_WHIP_PNPM") or shutil.which("pnpm")
    if not args.skip_build:
        if not pnpm:
            raise SystemExit("pnpm not found; set PLOW_WHIP_PNPM to its executable path")
        if not args.skip_install:
            subprocess.run([pnpm, "install", "--frozen-lockfile"], cwd=web, check=True)
        subprocess.run([pnpm, "run", "build"], cwd=web, check=True)
    built = web / "dist"
    if not (built / "index.html").is_file():
        raise SystemExit("web/dist is missing; run without --skip-build")
    package_static = root / "backend" / "plow_whip_web" / "static"
    shutil.rmtree(package_static, ignore_errors=True)
    shutil.copytree(built, package_static)
    output = root / "dist"
    output.mkdir(exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(output)],
        cwd=root, check=True,
    )


if __name__ == "__main__":
    main()
