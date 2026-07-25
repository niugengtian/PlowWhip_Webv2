"""Bounded local script runner used by the Host Bridge local-script adapter."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath


VERSION = "plowwhip-local-script 1"
MAX_SPEC_BYTES = 262_144
MAX_STDOUT_BYTES = 65_536
MAX_ARGV = 32
MAX_ARG_LEN = 512
SAFE_RELATIVE = re.compile(
    r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9][A-Za-z0-9._/-]{0,240}$"
)
ITEM_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ScriptRunError(RuntimeError):
    def __init__(self, message: str, code: str = "script_failed") -> None:
        super().__init__(message)
        self.code = code


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args or "-V" in args:
        print(VERSION)
        return 0
    raw = sys.stdin.buffer.read(MAX_SPEC_BYTES + 1)
    if not raw or len(raw) > MAX_SPEC_BYTES:
        _emit_error("prompt is empty or too large", "invalid_spec")
        return 2
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _emit_error("prompt is not valid JSON", "invalid_spec")
        return 2
    try:
        result = run_local_script(Path.cwd(), payload)
    except ScriptRunError as error:
        _emit_error(str(error), error.code)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


def run_local_script(project: Path, payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or payload.get("kind") != "local_script":
        raise ScriptRunError("local_script prompt kind is required", "invalid_spec")
    argv = payload.get("argv") or []
    if (
        not isinstance(argv, list)
        or len(argv) > MAX_ARGV
        or any(
            not isinstance(item, str) or len(item) > MAX_ARG_LEN for item in argv
        )
    ):
        raise ScriptRunError("argv must be a bounded string list", "invalid_spec")
    timeout_seconds = min(max(int(payload.get("timeout_seconds") or 120), 1), 3_600)
    script = payload.get("script")
    if not isinstance(script, dict):
        raise ScriptRunError("script contract is required", "invalid_spec")
    origin = str(script.get("origin") or "")
    if origin == "workspace":
        relative = str(script.get("path") or "")
        if not SAFE_RELATIVE.fullmatch(relative) or not relative.endswith(".py"):
            raise ScriptRunError("workspace script path is unsafe", "invalid_path")
        target = (project / PurePosixPath(relative)).resolve()
        try:
            target.relative_to(project.resolve())
        except ValueError as error:
            raise ScriptRunError(
                "workspace script escaped project root", "invalid_path"
            ) from error
        if not target.is_file():
            raise ScriptRunError("workspace script does not exist", "missing_script")
        script_ref = {"origin": "workspace", "path": relative}
    elif origin == "library":
        item_key = str(script.get("item_key") or "")
        revision = int(script.get("revision") or 0)
        body = str(script.get("body") or "")
        digest = str(script.get("sha256") or "")
        if (
            not ITEM_KEY.fullmatch(item_key)
            or revision < 1
            or not body
            or len(body.encode("utf-8")) > MAX_SPEC_BYTES
            or len(digest) != 64
        ):
            raise ScriptRunError("library script payload is incomplete", "invalid_spec")
        actual = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if actual != digest:
            raise ScriptRunError("library script body hash mismatch", "hash_mismatch")
        cache = project / ".plowwhip" / "library-cache"
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / f"{item_key}.revision-{revision:06d}.py"
        target.write_text(body, encoding="utf-8")
        os.chmod(target, 0o600)
        script_ref = {
            "origin": "library",
            "item_key": item_key,
            "revision": revision,
            "sha256": digest,
            "path": str(target.relative_to(project.resolve())),
        }
    else:
        raise ScriptRunError("unsupported script origin", "invalid_spec")

    started = time.time()
    completed = subprocess.run(
        [sys.executable, str(target), *argv],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
        env=_safe_environment(),
    )
    stdout = (completed.stdout or "")[:MAX_STDOUT_BYTES]
    stderr = (completed.stderr or "")[:MAX_STDOUT_BYTES]
    return {
        "kind": "local_script",
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "script": script_ref,
        "argv": list(argv),
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": int((time.time() - started) * 1000),
    }


def _safe_environment() -> dict[str, str]:
    allowed = {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TMP",
        "TEMP",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in allowed or key.startswith("PLOW_WHIP_")
    }
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def _emit_error(message: str, code: str) -> None:
    print(
        json.dumps(
            {
                "kind": "local_script",
                "ok": False,
                "error": {"code": code, "message": message},
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
