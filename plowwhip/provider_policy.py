"""Resolve eligible Provider candidates from frozen project settings."""

from __future__ import annotations

from typing import Iterable


MODEL_PROVIDER_KEYS = ("codex_cli", "cursor_cli", "deepseek", "kimi")


def provider_disabled_for_role(values: dict, role_key: str) -> set[str]:
    raw = values.get("provider_disabled", {})
    if isinstance(raw, list):
        return {str(item) for item in raw}
    if not isinstance(raw, dict):
        return set()
    role_list = raw.get(role_key, [])
    if not isinstance(role_list, list):
        return set()
    return {str(item) for item in role_list}


def provider_order_for_role(values: dict, role_key: str) -> list[str]:
    raw = values.get("provider_order", {})
    if isinstance(raw, list):
        order = [str(item) for item in raw]
    elif isinstance(raw, dict):
        configured = raw.get(role_key, [])
        order = [str(item) for item in configured] if isinstance(configured, list) else []
    else:
        order = []
    disabled = provider_disabled_for_role(values, role_key)
    return [provider for provider in order if provider not in disabled]


def first_eligible_provider(
    values: dict,
    role_key: str,
    *,
    requested: object = None,
    allowed: Iterable[str] | None = None,
) -> str:
    order = provider_order_for_role(values, role_key)
    if allowed is not None:
        allowed_set = {str(item) for item in allowed}
        order = [provider for provider in order if provider in allowed_set]
    if requested and str(requested) in order:
        return str(requested)
    if not order:
        raise ValueError(f"Provider order is empty for role {role_key}")
    return order[0]
