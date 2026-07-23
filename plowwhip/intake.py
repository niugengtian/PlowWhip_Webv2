from __future__ import annotations

import json
import re
import time
from pathlib import PurePosixPath
from uuid import uuid4

from .store import Store


PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
LIBRARY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TASK_ID = re.compile(r"^[0-9a-f]{32}$")
GIT_BRANCH = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,126}[A-Za-z0-9])?$"
)
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
WRITE_INSTRUCTION = re.compile(
    r"^(?:write|写入)\s+([^:\s]+)\s*:\s*([\s\S]*)$", re.IGNORECASE
)
PROVIDER_PROBE_INSTRUCTION = re.compile(
    r"^(?:probe\s+provider|探测\s*Provider)\s+"
    r"(codex_cli|cursor_cli|deepseek|kimi)\s*:\s*"
    r"(0token|minimal)(?:\s+确认\s+([a-z0-9_]+))?$",
    re.IGNORECASE,
)
READ_ONLY_INSTRUCTION = re.compile(
    r"^\s*(?:分析|审查|检查|查询|只读|audit|review|inspect|analy[sz]e)",
    re.IGNORECASE,
)
GITHUB_TREE_URL = re.compile(
    r"https://github\.com/"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/tree/"
    r"([A-Za-z0-9][A-Za-z0-9._/-]{0,126})",
    re.IGNORECASE,
)
PROJECT_SETTING_LIMITS = {
    "max_runtime_seconds": (1, 86_400),
    "stop_grace_seconds": (0, 300),
    "handoff_max_bytes": (512, 1_048_576),
    "checkpoint_max_bytes": (512, 1_048_576),
    "context_max_bytes": (1_024, 1_048_576),
    "session_segment_max_bytes": (1_024, 1_048_576),
    "native_compact_input_tokens": (1_000, 100_000_000),
    "rotation_input_tokens": (1_000, 100_000_000),
    "monitor_tail_lines": (1, 1_000),
    "monitor_tail_bytes": (256, 1_048_576),
    "retry_count": (0, 10),
    "retry_backoff_seconds": (0, 86_400),
}
PROJECT_PROVIDER_ROLES = {
    "planner",
    "fullstack",
    "independent_checker",
    "simple",
    "provider_probe",
    "deterministic",
    "deterministic_checker",
}
PROJECT_PROVIDERS = {"local", "codex_cli", "cursor_cli", "deepseek", "kimi"}


def submit_message(
    store: Store, project_id: str, content: str, idempotency_key: str
) -> str:
    if not PROJECT_ID.fullmatch(project_id):
        raise ValueError("project_id must be 1-64 safe identifier characters")
    if not content or len(content.encode()) > 65_536:
        raise ValueError("message must contain 1-65536 UTF-8 bytes")
    if not idempotency_key or len(idempotency_key) > 128:
        raise ValueError("idempotency_key must contain 1-128 characters")

    now = time.time()
    message_id = uuid4().hex
    with store.transaction() as connection:
        duplicate = connection.execute(
            "SELECT id FROM messages WHERE project_id = ? AND idempotency_key = ?",
            (project_id, idempotency_key),
        ).fetchone()
        if duplicate:
            return str(duplicate["id"])
        project = connection.execute(
            "SELECT archived_at FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not project:
            connection.execute(
                """
                INSERT INTO projects(id, archived_at, created_at)
                VALUES (?, ?, ?)
                """,
                (project_id, now, now),
            )
        elif project["archived_at"] is not None:
            pending_intake = connection.execute(
                """
                SELECT 1 FROM messages
                WHERE project_id = ? AND processed_at IS NULL
                  AND action_json IS NULL LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            if not pending_intake:
                raise ValueError("project is archived; restore it before sending messages")
        connection.execute(
            """
            INSERT OR IGNORE INTO messages(
                id, project_id, role, content, idempotency_key, created_at
            ) VALUES (?, ?, 'owner', ?, ?, ?)
            """,
            (message_id, project_id, content, idempotency_key, now),
        )
        row = connection.execute(
            "SELECT id FROM messages WHERE project_id = ? AND idempotency_key = ?",
            (project_id, idempotency_key),
        ).fetchone()
    return str(row["id"])


def create_project(
    store: Store,
    project_id: str,
    idempotency_key: str,
    host_path: str | None = None,
) -> str:
    _validate_project_action(project_id, idempotency_key)
    workspace = _normalize_host_path(host_path)
    now = time.time()
    action_id = uuid4().hex
    with store.transaction() as connection:
        duplicate = connection.execute(
            "SELECT id FROM messages WHERE project_id = ? AND idempotency_key = ?",
            (project_id, idempotency_key),
        ).fetchone()
        if duplicate:
            return str(duplicate["id"])
        existing = connection.execute(
            "SELECT id, host_path, archived_at FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        pending = (
            connection.execute(
                """
                SELECT id FROM messages
                WHERE project_id = ? AND processed_at IS NULL
                  AND json_extract(action_json, '$.kind') IN (
                      'create_project', 'restore_project',
                      'bind_project_workspace', 'archive_project'
                  )
                ORDER BY created_at LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            if existing
            else None
        )
        if pending:
            return str(pending["id"])
        if existing and existing["archived_at"] is None:
            kind = (
                "bind_project_workspace"
                if workspace and workspace != existing["host_path"]
                else "create_project"
            )
            if kind == "bind_project_workspace" and connection.execute(
                "SELECT 1 FROM tasks WHERE project_id = ? AND outcome IS NULL LIMIT 1",
                (project_id,),
            ).fetchone():
                raise ValueError("active project workspace cannot be changed")
        elif existing:
            kind = "restore_project"
        else:
            connection.execute(
                """
                INSERT INTO projects(id, host_path, archived_at, created_at)
                VALUES (?, NULL, ?, ?)
                """,
                (project_id, now, now),
            )
            kind = "create_project"
        connection.execute(
            """
            INSERT INTO messages(
                id, project_id, role, content, action_json,
                idempotency_key, created_at
            ) VALUES (?, ?, 'owner', ?, ?, ?, ?)
            """,
            (
                action_id,
                project_id,
                kind,
                canonical_json({"kind": kind, "host_path": workspace}),
                idempotency_key,
                now,
            ),
        )
    return action_id


def archive_project(
    store: Store, project_id: str, confirmation: str, idempotency_key: str
) -> str:
    _validate_project_action(project_id, idempotency_key)
    if confirmation != project_id:
        raise ValueError("confirmation must exactly match project_id")
    now = time.time()
    action_id = uuid4().hex
    with store.transaction() as connection:
        duplicate = connection.execute(
            "SELECT id FROM messages WHERE project_id = ? AND idempotency_key = ?",
            (project_id, idempotency_key),
        ).fetchone()
        if duplicate:
            return str(duplicate["id"])
        project = connection.execute(
            "SELECT archived_at FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not project:
            raise ValueError("project not found")
        if project["archived_at"] is not None:
            pending_create = connection.execute(
                """
                SELECT 1 FROM messages
                WHERE project_id = ? AND processed_at IS NULL
                  AND json_extract(action_json, '$.kind') IN (
                      'create_project', 'restore_project'
                  ) LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            if not pending_create:
                return project_id
        if connection.execute(
            "SELECT 1 FROM tasks WHERE project_id = ? AND outcome IS NULL LIMIT 1",
            (project_id,),
        ).fetchone():
            raise ValueError("project with an active task cannot be archived")
        connection.execute(
            """
            INSERT INTO messages(
                id, project_id, role, content, action_json,
                idempotency_key, created_at
            ) VALUES (?, ?, 'owner', 'archive_project', ?, ?, ?)
            """,
            (
                action_id,
                project_id,
                canonical_json(
                    {
                        "kind": "archive_project",
                        "confirmation": confirmation,
                    }
                ),
                idempotency_key,
                now,
            ),
        )
    return action_id


def set_project_setting(
    store: Store,
    project_id: str,
    setting_key: str,
    value: object,
    idempotency_key: str,
) -> str:
    _validate_project_action(project_id, idempotency_key)
    limits = PROJECT_SETTING_LIMITS.get(setting_key)
    if setting_key == "provider_order":
        _validate_provider_order(value)
    elif (
        not limits
        or isinstance(value, bool)
        or not isinstance(value, int)
        or not limits[0] <= value <= limits[1]
    ):
        raise ValueError("setting is not an allowed bounded project value")
    now = time.time()
    message_id = uuid4().hex
    with store.transaction() as connection:
        project = connection.execute(
            "SELECT archived_at FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not project or project["archived_at"] is not None:
            raise ValueError("active project not found")
        connection.execute(
            """
            INSERT OR IGNORE INTO messages(
                id, project_id, role, content, action_json,
                idempotency_key, created_at
            ) VALUES (?, ?, 'owner', ?, ?, ?, ?)
            """,
            (
                message_id,
                project_id,
                f"set {setting_key}={value}",
                canonical_json(
                    {
                        "kind": "set_project_setting",
                        "setting_key": setting_key,
                        "value": value,
                    }
                ),
                idempotency_key,
                now,
            ),
        )
        row = connection.execute(
            "SELECT id FROM messages WHERE project_id = ? AND idempotency_key = ?",
            (project_id, idempotency_key),
        ).fetchone()
    return str(row["id"])


def set_project_rule(
    store: Store,
    project_id: str,
    rule_key: str,
    content: str,
    idempotency_key: str,
) -> str:
    _validate_project_action(project_id, idempotency_key)
    if not LIBRARY_KEY.fullmatch(rule_key):
        raise ValueError("rule_key must be a safe 1-64 character identifier")
    if not content.strip() or len(content.encode()) > 65_536:
        raise ValueError("project rule must contain 1-65536 UTF-8 bytes")
    now = time.time()
    message_id = uuid4().hex
    with store.transaction() as connection:
        project = connection.execute(
            "SELECT archived_at FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not project or project["archived_at"] is not None:
            raise ValueError("active project not found")
        connection.execute(
            """
            INSERT OR IGNORE INTO messages(
                id, project_id, role, content, action_json,
                idempotency_key, created_at
            ) VALUES (?, ?, 'owner', ?, ?, ?, ?)
            """,
            (
                message_id,
                project_id,
                f"set project rule {rule_key}",
                canonical_json(
                    {
                        "kind": "set_project_rule",
                        "rule_key": rule_key,
                        "content": content,
                    }
                ),
                idempotency_key,
                now,
            ),
        )
        row = connection.execute(
            "SELECT id FROM messages WHERE project_id = ? AND idempotency_key = ?",
            (project_id, idempotency_key),
        ).fetchone()
    return str(row["id"])


def _validate_provider_order(value: object) -> None:
    if not isinstance(value, dict) or not value:
        raise ValueError("provider_order must contain at least one role")
    for role, providers in value.items():
        if (
            role not in PROJECT_PROVIDER_ROLES
            or not isinstance(providers, list)
            or not providers
            or len(providers) != len(set(providers))
            or any(provider not in PROJECT_PROVIDERS for provider in providers)
        ):
            raise ValueError("provider_order contains an invalid role or Provider list")
        if role in {"deterministic", "deterministic_checker"} and providers != ["local"]:
            raise ValueError("deterministic roles require provider_order ['local']")
        if role not in {"deterministic", "deterministic_checker"} and "local" in providers:
            raise ValueError("model roles cannot use the local deterministic Provider")


def _validate_project_action(project_id: str, idempotency_key: str) -> None:
    if not PROJECT_ID.fullmatch(project_id):
        raise ValueError("project_id must be 1-64 safe identifier characters")
    if not idempotency_key or len(idempotency_key) > 128:
        raise ValueError("idempotency_key must contain 1-128 characters")


def _normalize_host_path(value: str | None) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("host_path must be a string")
    if value is None or not value.strip():
        return None
    candidate = value.strip()
    if "\0" in candidate or len(candidate.encode()) > 4096:
        raise ValueError("host_path must contain at most 4096 safe UTF-8 bytes")
    path = PurePosixPath(candidate)
    if not path.is_absolute() or any(part in ("", ".", "..") for part in path.parts[1:]):
        raise ValueError("host_path must be an absolute path without traversal")
    return path.as_posix()


def submit_action(
    store: Store,
    project_id: str,
    task_id: str,
    kind: str,
    instruction: str,
    idempotency_key: str,
    plan: dict | None = None,
) -> str:
    if not PROJECT_ID.fullmatch(project_id) or not TASK_ID.fullmatch(task_id):
        raise ValueError("invalid project_id or task_id")
    if kind not in {
        "provide_decision",
        "provide_plan",
        "authorize",
        "cancel",
        "confirm_not_executed",
        "publish_new_branch",
        "force_publish_with_lease",
        "rerun",
        "wake",
    }:
        raise ValueError(
            "supported actions: provide_decision, provide_plan, authorize, cancel, "
            "confirm_not_executed, publish_new_branch, "
            "force_publish_with_lease, rerun, wake"
        )
    if kind == "provide_decision" and not instruction:
        raise ValueError("provide_decision requires instruction")
    if len(instruction.encode()) > 65_536:
        raise ValueError("instruction must contain at most 65536 UTF-8 bytes")
    if kind == "provide_plan" and not isinstance(plan, dict):
        raise ValueError("provide_plan requires plan")
    if not idempotency_key or len(idempotency_key) > 128:
        raise ValueError("idempotency_key must contain 1-128 characters")

    now = time.time()
    message_id = uuid4().hex
    action = {"kind": kind, "task_id": task_id, "instruction": instruction}
    if plan is not None:
        action["plan"] = plan
    with store.transaction() as connection:
        existing = connection.execute(
            "SELECT id FROM messages WHERE project_id = ? AND idempotency_key = ?",
            (project_id, idempotency_key),
        ).fetchone()
        if existing:
            return str(existing["id"])
        task = connection.execute(
            """
            SELECT task.public_status, task.phase, task.outcome, task.spec_revision,
                   task.fault_code, task.spec_json,
                   project.host_path
            FROM tasks task JOIN projects project ON project.id = task.project_id
            WHERE task.id = ? AND task.project_id = ?
            """,
            (task_id, project_id),
        ).fetchone()
        if not task:
            raise ValueError("task not found")
        if kind == "confirm_not_executed":
            job = connection.execute(
                """
                SELECT id FROM host_jobs
                WHERE task_id = ? AND status = 'dispatching'
                ORDER BY sequence DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if not job or instruction != job["id"]:
                raise ValueError(
                    "confirm_not_executed requires the exact active HostJob ID"
                )
            action = {
                "kind": kind,
                "task_id": task_id,
                "host_job_id": job["id"],
            }
        if kind == "authorize":
            if instruction != task_id:
                raise ValueError("authorize requires exact Task ID confirmation")
            proposal = connection.execute(
                """
                SELECT id, revision FROM plans
                WHERE goal_id = (
                    SELECT goal_id FROM tasks WHERE id = ?
                ) AND selected = 0 AND revision > 1
                ORDER BY revision DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if not proposal:
                raise ValueError("no Planner proposal is awaiting authorization")
            action = {
                "kind": "authorize",
                "task_id": task_id,
                "spec_revision": task["spec_revision"],
                "action_kind": "select_plan",
                "target_scope": task["host_path"] or f"project:{project_id}",
                "expires_at": now + 900,
                "plan_id": proposal["id"],
                "plan_revision": proposal["revision"],
            }
        if kind in {"publish_new_branch", "force_publish_with_lease"}:
            spec = json.loads(task["spec_json"])
            active_job = connection.execute(
                """
                SELECT 1 FROM host_jobs
                WHERE task_id = ? AND status IN (
                    'dispatching', 'running', 'cancelling'
                ) LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if (
                spec.get("kind") != "git_publish"
                or task["public_status"] != "needs_decision"
                or task["outcome"] is not None
                or active_job
            ):
                raise ValueError("Git publish recovery is not allowed for current task")
            branch = str(spec.get("branch") or "")
            publish_mode = "fast_forward"
            expected_remote_head = None
            action_kind = "git_publish"
            if kind == "publish_new_branch":
                branch = instruction.strip()
                if (
                    not GIT_BRANCH.fullmatch(branch)
                    or ".." in branch
                    or branch.endswith((".lock", ".", "/"))
                    or branch.startswith((".", "/"))
                    or branch == spec.get("branch")
                ):
                    raise ValueError(
                        "publish_new_branch requires a different safe branch"
                    )
            else:
                expected_remote_head = instruction.strip()
                if not GIT_SHA.fullmatch(expected_remote_head):
                    raise ValueError(
                        "force_publish_with_lease requires the exact "
                        "40-character remote SHA"
                    )
                publish_mode = "force_with_lease"
                action_kind = "git_publish_force_with_lease"
            target_scope = f"{spec.get('remote_ssh')}#refs/heads/{branch}"
            action = {
                "kind": kind,
                "task_id": task_id,
                "previous_spec_revision": task["spec_revision"],
                "next_spec_revision": task["spec_revision"] + 1,
                "branch": branch,
                "publish_mode": publish_mode,
                "expected_remote_head": expected_remote_head,
                "authorization": {
                    "source_message_id": message_id,
                    "project_id": project_id,
                    "task_id": task_id,
                    "spec_revision": task["spec_revision"] + 1,
                    "action_kind": action_kind,
                    "target_scope": target_scope,
                    "expected_remote_head": expected_remote_head,
                    "expires_at": now + 900,
                },
            }
        allowed = (
            kind == "provide_decision"
            and task["public_status"] == "needs_decision"
            and task["outcome"] != "cancelled"
        ) or (
            kind == "provide_plan"
            and task["public_status"] == "needs_decision"
            and task["outcome"] is None
        ) or (
            kind == "authorize"
            and task["public_status"] == "needs_decision"
            and task["phase"] == "plan"
            and task["outcome"] is None
        ) or (kind == "cancel" and task["outcome"] is None) or (
            kind == "confirm_not_executed"
            and task["public_status"] == "needs_decision"
            and task["fault_code"] == "unsafe_unknown"
            and task["outcome"] is None
        ) or (
            kind in {"publish_new_branch", "force_publish_with_lease"}
            and task["public_status"] == "needs_decision"
            and task["outcome"] is None
        ) or (
            kind == "rerun" and task["outcome"] == "cancelled"
        ) or (
            kind == "wake"
            and task["outcome"] is None
            and task["public_status"] in {"pending", "in_progress"}
        )
        if not allowed:
            raise ValueError(f"action {kind} is not allowed for current task")
        connection.execute(
            """
            INSERT INTO messages(
                id, project_id, role, content, action_json,
                idempotency_key, created_at
            ) VALUES (?, ?, 'owner', ?, ?, ?, ?)
            """,
            (
                message_id,
                project_id,
                instruction or kind,
                canonical_json(action),
                idempotency_key,
                now,
            ),
        )
    return message_id


def normalize_instruction(content: str) -> tuple[dict[str, object], list[dict[str, str]]]:
    probe = PROVIDER_PROBE_INSTRUCTION.fullmatch(content.strip())
    if probe:
        provider_key = probe.group(1).lower()
        mode = "zero" if probe.group(2).lower() == "0token" else "minimal"
        if mode == "minimal" and (probe.group(3) or "").lower() != provider_key:
            return (
                {
                    "kind": "authorization_required",
                    "instruction": content,
                    "wait_reason": (
                        f"minimal Token probe requires exact confirmation: {provider_key}"
                    ),
                },
                [],
            )
        return (
            {
                "kind": "provider_probe",
                "provider_key": provider_key,
                "mode": mode,
            },
            [
                {
                    "id": f"provider_{mode}_probe",
                    "kind": "provider_probe_contract",
                }
            ],
        )

    github = GITHUB_TREE_URL.search(content)
    lowered = content.lower()
    if github and (
        "上传" in content
        or "推送" in content
        or "push" in lowered
    ) and "ssh" in lowered:
        owner, repository, branch = github.groups()
        repository = repository.removesuffix(".git")
        remote = f"git@github.com:{owner}/{repository}.git"
        return (
            {
                "kind": "git_publish",
                "instruction": content,
                "remote_ssh": remote,
                "branch": branch,
                "workspace_change_required": True,
            },
            [
                {
                    "id": "git_publish_contract",
                    "kind": "secret_scan_and_remote_sha",
                },
            ],
        )

    match = WRITE_INSTRUCTION.fullmatch(content.strip())
    if not match:
        workspace_change_required = not bool(
            READ_ONLY_INSTRUCTION.match(content)
        )
        return (
            {
                "kind": "provider_task",
                "instruction": content,
                "workspace_change_required": workspace_change_required,
            },
            [
                {
                    "id": "owner_instruction",
                    "kind": "checker_evidence",
                    "expected": content,
                },
                {
                    "id": "relevant_checks",
                    "kind": "checker_evidence",
                    "expected": (
                        "smallest relevant deterministic checks pass"
                        if workspace_change_required
                        else "read-only findings are independently supported by bounded evidence"
                    ),
                },
            ],
        )

    target = PurePosixPath(match.group(1))
    if (
        target.is_absolute()
        or not target.parts
        or any(part in ("", ".", "..") for part in target.parts)
    ):
        return (
            {"kind": "unsafe_path", "instruction": content},
            [],
        )

    spec = {"kind": "write_text", "target": target.as_posix(), "content": match.group(2)}
    acceptance = [{"id": "artifact_content_sha256", "kind": "sha256_matches_spec"}]
    return spec, acceptance


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
