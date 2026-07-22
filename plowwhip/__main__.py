from __future__ import annotations

import argparse
import json

from .cronner import run_until_idle, tick
from .intake import submit_message
from .monitor import snapshot
from .store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="PlowWhip V1 minimal vertical slice")
    parser.add_argument("--db", default="data/plowwhip.db")
    parser.add_argument("--data-root", default="data")
    commands = parser.add_subparsers(dest="command", required=True)

    message = commands.add_parser("message", help="persist one owner instruction")
    message.add_argument("project_id")
    message.add_argument("content")
    message.add_argument("--key", required=True, help="idempotency key")

    cronner = commands.add_parser("cronner", help="run the only lifecycle wake entry")
    cronner.add_argument("--until-idle", action="store_true")

    monitor = commands.add_parser("monitor", help="read current state and bounded output")
    monitor.add_argument("project_id")

    args = parser.parse_args()
    if args.command == "monitor":
        result = snapshot(args.db, args.data_root, args.project_id)
    else:
        store = Store(args.db, args.data_root)
        store.initialize()
        if args.command == "message":
            result = {
                "message_id": submit_message(store, args.project_id, args.content, args.key)
            }
        else:
            result = run_until_idle(store) if args.until_idle else tick(store)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
