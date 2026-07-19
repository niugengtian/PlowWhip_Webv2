from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from plow_whip_web.domain.model import DomainError
from plow_whip_web.roles import ROLE_PROMPTS
from plow_whip_web.runtime.behavior_packs import (
    behavior_baseline_for_role,
    bundled_behavior_packs,
    principles_intact,
    role_receives_dev_behavior_baseline,
)
from plow_whip_web.store.convention_repository import ConventionRepository
from plow_whip_web.store.database import Database
from plow_whip_web.store.settings_repository import SettingsRepository
from plow_whip_web.store.task_repository import TaskRepository


class ContextCompiler:
    def __init__(
        self,
        data_dir: Path,
        database: Database,
        tasks: TaskRepository,
        conventions: ConventionRepository,
        settings: SettingsRepository,
    ) -> None:
        self.data_dir = data_dir
        self.database = database
        self.tasks = tasks
        self.conventions = conventions
        self.settings = settings

    def compile(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        project = self._project(task.project_id)
        effective_settings = self.settings.effective(
            project_id=task.project_id,
            task_id=task.id,
            role_id=task.role_id,
        )
        limits = effective_settings["values"]
        role = self._role(task.role_id)
        db_principle_rules = self._development_rules()
        baseline = behavior_baseline_for_role(
            role,
            rules=db_principle_rules,
            config_source="rule_versions:development",
        )
        role_instance = self._active_role_instance(task.id)
        spec = json.dumps(
            task.spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        spec_section = f"## Immutable TaskSpec\nRevision: {task.spec_revision}\n{spec}"
        # (text, trim_priority, floor_bytes, section_id, mandatory)
        sections: list[tuple[str, int, int, str, bool]] = [
            (
                spec_section,
                7,
                len(spec_section.encode("utf-8")),
                "task_spec",
                True,
            ),
            (
                "## Role\n" + ROLE_PROMPTS.get(role, ROLE_PROMPTS["fullstack"]),
                2,
                384,
                "role_prompt",
                False,
            ),
        ]
        injected: list[dict[str, Any]] = [
            {
                "kind": "task_spec",
                "source": "task.spec",
                "protected": True,
                "mandatory": True,
            },
            {
                "kind": "role_prompt",
                "source": f"roles:{role}",
                "protected": False,
                "mandatory": False,
            },
        ]
        # Prefer the RoleInstance snapshot ruleset when present; do not scan the
        # whole RuleLibrary. Fall back to bundled baseline only without an instance.
        snapshot_rules = list((role_instance or {}).get("ruleset") or [])
        context_rules = [
            rule for rule in snapshot_rules
            if rule.get("enforcement") in {"context", "verification"}
            and rule.get("content")
        ]
        principle_ids = {
            "dev.think_before_coding",
            "dev.simplicity_first",
            "dev.surgical_changes",
            "dev.goal_driven_execution",
        }
        snapshot_has_principles = principle_ids.issubset(
            {str(rule.get("id")) for rule in snapshot_rules}
        )
        if context_rules:
            for rule in context_rules:
                body = (
                    f"## RoleInstance rule: {rule['id']}@{rule['revision']}\n"
                    f"source: {rule.get('source')}\n"
                    f"precedence: {rule.get('precedence')}\n"
                    f"license: {rule.get('license')}\n"
                    f"{rule['content']}"
                )
                reserve = max(
                    256 if rule.get("mandatory") else 0,
                    len(body.encode("utf-8")),
                )
                sections.append((
                    body,
                    5 if rule.get("mandatory") else 3,
                    reserve,
                    f"role_instance_rule:{rule['id']}",
                    bool(rule.get("mandatory")),
                ))
                injected.append({
                    "kind": "role_instance_rule",
                    "id": rule["id"],
                    "revision": rule["revision"],
                    "source": rule.get("source"),
                    "license": rule.get("license"),
                    "precedence": rule.get("precedence"),
                    "source_scope": rule.get("source_scope"),
                    "mandatory": bool(rule.get("mandatory")),
                    "enforcement": rule.get("enforcement"),
                    "content_hash": rule.get("content_hash"),
                    "effective_reserve_bytes": reserve,
                    "template_id": (role_instance or {}).get("template_id"),
                    "instance_id": (role_instance or {}).get("id"),
                    "instance_hash": (role_instance or {}).get("instance_hash"),
                    "model_invoked": False,
                })
        if baseline["inject"] and not snapshot_has_principles:
            # Prefer DB rule_versions content; never invent a second Python body.
            pack_content = str(baseline["content"])
            reserve = max(
                int(baseline["effective_reserve_bytes"]),
                len(pack_content.encode("utf-8")),
            )
            sections.append((
                pack_content,
                int(baseline["trim_priority"]),
                reserve,
                str(baseline["id"]),
                True,
            ))
            injected.append({
                "kind": "rule_library_baseline",
                "id": baseline["id"],
                "source": baseline["source"],
                "license": baseline.get("license"),
                "revision": baseline["revision"],
                "version": baseline.get("version"),
                "role": baseline.get("role"),
                "applicable": True,
                "mandatory": True,
                "effective_reserve_bytes": reserve,
                "config_source": baseline.get("config_source"),
                "trim_priority": baseline["trim_priority"],
                "rule_ids": baseline.get("rule_ids"),
                "model_invoked": False,
            })
        elif baseline["inject"] and snapshot_has_principles:
            injected.append({
                "kind": "rule_library_baseline",
                "id": baseline["id"],
                "source": baseline["source"],
                "revision": baseline["revision"],
                "version": baseline.get("version"),
                "role": baseline.get("role"),
                "applicable": True,
                "not_applicable": False,
                "mandatory": True,
                "effective_reserve_bytes": int(baseline["effective_reserve_bytes"]),
                "config_source": "role_instance.snapshot",
                "superseded_by": "role_instance_ruleset",
                "model_invoked": False,
            })
        else:
            injected.append({
                "kind": "rule_library_baseline",
                "id": baseline["id"],
                "source": baseline["source"],
                "revision": baseline["revision"],
                "version": baseline.get("version"),
                "role": baseline.get("role"),
                "applicable": False,
                "not_applicable": True,
                "applicability": "not_applicable",
                "mandatory": False,
                "effective_reserve_bytes": 0,
                "config_source": baseline.get("config_source"),
                "reason": baseline.get("reason"),
                "model_invoked": False,
            })
        checkpoint = self._episode_checkpoint(task.id)
        if checkpoint:
            action = str(checkpoint.get("recovery_action") or "resume")
            instruction = {
                "resume": (
                    "Continue from the checkpoint and retained session. Do not replay "
                    "steps already reflected in the workspace."
                ),
                "replan": (
                    "Inspect the checkpoint and workspace, then make a bounded plan for "
                    "only the remaining work before continuing."
                ),
                "replacement": (
                    "This is a replacement session. Treat the workspace and checkpoint "
                    "as canonical; do not replay completed work."
                ),
            }.get(action, "Inspect the checkpoint before continuing.")
            payload = json.dumps(
                checkpoint, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
            checkpoint_section = _fit_utf8(
                "## ExecutionEpisode checkpoint\n"
                f"Recovery action: {action}\n{instruction}\n{payload}",
                int(limits["checkpoint_max_bytes"]),
            )
            sections.append((
                checkpoint_section,
                6,
                min(768, int(limits["checkpoint_max_bytes"])),
                "checkpoint",
                False,
            ))
            injected.append({"kind": "checkpoint", "source": "execution_episodes"})
        if task.last_error == "external_execution_interrupted":
            sections.append(
                (
                    "## Continuation\nThe previous host process was externally interrupted. "
                    "Inspect the existing workspace first, preserve completed work, and continue "
                    "the same objective from the retained CLI session without repeating finished steps.",
                    1,
                    0,
                    "continuation",
                    False,
                )
            )
        failure_delta = self.tasks.last_failure_delta(task.id)
        if failure_delta:
            evidence = json.dumps(
                failure_delta, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
            evidence_section = _fit_utf8(
                "## Previous verification Evidence Delta\n"
                "Fix only the failed checks below. Preserve all already-passing work "
                "and re-run the deterministic verification after the repair.\n"
                + evidence,
                int(limits["checkpoint_max_bytes"]),
            )
            sections.append((
                evidence_section,
                6,
                min(768, int(limits["checkpoint_max_bytes"])),
                "failure_delta",
                False,
            ))
        if task.handoff:
            handoff = json.dumps(
                task.handoff, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
            handoff_section = _fit_utf8(
                "## Role Handoff\n"
                "Structured pointers from the previous work item only. Do not assume "
                "cross-role chat history exists; inspect the listed artifact paths.\n"
                + handoff,
                int(limits["handoff_max_bytes"]),
            )
            sections.append((
                handoff_section,
                5,
                min(512, int(limits["handoff_max_bytes"])),
                "handoff",
                False,
            ))
        effective = self.conventions.effective_context(
            project_id=task.project_id,
            task_id=task.id,
            role_id=task.role_id,
            role_kind=role,
        )
        for layer in effective["layers"]:
            if layer["kind"] in {"bundled_behavior", "rule_library_baseline"}:
                continue
            if layer["kind"] != "mutable_convention" or not layer["inject"]:
                continue
            priority = int(layer["trim_priority"])
            # Direct human Task+role Convention is mandatory with TaskSpec / baseline.
            is_direct = layer["scope"] in {"task", "task_role"}
            floor = {
                1: 0,
                4: 768,
                5: len(str(layer["content"]).encode("utf-8")) if is_direct else 1024,
            }.get(priority, 0)
            sections.append((
                f"## Convention: {layer['scope']}\n"
                f"source: {layer['source']}\n"
                f"revision: {layer['revision']}\n"
                f"{layer['content']}",
                priority,
                floor,
                f"convention:{layer['scope']}",
                is_direct,
            ))
            injected.append({
                "kind": "mutable_convention",
                "scope": layer["scope"],
                "scope_id": layer["scope_id"],
                "source": layer["source"],
                "revision": layer["revision"],
                "mandatory": is_direct,
                "effective_order": layer["effective_order"],
            })
        protected = [
            f"## Project\nName: {project['name']}\nPath: {task.project_path}",
            (
                "## Boundaries\n"
                f"Project id: {task.project_id or 'unbound'}\n"
                f"Role id: {task.role_id or 'unbound'}\n"
                f"Task id: {task.id}\n"
                f"Worker id: {task.worker_id or 'pending'}\n"
                f"TaskSpec revision: {task.spec_revision}"
            ),
            "## Completion rule\nOnly verification evidence can move this task to completed.",
        ]
        max_bytes = int(limits["context_max_bytes"])
        fit_sections = [
            (text, priority, floor, mandatory)
            for text, priority, floor, _, mandatory in sections
        ]
        try:
            content = _fit_sections(fit_sections, protected, max_bytes)
        except DomainError as error:
            raise DomainError(
                "context configuration conflict: TaskSpec, boundaries, completion "
                "rule, direct Task+role Convention, and mandatory development "
                f"behavior baseline cannot coexist within context_max_bytes={max_bytes}"
            ) from error
        if baseline["inject"] and not principles_intact(content):
            raise DomainError(
                "context configuration conflict: mandatory development behavior "
                "baseline lost principle semantics under context pressure"
            )
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        relative = Path("contexts") / task.id / f"{digest}.md"
        target = self.data_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_suffix(".tmp")
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, target)
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT id FROM context_packs WHERE task_id = ? AND content_hash = ?",
                (task.id, digest),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO context_packs(id, task_id, worker_id, content_hash, byte_size, relative_path)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        task.id,
                        task.worker_id,
                        digest,
                        len(content.encode("utf-8")),
                        str(relative),
                    ),
                )
        return {
            "task_id": task.id,
            "role": role,
            "content": content,
            "content_hash": digest,
            "byte_size": len(content.encode("utf-8")),
            "max_bytes": max_bytes,
            "relative_path": str(relative),
            "model_invoked": False,
            "spec_revision": task.spec_revision,
            "settings_sources": effective_settings["sources"],
            "injected_sources": injected,
            "behavior_baseline": baseline,
            "effective_conventions": effective,
            "precedence": effective["precedence"],
            "dev_behavior_applicable": role_receives_dev_behavior_baseline(role),
            "role_instance": role_instance,
        }

    def preview_behavior_baseline(self, role_kind: str | None) -> dict[str, Any]:
        return behavior_baseline_for_role(
            role_kind,
            rules=self._development_rules(),
            config_source="rule_versions:development",
        )

    def _development_rules(self) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT rule_id, revision, source, license, content, content_hash,
                       mandatory, enforcement
                FROM rule_versions
                WHERE scope = 'development' AND status = 'active'
                ORDER BY rule_id, revision DESC
                """
            ).fetchall()
            latest: dict[str, dict[str, Any]] = {}
            for row in rows:
                rule_id = str(row["rule_id"])
                if rule_id in latest:
                    continue
                latest[rule_id] = {
                    "id": rule_id,
                    "rule_id": rule_id,
                    "revision": int(row["revision"]),
                    "source": row["source"],
                    "license": row["license"],
                    "content": row["content"],
                    "content_hash": row["content_hash"],
                    "mandatory": bool(row["mandatory"]),
                    "enforcement": row["enforcement"],
                }
            return list(latest.values())
        except Exception:
            return []
        finally:
            connection.close()

    def _active_role_instance(self, task_id: str) -> dict[str, Any] | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT id, revision, role_kind, template_id, template_revision,
                       template_hash, ruleset_hash, instance_hash,
                       task_spec_revision, snapshot_json
                FROM role_instances
                WHERE task_id = ? AND status = 'active'
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            snapshot = json.loads(row["snapshot_json"])
            return {
                "id": row["id"],
                "revision": row["revision"],
                "role_kind": row["role_kind"],
                "template_id": row["template_id"],
                "template_revision": row["template_revision"],
                "template_hash": row["template_hash"],
                "ruleset_hash": row["ruleset_hash"],
                "instance_hash": row["instance_hash"],
                "task_spec_revision": row["task_spec_revision"],
                "ruleset": snapshot.get("ruleset") or [],
                "source_refs": snapshot.get("source_refs") or [],
            }
        except Exception:
            return None
        finally:
            connection.close()

    def _role(self, role_id: str | None) -> str:
        if role_id is None:
            return "fullstack"
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT kind FROM roles WHERE id = ?", (role_id,)
            ).fetchone()
            if row is None:
                return "fullstack"
            kind = str(row["kind"])
            base = kind.split(":", 1)[0]
            # Preserve named development roles; never collapse to a generic alias.
            if base in ROLE_PROMPTS or base in {
                "backend", "frontend", "ui", "fullstack", "devops_sre",
                "verification", "web3", "butler", "coordination", "simple-worker",
            }:
                return base
            return base or "fullstack"
        finally:
            connection.close()

    def _project(self, project_id: str | None) -> dict[str, str]:
        if project_id is None:
            return {"name": "unbound"}
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT name FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            return {"name": str(row["name"])} if row else {"name": "unbound"}
        finally:
            connection.close()

    def _episode_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT checkpoint_json FROM execution_episodes
                WHERE task_id = ? AND checkpoint_json IS NOT NULL
                ORDER BY ordinal DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            return json.loads(row["checkpoint_json"]) if row else None
        finally:
            connection.close()


def _fit_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    marker = "\n\n[context truncated deterministically]\n"
    room = max(0, limit - len(marker.encode("utf-8")))
    prefix = encoded[:room]
    while prefix:
        try:
            return prefix.decode("utf-8") + marker
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    return marker.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _fit_sections(
    sections: list[tuple[str, int, int, bool]],
    protected: list[str],
    limit: int,
) -> str:
    prefix = "# Execution Context"
    full = "\n\n".join([prefix, *(text for text, _, _, _ in sections), *protected]) + "\n"
    if len(full.encode("utf-8")) <= limit:
        return full

    truncation = "[context sections truncated deterministically by scope priority]"
    fixed = [prefix, truncation, *protected]
    if len(("\n\n".join(fixed) + "\n").encode("utf-8")) > limit:
        raise DomainError("context limit cannot preserve boundaries and completion rule")

    # Mandatory floors alone must fit with protected + header.
    mandatory_floor = sum(
        min(len(text.encode("utf-8")), floor)
        for text, _, floor, mandatory in sections
        if mandatory
    )
    skeleton = "\n\n".join(fixed) + "\n"
    if len(skeleton.encode("utf-8")) + mandatory_floor > limit:
        raise DomainError(
            "context limit cannot preserve mandatory TaskSpec, conventions, "
            "and development behavior baseline with boundaries/completion rule"
        )

    fitted = [text for text, _, _, _ in sections]

    def render() -> str:
        return "\n\n".join([
            prefix,
            *(text for text in fitted if text),
            truncation,
            *protected,
        ]) + "\n"

    # Trim lowest priority first; never cut mandatory sections below floor.
    for index in sorted(range(len(sections)), key=lambda item: sections[item][1]):
        over = len(render().encode("utf-8")) - limit
        if over <= 0:
            break
        current_size = len(fitted[index].encode("utf-8"))
        floor = min(current_size, sections[index][2])
        target = max(floor, current_size - over)
        if sections[index][3] and target < floor:
            continue
        fitted[index] = _fit_utf8(fitted[index], target)
    content = render()
    if len(content.encode("utf-8")) > limit:
        raise DomainError(
            "context limit cannot preserve task/project rules, boundaries, "
            "completion rule, and mandatory development behavior baseline"
        )
    return content


# Re-export for tests that previously imported packs via context.
__all__ = ["ContextCompiler", "bundled_behavior_packs"]
