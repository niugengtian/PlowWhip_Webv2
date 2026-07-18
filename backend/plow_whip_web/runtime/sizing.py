from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

BOOTSTRAP_VERSION = "sprint10-v1"

SizeClass = Literal["XS", "S", "M", "L", "XL"]
RiskLevel = Literal["low", "medium", "high"]
EstimateStatus = Literal["estimated", "needs_planning"]

GATE_LABELS: dict[str, str] = {
    "artifact": "verifiable artifact",
    "boundary": "file or component boundary",
    "verification": "verification commands",
    "dependency": "external dependencies",
    "independent_review_orchestration": "independent review orchestration",
}

@dataclass(frozen=True, slots=True)
class BootstrapTier:
    size_class: SizeClass
    soft_deadline_seconds: int
    hard_deadline_seconds: int
    max_turns: int
    max_attempts: int
    verification_timeout_seconds: int
    progress_extension_seconds: int


BOOTSTRAP_TIERS: dict[SizeClass, BootstrapTier] = {
    "XS": BootstrapTier(
        size_class="XS",
        soft_deadline_seconds=120,
        hard_deadline_seconds=300,
        max_turns=10,
        max_attempts=2,
        verification_timeout_seconds=60,
        progress_extension_seconds=60,
    ),
    "S": BootstrapTier(
        size_class="S",
        soft_deadline_seconds=240,
        hard_deadline_seconds=600,
        max_turns=20,
        max_attempts=2,
        verification_timeout_seconds=120,
        progress_extension_seconds=90,
    ),
    "M": BootstrapTier(
        size_class="M",
        soft_deadline_seconds=480,
        hard_deadline_seconds=1200,
        max_turns=40,
        max_attempts=3,
        verification_timeout_seconds=300,
        progress_extension_seconds=120,
    ),
    "L": BootstrapTier(
        size_class="L",
        soft_deadline_seconds=900,
        hard_deadline_seconds=2400,
        max_turns=80,
        max_attempts=3,
        verification_timeout_seconds=600,
        progress_extension_seconds=180,
    ),
    "XL": BootstrapTier(
        size_class="XL",
        soft_deadline_seconds=1800,
        hard_deadline_seconds=4800,
        max_turns=120,
        max_attempts=4,
        verification_timeout_seconds=900,
        progress_extension_seconds=300,
    ),
}


@dataclass(frozen=True, slots=True)
class TaskSizingInputs:
    layers_touched: int
    components_touched: int
    estimated_files_changed: int
    has_migration: bool
    has_deploy: bool
    verification_commands_count: int
    estimated_verification_seconds: int
    external_dependencies_count: int
    risk_level: RiskLevel
    independent_review_required: bool
    gate_artifact: bool
    gate_boundary: bool
    gate_verification: bool
    gate_dependency: bool


def estimate_task_sizing(inputs: TaskSizingInputs) -> dict[str, Any]:
    missing_gates = [
        gate
        for gate, ready in (
            ("artifact", inputs.gate_artifact),
            ("boundary", inputs.gate_boundary),
            ("verification", inputs.gate_verification),
            ("dependency", inputs.gate_dependency),
        )
        if not ready
    ]
    # A task-local verification Gate is authoritative. A separate reviewer is
    # optional context, never a dispatch dependency.
    if missing_gates:
        return _needs_planning(missing_gates)

    score, rationale_items = _score_inputs(inputs)
    size_class = _size_class_for_score(score)
    tier = BOOTSTRAP_TIERS[size_class]
    rationale_items.append(f"complexity_score={score}")
    rationale_items.append(f"size_class={size_class}")

    return {
        "status": "estimated",
        "missing_gates": [],
        "size_class": size_class,
        "rationale": rationale_items,
        "soft_deadline_seconds": tier.soft_deadline_seconds,
        "hard_deadline_seconds": tier.hard_deadline_seconds,
        "max_turns": tier.max_turns,
        "max_attempts": tier.max_attempts,
        "verification_timeout_seconds": tier.verification_timeout_seconds,
        "progress_extension_seconds": tier.progress_extension_seconds,
        "model_invoked": False,
        "bootstrap_version": BOOTSTRAP_VERSION,
    }


def _needs_planning(missing_gates: list[str]) -> dict[str, Any]:
    labels = [GATE_LABELS[gate] for gate in missing_gates]
    return {
        "status": "needs_planning",
        "missing_gates": missing_gates,
        "size_class": None,
        "rationale": [
            "dispatch blocked until all required gates are satisfied",
            f"missing_gates={','.join(missing_gates)}",
            f"missing={'; '.join(labels)}",
        ],
        "soft_deadline_seconds": None,
        "hard_deadline_seconds": None,
        "max_turns": None,
        "max_attempts": None,
        "verification_timeout_seconds": None,
        "progress_extension_seconds": None,
        "model_invoked": False,
        "bootstrap_version": BOOTSTRAP_VERSION,
    }


def _score_inputs(inputs: TaskSizingInputs) -> tuple[int, list[str]]:
    rationale: list[str] = []
    score = 0

    layer_points = inputs.layers_touched * 8
    score += layer_points
    rationale.append(f"layers_touched={inputs.layers_touched} (+{layer_points})")

    component_points = inputs.components_touched * 5
    score += component_points
    rationale.append(f"components_touched={inputs.components_touched} (+{component_points})")

    file_points = inputs.estimated_files_changed * 3
    score += file_points
    rationale.append(
        f"estimated_files_changed={inputs.estimated_files_changed} (+{file_points})"
    )

    if inputs.has_migration:
        score += 12
        rationale.append("has_migration=true (+12)")
    if inputs.has_deploy:
        score += 10
        rationale.append("has_deploy=true (+10)")

    verification_points = inputs.verification_commands_count * 4
    score += verification_points
    rationale.append(
        "verification_commands_count="
        f"{inputs.verification_commands_count} (+{verification_points})"
    )

    verification_time_points = inputs.estimated_verification_seconds // 30
    score += verification_time_points
    rationale.append(
        "estimated_verification_seconds="
        f"{inputs.estimated_verification_seconds} (+{verification_time_points})"
    )

    dependency_points = inputs.external_dependencies_count * 6
    score += dependency_points
    rationale.append(
        "external_dependencies_count="
        f"{inputs.external_dependencies_count} (+{dependency_points})"
    )

    risk_points = {"low": 0, "medium": 15, "high": 30}[inputs.risk_level]
    score += risk_points
    rationale.append(f"risk_level={inputs.risk_level} (+{risk_points})")

    return score, rationale


def _size_class_for_score(score: int) -> SizeClass:
    if score < 25:
        return "XS"
    if score < 60:
        return "S"
    if score < 120:
        return "M"
    if score < 200:
        return "L"
    return "XL"
