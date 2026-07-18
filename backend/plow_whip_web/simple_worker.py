"""Bounded DeepSeek worker implementing the Host Bridge JSON-worker contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

VERSION = "0.1.0"
DEFAULT_MODEL = "deepseek-v4-flash"
MAX_TOOL_OUTPUT = 16_000
MAX_SESSION_BYTES = 1_048_576
MAX_TURNS = 24
SENSITIVE_NAMES = {".env", ".env.local", ".env.production", "credentials.json"}
SENSITIVE_ENV = re.compile(r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)
FORBIDDEN_COMMANDS = {"rm", "sudo", "env", "printenv", "shutdown", "reboot"}
FORBIDDEN_GIT_ACTIONS = {
    "commit", "push", "merge", "rebase", "reset", "clean", "switch", "checkout",
}
ALLOWED_EXECUTABLES = {
    "python", "python3", "pytest", "git", "node", "npm", "npx", "pnpm",
    "yarn", "go", "cargo", "make", "ruff", "mypy", "eslint", "tsc",
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file", "description": "Read a UTF-8 project file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"}, "start_line": {"type": "integer"},
                    "max_lines": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files", "description": "List project files by glob.",
            "parameters": {
                "type": "object", "properties": {"pattern": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text", "description": "Search project text with ripgrep.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "path": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch", "description": "Apply one unified Git patch.",
            "parameters": {
                "type": "object", "properties": {"patch": {"type": "string"}},
                "required": ["patch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run one allowlisted command without a shell.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"}, "timeout": {"type": "integer"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff", "description": "Read the current Git diff.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class WorkerFailure(RuntimeError):
    pass


def credential_names(environ: dict[str, str] | None = None) -> list[str]:
    values = environ if environ is not None else os.environ
    pattern = re.compile(r"^DEEPSEEK_API_KEY(?:_\d+)?$")
    return sorted(name for name, value in values.items() if pattern.fullmatch(name) and value)


class DeepSeekClient:
    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self.environ = environ if environ is not None else dict(os.environ)
        self.key_names = credential_names(self.environ)
        self.model = self.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL)
        base = self.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        self.url = f"{base}/chat/completions"
        self.input_tokens = 0
        self.output_tokens = 0
        self.last_key_ref: str | None = None

    def chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.key_names:
            raise WorkerFailure("DEEPSEEK_API_KEY is not configured")
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "max_tokens": 4000,
            "thinking": {"type": "disabled"},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        errors: list[str] = []
        for name in self.key_names:
            secret = self.environ[name]
            self.last_key_ref = _key_ref(name, secret)
            request = Request(
                self.url, data=body, method="POST",
                headers={
                    "Authorization": f"Bearer {secret}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urlopen(request, timeout=120) as response:
                    result = json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")[:500]
                errors.append(f"{name}: HTTP {error.code} {detail}")
                continue
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
                errors.append(f"{name}: {type(error).__name__}: {error}")
                continue
            usage = result.get("usage") or {}
            self.input_tokens += int(usage.get("prompt_tokens") or 0)
            self.output_tokens += int(usage.get("completion_tokens") or 0)
            choices = result.get("choices") or []
            if not choices or not isinstance(choices[0].get("message"), dict):
                errors.append(f"{name}: response has no assistant message")
                continue
            return choices[0]["message"]
        raise WorkerFailure("; ".join(errors) or "DeepSeek key pool exhausted")


class SimpleWorker:
    def __init__(
        self, project: Path, session_id: str, client: DeepSeekClient | Any | None = None,
        state_root: Path | None = None,
    ) -> None:
        self.project = project.expanduser().resolve()
        if not self.project.is_dir():
            raise WorkerFailure(f"project is not a directory: {self.project}")
        self.session_id = _safe_id(session_id)
        project_key = hashlib.sha256(str(self.project).encode("utf-8")).hexdigest()[:16]
        root = state_root or Path(
            os.environ.get(
                "PLOW_WHIP_SIMPLE_WORKER_STATE_DIR",
                str(Path.home() / ".plow-whip-web" / "simple-worker"),
            )
        )
        self.state_dir = root.expanduser().resolve() / project_key
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.session_path = self.state_dir / f"{self.session_id}.jsonl"
        self.client = client or DeepSeekClient()

    def run(self, prompt: str) -> dict[str, Any]:
        if not prompt.strip():
            raise WorkerFailure("prompt is empty")
        self._append({"type": "message", "message": {"role": "user", "content": prompt}})
        format_retries = 0
        for turn in range(1, MAX_TURNS + 1):
            _emit({
                "type": "worker.progress", "session_id": self.session_id,
                "turn": turn, "input_tokens": self.client.input_tokens,
                "output_tokens": self.client.output_tokens,
            })
            assistant = self.client.chat(self._messages())
            message = {
                key: value for key, value in assistant.items()
                if key in {"role", "content", "tool_calls", "reasoning_content"}
            }
            message.setdefault("role", "assistant")
            self._append({"type": "message", "message": message})
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                for call in tool_calls:
                    function = call.get("function") or {}
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except (json.JSONDecodeError, TypeError) as error:
                        result = {"error": f"invalid tool arguments: {error}"}
                    else:
                        result = self._tool_result(str(function.get("name") or ""), arguments)
                    self._append({
                        "type": "message",
                        "message": {
                            "role": "tool", "tool_call_id": call.get("id"),
                            "content": json.dumps(result, ensure_ascii=False)[:MAX_TOOL_OUTPUT],
                        },
                    })
                continue
            final = _final_payload(str(message.get("content") or ""))
            if final:
                return self._result(final)
            format_retries += 1
            if format_retries >= 2:
                return self._result({
                    "status": "failed",
                    "reason": "model did not return the required structured status",
                })
            self._append({
                "type": "message",
                "message": {
                    "role": "user",
                    "content": "Return the required final JSON status, or continue using tools.",
                },
            })
        return self._result({
            "status": "failed", "reason": f"bounded tool loop exceeded {MAX_TURNS} turns",
        })

    def _result(self, result: dict[str, Any]) -> dict[str, Any]:
        return {
            **result, "session_id": self.session_id,
            "input_tokens": self.client.input_tokens,
            "output_tokens": self.client.output_tokens,
            "key_ref": self.client.last_key_ref,
        }

    def _messages(self) -> list[dict[str, Any]]:
        messages = [{
            "role": "system",
            "content": (
                "You are plow-whip simple-worker, a bounded low-cost implementation worker. "
                "Work only through the provided project tools. Never read secrets, escape the "
                "project, or manage Git branches, commits, pushes, resets, or checkouts. Inspect "
                "before editing and run relevant checks. For a trivial read-only or answer-only "
                "task, do not call tools: return the completed JSON directly. If work is "
                "architectural or ambiguous, "
                "return JSON {\"status\":\"needs_planner\",\"reason\":\"...\"}. When finished, "
                "return JSON {\"status\":\"completed\",\"summary\":\"...\","
                "\"verify_commands\":[\"...\"]}."
            ),
        }]
        for event in self._events():
            if event.get("type") == "message" and isinstance(event.get("message"), dict):
                messages.append(event["message"])
        while len(json.dumps(messages, ensure_ascii=False)) > 250_000 and len(messages) > 12:
            del messages[1]
        return [messages[0], *messages[-80:]] if len(messages) > 81 else messages

    def _events(self) -> list[dict[str, Any]]:
        if not self.session_path.is_file():
            return []
        events: list[dict[str, Any]] = []
        for line in self.session_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def _append(self, event: dict[str, Any]) -> None:
        _rotate(self.session_path)
        with self.session_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _safe_path(self, value: str) -> Path:
        if not value or "\x00" in value:
            raise WorkerFailure("invalid project path")
        candidate = (
            Path(value).expanduser().resolve()
            if Path(value).is_absolute() else (self.project / value).resolve()
        )
        if not candidate.is_relative_to(self.project):
            raise WorkerFailure(f"path escapes project: {value}")
        if candidate.name in SENSITIVE_NAMES or candidate.suffix in {".pem", ".key"}:
            raise WorkerFailure(f"sensitive path denied: {value}")
        return candidate

    def _tool_result(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "read_file":
                target = self._safe_path(str(arguments["path"]))
                start = max(1, int(arguments.get("start_line", 1)))
                limit = min(500, max(1, int(arguments.get("max_lines", 240))))
                lines = target.read_text(encoding="utf-8", errors="replace").splitlines(True)
                return {
                    "path": str(target.relative_to(self.project)), "start_line": start,
                    "content": "".join(lines[start - 1:start - 1 + limit]),
                }
            if name == "list_files":
                pattern = str(arguments.get("pattern") or "**/*")
                if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                    raise WorkerFailure("glob escapes project")
                files = [
                    str(path.relative_to(self.project)) for path in self.project.glob(pattern)
                    if path.is_file() and ".git" not in path.parts
                    and path.name not in SENSITIVE_NAMES and path.suffix not in {".pem", ".key"}
                ][:500]
                return {"files": sorted(files), "truncated": len(files) == 500}
            if name == "search_text":
                if not shutil.which("rg"):
                    raise WorkerFailure("rg is required")
                root = self._safe_path(str(arguments.get("path") or "."))
                completed = subprocess.run(
                    [
                        "rg", "-n", "--no-heading", "--color", "never",
                        "--glob", "!.env*", "--glob", "!*.pem", "--glob", "!*.key",
                        "--", str(arguments["query"]), str(root),
                    ],
                    cwd=self.project, capture_output=True, text=True, check=False,
                )
                return {
                    "returncode": completed.returncode,
                    "output": completed.stdout[-MAX_TOOL_OUTPUT:],
                }
            if name == "apply_patch":
                patch = str(arguments["patch"])
                paths = [
                    match.group(1) for match in re.finditer(
                        r"^(?:---|\+\+\+)\s+(?:[ab]/)?([^\t\n]+)", patch, re.MULTILINE
                    ) if match.group(1) != "/dev/null"
                ]
                if not paths:
                    raise WorkerFailure("patch does not name a project file")
                for value in paths:
                    self._safe_path(value)
                check = subprocess.run(
                    ["git", "apply", "--check", "--whitespace=nowarn", "-"],
                    cwd=self.project, input=patch, capture_output=True, text=True, check=False,
                )
                if check.returncode:
                    return {"applied": False, "error": check.stderr[-MAX_TOOL_OUTPUT:]}
                applied = subprocess.run(
                    ["git", "apply", "--whitespace=nowarn", "-"],
                    cwd=self.project, input=patch, capture_output=True, text=True, check=False,
                )
                return {
                    "applied": applied.returncode == 0,
                    "error": applied.stderr[-MAX_TOOL_OUTPUT:],
                }
            if name == "run_command":
                return self._run_command(
                    str(arguments["command"]), int(arguments.get("timeout", 120))
                )
            if name == "git_diff":
                completed = subprocess.run(
                    ["git", "diff", "--no-ext-diff"], cwd=self.project,
                    capture_output=True, text=True, check=False,
                )
                return {
                    "returncode": completed.returncode,
                    "output": completed.stdout[-MAX_TOOL_OUTPUT:],
                }
            return {"error": f"unknown tool: {name}"}
        except (OSError, ValueError, WorkerFailure) as error:
            return {"error": str(error)}

    def _run_command(self, command: str, timeout: int) -> dict[str, Any]:
        if any(char in command for char in ("|", ";", ">", "<", "`", "\n", "\r")):
            raise WorkerFailure("shell operators are not allowed")
        if "$(" in command or "${" in command:
            raise WorkerFailure("shell expansion is not allowed")
        argv = shlex.split(command)
        if not argv or argv[0] in FORBIDDEN_COMMANDS:
            raise WorkerFailure("command is not allowed")
        executable = Path(argv[0]).name
        if executable not in ALLOWED_EXECUTABLES:
            raise WorkerFailure(f"executable is not allowlisted: {executable}")
        if executable == "git" and len(argv) > 1 and argv[1] in FORBIDDEN_GIT_ACTIONS:
            raise WorkerFailure(f"Git lifecycle command is not allowed: {argv[1]}")
        if executable == "git" and (
            len(argv) < 2 or argv[1] not in {"status", "diff", "log", "show", "grep", "ls-files"}
        ):
            raise WorkerFailure("Git command is not read-only allowlisted")
        if executable in {"python", "python3"} and any(
            value in {"-c", "-"} for value in argv[1:]
        ):
            raise WorkerFailure("inline Python is not allowed")
        if executable == "node" and any(
            value in {"-e", "--eval", "-p", "--print"} for value in argv[1:]
        ):
            raise WorkerFailure("inline Node.js is not allowed")
        for value in argv[1:]:
            if value == ".." or value.startswith("../"):
                raise WorkerFailure("command argument escapes project")
            if Path(value).is_absolute():
                self._safe_path(value)
        environment = {
            name: value for name, value in os.environ.items()
            if not SENSITIVE_ENV.search(name)
        }
        try:
            completed = subprocess.run(
                argv, cwd=self.project, capture_output=True, text=True, check=False,
                timeout=min(600, max(1, timeout)), env=environment,
            )
        except subprocess.TimeoutExpired as error:
            return {"returncode": 124, "output": f"command timed out: {error}"}
        return {
            "returncode": completed.returncode,
            "output": (completed.stdout + completed.stderr)[-MAX_TOOL_OUTPUT:],
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plow Whip DeepSeek simple worker")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--session")
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.version:
        print(f"simple-worker {VERSION}")
        return
    if args.probe:
        names = credential_names()
        if not names:
            print("simple-worker 已安装，但缺少 DEEPSEEK_API_KEY", file=sys.stderr)
            raise SystemExit(2)
        model = os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL)
        print(f"simple-worker {VERSION} · {model} · {len(names)} credential slot(s)")
        return
    if args.project is None or not args.session:
        raise SystemExit("--project and --session are required")
    prompt = sys.stdin.read()
    session_id = _safe_id(args.session)
    _emit({"type": "session.started", "session_id": session_id})
    try:
        result = SimpleWorker(args.project, session_id).run(prompt)
    except WorkerFailure as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(78) from None
    _emit({"type": "worker.completed", **result})
    raise SystemExit(0 if result.get("status") == "completed" else 2)


def _final_payload(content: str) -> dict[str, Any] | None:
    candidates = [content.strip()]
    candidates.extend(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.I | re.S))
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", content):
        try:
            value, _end = decoder.raw_decode(content, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(json.dumps(value, ensure_ascii=False))
    for candidate in reversed(candidates):
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict) and value.get("status") in {
            "completed", "needs_planner", "failed",
        }:
            return value
    return None


def _safe_id(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.:-]", "_", value)[:160]
    return sanitized or str(uuid.uuid4())


def _key_ref(name: str, secret: str) -> str:
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]
    return f"{name.lower()}/****{secret[-4:]}/fp-{digest}"


def _rotate(path: Path) -> None:
    if not path.is_file() or path.stat().st_size < MAX_SESSION_BYTES:
        return
    for index in range(4, 0, -1):
        source = path.with_suffix(path.suffix + f".{index}")
        target = path.with_suffix(path.suffix + f".{index + 1}")
        if source.exists():
            source.replace(target)
    path.replace(path.with_suffix(path.suffix + ".1"))


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
