from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path, PurePosixPath
from typing import Mapping
from uuid import uuid4

from .intake import canonical_json
from .store import Store


ARTIFACT_KINDS = {"output", "evidence", "handoff", "log"}
SCOPE_KINDS = {"task_data", "workspace"}


def result_coverage(spec: object) -> list[str]:
    contract = spec.get("task_contract") if isinstance(spec, dict) else None
    result = contract.get("result") if isinstance(contract, dict) else None
    coverage = result.get("coverage") if isinstance(result, dict) else None
    return (
        list(coverage)
        if isinstance(coverage, list)
        and all(isinstance(value, str) for value in coverage)
        else []
    )


def task_data_scope(coverage: list[str] | None = None) -> dict[str, object]:
    return {
        "kind": "task_data",
        "root": "data_root",
        "coverage": list(coverage or []),
    }


def workspace_scope(
    workspace_root: str, coverage: list[str]
) -> dict[str, object]:
    root = Path(workspace_root).resolve()
    if not root.is_absolute():
        raise ValueError("workspace Artifact root must be absolute")
    return {
        "kind": "workspace",
        "root": "project_workspace",
        "workspace_root": str(root),
        "coverage": list(coverage),
    }


def register_artifact(
    store: Store,
    connection: sqlite3.Connection,
    *,
    project_id: str,
    task_id: str,
    kind: str,
    path: Path,
    stored_path: str,
    acceptance_id: str | None,
    revision: int,
    scope: dict[str, object],
    source_task_id: str,
    created_at: float,
) -> dict[str, object]:
    if kind not in ARTIFACT_KINDS:
        raise ValueError("invalid Artifact kind")
    _validate_scope(scope)
    resolved = resolve_artifact_path(store, stored_path, scope)
    if resolved != path.resolve() or not resolved.is_file():
        raise ValueError("Artifact path does not resolve to a regular file")
    body = resolved.read_bytes()
    entry = {
        "kind": kind,
        "path": stored_path,
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
        "acceptance_id": acceptance_id,
        "revision": int(revision),
        "scope": scope,
        "source_task_id": source_task_id,
    }
    connection.execute(
        """
        INSERT INTO artifacts(
            id, project_id, task_id, kind, path, sha256, bytes,
            acceptance_id, revision, scope_json, source_task_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid4().hex,
            project_id,
            task_id,
            kind,
            stored_path,
            entry["sha256"],
            entry["bytes"],
            acceptance_id,
            revision,
            canonical_json(scope),
            source_task_id,
            created_at,
        ),
    )
    return entry


def register_indexed_artifact(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    task_id: str,
    kind: str,
    stored_path: str,
    sha256: str,
    bytes_count: int,
    acceptance_id: str | None,
    revision: int,
    scope: dict[str, object],
    source_task_id: str,
    created_at: float,
) -> dict[str, object]:
    if (
        kind not in ARTIFACT_KINDS
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
        or isinstance(bytes_count, bool)
        or not isinstance(bytes_count, int)
        or bytes_count < 0
    ):
        raise ValueError("invalid indexed Artifact facts")
    _validate_scope(scope)
    entry = {
        "kind": kind,
        "path": stored_path,
        "sha256": sha256,
        "bytes": bytes_count,
        "acceptance_id": acceptance_id,
        "revision": int(revision),
        "scope": scope,
        "source_task_id": source_task_id,
    }
    # LIVE-P1-02 / LIVE-DS-20: verify→continue re-executes at the same
    # spec_revision and re-indexes the same workspace Artifact path. A hard
    # INSERT crash rolled back finalize, left HostJob "running", and surfaced
    # as endless "Host Bridge poll unavailable".
    existing = connection.execute(
        """
        SELECT id FROM artifacts
        WHERE task_id = ? AND kind = ? AND path = ? AND revision = ?
        ORDER BY rowid DESC LIMIT 1
        """,
        (task_id, kind, stored_path, int(revision)),
    ).fetchone()
    if existing:
        connection.execute(
            """
            UPDATE artifacts
            SET project_id = ?, sha256 = ?, bytes = ?, acceptance_id = ?,
                scope_json = ?, source_task_id = ?, created_at = ?
            WHERE id = ?
            """,
            (
                project_id,
                sha256,
                bytes_count,
                acceptance_id,
                canonical_json(scope),
                source_task_id,
                created_at,
                existing["id"],
            ),
        )
        return entry
    connection.execute(
        """
        INSERT INTO artifacts(
            id, project_id, task_id, kind, path, sha256, bytes,
            acceptance_id, revision, scope_json, source_task_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid4().hex,
            project_id,
            task_id,
            kind,
            stored_path,
            sha256,
            bytes_count,
            acceptance_id,
            revision,
            canonical_json(scope),
            source_task_id,
            created_at,
        ),
    )
    return entry


def verified_artifact(
    store: Store,
    row: Mapping[str, object],
    *,
    expected_source_task_id: str | None = None,
    expected_revision: int | None = None,
) -> tuple[dict[str, object], bytes]:
    scope_raw = row["scope_json"]
    try:
        scope = json.loads(str(scope_raw))
    except json.JSONDecodeError as error:
        raise ValueError("Artifact scope is invalid") from error
    _validate_scope(scope)
    source_task_id = str(row["source_task_id"] or "")
    revision = int(row["revision"])
    if (
        expected_source_task_id is not None
        and source_task_id != expected_source_task_id
    ):
        raise ValueError("Artifact source Task does not match its consumer")
    if expected_revision is not None and revision != expected_revision:
        raise ValueError("Artifact revision does not match its consumer")
    path = resolve_artifact_path(store, str(row["path"]), scope)
    if not path.is_file():
        raise ValueError("Artifact file is missing")
    body = path.read_bytes()
    if len(body) != int(row["bytes"]):
        raise ValueError("Artifact byte count does not match its index")
    digest = hashlib.sha256(body).hexdigest()
    if digest != str(row["sha256"]):
        raise ValueError("Artifact SHA-256 does not match its index")
    return (
        {
            "kind": str(row["kind"]),
            "path": str(row["path"]),
            "sha256": digest,
            "bytes": len(body),
            "acceptance_id": row["acceptance_id"],
            "revision": revision,
            "scope": scope,
            "source_task_id": source_task_id,
        },
        body,
    )


def resolve_artifact_path(
    store: Store, stored_path: str, scope: Mapping[str, object]
) -> Path:
    _validate_scope(scope)
    if scope["kind"] == "task_data":
        return store.resolve_data_path(stored_path).resolve()
    relative = PurePosixPath(stored_path)
    if (
        relative.is_absolute()
        or relative.as_posix() != stored_path
        or ".." in relative.parts
    ):
        raise ValueError("workspace Artifact path must be safe and relative")
    root = Path(str(scope["workspace_root"])).resolve()
    target = (root / Path(*relative.parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError("workspace Artifact escapes its root") from error
    return target


def _validate_scope(scope: object) -> None:
    if not isinstance(scope, dict) or scope.get("kind") not in SCOPE_KINDS:
        raise ValueError("Artifact requires an explicit scope")
    coverage = scope.get("coverage")
    if (
        not isinstance(coverage, list)
        or any(not isinstance(value, str) or not value for value in coverage)
        or len(coverage) != len(set(coverage))
    ):
        raise ValueError("Artifact scope coverage is invalid")
    if scope["kind"] == "task_data":
        if scope.get("root") != "data_root":
            raise ValueError("task-data Artifact has an invalid root")
    elif (
        scope.get("root") != "project_workspace"
        or not isinstance(scope.get("workspace_root"), str)
        or not Path(str(scope["workspace_root"])).is_absolute()
    ):
        raise ValueError("workspace Artifact has an invalid root")
