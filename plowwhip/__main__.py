from __future__ import annotations

import argparse
import json

from .app import serve
from .monitor import snapshot
from .store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="PlowWhip Web frozen V1 local runtime")
    parser.add_argument("--db", default="data/plowwhip.db")
    parser.add_argument("--data-root", default="data")
    commands = parser.add_subparsers(dest="command", required=True)

    server = commands.add_parser("serve", help="run Web/API with the in-app Cronner")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8742)
    server.add_argument("--cronner-interval", type=float, default=1.0)
    server.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="explicitly allow a container-facing bind",
    )

    monitor = commands.add_parser("monitor", help="read current state and bounded output")
    monitor.add_argument("project_id")

    args = parser.parse_args()
    if args.command == "monitor":
        result = snapshot(args.db, args.data_root, args.project_id)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return
    serve(
        Store(args.db, args.data_root),
        args.host,
        args.port,
        args.cronner_interval,
        args.allow_non_loopback,
    )


if __name__ == "__main__":
    main()
