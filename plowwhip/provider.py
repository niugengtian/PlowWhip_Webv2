from __future__ import annotations

import json
import os
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from .store import DEFAULT_SETTINGS


PROVIDERS = {
    "codex_cli": {
        "display_name": "Codex CLI",
        "adapter": "codex",
        "executable": "/Applications/ChatGPT.app/Contents/Resources/codex",
        "minimal_probe": True,
    },
    "cursor_cli": {
        "display_name": "Cursor CLI",
        "adapter": "cursor",
        "executable": "/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
        "minimal_probe": False,
    },
    "deepseek": {
        "display_name": "DeepSeek",
        "adapter": "json-worker",
        "executable": "simple-worker",
        "minimal_probe": False,
    },
    "kimi": {
        "display_name": "Kimi",
        "adapter": "json-worker",
        "executable": "kimi-worker",
        "minimal_probe": False,
    },
}
PROBE_MARKER = "PLOWWHIP_PROBE_OK"
PROBE_TOKEN_CAP = 4096
CHECKER_PASS_MARKER = "PLOWWHIP_CHECKER_PASS"


PROVIDER_ORDERS = {
    role: tuple(order)
    for role, order in DEFAULT_SETTINGS["provider_order"].items()
}


class LocalProvider:
    supports_native_compact = False
    supports_resume = False

    @staticmethod
    def report_context_usage() -> None:
        return None

    @staticmethod
    def compact() -> None:
        raise RuntimeError("local deterministic Provider has no model context")

    @staticmethod
    def start_session() -> None:
        return None


def provider_adapter(provider_key: str) -> LocalProvider:
    if provider_key != "local":
        raise RuntimeError(f"Provider {provider_key} is disabled by V1 scope")
    return LocalProvider()


def provider_facts(role_key: str) -> list[dict[str, object]]:
    """Report local facts without probing or charging external providers."""
    return [
        {
            "provider_key": provider,
            "display_name": PROVIDERS.get(provider, {}).get(
                "display_name", "Local deterministic"
            ),
            "available": provider == "local",
            "reason": (
                "local deterministic adapter"
                if provider == "local"
                else "not_probed"
            ),
            "zero_token_probe": provider in PROVIDERS,
            "minimal_token_probe": bool(
                PROVIDERS.get(provider, {}).get("minimal_probe")
            ),
        }
        for provider in PROVIDER_ORDERS.get(role_key, ())
    ]


def run_provider_probe(provider_key: str, mode: str) -> dict[str, object]:
    """Run one bounded Host Bridge diagnostic without persisting a second truth."""
    provider = PROVIDERS.get(provider_key)
    if not provider:
        raise ValueError("unknown Provider")
    if mode not in {"zero", "minimal"}:
        raise ValueError("probe mode must be zero or minimal")
    if mode == "minimal" and not provider["minimal_probe"]:
        raise ValueError("minimal Token probe requires the read-only Codex adapter")

    checked_at = time.time()
    base_url = os.environ.get(
        "PLOW_WHIP_BRIDGE_URL", "http://host.docker.internal:8765"
    )
    token = os.environ.get("PLOW_WHIP_BRIDGE_TOKEN")
    if not token:
        return _probe_result(
            provider_key,
            mode,
            checked_at,
            configured=False,
            available=False,
            detail="Host Bridge token is not configured",
        )

    if mode == "zero":
        payload = {
            "adapter": provider["adapter"],
            "executable": provider["executable"],
        }
        try:
            response = _bridge_post(base_url, token, "/v1/probe", payload, 20)
        except RuntimeError as error:
            return _probe_result(
                provider_key,
                mode,
                checked_at,
                configured=True,
                available=False,
                detail=str(error),
            )
        return _probe_result(
            provider_key,
            mode,
            checked_at,
            configured=True,
            available=bool(response.get("available")),
            detail=str(response.get("detail") or "no probe detail")[:500],
        )

    project_path = os.environ.get("PLOW_WHIP_PROBE_PROJECT_PATH")
    if not project_path:
        return _probe_result(
            provider_key,
            mode,
            checked_at,
            configured=False,
            available=False,
            detail="PLOW_WHIP_PROBE_PROJECT_PATH is not configured",
            model_invoked=False,
        )
    payload = {
        "adapter": provider["adapter"],
        "executable": provider["executable"],
        "project_path": project_path,
        "prompt": (
            f"Reply with exactly {PROBE_MARKER}. "
            "Do not inspect or modify files. Do not call tools."
        ),
        "session_id": None,
        "timeout_seconds": 60,
        "access": "read",
        "context_policy": {"max_turns": 1, "tool_no_progress_limit": 1},
    }
    try:
        response = _bridge_post(base_url, token, "/v1/execute", payload, 80)
    except RuntimeError as error:
        return _probe_result(
            provider_key,
            mode,
            checked_at,
            configured=True,
            available=False,
            detail=str(error),
            model_invoked=False,
        )
    input_tokens = max(0, int(response.get("input_tokens") or 0))
    cached_tokens = max(0, int(response.get("cached_input_tokens") or 0))
    output_tokens = max(0, int(response.get("output_tokens") or 0))
    if cached_tokens > input_tokens:
        cached_tokens = input_tokens
    return _probe_result(
        provider_key,
        mode,
        checked_at,
        configured=True,
        available=(
            int(response.get("returncode") or 0) == 0
            and PROBE_MARKER in str(response.get("stdout") or "")
        ),
        detail=(
            "minimal terminal probe returned the expected marker"
            if PROBE_MARKER in str(response.get("stdout") or "")
            else "minimal terminal probe did not return the expected marker"
        ),
        model_invoked=True,
        returncode=int(response.get("returncode") or 0),
        marker_found=PROBE_MARKER in str(response.get("stdout") or ""),
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        output_tokens=output_tokens,
        model=str(response.get("model") or provider_key),
    )


def workspace_snapshot(project_path: str) -> dict[str, object]:
    base_url, token = _bridge_configuration()
    return _bridge_post(
        base_url,
        token,
        "/v1/evidence/snapshot",
        {"project_path": project_path, "paths": []},
        30,
    )


def run_provider_task(
    provider_key: str,
    project_path: str,
    prompt: str,
    *,
    session_id: str | None = None,
    access: str = "write",
    timeout_seconds: int = 600,
) -> dict[str, object]:
    provider = PROVIDERS.get(provider_key)
    if not provider:
        raise ValueError("unknown Provider")
    if access not in {"read", "write"}:
        raise ValueError("Provider access must be read or write")
    if access == "read" and provider["adapter"] != "codex":
        raise ValueError("read-only checking requires Codex CLI")
    base_url, token = _bridge_configuration()
    response = _bridge_post(
        base_url,
        token,
        "/v1/execute",
        {
            "adapter": provider["adapter"],
            "executable": provider["executable"],
            "project_path": project_path,
            "prompt": prompt,
            "session_id": session_id,
            "timeout_seconds": min(max(int(timeout_seconds), 10), 86_400),
            "access": access,
            "context_policy": {"max_turns": 24, "tool_no_progress_limit": 6},
        },
        min(max(int(timeout_seconds) + 20, 30), 86_420),
        max_bytes=1_048_576,
    )
    input_tokens = max(0, int(response.get("input_tokens") or 0))
    cached_tokens = min(input_tokens, max(0, int(response.get("cached_input_tokens") or 0)))
    output_tokens = max(0, int(response.get("output_tokens") or 0))
    return {
        "provider_key": provider_key,
        "model": str(response.get("model") or provider_key),
        "returncode": int(response.get("returncode") or 0),
        "failure_class": response.get("failure_class"),
        "stdout": str(response.get("stdout") or ""),
        "stderr": str(response.get("stderr") or ""),
        "duration_ms": max(0, int(response.get("duration_ms") or 0)),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "session_id": str(response.get("session_id") or "") or session_id,
    }


def _bridge_configuration() -> tuple[str, str]:
    token = os.environ.get("PLOW_WHIP_BRIDGE_TOKEN")
    if not token:
        raise RuntimeError("Host Bridge token is not configured")
    return (
        os.environ.get(
            "PLOW_WHIP_BRIDGE_URL", "http://host.docker.internal:8765"
        ),
        token,
    )


def _probe_result(
    provider_key: str,
    mode: str,
    checked_at: float,
    *,
    configured: bool,
    available: bool,
    detail: str,
    model_invoked: bool = False,
    returncode: int | None = None,
    marker_found: bool = False,
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    output_tokens: int = 0,
    model: str | None = None,
) -> dict[str, object]:
    return {
        "provider_key": provider_key,
        "display_name": PROVIDERS[provider_key]["display_name"],
        "mode": mode,
        "configured": configured,
        "available": available,
        "detail": detail[:500],
        "model_invoked": model_invoked,
        "returncode": returncode,
        "marker_found": marker_found,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "token_cap": PROBE_TOKEN_CAP if mode == "minimal" else 0,
        "model": model,
        "checked_at": checked_at,
    }


def _bridge_post(
    base_url: str,
    token: str,
    path: str,
    payload: dict[str, object],
    timeout: int,
    *,
    max_bytes: int = 65_536,
) -> dict[str, object]:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "host.docker.internal"}
        or parsed.username
        or parsed.password
    ):
        raise RuntimeError("Host Bridge URL must be local HTTP")
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
    except HTTPError as error:
        raise RuntimeError(f"Host Bridge rejected request: HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"Host Bridge is unreachable: {type(error).__name__}") from error
    if len(body) > max_bytes:
        raise RuntimeError("Host Bridge response is too large")
    try:
        value = json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeError("Host Bridge returned invalid JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError("Host Bridge returned an invalid response")
    return value


def record_model_call(
    connection: sqlite3.Connection,
    task_id: str,
    task_session_id: str,
    session_generation: int,
    provider_key: str,
    usage_kind: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    model: str | None = None,
) -> int:
    if usage_kind not in {"single", "cumulative"}:
        raise ValueError("usage_kind must be single or cumulative")
    if min(input_tokens, cached_input_tokens, output_tokens) < 0:
        raise ValueError("token usage cannot be negative")
    if cached_input_tokens > input_tokens:
        raise ValueError("cached_input_tokens is a subset of input_tokens")

    normalized = input_tokens + output_tokens
    if usage_kind == "cumulative":
        previous = connection.execute(
            """
            SELECT input_tokens, cached_input_tokens, output_tokens FROM model_calls
            WHERE task_session_id = ? AND session_generation = ?
              AND provider_key = ? AND usage_kind = 'cumulative'
            ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            (task_session_id, session_generation, provider_key),
        ).fetchone()
        if previous:
            if (
                input_tokens < previous["input_tokens"]
                or cached_input_tokens < previous["cached_input_tokens"]
                or output_tokens < previous["output_tokens"]
                or input_tokens - cached_input_tokens
                < previous["input_tokens"] - previous["cached_input_tokens"]
            ):
                raise ValueError("cumulative usage cannot decrease within one generation")
            normalized = (
                input_tokens
                - previous["input_tokens"]
                + output_tokens
                - previous["output_tokens"]
            )
    connection.execute(
        """
        INSERT INTO model_calls(
            id, task_id, task_session_id, session_generation, provider_key,
            model, usage_kind, input_tokens, cached_input_tokens, output_tokens,
            normalized_total, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid4().hex,
            task_id,
            task_session_id,
            session_generation,
            provider_key,
            model or ("deterministic" if provider_key == "local" else provider_key),
            usage_kind,
            input_tokens,
            cached_input_tokens,
            output_tokens,
            normalized,
            time.time(),
        ),
    )
    return normalized
