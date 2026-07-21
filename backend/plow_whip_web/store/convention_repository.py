from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from plow_whip_web.domain.model import DomainError, RevisionConflictError
from plow_whip_web.runtime.behavior_packs import (
    behavior_baseline_for_role,
    bundled_behavior_packs,
)
from plow_whip_web.store.database import Database

CONVENTION_SCOPES = ("global", "project", "task", "task_role")
_DEV_CONFIG_SOURCE = "rule_versions:development"
DEFAULT_GLOBAL_CONVENTION_PATH = (
    Path(__file__).resolve().parent.parent
    / "defaults"
    / "global_convention.md"
)


def default_global_convention() -> str:
    content = DEFAULT_GLOBAL_CONVENTION_PATH.read_text(encoding="utf-8").strip()
    if not content:
        raise RuntimeError("bundled global Convention is empty")
    return content


class ConventionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def seed_global_if_absent(self) -> dict[str, Any]:
        """Bootstrap a fresh install without overwriting an existing Convention."""
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT * FROM conventions
                WHERE scope = 'global' AND scope_id = 'global'
                """
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO conventions(id, scope, scope_id, content, revision)
                    VALUES (?, 'global', 'global', ?, 1)
                    """,
                    (
                        str(uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            "plow-whip-web:global-convention",
                        )),
                        default_global_convention(),
                    ),
                )
        return self.get(scope="global", scope_id="global")

    def put(self, *, scope: str, scope_id: str, content: str, expected_revision: int) -> dict[str, Any]:
        self._validate_scope(scope, scope_id)
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT id, revision FROM conventions WHERE scope = ? AND scope_id = ?",
                (scope, scope_id),
            ).fetchone()
            current = row["revision"] if row else 0
            if current != expected_revision:
                raise RevisionConflictError(
                    f"expected convention revision {expected_revision}, current {current}"
                )
            convention_id = row["id"] if row else str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO conventions(id, scope, scope_id, content, revision)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope, scope_id) DO UPDATE SET content = excluded.content,
                    revision = excluded.revision, updated_at = CURRENT_TIMESTAMP
                """,
                (convention_id, scope, scope_id, content, current + 1),
            )
        return self.get(scope=scope, scope_id=scope_id)

    def get(self, *, scope: str, scope_id: str) -> dict[str, Any]:
        self._validate_scope(scope, scope_id)
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM conventions WHERE scope = ? AND scope_id = ?",
                (scope, scope_id),
            ).fetchone()
            if row is None:
                return self._empty(scope=scope, scope_id=scope_id)
            return self._present(dict(row))
        finally:
            connection.close()

    def resolve(
        self,
        *,
        project_id: str | None,
        task_id: str,
        role_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Mutable conventions only, in effective order (global < project < task_role)."""
        scopes: list[tuple[str, str]] = [("global", "global")]
        if project_id:
            scopes.append(("project", project_id))
        # Direct human Task+role overrides project/global. Empty task_role falls
        # back to legacy task-scoped rows without fabricating defaults.
        if role_id:
            task_role = self.get(scope="task_role", scope_id=f"{task_id}:{role_id}")
            if task_role["present"] and not task_role["empty"]:
                scopes.append(("task_role", f"{task_id}:{role_id}"))
            else:
                scopes.append(("task", task_id))
        else:
            scopes.append(("task", task_id))
        return [self.get(scope=scope, scope_id=scope_id) for scope, scope_id in scopes]

    def list_inventory(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        role_id: str | None = None,
        role_kind: str | None = None,
    ) -> dict[str, Any]:
        """Read-only inventory: mutable scopes + bundled packs. No secrets/history."""
        items: list[dict[str, Any]] = [self.get(scope="global", scope_id="global")]
        if project_id:
            items.append(self.get(scope="project", scope_id=project_id))
        if task_id and role_id:
            items.append(self.get(scope="task_role", scope_id=f"{task_id}:{role_id}"))
        elif task_id:
            items.append(self.get(scope="task", scope_id=task_id))
        resolved_role = role_kind or (self._role_kind(role_id) if role_id else None)
        rules = self._development_rules()
        baseline = behavior_baseline_for_role(
            resolved_role, rules=rules, config_source=_DEV_CONFIG_SOURCE,
        )
        bundled = [
            {
                "scope": pack["scope"],
                "scope_id": pack["scope_id"],
                "kind": pack["kind"],
                "source": pack["source"],
                "revision": pack["revision"],
                "version": pack.get("version"),
                "present": True,
                "empty": False,
                "mandatory": pack.get("mandatory"),
                "reserve_bytes": pack.get("reserve_bytes"),
                "config_source": pack.get("config_source"),
                "applicable_roles": pack.get("applicable_roles"),
                "content_preview": _preview(pack["content"]),
                "model_invoked": False,
                "license": pack.get("license"),
            }
            for pack in bundled_behavior_packs(
                rules=rules, config_source=_DEV_CONFIG_SOURCE,
            )
        ]
        return {
            "mutable_conventions": [
                {
                    "scope": item["scope"],
                    "scope_id": item["scope_id"],
                    "kind": "mutable_convention",
                    "source": f"conventions:{item['scope']}:{item['scope_id']}",
                    "revision": item["revision"],
                    "present": item["present"],
                    "empty": item["empty"],
                    "content_preview": _preview(item["content"]) if item["present"] else "",
                    "updated_at": item.get("updated_at"),
                }
                for item in items
            ],
            "bundled_behaviors": bundled,
            "behavior_baseline": baseline,
            "precedence": [
                "task_role", "project", "global", "rule_library_baseline",
            ],
            "model_invoked": False,
        }

    def effective_context(
        self,
        *,
        project_id: str | None,
        task_id: str,
        role_id: str | None = None,
        role_kind: str | None = None,
    ) -> dict[str, Any]:
        """One view that distinguishes product protocol packs from mutable conventions."""
        mutable = self.resolve(project_id=project_id, task_id=task_id, role_id=role_id)
        resolved_role = role_kind or (self._role_kind(role_id) if role_id else None)
        rules = self._development_rules()
        baseline = behavior_baseline_for_role(
            resolved_role, rules=rules, config_source=_DEV_CONFIG_SOURCE,
        )
        layers: list[dict[str, Any]] = []
        order = 0
        order += 1
        layers.append({
            **baseline,
            "effective_order": order,
            "content_preview": (
                _preview(str(baseline["content"]))
                if baseline.get("inject")
                else ""
            ),
        })
        for item in mutable:
            order += 1
            layers.append({
                "id": f"{item['scope']}:{item['scope_id']}",
                "kind": "mutable_convention",
                "scope": item["scope"],
                "scope_id": item["scope_id"],
                "source": f"conventions:{item['scope']}:{item['scope_id']}",
                "revision": item["revision"],
                "present": item["present"],
                "empty": item["empty"],
                "content": item["content"],
                "content_preview": _preview(item["content"]) if item["present"] else "",
                "protected": False,
                "mandatory": item["scope"] in {"task", "task_role"},
                "trim_priority": {
                    "global": 1,
                    "project": 4,
                    "task": 5,
                    "task_role": 5,
                }[item["scope"]],
                "effective_order": order,
                "inject": bool(item["content"]),
                "model_invoked": False,
            })
        return {
            "project_id": project_id,
            "task_id": task_id,
            "role_id": role_id,
            "role": resolved_role,
            "behavior_baseline": baseline,
            "precedence": [
                "task_role", "project", "global", "rule_library_baseline",
            ],
            "layers": layers,
            "empty_scopes": [
                layer["scope"]
                for layer in layers
                if layer["kind"] == "mutable_convention" and layer["empty"]
            ],
            "model_invoked": False,
        }

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

    def _role_kind(self, role_id: str) -> str | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT kind FROM roles WHERE id = ?", (role_id,)
            ).fetchone()
            if row is None:
                return None
            return str(row["kind"]).split(":", 1)[0]
        finally:
            connection.close()

    @staticmethod
    def _empty(*, scope: str, scope_id: str) -> dict[str, Any]:
        return {
            "scope": scope,
            "scope_id": scope_id,
            "content": "",
            "revision": 0,
            "updated_at": None,
            "present": False,
            "empty": True,
            "kind": "mutable_convention",
            "source": f"conventions:{scope}:{scope_id}",
        }

    @staticmethod
    def _present(row: dict[str, Any]) -> dict[str, Any]:
        content = str(row.get("content") or "")
        return {
            **row,
            "present": True,
            "empty": not bool(content.strip()),
            "kind": "mutable_convention",
            "source": f"conventions:{row['scope']}:{row['scope_id']}",
        }

    @staticmethod
    def _validate_scope(scope: str, scope_id: str) -> None:
        if scope not in CONVENTION_SCOPES:
            raise DomainError(f"unsupported convention scope: {scope}")
        if scope == "global" and scope_id != "global":
            raise DomainError("global convention scope_id must be global")
        if scope == "task_role" and ":" not in scope_id:
            raise DomainError("task_role scope_id must be task_id:role_id")


def _preview(content: str, limit: int = 480) -> str:
    text = content.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
