"""Unified Provider result ingestion.

Evidence is not limited to Cursor-shaped stdout. json-worker sessions may place
structured markers in assistant/tool messages; Bridge harvest appends those into
stdout, and this module is the control-plane contract for reading them.
"""

from __future__ import annotations

import json
from typing import Any

from .provider import provider_agent_text

STRUCTURED_MARKERS = (
    "PLOWWHIP_PLANNER_RESULT ",
    "PLOWWHIP_CHECKER_RESULT ",
)


def _payload_candidates(rest: str) -> list[str]:
    """Yield parse attempts for marker payloads (plain or JSON-string-escaped)."""
    text = rest.lstrip()
    if not text:
        return []
    attempts = [text]
    # Cursor/CLI envelopes often leave {\\"verdict\\":...} inside one stdout line.
    if '\\"' in text[:80] or text.startswith('{\\"'):
        attempts.append(
            text.replace("\\\\", "\0")
            .replace('\\"', '"')
            .replace("\0", "\\")
        )
    return attempts


def extract_structured_json(text: str, marker: str) -> dict[str, Any] | None:
    """Find the last successfully decoded JSON object after ``marker``.

    Compatible shapes:
    - whole line is ``MARKER {json}``
    - marker embedded mid-line in a CLI JSONL envelope
    - escaped JSON body (``{\\"verdict\\":...}``) beside a clean unescaped copy
    """
    if not text or not marker:
        return None
    decoder = json.JSONDecoder()
    # Walk from the end so a trailing escaped duplicate does not hide an
    # earlier clean PASS (LIVE-DS-24 / Cursor Checker).
    haystack = text if isinstance(text, str) else ""
    start = len(haystack)
    while start > 0:
        index = haystack.rfind(marker, 0, start)
        if index < 0:
            break
        rest = haystack[index + len(marker) :]
        for attempt in _payload_candidates(rest):
            if not attempt.startswith("{"):
                continue
            try:
                value, _end = decoder.raw_decode(attempt)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value:
                return value
        start = index
    return None


def structured_marker_lines(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for marker in STRUCTURED_MARKERS:
        payload = extract_structured_json(text, marker)
        if not isinstance(payload, dict):
            continue
        candidate = marker + json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )
        if candidate not in seen:
            seen.add(candidate)
            found.append(candidate)
    # Also keep any clean whole-line markers for backward-compatible tests.
    for line in text.splitlines():
        stripped = line.strip()
        for marker in STRUCTURED_MARKERS:
            index = stripped.find(marker)
            if index < 0:
                continue
            candidate = stripped[index:].strip()
            payload = candidate[len(marker) :].lstrip()
            if (
                candidate.startswith(marker)
                and payload.startswith("{")
                and "\\" not in payload[:20]
                and candidate not in seen
            ):
                # Prefer raw_decode-bounded clean form when possible.
                decoded = extract_structured_json(candidate, marker)
                if isinstance(decoded, dict):
                    clean = marker + json.dumps(
                        decoded, ensure_ascii=False, separators=(",", ":")
                    )
                    if clean not in seen:
                        seen.add(clean)
                        found.append(clean)
                else:
                    seen.add(candidate)
                    found.append(candidate)
            break
    return found


def ingest_provider_result(stdout: str) -> dict[str, object]:
    """Return first-class Evidence derived from Provider stdout / harvest.

    Sources may include raw stdout lines and JSONL assistant/tool messages.
    Marker lines are always folded into ``agent_text`` so Planner/Checker parsers
    do not depend on a single stdout shape.
    """
    raw = stdout if isinstance(stdout, str) else ""
    agent_text = provider_agent_text(raw)
    markers = structured_marker_lines(raw)
    if not markers:
        markers = structured_marker_lines(agent_text)
    pieces = [agent_text.strip()] if agent_text.strip() else []
    for marker in markers:
        if marker not in agent_text:
            pieces.append(marker)
    merged = "\n".join(piece for piece in pieces if piece).strip() or raw
    sources = ["stdout"]
    if markers:
        sources.append("structured_marker")
    if agent_text != raw:
        sources.append("jsonl_message")
    return {
        "agent_text": merged,
        "markers": markers,
        "marker_count": len(markers),
        "has_structured_marker": bool(markers),
        "sources": sources,
    }
