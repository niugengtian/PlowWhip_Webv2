from __future__ import annotations

import ipaddress
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .butler import conversation, route_global_message, search
from .cronner import acquire_scheduler_lock, run as run_cronner
from .intake import (
    PROJECT_ID,
    TASK_ID,
    archive_project,
    create_project,
    set_project_rule,
    set_project_setting,
    submit_action,
    submit_message,
)
from .monitor import (
    monitor_snapshot,
    projects_snapshot,
    settings_library_snapshot,
    snapshot,
    task_snapshot,
    token_snapshot,
)
from .store import Store
from .ui import HTML


MAX_BODY_BYTES = 65_536


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if not self._local_host():
            self._send(400, {"error": "invalid host"})
            return
        path = urlsplit(self.path).path
        if path == "/":
            self._send_bytes(200, HTML.encode(), "text/html; charset=utf-8")
            return
        if path == "/health":
            connection = self.server.store.connect_readonly()  # type: ignore[attr-defined]
            try:
                connection.execute("SELECT 1").fetchone()
            finally:
                connection.close()
            self._send(
                200,
                {
                    "status": "ok",
                    "cronner": (
                        "enabled"
                        if getattr(self.server, "cronner_enabled", False)
                        else "disabled"
                    ),
                },
            )
            return
        if path == "/api/projects":
            store = self.server.store  # type: ignore[attr-defined]
            self._send(200, projects_snapshot(store.db_path, store.data_root))
            return
        if path == "/api/settings-library":
            store = self.server.store  # type: ignore[attr-defined]
            self._send(200, settings_library_snapshot(store.db_path, store.data_root))
            return
        if path == "/api/token":
            store = self.server.store  # type: ignore[attr-defined]
            self._send(200, token_snapshot(store.db_path, store.data_root))
            return
        if path == "/api/monitor":
            store = self.server.store  # type: ignore[attr-defined]
            self._send(200, monitor_snapshot(store.db_path, store.data_root))
            return
        if path == "/api/butler":
            store = self.server.store  # type: ignore[attr-defined]
            project_id = parse_qs(urlsplit(self.path).query).get(
                "project_id", [""]
            )[0]
            if not PROJECT_ID.fullmatch(project_id):
                self._send(400, {"error": "invalid project_id"})
                return
            self._send(
                200,
                conversation(store.db_path, store.data_root, project_id),
            )
            return
        if path == "/api/search":
            store = self.server.store  # type: ignore[attr-defined]
            values = parse_qs(urlsplit(self.path).query)
            try:
                result = search(
                    store.db_path,
                    store.data_root,
                    values.get("q", [""])[0],
                )
            except ValueError as error:
                self._send(400, {"error": str(error)})
                return
            self._send(200, result)
            return
        prefix = "/api/projects/"
        if path.startswith(prefix) and path != prefix:
            store = self.server.store  # type: ignore[attr-defined]
            project_id = unquote(path[len(prefix) :])
            if not PROJECT_ID.fullmatch(project_id):
                self._send(400, {"error": "invalid project_id"})
                return
            self._send(
                200,
                snapshot(store.db_path, store.data_root, project_id),
            )
            return
        prefix = "/api/tasks/"
        if path.startswith(prefix) and path != prefix:
            store = self.server.store  # type: ignore[attr-defined]
            task_id = unquote(path[len(prefix) :])
            if not TASK_ID.fullmatch(task_id):
                self._send(400, {"error": "invalid task_id"})
                return
            self._send(200, task_snapshot(store.db_path, store.data_root, task_id))
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:
        try:
            if not self._local_host() or not self._same_origin():
                raise ValueError("cross-origin write is not allowed")
            body = self._body()
            store = self.server.store  # type: ignore[attr-defined]
            path = urlsplit(self.path).path
            action_result = None
            if path == "/api/messages":
                routed = route_global_message(
                    store,
                    body["content"],
                    body["idempotency_key"],
                    body.get("project_id"),
                )
                identifier = routed["message_id"]
            elif path == "/api/actions":
                if body.get("kind") == "create_project":
                    action_result = create_project(
                        store,
                        body.get("project_id"),
                        body["idempotency_key"],
                        body.get("host_path"),
                        body.get("display_name"),
                    )
                    identifier = action_result["message_id"]
                elif body.get("kind") == "archive_project":
                    identifier = archive_project(
                        store,
                        body["project_id"],
                        body.get("confirmation", ""),
                        body["idempotency_key"],
                    )
                elif body.get("kind") == "set_project_setting":
                    identifier = set_project_setting(
                        store,
                        body["project_id"],
                        body["setting_key"],
                        body["value"],
                        body["idempotency_key"],
                    )
                elif body.get("kind") == "set_project_rule":
                    identifier = set_project_rule(
                        store,
                        body["project_id"],
                        body["rule_key"],
                        body["content"],
                        body["idempotency_key"],
                    )
                else:
                    identifier = submit_action(
                        store,
                        body["project_id"],
                        body["task_id"],
                        body["kind"],
                        body.get("instruction", ""),
                        body["idempotency_key"],
                        body.get("plan"),
                    )
            else:
                self._send(404, {"error": "not_found"})
                return
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._send(400, {"error": str(error)})
            return
        except sqlite3.Error:
            self._send(500, {"error": "store_error"})
            return
        response = {"message_id": identifier}
        if action_result:
            response.update(
                {
                    "project_id": action_result["project_id"],
                    "result": action_result["result"],
                }
            )
        if path == "/api/messages":
            response.update(
                {
                    "project_id": routed["project_id"],
                    "routed_only": routed["routed_only"],
                }
            )
        self._send(202, response)

    def _body(self) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("content-type must be application/json")
        size = int(self.headers.get("Content-Length", "0"))
        if size < 1 or size > MAX_BODY_BYTES:
            raise ValueError("body must contain 1-65536 bytes")
        body = json.loads(self.rfile.read(size))
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
        return body

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlsplit(origin)
        return parsed.scheme == "http" and parsed.netloc == self.headers.get("Host")

    def _local_host(self) -> bool:
        hostname = urlsplit("//" + self.headers.get("Host", "")).hostname
        if hostname == "localhost":
            return True
        try:
            return bool(hostname and ipaddress.ip_address(hostname).is_loopback)
        except ValueError:
            return False


def make_server(
    store: Store, host: str, port: int, allow_non_loopback: bool = False
) -> ThreadingHTTPServer:
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError as error:
        raise ValueError("V1 Web/API may bind only to localhost or a loopback IP") from error
    if not loopback and not allow_non_loopback:
        raise ValueError("V1 Web/API may bind only to a loopback address")
    server = ThreadingHTTPServer((host, port), Handler)
    server.store = store  # type: ignore[attr-defined]
    server.daemon_threads = True
    return server


def serve(
    store: Store,
    host: str,
    port: int,
    interval_seconds: float,
    allow_non_loopback: bool = False,
    cronner_enabled: bool = True,
) -> None:
    if interval_seconds <= 0:
        raise ValueError("cronner interval must be positive")
    store.initialize()
    server = make_server(store, host, port, allow_non_loopback)
    server.cronner_enabled = cronner_enabled  # type: ignore[attr-defined]
    stop = threading.Event()
    scheduler_lock = (
        acquire_scheduler_lock(store.data_root) if cronner_enabled else None
    )
    cronner = (
        threading.Thread(
            target=run_cronner,
            args=(store, stop, interval_seconds),
            daemon=True,
        )
        if cronner_enabled
        else None
    )
    if cronner:
        cronner.start()
    try:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        stop.set()
        server.server_close()
        if cronner:
            cronner.join()
        if scheduler_lock:
            scheduler_lock.close()
