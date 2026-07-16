from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run plow-whip Web v2")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8742)
    parser.add_argument("--data-dir", type=Path, default=Path("runtime"))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings(data_dir=args.data_dir.resolve())
    uvicorn.run(create_app(settings), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
