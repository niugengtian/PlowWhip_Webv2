from __future__ import annotations

from typing import Any, Literal


Disposition = Literal["retain", "reject"]


# Keep a single matrix in code so docs and tests cannot drift into a second
# truth source. Retained items map to existing mechanisms; rejected items must
# not be reintroduced as parallel Prompt copies or platform-specific schedulers.
LEGACY_RULES_MATRIX: tuple[dict[str, Any], ...] = (
    {
        "id": "project_root_boundary",
        "disposition": "retain",
        "owner": "security + task claim path checks",
        "summary": "命令与产物不得逃出项目根。",
    },
    {
        "id": "nondestructive_project_file_delete",
        "disposition": "retain",
        "owner": "task deletion / project purge guards",
        "summary": "项目文件删除保持非破坏性与可解释约束。",
    },
    {
        "id": "api_reducer_state_migration",
        "disposition": "retain",
        "owner": "Goal/Task reducers + SQLite migrations",
        "summary": "状态迁移走 API/Reducer 与有序迁移，不手改终态。",
    },
    {
        "id": "hot_warm_cold_bounded_context",
        "disposition": "retain",
        "owner": "ContextCompiler + settings budgets",
        "summary": "Hot/Warm/Cold 有界上下文；无界历史不进 Worker Prompt。",
    },
    {
        "id": "terminal_no_redispatch",
        "disposition": "retain",
        "owner": "TaskRepository finish / terminal guards",
        "summary": "终态任务不得重派。",
    },
    {
        "id": "task_physical_session",
        "disposition": "retain",
        "owner": "task_sessions + provider session binding",
        "summary": "物理 Session 绑定 project+role+task。",
    },
    {
        "id": "lease_fencing_evidence_completion",
        "disposition": "retain",
        "owner": "leases, resource_locks, EvidenceManifest",
        "summary": "租约/fencing 与证据完成原则。",
    },
    {
        "id": "fixed_agent_roster",
        "disposition": "reject",
        "owner": "capability Workers via Butler DAG",
        "summary": "拒绝照搬旧固定 Agent 阵容。",
    },
    {
        "id": "mandatory_independent_reviewer",
        "disposition": "reject",
        "owner": "task-local verification Gate",
        "summary": "拒绝所有任务强制独立 Reviewer。",
    },
    {
        "id": "markdown_agent_state_dual_truth",
        "disposition": "reject",
        "owner": "SQLite Goal/Task aggregates",
        "summary": "拒绝 Markdown/AGENT_STATE 双真源。",
    },
    {
        "id": "platform_specific_scheduler",
        "disposition": "reject",
        "owner": "in-process SchedulerService",
        "summary": "拒绝平台专用 scheduler。",
    },
    {
        "id": "legacy_file_inbox",
        "disposition": "reject",
        "owner": "Butler conversation + outbox APIs",
        "summary": "拒绝旧 file inbox。",
    },
)


def legacy_rules_matrix() -> list[dict[str, Any]]:
    return [dict(item) for item in LEGACY_RULES_MATRIX]


def retained_rule_ids() -> set[str]:
    return {item["id"] for item in LEGACY_RULES_MATRIX if item["disposition"] == "retain"}


def rejected_rule_ids() -> set[str]:
    return {item["id"] for item in LEGACY_RULES_MATRIX if item["disposition"] == "reject"}
