from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# Seed definitions only. Runtime authoritative data lives in SQLite.
# Attribution only — do not vendor or execute install scripts.
AGENCY_AGENTS_ZH_SOURCE = "https://github.com/jnMetaCode/agency-agents-zh"
AGENCY_AGENTS_ZH_LICENSE = "MIT"
AGENCY_AGENTS_ZH_UPSTREAM_COMMIT = "6d446b28c802f7ffbbe7885e22b43aa0561d1043"
AGENCY_AGENTS_ZH_NOTE = (
    "Structure reference (capability / workflow / deliverables). "
    "Content is restated; repository is not vendored; install.sh is never run."
)

KARPATHY_SOURCE = (
    "https://github.com/niugengtian/andrej-karpathy-skills/blob/main/"
    "skills/karpathy-guidelines/SKILL.md"
)
KARPATHY_LICENSE = "MIT"

# Local deterministic CommandSpec: argv[0] basename must be in this set.
_LOCAL_ALLOWED_EXECUTABLES = frozenset({
    "python3", "python", "pytest", "node", "pnpm", "npm", "bash", "sh", "make",
})

# Bound auto-generated templates per capability to prevent unbounded growth.
MAX_GENERATED_TEMPLATES_PER_CAPABILITY = 32


def content_hash(value: str | dict[str, Any] | list[Any]) -> str:
    if isinstance(value, str):
        raw = value
    else:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def seed_rules() -> list[dict[str, Any]]:
    """Idempotent seed input for empty rule_versions. Not a runtime source."""
    defs = [
        (
            "dev.think_before_coding",
            "development",
            KARPATHY_SOURCE,
            KARPATHY_LICENSE,
            "### Think Before Coding\n"
            "Do not silently assume intent. State assumptions, name ambiguity, and "
            "surface tradeoffs before changing code.",
            ["backend", "frontend", "ui", "fullstack", "devops_sre", "verification"],
            True,
            "context",
        ),
        (
            "dev.simplicity_first",
            "development",
            KARPATHY_SOURCE,
            KARPATHY_LICENSE,
            "### Simplicity First\n"
            "Ship the smallest change that meets the Goal; avoid speculative "
            "abstractions and unrequested configurability.",
            ["backend", "frontend", "ui", "fullstack", "devops_sre", "verification"],
            True,
            "context",
        ),
        (
            "dev.surgical_changes",
            "development",
            KARPATHY_SOURCE,
            KARPATHY_LICENSE,
            "### Surgical Changes\n"
            "Edit only what the Goal requires; respect existing style; every "
            "changed line must trace to the request.",
            ["backend", "frontend", "ui", "fullstack", "devops_sre", "verification"],
            True,
            "context",
        ),
        (
            "dev.goal_driven_execution",
            "development",
            KARPATHY_SOURCE,
            KARPATHY_LICENSE,
            "### Goal-Driven Execution\n"
            "Turn work into verifiable checks and loop until evidence passes; "
            "claims are not completion.",
            ["backend", "frontend", "ui", "fullstack", "devops_sre", "verification"],
            True,
            "context",
        ),
        (
            "project_butler.one_question_95",
            "project_butler",
            "runtime.rule_library.seed",
            "MIT",
            "Large natural-language goals: ask one highest-value question per "
            "turn, score unresolved semantic gaps, reach >=95% confidence, "
            "propose objective/boundaries/acceptance, wait for owner confirm.",
            ["project_butler", "butler"],
            True,
            "code",
        ),
        (
            "global_butler.readonly_route",
            "global_butler",
            "runtime.rule_library.seed",
            "MIT",
            "Global butler: read-only cross-project index/query/route only. "
            "Never share project sessions, inject cross-project context, or "
            "run 95% clarification.",
            ["global_butler"],
            True,
            "code",
        ),
        (
            "dispatch.require_role_instance",
            "dispatch",
            "runtime.rule_library.seed",
            "MIT",
            "Model-invoking Workers require a valid RoleInstance and "
            "SessionBinding before dispatch.",
            ["*"],
            True,
            "code",
        ),
    ]
    rules = []
    for rule_id, scope, source, license_name, content, applies, mandatory, enforcement in defs:
        rules.append({
            "rule_id": rule_id,
            "revision": 1,
            "scope": scope,
            "source": source,
            "license": license_name,
            "content": content,
            "content_hash": content_hash(content),
            "applies_to": applies,
            "mandatory": mandatory,
            "enforcement": enforcement,
        })
    return rules


# Pinned upstream blob SHA-256 for attribution (fetched once at seed authoring).
_AGENCY_SOURCE_HASHES = {
    "engineering/engineering-frontend-developer.md": (
        "d6a428dd719c950ee0137fa4053ef0fb2cc9403aa2fdf5613832bbd4d5344055"
    ),
    "engineering/engineering-backend-architect.md": (
        "8fe4312fa28978e6939d77cb09170b6fbb3b353306a6f55595f6ca27d56e6793"
    ),
    "testing/testing-evidence-collector.md": (
        "10471bdc6334e5cd929676938153032781c5df3e30f244551fb8aa605a973151"
    ),
    "engineering/engineering-devops-automator.md": (
        "ea09f7bf48628a20b1132cc45d1510933c98be441c35f7fa77fecabd9576147d"
    ),
    "engineering/engineering-security-engineer.md": (
        "2d71dada5f2d938da90fbff1d6933b0c0c7775ef2c67c13529af0e80bdd6b52f"
    ),
}


def _agency_ref(file_name: str) -> dict[str, str]:
    return {
        "repository": AGENCY_AGENTS_ZH_SOURCE,
        "license": AGENCY_AGENTS_ZH_LICENSE,
        "note": AGENCY_AGENTS_ZH_NOTE,
        "file": file_name,
        "upstream_commit": AGENCY_AGENTS_ZH_UPSTREAM_COMMIT,
        "source_content_sha256": _AGENCY_SOURCE_HASHES.get(file_name, ""),
    }


_DEV_RULES = [
    "dev.think_before_coding",
    "dev.simplicity_first",
    "dev.surgical_changes",
    "dev.goal_driven_execution",
]


def seed_templates() -> list[dict[str, Any]]:
    """Idempotent seed input for empty role_template_versions. Not runtime source."""
    retention = {
        "mandatory_rule_reserve_bytes": 1400,
        "trim_observations_first": True,
        "exclude_full_transcripts": True,
    }
    specs = [
        ("tmpl.frontend", "frontend", "frontend",
         "engineering/engineering-frontend-developer.md",
         ["host-bridge", "browser-verify"], ["cursor", "codex"],
         ["UI/page/contracts only", "no unrelated backend rewrites"],
         ["inspect contracts", "minimal UI change", "verify"],
         ["page/interaction diff", "verification evidence"], ["exit_code", "artifact"]),
        ("tmpl.backend", "backend", "backend",
         "engineering/engineering-backend-architect.md",
         ["host-bridge", "pytest"], ["cursor", "codex"],
         ["API/data/service boundaries", "no drive-by refactors"],
         ["inspect API surface", "minimal change", "test"],
         ["API/service diff", "test evidence"], ["exit_code", "artifact"]),
        ("tmpl.verification", "verification", "verification",
         "testing/testing-evidence-collector.md",
         ["host-bridge", "pytest"], ["cursor", "codex", "generic-command"],
         ["reproduce and verify", "EvidenceManifest is canonical"],
         ["map acceptance", "reproduce", "report evidence"],
         ["verification report", "EvidenceManifest"], ["exit_code"]),
        ("tmpl.devops_sre", "devops_sre", "devops_sre",
         "engineering/engineering-devops-automator.md",
         ["host-bridge"], ["cursor", "codex"],
         ["rollback-first", "least privilege"],
         ["diagnose", "bounded change", "observe"],
         ["ops change", "health evidence"], ["exit_code"]),
        ("tmpl.review_security", "review", "review",
         "engineering/engineering-security-engineer.md",
         ["host-bridge"], ["cursor", "codex"],
         ["read-focused review", "no silent production changes"],
         ["threat pass", "diff review", "report"],
         ["review findings", "risk notes"], ["exit_code"]),
        ("tmpl.project_butler", "project_butler", "project_butler", None,
         ["control-plane"], [],
         ["project-isolated", "no worker clarification duties"],
         ["clarify", "confirm", "plan DAG", "wake workers"],
         ["GoalSpec", "WorkItem DAG", "RoleInstances"], ["owner_confirm"]),
        ("tmpl.global_butler", "global_butler", "global_butler", None,
         ["control-plane"], [],
         ["read-only index/route", "no project session share"],
         ["index", "query", "route"],
         ["resource index", "route pointer"], ["deterministic"]),
    ]
    templates = []
    for (
        template_id, capability, capability_key, agency_file,
        tools, providers, boundaries, workflow, deliverables, verification,
    ) in specs:
        rule_ids = (
            list(_DEV_RULES)
            if capability_key in {
                "frontend", "backend", "verification", "devops_sre", "review",
            }
            else (
                ["project_butler.one_question_95"]
                if capability_key == "project_butler"
                else ["global_butler.readonly_route"]
            )
        )
        if capability_key == "project_butler":
            ctx = {"mandatory_rule_reserve_bytes": 512}
        elif capability_key == "global_butler":
            ctx = {"mandatory_rule_reserve_bytes": 256}
        else:
            ctx = retention
        body = {
            "template_id": template_id,
            "revision": 1,
            "capability": capability,
            "capability_key": capability_key,
            "rule_ids": rule_ids,
            "tools": tools,
            "provider_requirements": providers,
            "boundaries": boundaries,
            "workflow": workflow,
            "deliverables": deliverables,
            "verification": verification,
            "context_retention": ctx,
            "source_refs": [_agency_ref(agency_file)] if agency_file else [],
        }
        body["template_hash"] = content_hash({
            k: body[k] for k in (
                "capability_key", "rule_ids", "tools", "provider_requirements",
                "boundaries", "workflow", "deliverables", "verification",
                "context_retention",
            )
        })
        templates.append(body)
    return templates


def capability_key_for_role(role_kind: str) -> str:
    mapping = {
        "ui": "frontend",
        "fullstack": "backend",
        "simple-worker": "backend",
        "butler": "project_butler",
        "coordination": "project_butler",
    }
    return mapping.get(role_kind, role_kind)


def provider_invokes_model(
    *,
    provider: str,
    provider_config: dict[str, Any] | None = None,
) -> bool:
    """Fail-closed: unknown / model providers require RoleInstance."""
    if provider_config is not None and "model_invoked" in provider_config:
        return bool(provider_config["model_invoked"])
    if provider == "generic-command":
        return False
    return True


def is_local_deterministic_worker(
    *,
    provider: str,
    command: dict[str, Any] | None,
    model_invoked: bool,
) -> bool:
    """Narrow exception: model_invoked=false + restricted local CommandSpec only."""
    if model_invoked:
        return False
    if provider != "generic-command":
        return False
    argv = (command or {}).get("argv")
    if not isinstance(argv, list) or not argv:
        return False
    executable = Path(str(argv[0])).name
    return executable in _LOCAL_ALLOWED_EXECUTABLES


# Back-compat aliases used by older tests during transition; not runtime sources.
def bundled_rules() -> list[dict[str, Any]]:
    return [
        {
            "id": item["rule_id"],
            "revision": item["revision"],
            "version": item["revision"],
            **{k: item[k] for k in item if k not in {"rule_id"}},
        }
        for item in seed_rules()
    ]


def bundled_role_templates() -> list[dict[str, Any]]:
    return [
        {
            "id": item["template_id"],
            "revision": item["revision"],
            "version": item["revision"],
            **{k: item[k] for k in item if k not in {"template_id"}},
        }
        for item in seed_templates()
    ]


__all__ = [
    "AGENCY_AGENTS_ZH_LICENSE",
    "AGENCY_AGENTS_ZH_SOURCE",
    "AGENCY_AGENTS_ZH_UPSTREAM_COMMIT",
    "KARPATHY_LICENSE",
    "KARPATHY_SOURCE",
    "MAX_GENERATED_TEMPLATES_PER_CAPABILITY",
    "bundled_role_templates",
    "bundled_rules",
    "capability_key_for_role",
    "content_hash",
    "is_local_deterministic_worker",
    "provider_invokes_model",
    "seed_rules",
    "seed_templates",
]
