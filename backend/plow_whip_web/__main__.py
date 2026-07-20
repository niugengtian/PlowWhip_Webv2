from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import uvicorn

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run plow-whip Web v2")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8742)
    parser.add_argument("--data-dir", type=Path, default=Path("runtime"))
    parser.add_argument("command", nargs="?", choices=("serve", "scheduler-tick"), default="serve")
    parser.add_argument("--embedded-cron", action="store_true", default=_env_flag("PLOW_WHIP_EMBEDDED_CRON"))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    bridge_token = os.environ.get("PLOW_WHIP_BRIDGE_TOKEN")
    settings = Settings(
        data_dir=args.data_dir.resolve(), bind_host=args.host,
        api_token=os.environ.get("PLOW_WHIP_API_TOKEN"),
        embedded_cron=args.embedded_cron,
        container_loopback=_env_flag("PLOW_WHIP_CONTAINER_LOOPBACK"),
        host_bridge_url=os.environ.get("PLOW_WHIP_BRIDGE_URL", "http://host.docker.internal:8765"),
        host_bridge_token=bridge_token,
        butler_planner_provider=os.environ.get(
            "PLOW_WHIP_BUTLER_PLANNER_PROVIDER", "codex"
        ),
        butler_planner_timeout_seconds=int(
            os.environ.get("PLOW_WHIP_BUTLER_PLANNER_TIMEOUT_SECONDS", "180")
        ),
    )
    app = create_app(settings)
    if args.command == "scheduler-tick":
        result = app.state.scheduler_service.tick()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return
    if bridge_token:
        print(
            "Worker Pool 启动提醒：请确认 Host Bridge 正在运行，"
            "并在 Provider 页面执行 0 Token 探测。"
        )
    else:
        print(
            "Worker Pool 未就绪：缺少 PLOW_WHIP_BRIDGE_TOKEN。"
            "请从 .env.local.example 创建 .env.local，再启动 Host Bridge。"
        )
    runner = app.state.embedded_cron_runner
    if settings.embedded_cron:
        runner.start()
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        if settings.embedded_cron:
            runner.stop()


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    main()
