from __future__ import annotations

import sqlite3
import time
from uuid import uuid4

from .store import DEFAULT_SETTINGS


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
            "available": provider == "local",
            "reason": (
                "local deterministic adapter"
                if provider == "local"
                else "disabled_by_v1_scope"
            ),
        }
        for provider in PROVIDER_ORDERS.get(role_key, ())
    ]


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
