from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from plow_whip_web.domain.model import DomainError, NotFoundError
from plow_whip_web.runtime.butler import project_execution_policy
from plow_whip_web.runtime.rule_library import (
    MAX_GENERATED_TEMPLATES_PER_CAPABILITY,
    capability_key_for_role,
    content_hash,
    is_local_deterministic_worker,
    seed_rules,
    seed_templates,
)
from plow_whip_web.store.database import Database


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _binding_hash(
    *,
    project_id: str,
    role_instance_id: str,
    task_id: str,
    provider: str,
    session_generation: int,
) -> str:
    return content_hash({
        "project_id": project_id,
        "role_instance_id": role_instance_id,
        "task_id": task_id,
        "provider": provider,
        "session_generation": session_generation,
    })


class RoleInstanceRepository:
    """DB-canonical RuleLibrary, templates, ProjectRoleRule, instances, bindings."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def seed_catalog_if_empty(self) -> dict[str, int]:
        """Idempotent seed into empty tables. Never overwrites existing revisions."""
        inserted_rules = 0
        inserted_templates = 0
        with self.database.transaction(immediate=True) as connection:
            rule_count = int(connection.execute(
                "SELECT COUNT(*) FROM rule_versions"
            ).fetchone()[0])
            if rule_count == 0:
                for rule in seed_rules():
                    connection.execute(
                        """
                        INSERT INTO rule_versions(
                            rule_id, revision, scope, source, license, content,
                            content_hash, applies_to_json, mandatory, enforcement, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                        """,
                        (
                            rule["rule_id"], rule["revision"], rule["scope"],
                            rule["source"], rule["license"], rule["content"],
                            rule["content_hash"], _json(rule["applies_to"]),
                            1 if rule["mandatory"] else 0, rule["enforcement"],
                        ),
                    )
                    inserted_rules += 1
            template_count = int(connection.execute(
                "SELECT COUNT(*) FROM role_template_versions"
            ).fetchone()[0])
            if template_count == 0:
                for template in seed_templates():
                    connection.execute(
                        """
                        INSERT INTO role_template_versions(
                            template_id, revision, capability, capability_key,
                            tools_json, provider_requirements_json, boundaries_json,
                            workflow_json, deliverables_json, verification_json,
                            context_retention_json, source_refs_json, template_hash,
                            status, generated_by_project_butler
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 0)
                        """,
                        (
                            template["template_id"], template["revision"],
                            template["capability"], template["capability_key"],
                            _json(template["tools"]),
                            _json(template["provider_requirements"]),
                            _json(template["boundaries"]),
                            _json(template["workflow"]),
                            _json(template["deliverables"]),
                            _json(template["verification"]),
                            _json(template["context_retention"]),
                            _json(template["source_refs"]),
                            template["template_hash"],
                        ),
                    )
                    for ordinal, rule_id in enumerate(template["rule_ids"]):
                        rule_rev = connection.execute(
                            """
                            SELECT revision FROM rule_versions
                            WHERE rule_id = ? AND status = 'active'
                            ORDER BY revision DESC LIMIT 1
                            """,
                            (rule_id,),
                        ).fetchone()
                        if rule_rev is None:
                            raise DomainError(f"seed missing rule: {rule_id}")
                        connection.execute(
                            """
                            INSERT INTO role_template_rule_refs(
                                template_id, template_revision, rule_id,
                                rule_revision, ordinal
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                template["template_id"], template["revision"],
                                rule_id, int(rule_rev["revision"]), ordinal,
                            ),
                        )
                    inserted_templates += 1
            # Ensure singleton global butler identity exists.
            connection.execute(
                """
                INSERT OR IGNORE INTO global_butler_identity(id, role_kind)
                VALUES ('global', 'global_butler')
                """
            )
        return {"rules": inserted_rules, "templates": inserted_templates}

    def list_rules(self, *, scope: str | None = None) -> list[dict[str, Any]]:
        clauses = ["status = 'active'"]
        params: list[Any] = []
        if scope:
            clauses.append("scope = ?")
            params.append(scope)
        where = " AND ".join(clauses)
        connection = self.database.connect()
        try:
            rows = connection.execute(
                f"""
                SELECT * FROM rule_versions
                WHERE {where}
                ORDER BY rule_id, revision DESC
                """,
                params,
            ).fetchall()
            latest: dict[str, dict[str, Any]] = {}
            for row in rows:
                item = dict(row)
                if item["rule_id"] in latest:
                    continue
                latest[item["rule_id"]] = self._rule_view(item)
            return list(latest.values())
        finally:
            connection.close()

    def list_templates(self, *, capability: str | None = None) -> list[dict[str, Any]]:
        clauses = ["status = 'active'"]
        params: list[Any] = []
        if capability:
            clauses.append("capability = ?")
            params.append(capability)
        where = " AND ".join(clauses)
        connection = self.database.connect()
        try:
            rows = connection.execute(
                f"""
                SELECT * FROM role_template_versions
                WHERE {where}
                ORDER BY template_id, revision DESC
                """,
                params,
            ).fetchall()
            latest: dict[str, dict[str, Any]] = {}
            for row in rows:
                item = dict(row)
                if item["template_id"] in latest:
                    continue
                latest[item["template_id"]] = self._template_view(connection, item)
            return list(latest.values())
        finally:
            connection.close()

    def get_instance(self, instance_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM role_instances WHERE id = ?", (instance_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"role instance not found: {instance_id}")
            return self._instance_view(dict(row))
        finally:
            connection.close()

    def list_instances(
        self,
        *,
        project_id: str | None = None,
        goal_id: str | None = None,
        task_id: str | None = None,
        status: str | None = "active",
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if goal_id:
            clauses.append("goal_id = ?")
            params.append(goal_id)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        connection = self.database.connect()
        try:
            rows = connection.execute(
                f"SELECT * FROM role_instances {where} ORDER BY created_at DESC, id DESC",
                params,
            ).fetchall()
            return [self._instance_view(dict(row)) for row in rows]
        finally:
            connection.close()

    def list_bindings(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        status: str | None = "bound",
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        connection = self.database.connect()
        try:
            rows = connection.execute(
                f"SELECT * FROM session_bindings {where} ORDER BY created_at DESC, id DESC",
                params,
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def list_project_role_rules(
        self, *, project_id: str, status: str = "active"
    ) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM project_role_rules
                WHERE project_id = ? AND status = ?
                ORDER BY revision DESC, created_at DESC
                """,
                (project_id, status),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def create_project_role_rule(
        self,
        *,
        project_id: str,
        rule_id: str,
        rule_revision: int | None = None,
        reason: str,
        source: str,
        capability: str | None = None,
        template_id: str | None = None,
    ) -> dict[str, Any]:
        """Append a project overlay. Never mutates global templates."""
        with self.database.transaction(immediate=True) as connection:
            project = connection.execute(
                "SELECT id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise NotFoundError(f"project not found: {project_id}")
            if rule_revision is None:
                row = connection.execute(
                    """
                    SELECT revision FROM rule_versions
                    WHERE rule_id = ? AND status = 'active'
                    ORDER BY revision DESC LIMIT 1
                    """,
                    (rule_id,),
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"rule not found: {rule_id}")
                rule_revision = int(row["revision"])
            else:
                row = connection.execute(
                    """
                    SELECT revision FROM rule_versions
                    WHERE rule_id = ? AND revision = ?
                    """,
                    (rule_id, rule_revision),
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"rule version not found: {rule_id}@{rule_revision}")
            next_rev = int(connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) + 1
                FROM project_role_rules WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()[0])
            rule_hash = connection.execute(
                """
                SELECT content_hash FROM rule_versions
                WHERE rule_id = ? AND revision = ?
                """,
                (rule_id, rule_revision),
            ).fetchone()["content_hash"]
            overlay_id = str(uuid.uuid4())
            digest = content_hash({
                "project_id": project_id,
                "revision": next_rev,
                "rule_id": rule_id,
                "rule_revision": rule_revision,
                "capability": capability,
                "template_id": template_id,
                "reason": reason,
                "source": source,
                "rule_content_hash": rule_hash,
            })
            connection.execute(
                """
                INSERT INTO project_role_rules(
                    id, project_id, revision, capability, template_id,
                    rule_id, rule_revision, reason, source, content_hash, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (
                    overlay_id, project_id, next_rev, capability, template_id,
                    rule_id, rule_revision, reason, source, digest,
                ),
            )
            return dict(connection.execute(
                "SELECT * FROM project_role_rules WHERE id = ?", (overlay_id,)
            ).fetchone())

    def select_or_create_template(
        self,
        connection: Any,
        *,
        role_kind: str,
        provider: str,
        work_item: dict[str, Any],
        project_id: str,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Match active template by capability/constraints; else generate+persist."""
        key = capability_key_for_role(role_kind)
        requirements = {
            "capability_key": key,
            "tools": list(work_item.get("tools") or []),
            "provider": provider,
            "boundaries": list(work_item.get("boundaries") or []),
            "deliverables": list(work_item.get("deliverables") or []),
            "verification": list(work_item.get("verification") or []),
        }
        candidates = connection.execute(
            """
            SELECT * FROM role_template_versions
            WHERE capability_key = ? AND status = 'active'
            ORDER BY revision DESC, created_at DESC
            """,
            (key,),
        ).fetchall()
        scored: list[tuple[int, dict[str, Any], str]] = []
        for row in candidates:
            template = self._template_view(connection, dict(row))
            score, reason = self._score_template(template, requirements)
            if score >= 0:
                scored.append((score, template, reason))
        if scored:
            scored.sort(key=lambda item: (-item[0], -int(item[1]["revision"])))
            best = scored[0]
            return {
                **best[1],
                "match": {
                    "reused": True,
                    "score": best[0],
                    "reason": best[2],
                    "candidates": [
                        {
                            "template_id": item[1]["template_id"],
                            "revision": item[1]["revision"],
                            "score": item[0],
                            "reason": item[2],
                        }
                        for item in scored[:5]
                    ],
                },
            }
        return self._generate_template(
            connection,
            capability_key=key,
            role_kind=role_kind,
            requirements=requirements,
            project_id=project_id,
            task_id=task_id,
        )

    def create_for_task(
        self,
        connection: Any,
        *,
        project_id: str,
        goal_id: str | None,
        task_id: str,
        role_kind: str,
        role_id: str | None,
        provider: str,
        task_spec_revision: int,
        work_item: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        selection = self.select_or_create_template(
            connection,
            role_kind=role_kind,
            provider=provider,
            work_item=work_item or {"boundaries": [], "deliverables": [], "verification": []},
            project_id=project_id,
            task_id=task_id,
        )
        snapshot = self._resolve_snapshot(
            connection,
            project_id=project_id,
            goal_id=goal_id or "",
            task_id=task_id,
            role_kind=role_kind,
            role_id=role_id,
            provider=provider,
            task_spec_revision=task_spec_revision,
            template=selection,
        )
        instance_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO role_instances(
                id, revision, project_id, goal_id, task_id, role_id, role_kind,
                template_id, template_revision, template_hash, ruleset_hash,
                instance_hash, task_spec_revision, provider, match_reason_json,
                snapshot_json, status
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """,
            (
                instance_id, project_id, goal_id, task_id, role_id, role_kind,
                snapshot["template_id"], snapshot["template_revision"],
                snapshot["template_hash"], snapshot["ruleset_hash"],
                snapshot["instance_hash"], task_spec_revision, provider,
                _json(selection.get("match") or {"reused": True}),
                _json(snapshot),
            ),
        )
        binding = self.bind_session(
            connection,
            project_id=project_id,
            role_instance_id=instance_id,
            task_id=task_id,
            provider=provider,
            session_generation=1,
        )
        return {**self._instance_row(connection, instance_id), "session_binding": binding}

    def bind_session(
        self,
        connection: Any,
        *,
        project_id: str,
        role_instance_id: str,
        task_id: str,
        provider: str,
        session_generation: int,
        fencing_token: int = 0,
        external_session_id: str | None = None,
    ) -> dict[str, Any]:
        binding_id = str(uuid.uuid4())
        digest = _binding_hash(
            project_id=project_id,
            role_instance_id=role_instance_id,
            task_id=task_id,
            provider=provider,
            session_generation=session_generation,
        )
        connection.execute(
            """
            INSERT INTO session_bindings(
                id, project_id, role_instance_id, task_id, provider,
                session_generation, external_session_id, fencing_token,
                status, binding_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'bound', ?)
            """,
            (
                binding_id, project_id, role_instance_id, task_id, provider,
                session_generation, external_session_id, fencing_token, digest,
            ),
        )
        existing = connection.execute(
            "SELECT task_id FROM task_sessions WHERE task_id = ?", (task_id,)
        ).fetchone()
        if existing:
            connection.execute(
                """
                UPDATE task_sessions
                SET role_instance_id = ?, session_binding_id = ?,
                    session_generation = ?, provider = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ?
                """,
                (role_instance_id, binding_id, session_generation, provider, task_id),
            )
        else:
            connection.execute(
                """
                INSERT INTO task_sessions(
                    task_id, project_id, role_id, provider, session_generation,
                    role_instance_id, session_binding_id
                )
                SELECT ?, ?, role_id, ?, ?, ?, ?
                FROM role_instances WHERE id = ?
                """,
                (
                    task_id, project_id, provider, session_generation,
                    role_instance_id, binding_id, role_instance_id,
                ),
            )
        return dict(connection.execute(
            "SELECT * FROM session_bindings WHERE id = ?", (binding_id,)
        ).fetchone())

    def replace_instance_for_amend(
        self,
        *,
        task_id: str,
        task_spec_revision: int,
        provider: str | None = None,
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            current = connection.execute(
                """
                SELECT * FROM role_instances
                WHERE task_id = ? AND status = 'active'
                """,
                (task_id,),
            ).fetchone()
            if current is None:
                raise DomainError(f"no active role instance for task: {task_id}")
            binding = connection.execute(
                """
                SELECT * FROM session_bindings
                WHERE task_id = ? AND status = 'bound'
                ORDER BY session_generation DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            next_generation = int(binding["session_generation"] if binding else 1) + 1
            if binding:
                connection.execute(
                    """
                    UPDATE session_bindings
                    SET status = 'terminated', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (binding["id"],),
                )
            connection.execute(
                """
                UPDATE role_instances
                SET status = 'replaced', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (current["id"],),
            )
            created = self.create_for_task(
                connection,
                project_id=current["project_id"],
                goal_id=current["goal_id"],
                task_id=task_id,
                role_kind=current["role_kind"],
                role_id=current["role_id"],
                provider=provider or current["provider"],
                task_spec_revision=task_spec_revision,
            )
            binding_id = created["session_binding"]["id"]
            refreshed_hash = _binding_hash(
                project_id=str(current["project_id"]),
                role_instance_id=str(created["id"]),
                task_id=str(task_id),
                provider=str(provider or current["provider"]),
                session_generation=next_generation,
            )
            connection.execute(
                """
                UPDATE session_bindings
                SET session_generation = ?, binding_hash = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (next_generation, refreshed_hash, binding_id),
            )
            connection.execute(
                """
                UPDATE task_sessions
                SET session_generation = ?, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ?
                """,
                (next_generation, task_id),
            )
            connection.execute(
                """
                UPDATE workers
                SET session_generation = ?, external_session_id = NULL,
                    active_fencing_token = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = (
                    SELECT worker_id FROM task_sessions WHERE task_id = ?
                )
                """,
                (next_generation, task_id),
            )
            connection.execute(
                "UPDATE role_instances SET replaced_by = ? WHERE id = ?",
                (created["id"], current["id"]),
            )
            return self._instance_row(connection, created["id"])

    def ensure_for_task(
        self, task: Any, *, model_invoked: bool | None = None
    ) -> dict[str, Any] | None:
        """Create RoleInstance+SessionBinding for model tasks that lack one."""
        from plow_whip_web.runtime.rule_library import provider_invokes_model

        invoked = (
            bool(model_invoked)
            if model_invoked is not None
            else provider_invokes_model(provider=str(getattr(task, "provider", "")))
        )
        if not invoked:
            return None
        current_rev = int(getattr(task, "spec_revision", 1) or 1)
        existing = self.list_instances(task_id=task.id, status="active")
        if existing:
            if int(existing[0]["task_spec_revision"]) == current_rev:
                return existing[0]
            # Amend/restart bumps TaskSpec revision → new instance + session generation.
            return self.replace_instance_for_amend(
                task_id=task.id,
                task_spec_revision=current_rev,
                provider=str(task.provider),
            )
        role_kind = "fullstack"
        with self.database.transaction(immediate=True) as connection:
            project_id = self._ensure_project_id(connection, task)
            if task.role_id:
                row = connection.execute(
                    "SELECT kind FROM roles WHERE id = ?", (task.role_id,)
                ).fetchone()
                if row:
                    role_kind = str(row["kind"]).split(":", 1)[0]
            return self.create_for_task(
                connection,
                project_id=project_id,
                goal_id=getattr(task, "goal_id", None),
                task_id=task.id,
                role_kind=role_kind,
                role_id=task.role_id,
                provider=task.provider,
                task_spec_revision=current_rev,
                work_item={
                    "boundaries": list((task.spec or {}).get("scope") or []),
                    "deliverables": list((task.spec or {}).get("artifacts") or []),
                    "verification": [
                        str(item.get("kind") if isinstance(item, dict) else item)
                        for item in ((task.spec or {}).get("verification") or [])
                    ],
                },
            )

    def _ensure_project_id(self, connection: Any, task: Any) -> str:
        project_id = getattr(task, "project_id", None)
        if project_id:
            return str(project_id)
        path = str(Path(str(task.project_path)).expanduser().resolve())
        row = connection.execute(
            "SELECT id FROM projects WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            project_id = str(uuid.uuid4())
            policy = project_execution_policy(None)
            connection.execute(
                """
                INSERT INTO projects(id, name, path, execution_policy_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    project_id,
                    Path(path).name or "ad-hoc",
                    path,
                    _json(policy),
                ),
            )
            connection.execute(
                """
                INSERT INTO roles(id, project_id, kind, legacy)
                VALUES (?, ?, 'butler', 0)
                """,
                (str(uuid.uuid4()), project_id),
            )
        else:
            project_id = str(row["id"])
        connection.execute(
            "UPDATE tasks SET project_id = ? WHERE id = ? AND project_id IS NULL",
            (project_id, task.id),
        )
        return project_id

    def require_dispatchable(
        self,
        *,
        task_id: str,
        provider: str,
        command: dict[str, Any] | None,
        model_invoked: bool,
        expected_task_spec_revision: int | None = None,
    ) -> dict[str, Any]:
        if is_local_deterministic_worker(
            provider=provider, command=command, model_invoked=model_invoked,
        ):
            return {
                "allowed": True,
                "exception": "local_deterministic_worker",
                "role_instance": None,
                "session_binding": None,
            }
        connection = self.database.connect()
        try:
            instance = connection.execute(
                """
                SELECT * FROM role_instances
                WHERE task_id = ? AND status = 'active'
                """,
                (task_id,),
            ).fetchone()
            if instance is None:
                raise DomainError(
                    "dispatch rejected: missing RoleInstance for model-invoking worker"
                )
            snapshot = json.loads(instance["snapshot_json"])
            if instance["instance_hash"] != snapshot.get("instance_hash"):
                raise DomainError("dispatch rejected: RoleInstance hash mismatch")
            if (
                expected_task_spec_revision is not None
                and int(instance["task_spec_revision"]) != int(expected_task_spec_revision)
            ):
                raise DomainError(
                    "dispatch rejected: RoleInstance task_spec_revision expired"
                )
            # Ensure referenced template/rules still exist (FK replay safety).
            template = connection.execute(
                """
                SELECT 1 FROM role_template_versions
                WHERE template_id = ? AND revision = ?
                """,
                (instance["template_id"], instance["template_revision"]),
            ).fetchone()
            if template is None:
                raise DomainError("dispatch rejected: template revision missing")
            binding = connection.execute(
                """
                SELECT * FROM session_bindings
                WHERE task_id = ? AND role_instance_id = ? AND status = 'bound'
                ORDER BY session_generation DESC LIMIT 1
                """,
                (task_id, instance["id"]),
            ).fetchone()
            if binding is None:
                raise DomainError(
                    "dispatch rejected: missing SessionBinding for RoleInstance"
                )
            if str(binding["provider"]) != str(provider):
                raise DomainError(
                    "dispatch rejected: SessionBinding provider mismatch"
                )
            expected_binding_hash = _binding_hash(
                project_id=str(binding["project_id"]),
                role_instance_id=str(binding["role_instance_id"]),
                task_id=str(binding["task_id"]),
                provider=str(binding["provider"]),
                session_generation=int(binding["session_generation"]),
            )
            if str(binding["binding_hash"]) != expected_binding_hash:
                raise DomainError(
                    "dispatch rejected: SessionBinding hash mismatch"
                )
            return {
                "allowed": True,
                "exception": None,
                "role_instance": self._instance_view(dict(instance)),
                "session_binding": dict(binding),
            }
        finally:
            connection.close()

    def _generate_template(
        self,
        connection: Any,
        *,
        capability_key: str,
        role_kind: str,
        requirements: dict[str, Any],
        project_id: str,
        task_id: str | None,
    ) -> dict[str, Any]:
        generated_count = int(connection.execute(
            """
            SELECT COUNT(*) FROM role_template_versions
            WHERE capability_key = ? AND generated_by_project_butler = 1
              AND status = 'active'
            """,
            (capability_key,),
        ).fetchone()[0])
        if generated_count >= MAX_GENERATED_TEMPLATES_PER_CAPABILITY:
            raise DomainError(
                f"template generation bound exceeded for capability={capability_key}"
            )
        rule_ids = (
            list(_DEV_RULE_IDS)
            if capability_key not in {"project_butler", "global_butler"}
            else (
                ["project_butler.one_question_95"]
                if capability_key == "project_butler"
                else ["global_butler.readonly_route"]
            )
        )
        body = {
            "capability": role_kind,
            "capability_key": capability_key,
            "rule_ids": rule_ids,
            "tools": requirements.get("tools") or ["host-bridge"],
            "provider_requirements": [requirements["provider"]],
            "boundaries": requirements.get("boundaries") or [
                f"capability:{capability_key}"
            ],
            "workflow": ["inspect", "minimal change", "verify"],
            "deliverables": requirements.get("deliverables") or ["verification evidence"],
            "verification": requirements.get("verification") or ["exit_code"],
            "context_retention": {
                "mandatory_rule_reserve_bytes": 1400,
                "trim_observations_first": True,
            },
            "source_refs": [],
        }
        # Reject executable / plugin payloads in auto-generated templates.
        banned = ("curl|", "install.sh", "#!/", "eval(", "os.system")
        blob = _json(body).lower()
        if any(token in blob for token in banned):
            raise DomainError("generated template failed safety validation")
        template_hash = content_hash({
            k: body[k] for k in (
                "capability_key", "rule_ids", "tools", "provider_requirements",
                "boundaries", "workflow", "deliverables", "verification",
                "context_retention",
            )
        })
        existing = connection.execute(
            """
            SELECT * FROM role_template_versions
            WHERE capability_key = ? AND template_hash = ? AND status = 'active'
            """,
            (capability_key, template_hash),
        ).fetchone()
        if existing:
            view = self._template_view(connection, dict(existing))
            return {
                **view,
                "match": {
                    "reused": True,
                    "score": 100,
                    "reason": "dedup_hit_structural_hash",
                    "candidates": [],
                },
            }
        template_id = f"tmpl.generated.{capability_key}.{template_hash[:12]}"
        revision = 1
        connection.execute(
            """
            INSERT INTO role_template_versions(
                template_id, revision, capability, capability_key,
                tools_json, provider_requirements_json, boundaries_json,
                workflow_json, deliverables_json, verification_json,
                context_retention_json, source_refs_json, template_hash,
                status, generated_by_project_butler, source_project_id,
                source_task_id, generation_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?, ?)
            """,
            (
                template_id, revision, role_kind, capability_key,
                _json(body["tools"]), _json(body["provider_requirements"]),
                _json(body["boundaries"]), _json(body["workflow"]),
                _json(body["deliverables"]), _json(body["verification"]),
                _json(body["context_retention"]), _json(body["source_refs"]),
                template_hash, project_id, task_id,
                "no matching active template; auto-generated after schema/safety checks",
            ),
        )
        for ordinal, rule_id in enumerate(rule_ids):
            rule_rev = connection.execute(
                """
                SELECT revision FROM rule_versions
                WHERE rule_id = ? AND status = 'active'
                ORDER BY revision DESC LIMIT 1
                """,
                (rule_id,),
            ).fetchone()
            if rule_rev is None:
                raise DomainError(f"rule reference broken: {rule_id}")
            connection.execute(
                """
                INSERT INTO role_template_rule_refs(
                    template_id, template_revision, rule_id, rule_revision, ordinal
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (template_id, revision, rule_id, int(rule_rev["revision"]), ordinal),
            )
        view = self._template_view(connection, dict(connection.execute(
            """
            SELECT * FROM role_template_versions
            WHERE template_id = ? AND revision = ?
            """,
            (template_id, revision),
        ).fetchone()))
        return {
            **view,
            "match": {
                "reused": False,
                "score": 0,
                "reason": "generated_new_template",
                "candidates": [],
            },
        }

    def _resolve_snapshot(
        self,
        connection: Any,
        *,
        project_id: str,
        goal_id: str,
        task_id: str,
        role_kind: str,
        role_id: str | None,
        provider: str,
        task_spec_revision: int,
        template: dict[str, Any],
    ) -> dict[str, Any]:
        """Precedence: task_role human > ProjectRoleRule > template rules > global."""
        template_rules = []
        for ref in template.get("rule_refs") or []:
            row = connection.execute(
                """
                SELECT * FROM rule_versions
                WHERE rule_id = ? AND revision = ?
                """,
                (ref["rule_id"], ref["rule_revision"]),
            ).fetchone()
            if row is None:
                raise DomainError(
                    f"template rule reference broken: {ref['rule_id']}@{ref['rule_revision']}"
                )
            template_rules.append(self._rule_view(dict(row)))

        project_rules = connection.execute(
            """
            SELECT prr.*, rv.content, rv.source AS rule_source, rv.license,
                   rv.enforcement, rv.mandatory, rv.content_hash AS rule_content_hash,
                   rv.scope
            FROM project_role_rules prr
            JOIN rule_versions rv
              ON rv.rule_id = prr.rule_id AND rv.revision = prr.rule_revision
            WHERE prr.project_id = ? AND prr.status = 'active'
              AND (prr.capability IS NULL OR prr.capability = ?)
              AND (prr.template_id IS NULL OR prr.template_id = ?)
            ORDER BY prr.revision DESC
            """,
            (project_id, capability_key_for_role(role_kind), template["template_id"]),
        ).fetchall()

        resolved: dict[str, dict[str, Any]] = {}
        # Lowest precedence first so higher layers overwrite.
        for rule in template_rules:
            resolved[rule["id"]] = {
                **rule,
                "precedence": "role_template",
                "source_scope": "template",
            }
        for row in project_rules:
            item = dict(row)
            resolved[item["rule_id"]] = {
                "id": item["rule_id"],
                "revision": item["rule_revision"],
                "scope": item["scope"],
                "source": item["rule_source"],
                "license": item["license"],
                "content": item["content"],
                "content_hash": item["rule_content_hash"],
                "mandatory": bool(item["mandatory"]),
                "enforcement": item["enforcement"],
                "precedence": "project_role_rule",
                "source_scope": "project",
                "project_rule_id": item["id"],
                "reason": item["reason"],
            }
        # Code-enforced rules cannot be cancelled by overlays.
        for rule_id, rule in list(resolved.items()):
            if rule.get("enforcement") == "code" and rule.get("precedence") == "project_role_rule":
                # Keep project overlay metadata but mark non-cancelling.
                rule["cannot_cancel_code_enforcement"] = True
                resolved[rule_id] = rule

        ruleset = list(resolved.values())
        snapshot = {
            "project_id": project_id,
            "goal_id": goal_id,
            "task_id": task_id,
            "role_kind": role_kind,
            "role_id": role_id,
            "template_id": template["template_id"],
            "template_revision": template["revision"],
            "template_hash": template["template_hash"],
            "ruleset": [
                {
                    "id": rule["id"],
                    "revision": rule["revision"],
                    "content_hash": rule["content_hash"],
                    "content": rule.get("content"),
                    "mandatory": rule["mandatory"],
                    "enforcement": rule["enforcement"],
                    "source": rule["source"],
                    "license": rule.get("license"),
                    "precedence": rule.get("precedence"),
                    "source_scope": rule.get("source_scope"),
                }
                for rule in ruleset
            ],
            "ruleset_hash": content_hash([
                {
                    "id": rule["id"],
                    "revision": rule["revision"],
                    "content_hash": rule["content_hash"],
                    "precedence": rule.get("precedence"),
                }
                for rule in ruleset
            ]),
            "provider": provider,
            "tools": template.get("tools") or [],
            "boundaries": template.get("boundaries") or [],
            "deliverables": template.get("deliverables") or [],
            "verification": template.get("verification") or [],
            "context_retention": template.get("context_retention") or {},
            "task_spec_revision": task_spec_revision,
            "source_refs": template.get("source_refs") or [],
            "precedence": [
                "direct_human_task_role",
                "project_role_rule",
                "role_template",
                "global_applicable",
            ],
        }
        snapshot["instance_hash"] = content_hash(snapshot)
        return snapshot

    @staticmethod
    def _score_template(
        template: dict[str, Any], requirements: dict[str, Any]
    ) -> tuple[int, str]:
        if template.get("capability_key") != requirements["capability_key"]:
            return -1, "capability_mismatch"
        score = 50
        reasons = ["capability_key_match"]
        provider = requirements.get("provider")
        providers = template.get("provider_requirements") or []
        if providers and provider and provider not in providers:
            # Still usable; prefer templates that list the provider.
            score -= 10
            reasons.append("provider_not_listed")
        else:
            score += 20
            reasons.append("provider_ok")
        req_bounds = set(requirements.get("boundaries") or [])
        tmpl_bounds = set(template.get("boundaries") or [])
        if req_bounds and req_bounds.issubset(tmpl_bounds):
            score += 20
            reasons.append("boundaries_covered")
        elif req_bounds and tmpl_bounds.isdisjoint(req_bounds):
            score -= 5
            reasons.append("boundaries_partial")
        return score, "+".join(reasons)

    def _instance_row(self, connection: Any, instance_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM role_instances WHERE id = ?", (instance_id,)
        ).fetchone()
        view = self._instance_view(dict(row))
        binding = connection.execute(
            """
            SELECT * FROM session_bindings
            WHERE role_instance_id = ? AND status = 'bound'
            ORDER BY session_generation DESC LIMIT 1
            """,
            (instance_id,),
        ).fetchone()
        view["session_binding"] = dict(binding) if binding else None
        return view

    @staticmethod
    def _instance_view(row: dict[str, Any]) -> dict[str, Any]:
        snapshot = json.loads(row["snapshot_json"])
        match_reason = json.loads(row.get("match_reason_json") or "{}")
        return {
            **{k: v for k, v in row.items() if k not in {"snapshot_json", "match_reason_json"}},
            "snapshot": snapshot,
            "match_reason": match_reason,
            "source_chain": {
                "template_id": row["template_id"],
                "template_revision": row["template_revision"],
                "template_hash": row["template_hash"],
                "ruleset_hash": row["ruleset_hash"],
                "instance_hash": row["instance_hash"],
                "source_refs": snapshot.get("source_refs") or [],
                "precedence": snapshot.get("precedence") or [],
            },
        }

    def _template_view(self, connection: Any, row: dict[str, Any]) -> dict[str, Any]:
        refs = connection.execute(
            """
            SELECT rule_id, rule_revision, ordinal
            FROM role_template_rule_refs
            WHERE template_id = ? AND template_revision = ?
            ORDER BY ordinal
            """,
            (row["template_id"], row["revision"]),
        ).fetchall()
        return {
            "id": row["template_id"],
            "template_id": row["template_id"],
            "revision": row["revision"],
            "version": row["revision"],
            "capability": row["capability"],
            "capability_key": row["capability_key"],
            "tools": json.loads(row["tools_json"]),
            "provider_requirements": json.loads(row["provider_requirements_json"]),
            "boundaries": json.loads(row["boundaries_json"]),
            "workflow": json.loads(row["workflow_json"]),
            "deliverables": json.loads(row["deliverables_json"]),
            "verification": json.loads(row["verification_json"]),
            "context_retention": json.loads(row["context_retention_json"]),
            "source_refs": json.loads(row["source_refs_json"]),
            "template_hash": row["template_hash"],
            "status": row["status"],
            "generated_by_project_butler": bool(row["generated_by_project_butler"]),
            "rule_ids": [ref["rule_id"] for ref in refs],
            "rule_refs": [dict(ref) for ref in refs],
        }

    @staticmethod
    def _rule_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["rule_id"],
            "rule_id": row["rule_id"],
            "revision": row["revision"],
            "version": row["revision"],
            "scope": row["scope"],
            "source": row["source"],
            "license": row["license"],
            "content": row["content"],
            "content_hash": row["content_hash"],
            "applies_to": json.loads(row["applies_to_json"]),
            "mandatory": bool(row["mandatory"]),
            "enforcement": row["enforcement"],
            "status": row["status"],
        }


_DEV_RULE_IDS = [
    "dev.think_before_coding",
    "dev.simplicity_first",
    "dev.surgical_changes",
    "dev.goal_driven_execution",
]
