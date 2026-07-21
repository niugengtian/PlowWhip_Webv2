#!/usr/bin/env python3
"""Validate, render and select project-scoped engineering ledger context.

Canonical source:
  docs/engineering-ledger/manifest.toml
  docs/engineering-ledger/POLICY.md
  docs/engineering-ledger/CURRENT.md
  docs/engineering-ledger/requirements/*.md
  docs/engineering-ledger/incidents/{open,archive}/**/*.md

The two top-level ledger Markdown files are generated views and must not be edited
directly after migration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import tomllib
except ModuleNotFoundError:
    print("engineering-ledger requires Python 3.11 or newer", file=sys.stderr)
    raise SystemExit(2)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "engineering-ledger"
MANIFEST_PATH = SOURCE / "manifest.toml"
HUMAN_PATH = ROOT / "docs" / "ENGINEERING_REQUIREMENTS_AND_INCIDENT_LEDGER.md"
MODEL_PATH = ROOT / "docs" / "ENGINEERING_MODEL_LEDGER.md"
ALLOWED_REQUIREMENT_STATUSES = {
    "REQUIREMENT_OPEN",
    "MITIGATED_UNVERIFIED",
    "VERIFIED_CLOSED",
    "SUPERSEDED",
}
ALLOWED_INCIDENT_STATUSES = {
    "USER_REPORTED",
    "VERIFIED_OPEN",
    "MITIGATED_UNVERIFIED",
    "VERIFIED_CLOSED",
    "REQUIREMENT_OPEN",
    "SUPERSEDED",
}


@dataclass(frozen=True)
class Entry:
    id: str
    kind: str
    title: str
    status: str
    revision: int
    domains: tuple[str, ...]
    model_summary: str
    body: str
    path: Path
    archived: bool

    @property
    def source_ref(self) -> str:
        return self.path.relative_to(ROOT).as_posix()


def manifest() -> dict[str, Any]:
    return tomllib.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _front_matter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    if not text.startswith("+++\n"):
        raise ValueError(f"{path}: missing TOML front matter")
    try:
        raw, body = text[4:].split("\n+++\n", 1)
    except ValueError as error:
        raise ValueError(f"{path}: unterminated TOML front matter") from error
    return tomllib.loads(raw), body.strip() + "\n"


def load_entries() -> list[Entry]:
    paths = sorted((SOURCE / "requirements").glob("*.md"))
    paths += sorted((SOURCE / "incidents" / "open").glob("*.md"))
    paths += sorted((SOURCE / "incidents" / "archive").glob("**/*.md"))
    entries: list[Entry] = []
    for path in paths:
        meta, body = _front_matter(path.read_text(encoding="utf-8"), path)
        entry_id = str(meta["id"])
        kind = str(meta["kind"])
        match = re.match(rf"^### {re.escape(entry_id)} ([^\n]+)", body)
        if not match:
            raise ValueError(f"{path}: body heading must start with '### {entry_id}'")
        entries.append(
            Entry(
                id=entry_id,
                kind=kind,
                title=match.group(1).strip(),
                status=str(meta["status"]),
                revision=int(meta["revision"]),
                domains=tuple(str(item) for item in meta.get("domains", [])),
                model_summary=str(meta["model_summary"]).strip(),
                body=body,
                path=path,
                archived="archive" in path.parts,
            )
        )
    return entries


def _entry_sort_key(entry: Entry) -> tuple[str, int]:
    prefix, number = entry.id.split("-", 1)
    return prefix, int(number)


def validate(data: dict[str, Any], entries: list[Entry]) -> list[str]:
    errors: list[str] = []
    if int(data.get("schema_version", 0)) != 1:
        errors.append("manifest schema_version must be 1")
    if not str(data.get("project_key", "")).strip():
        errors.append("manifest project_key is required")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\d+", str(data.get("ledger_revision", ""))):
        errors.append("manifest ledger_revision must match YYYY-MM-DD.N")

    by_id: dict[str, Entry] = {}
    for entry in entries:
        if entry.id in by_id:
            errors.append(f"duplicate entry id: {entry.id}")
        by_id[entry.id] = entry
        expected_kind = "requirement" if entry.id.startswith("R-") else "incident"
        if entry.kind != expected_kind:
            errors.append(f"{entry.source_ref}: kind must be {expected_kind}")
        if entry.path.stem != entry.id:
            errors.append(f"{entry.source_ref}: filename must be {entry.id}.md")
        allowed = (
            ALLOWED_REQUIREMENT_STATUSES
            if entry.kind == "requirement"
            else ALLOWED_INCIDENT_STATUSES
        )
        if entry.status not in allowed:
            errors.append(f"{entry.id}: invalid status {entry.status}")
        if entry.revision < 1:
            errors.append(f"{entry.id}: revision must be >= 1")
        if not entry.domains:
            errors.append(f"{entry.id}: at least one domain is required")
        if not entry.model_summary:
            errors.append(f"{entry.id}: model_summary is required")
        if entry.archived and entry.status not in {"VERIFIED_CLOSED", "SUPERSEDED"}:
            errors.append(f"{entry.id}: archived incident must be closed or superseded")
        if (
            entry.kind == "incident"
            and not entry.archived
            and entry.status in {"VERIFIED_CLOSED", "SUPERSEDED"}
        ):
            errors.append(f"{entry.id}: closed incident must move to incidents/archive")

    routed: set[str] = set(str(item) for item in data.get("always_load", []))
    route_domains: set[str] = set()
    for route in data.get("routes", []):
        domain = str(route.get("domain", "")).strip()
        if not domain or domain in route_domains:
            errors.append(f"duplicate or empty route domain: {domain!r}")
        route_domains.add(domain)
        for entry_id in route.get("ids", []):
            entry_id = str(entry_id)
            routed.add(entry_id)
            if entry_id not in by_id:
                errors.append(f"route {domain}: unknown id {entry_id}")
    for entry_id in data.get("always_load", []):
        if str(entry_id) not in by_id:
            errors.append(f"always_load: unknown id {entry_id}")
    for entry in entries:
        if not entry.archived and entry.id not in routed:
            errors.append(f"{entry.id}: active entry is not reachable from any route")
        unknown_domains = set(entry.domains) - route_domains
        if unknown_domains:
            errors.append(f"{entry.id}: unknown domains {sorted(unknown_domains)}")
    return errors


def _generated_header(data: dict[str, Any]) -> str:
    return (
        "# PlowWhip Web 工程需求与故障账本\n\n"
        "> 本文是结构化项目台账的自动生成人类视图；禁止直接编辑。完整真源位于 "
        "`docs/engineering-ledger/`。\n"
        ">\n"
        f"> **ledger revision：** `{data['ledger_revision']}`  \n"
        "> **模型视图：** [`ENGINEERING_MODEL_LEDGER.md`](ENGINEERING_MODEL_LEDGER.md)。"
        "两份视图由同一真源一次生成，不再手工同步。\n\n"
    )


def render_human(data: dict[str, Any], entries: list[Entry]) -> str:
    policy = (SOURCE / "POLICY.md").read_text(encoding="utf-8").strip()
    current = (SOURCE / "CURRENT.md").read_text(encoding="utf-8").strip()
    requirements = sorted(
        (entry for entry in entries if entry.kind == "requirement"),
        key=_entry_sort_key,
    )
    open_incidents = sorted(
        (
            entry
            for entry in entries
            if entry.kind == "incident" and not entry.archived
        ),
        key=_entry_sort_key,
    )
    archived = sorted(
        (entry for entry in entries if entry.kind == "incident" and entry.archived),
        key=_entry_sort_key,
    )
    blocks = [
        _generated_header(data).rstrip(),
        policy,
        *(entry.body.strip() for entry in requirements),
        "## 4. 故障账本",
        *(entry.body.strip() for entry in open_incidents),
    ]
    if archived:
        blocks.extend(
            [
                "## 4A. 已归档故障",
                *(entry.body.strip() for entry in archived),
            ]
        )
    blocks.append(current)
    return "\n\n".join(blocks).rstrip() + "\n"


def _escape_cell(value: str) -> str:
    return " ".join(value.replace("|", "\\|").split())


def render_model(data: dict[str, Any], entries: list[Entry]) -> str:
    by_id = {entry.id: entry for entry in entries}
    core = [by_id[str(entry_id)] for entry_id in data.get("always_load", [])]
    open_incidents = [
        entry
        for entry in entries
        if entry.kind == "incident" and not entry.archived
    ]
    status_counts: dict[str, int] = {}
    for entry in open_incidents:
        status_counts[entry.status] = status_counts.get(entry.status, 0) + 1
    lines = [
        "# PlowWhip Web 模型执行台账",
        "",
        "> 自动生成的路由索引；禁止直接编辑。真源：`docs/engineering-ledger/`。",
        "",
        "```yaml",
        f"project: {data['project_name']}",
        f"project_key: {data['project_key']}",
        f"ledger_revision: {data['ledger_revision']}",
        "scope: this project only",
        "```",
        "",
        "## LOAD",
        "",
        "1. 重大改造前完整读取本文件。",
        "2. 从 `ROUTE` 选择最具体领域，用 `context --domains` 生成 Task 专属包；不要读取 OPEN 全集。",
        "3. 只有 Task 包中的条目才读取独立真源正文；现场另行核对 Git/DB/image/活动执行。",
        "4. 超过 Context 上限时缩小领域、指定条目或拆 Task，禁止截断必需合同。",
        "5. TaskSpec 保存入选 ID、entry/ledger revision、hash、领域和选择原因。",
        "",
        "## CORE",
        "",
        "| ID | rev | 执行摘要 |",
        "| --- | ---: | --- |",
    ]
    for entry in core:
        lines.append(
            f"| {entry.id} | {entry.revision} | {_escape_cell(entry.model_summary)} |"
        )
    lines.extend(
        [
            "",
            "## ACTIVE INDEX",
            "",
            f"- active incidents: `{len(open_incidents)}`",
            "- status counts: `"
            + ", ".join(f"{key}={value}" for key, value in sorted(status_counts.items()))
            + "`",
            f"- Task Context limits: `{data['context_max_entries']} entries / "
            f"{data['context_max_chars']} summary chars`",
            "- 具体问题只在路由后的 Task Context Pack 中展开。",
        ]
    )
    lines.extend(
        [
            "",
            "## ROUTE",
            "",
            "| domain | 说明 | 必读条目 |",
            "| --- | --- | --- |",
        ]
    )
    for route in data.get("routes", []):
        lines.append(
            f"| {route['domain']} | {_escape_cell(str(route['description']))} | "
            f"{','.join(str(item) for item in route['ids'])} |"
        )
    lines.extend(
        [
            "",
            "## FORBIDDEN",
            "",
            "- 不用 queued/accepted/heartbeat/模型声明/单独 exit 0 证明完成。",
            "- 不把 CHANGES_REQUIRED、网络或 Provider 故障写成 PASS。",
            "- 不通过重复 Task、特殊状态、no-op 改文件或人工改库推进流程。",
            "- 不重放旧聊天、完整日志、完整 DOM 或跨项目 ledger。",
            "- 不在活动 Host Job/模型调用期间重启、部署或迁移。",
            "- 不把工作树、提交、远端、image 和运行数据库称为同一版本。",
            "",
            "## SOURCE",
            "",
            f"- manifest revision: `{data['ledger_revision']}`",
            "- `scripts/engineering_ledger.py check` 验证真源和两个生成视图一致。",
            "- `scripts/engineering_ledger.py context --domains <domain,...>` 生成 Task 最小上下文。",
            "- 新事故写入 `incidents/open/`；关闭后移入 `incidents/archive/YYYY/`，模型视图不再加载正文。",
            "",
        ]
    )
    return "\n".join(lines)


def _toml_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _status(body: str, kind: str) -> str:
    if kind == "requirement":
        match = re.search(r"\*\*状态：\*\*\s*`([^`]+)`", body)
        if not match:
            raise ValueError("entry status not found")
        return match.group(1)
    else:
        line = next(
            (
                item
                for item in body.splitlines()
                if item.startswith("- **日期/来源/状态：**")
            ),
            "",
        )
        values = re.findall(r"`([^`]+)`", line)
        if not values:
            raise ValueError("entry status not found")
        return values[-1]


def _summary(body: str, kind: str) -> str:
    if kind == "incident":
        match = re.search(r"^- \*\*故障现象：\*\*\s*(.+)$", body, re.MULTILINE)
        if not match:
            raise ValueError("incident phenomenon not found")
        return match.group(1).strip()
    for line in body.splitlines():
        if line.startswith("- ") and "**状态：**" not in line:
            return line[2:].strip()
    raise ValueError("requirement summary not found")


def _sections(block: str, prefix: str) -> list[tuple[str, str]]:
    pattern = re.compile(
        rf"(?ms)^(### ({prefix}-\d+) .+?\n.*?)(?=^### {prefix}-\d+ |\Z)"
    )
    return [(match.group(2), match.group(1).strip() + "\n") for match in pattern.finditer(block)]


def migrate(data: dict[str, Any]) -> None:
    requirements_dir = SOURCE / "requirements"
    incidents_dir = SOURCE / "incidents" / "open"
    if any(requirements_dir.glob("*.md")) or any(incidents_dir.glob("*.md")):
        raise ValueError("structured entries already exist; migration is one-time only")
    text = HUMAN_PATH.read_text(encoding="utf-8")
    policy_start = text.index("## 1. 强制使用规则")
    first_requirement = text.index("### R-001 ")
    incidents_heading = text.index("## 4. 故障账本")
    first_incident = text.index("### I-001 ")
    current_start = text.index("## 5. 当前不可忽略的开放风险")
    policy = text[policy_start:first_requirement].strip() + "\n"
    requirement_block = text[first_requirement:incidents_heading]
    incident_block = text[first_incident:current_start]
    current = text[current_start:].strip() + "\n"

    requirements_dir.mkdir(parents=True, exist_ok=True)
    incidents_dir.mkdir(parents=True, exist_ok=True)
    (SOURCE / "incidents" / "archive").mkdir(parents=True, exist_ok=True)
    (SOURCE / "POLICY.md").write_text(policy, encoding="utf-8")
    (SOURCE / "CURRENT.md").write_text(current, encoding="utf-8")

    route_domains: dict[str, set[str]] = {}
    for route in data.get("routes", []):
        for entry_id in route["ids"]:
            route_domains.setdefault(str(entry_id), set()).add(str(route["domain"]))
    for entry_id in data.get("always_load", []):
        route_domains.setdefault(str(entry_id), set())

    records = [
        ("requirement", requirements_dir, _sections(requirement_block, "R")),
        ("incident", incidents_dir, _sections(incident_block, "I")),
    ]
    for kind, directory, sections in records:
        for entry_id, body in sections:
            domains = sorted(route_domains.get(entry_id, set()))
            front = "\n".join(
                [
                    "+++",
                    f"id = {_toml_value(entry_id)}",
                    f"kind = {_toml_value(kind)}",
                    f"status = {_toml_value(_status(body, kind))}",
                    "revision = 1",
                    f"domains = {_toml_value(domains)}",
                    f"model_summary = {_toml_value(_summary(body, kind))}",
                    "+++",
                    "",
                ]
            )
            (directory / f"{entry_id}.md").write_text(front + body, encoding="utf-8")


def _write_views(data: dict[str, Any], entries: list[Entry]) -> None:
    HUMAN_PATH.write_text(render_human(data, entries), encoding="utf-8")
    MODEL_PATH.write_text(render_model(data, entries), encoding="utf-8")


def _check_views(data: dict[str, Any], entries: list[Entry]) -> list[str]:
    errors: list[str] = []
    expected = {
        HUMAN_PATH: render_human(data, entries),
        MODEL_PATH: render_model(data, entries),
    }
    for path, content in expected.items():
        if not path.exists():
            errors.append(f"missing generated view: {path.relative_to(ROOT)}")
        elif path.read_text(encoding="utf-8") != content:
            errors.append(
                f"stale generated view: {path.relative_to(ROOT)}; run render"
            )
    return errors


def context_pack(
    data: dict[str, Any],
    entries: list[Entry],
    domains: Iterable[str],
    direct_ids: Iterable[str],
) -> dict[str, Any]:
    requested_domains = {item.strip() for item in domains if item.strip()}
    selected = {str(item) for item in data.get("always_load", [])}
    reasons: dict[str, set[str]] = {
        entry_id: {"always_load"} for entry_id in selected
    }
    known_domains = {str(route["domain"]) for route in data.get("routes", [])}
    unknown = requested_domains - known_domains
    if unknown:
        raise ValueError(f"unknown domains: {sorted(unknown)}")
    for route in data.get("routes", []):
        domain = str(route["domain"])
        if domain not in requested_domains:
            continue
        for entry_id in route["ids"]:
            selected.add(str(entry_id))
            reasons.setdefault(str(entry_id), set()).add(f"domain:{domain}")
    for entry_id in direct_ids:
        selected.add(entry_id)
        reasons.setdefault(entry_id, set()).add("direct")
    by_id = {entry.id: entry for entry in entries}
    unknown_ids = selected - by_id.keys()
    if unknown_ids:
        raise ValueError(f"unknown entry ids: {sorted(unknown_ids)}")
    items = []
    for entry_id in sorted(selected, key=lambda item: (item[0], int(item[2:]))):
        entry = by_id[entry_id]
        items.append(
            {
                "id": entry.id,
                "kind": entry.kind,
                "status": entry.status,
                "revision": entry.revision,
                "domains": list(entry.domains),
                "summary": entry.model_summary,
                "source": entry.source_ref,
                "reasons": sorted(reasons[entry_id]),
                "sha256": hashlib.sha256(entry.path.read_bytes()).hexdigest(),
            }
        )
    summary_chars = sum(len(str(item["summary"])) for item in items)
    max_entries = int(data["context_max_entries"])
    max_chars = int(data["context_max_chars"])
    if len(items) > max_entries or summary_chars > max_chars:
        raise ValueError(
            "context selection exceeds configured limit "
            f"({len(items)}/{max_entries} entries, "
            f"{summary_chars}/{max_chars} summary chars); "
            "choose a more specific domain, direct ids, or split the Task"
        )
    return {
        "schema_version": 1,
        "project_key": data["project_key"],
        "ledger_revision": data["ledger_revision"],
        "domains": sorted(requested_domains),
        "summary_chars": summary_chars,
        "limits": {"entries": max_entries, "summary_chars": max_chars},
        "entries": items,
    }


def _context_markdown(pack: dict[str, Any]) -> str:
    lines = [
        f"# Task ledger context: {pack['project_key']}",
        "",
        f"- ledger_revision: `{pack['ledger_revision']}`",
        f"- domains: `{','.join(pack['domains']) or 'core-only'}`",
        f"- selected: `{len(pack['entries'])}/{pack['limits']['entries']} entries`",
        f"- summary_chars: `{pack['summary_chars']}/{pack['limits']['summary_chars']}`",
        "",
    ]
    for entry in pack["entries"]:
        lines.extend(
            [
                f"## {entry['id']} · {entry['status']} · rev {entry['revision']}",
                "",
                entry["summary"],
                "",
                f"- source: `{entry['source']}`",
                f"- selected_by: `{','.join(entry['reasons'])}`",
                f"- sha256: `{entry['sha256']}`",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("migrate", help="one-time import from the human Markdown")
    subparsers.add_parser("render", help="render both Markdown views from source")
    subparsers.add_parser("check", help="validate source and generated views")
    context_parser = subparsers.add_parser(
        "context", help="emit a bounded Task ledger context pack"
    )
    context_parser.add_argument("--domains", default="")
    context_parser.add_argument("--ids", default="")
    context_parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    args = parser.parse_args()

    try:
        data = manifest()
        if args.command == "migrate":
            migrate(data)
        entries = load_entries()
        errors = validate(data, entries)
        if errors:
            raise ValueError("\n".join(errors))
        if args.command in {"migrate", "render"}:
            _write_views(data, entries)
        elif args.command == "check":
            errors = _check_views(data, entries)
            if errors:
                raise ValueError("\n".join(errors))
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "project_key": data["project_key"],
                        "ledger_revision": data["ledger_revision"],
                        "entries": len(entries),
                        "open_incidents": sum(
                            entry.kind == "incident" and not entry.archived
                            for entry in entries
                        ),
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "context":
            pack = context_pack(
                data,
                entries,
                args.domains.split(",") if args.domains else (),
                args.ids.split(",") if args.ids else (),
            )
            if args.format == "json":
                print(json.dumps(pack, ensure_ascii=False, indent=2))
            else:
                print(_context_markdown(pack), end="")
    except (KeyError, OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"engineering-ledger: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
