from __future__ import annotations

from typing import Any

from plow_whip_web.runtime.rule_library import (
    KARPATHY_LICENSE,
    KARPATHY_SOURCE,
    content_hash,
    seed_rules,
)

# Applicability matrix is code-enforced. Principle *content* comes from
# rule_versions (DB) or the same seed definitions used to populate DB.
KARPATHY_PACK_ID = "karpathy-guidelines"
KARPATHY_REVISION = 1
KARPATHY_MANDATORY_RESERVE_BYTES = 1400
KARPATHY_CONFIG_SOURCE = "rule_versions:development"
KARPATHY_SOURCE_URL = KARPATHY_SOURCE

# Roles that implement / review / verify code receive the baseline.
DEVELOPMENT_ROLE_KINDS: frozenset[str] = frozenset({
    "backend",
    "frontend",
    "ui",
    "fullstack",
    "devops_sre",
    "verification",
})

# Control / coordination paths must never receive the four principles, and
# Workers must not inherit project-butler clarification duties.
EXCLUDED_BEHAVIOR_ROLE_KINDS: frozenset[str] = frozenset({
    "butler",
    "global_butler",
    "project_butler",
    "coordination",
    "scheduler",
    "router",
    "reducer",
})

_PRINCIPLE_RULE_IDS = (
    "dev.think_before_coding",
    "dev.simplicity_first",
    "dev.surgical_changes",
    "dev.goal_driven_execution",
)

_PRINCIPLE_MARKERS = (
    "### Think Before Coding",
    "### Simplicity First",
    "### Surgical Changes",
    "### Goal-Driven Execution",
)


def base_role_kind(role_kind: str | None) -> str | None:
    if not role_kind:
        return None
    return str(role_kind).split(":", 1)[0]


def role_receives_dev_behavior_baseline(role_kind: str | None) -> bool:
    """True only for development / code-capability Workers."""
    base = base_role_kind(role_kind)
    if base is None:
        return False
    if base in EXCLUDED_BEHAVIOR_ROLE_KINDS:
        return False
    if base in DEVELOPMENT_ROLE_KINDS:
        return True
    # Dynamic capability Workers: kind "capability:<impl|review|verify>:..."
    if base == "capability":
        rest = str(role_kind).split(":", 2)
        if len(rest) >= 2 and rest[1] in {
            "implementation", "implement", "review", "verification", "verify",
        }:
            return True
    return False


def _principle_rules_from_seed() -> list[dict[str, Any]]:
    by_id = {item["rule_id"]: item for item in seed_rules()}
    return [by_id[rule_id] for rule_id in _PRINCIPLE_RULE_IDS if rule_id in by_id]


def _normalize_rules(rules: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not rules:
        return _principle_rules_from_seed()
    by_id: dict[str, dict[str, Any]] = {}
    for item in rules:
        rule_id = str(item.get("id") or item.get("rule_id") or "")
        if rule_id in _PRINCIPLE_RULE_IDS and rule_id not in by_id:
            by_id[rule_id] = item
    ordered = [by_id[rule_id] for rule_id in _PRINCIPLE_RULE_IDS if rule_id in by_id]
    return ordered or _principle_rules_from_seed()


def assemble_principle_content(rules: list[dict[str, Any]] | None = None) -> str:
    """Build four-principle body from rule records (DB or seed)."""
    parts = [
        "## Development behavior baseline (four principles)\n"
        f"Source: {KARPATHY_SOURCE}\n"
        f"License: {KARPATHY_LICENSE} (restated; not an external plugin download)\n"
        "model_invoked: false\n"
    ]
    for rule in _normalize_rules(rules):
        content = str(rule.get("content") or "").strip()
        if content:
            parts.append(content)
    return "\n\n".join(parts)


def karpathy_behavior_pack(
    *,
    rules: list[dict[str, Any]] | None = None,
    config_source: str | None = None,
) -> dict[str, Any]:
    """Preview metadata + content assembled from rule records (not a second truth)."""
    normalized = _normalize_rules(rules)
    content = assemble_principle_content(normalized)
    source = str(normalized[0].get("source") or KARPATHY_SOURCE) if normalized else KARPATHY_SOURCE
    revision = int(normalized[0].get("revision") or KARPATHY_REVISION) if normalized else KARPATHY_REVISION
    return {
        "id": KARPATHY_PACK_ID,
        "kind": "rule_library_baseline",
        "scope": "development",
        "scope_id": KARPATHY_PACK_ID,
        "source": source,
        "license": KARPATHY_LICENSE,
        "revision": revision,
        "version": revision,
        "present": True,
        "empty": False,
        "content": content,
        "content_hash": content_hash(content),
        "model_invoked": False,
        "trim_priority": 6,
        "mandatory": True,
        "protected": True,
        "reserve_bytes": KARPATHY_MANDATORY_RESERVE_BYTES,
        "config_source": config_source or KARPATHY_CONFIG_SOURCE,
        "applicable_roles": sorted(DEVELOPMENT_ROLE_KINDS),
        "principle_markers": list(_PRINCIPLE_MARKERS),
        "rule_ids": list(_PRINCIPLE_RULE_IDS),
    }


def bundled_behavior_packs(
    *,
    rules: list[dict[str, Any]] | None = None,
    config_source: str | None = None,
) -> list[dict[str, Any]]:
    return [karpathy_behavior_pack(rules=rules, config_source=config_source)]


def behavior_baseline_for_role(
    role_kind: str | None,
    *,
    rules: list[dict[str, Any]] | None = None,
    config_source: str | None = None,
) -> dict[str, Any]:
    """Effective preview layer: inject for development roles, else not_applicable."""
    pack = karpathy_behavior_pack(rules=rules, config_source=config_source)
    applicable = role_receives_dev_behavior_baseline(role_kind)
    base = base_role_kind(role_kind)
    if not applicable:
        return {
            **pack,
            "inject": False,
            "applicable": False,
            "not_applicable": True,
            "applicability": "not_applicable",
            "role": base,
            "role_kind": role_kind,
            "mandatory": False,
            "effective_reserve_bytes": 0,
            "content": "",
            "content_preview": "",
            "reason": (
                f"role {base or 'unknown'} is a control/coordination path; "
                "development behavior baseline is not injected"
            ),
        }
    return {
        **pack,
        "inject": True,
        "applicable": True,
        "not_applicable": False,
        "applicability": "applicable",
        "role": base,
        "role_kind": role_kind,
        "mandatory": True,
        "effective_reserve_bytes": int(pack["reserve_bytes"]),
        "content_preview": _preview(str(pack["content"])),
        "reason": "development role receives mandatory four-principle baseline",
    }


def principles_intact(content: str) -> bool:
    return all(marker in content for marker in _PRINCIPLE_MARKERS)


def _preview(content: str, limit: int = 480) -> str:
    text = content.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
