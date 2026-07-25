"""Structured Planner contract errors for bounded fallback (MECH-03)."""

from __future__ import annotations


class PlannerContractError(ValueError):
    """Planner output violated a typed contract.

    ``fallback_eligible`` marks errors that another Provider generation may
    recover (malformed model output). Owner-blocking insufficient results are
    returned as a successful parse with ``plan=None``, not this error.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        fallback_eligible: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.fallback_eligible = fallback_eligible


# Stable codes observed / emitted by parse_planner_result / normalize_plan.
SANDBOX_FALSE_INSUFFICIENT = "planner.sandbox_false_insufficient"
MISSING_STRUCTURED_RESULT = "planner.missing_structured_result"
INVALID_UPGRADE_PATH = "planner.invalid_upgrade_path"
INVALID_COVERAGE = "planner.invalid_coverage"
INVALID_CLASSIFICATION = "planner.invalid_classification"
INVALID_PLAN = "planner.invalid_plan"
