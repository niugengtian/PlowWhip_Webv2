from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from .cronner import run as run_cronner
from .intake import PROJECT_ID, TASK_ID, submit_action, submit_message
from .monitor import projects_snapshot, snapshot, task_snapshot
from .store import Store


MAX_BODY_BYTES = 65_536


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            connection = self.server.store.connect_readonly()  # type: ignore[attr-defined]
            try:
                connection.execute("SELECT 1").fetchone()
            finally:
                connection.close()
            self._send(200, {"status": "ok"})
            return
        if path == "/api/projects":
            store = self.server.store  # type: ignore[attr-defined]
            self._send(200, projects_snapshot(store.db_path, store.data_root))
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
            body = self._body()
            store = self.server.store  # type: ignore[attr-defined]
            if self.path == "/api/messages":
                identifier = submit_message(
                    store,
                    body["project_id"],
                    body["content"],
                    body["idempotency_key"],
                )
            elif self.path == "/api/actions":
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
        self._send(202, {"message_id": identifier})

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
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(store: Store, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.store = store  # type: ignore[attr-defined]
    server.daemon_threads = True
    return server


def serve(store: Store, host: str, port: int, interval_seconds: float) -> None:
    if interval_seconds <= 0:
        raise ValueError("cronner interval must be positive")
    store.initialize()
    server = make_server(store, host, port)
    stop = threading.Event()
    cronner = threading.Thread(
        target=run_cronner, args=(store, stop, interval_seconds), daemon=True
    )
    cronner.start()
    try:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        stop.set()
        server.server_close()
        cronner.join()
