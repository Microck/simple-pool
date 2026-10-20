"""HTTP server exposing /v1/models and /v1/chat/completions."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .router import route_request
from .state import PoolState

logger = logging.getLogger("simple_pool")


class PoolHandler(BaseHTTPRequestHandler):
    server_version = "simple-pool/0.1"

    config: dict[str, Any]
    backends: list[dict[str, Any]]
    state: PoolState

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug(format, *args)

    def _check_auth(self) -> bool:
        token = self.server.config.get("auth_token")  # type: ignore[attr-defined]
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {token}":
            return True
        self._send_json(401, {"error": {"message": "unauthorized", "type": "auth_error"}})
        return False

    def do_GET(self) -> None:
        if not self._check_auth():
            return
        if self.path.rstrip("/") == "/v1/models":
            model_id = self.server.config.get("model", "default")  # type: ignore[attr-defined]
            payload = {
                "object": "list",
                "data": [{"id": model_id, "object": "model", "owned_by": "simple-pool"}],
            }
            self._send_json(200, payload)
            return
        if self.path.rstrip("/") == "/health":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        if not self._check_auth():
            return
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": "not found"}})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError("negative Content-Length")
            body = self.rfile.read(length)
            if not isinstance(json.loads(body), dict):
                raise ValueError("request must be a JSON object")
        except (ValueError, json.JSONDecodeError):
            err = {"message": "invalid JSON request", "type": "invalid_request_error"}
            self._send_json(400, {"error": err})
            return

        config = self.server.config  # type: ignore[attr-defined]
        backends = config["backends"]
        state = self.server.state  # type: ignore[attr-defined]

        if not backends:
            err = {"message": "no backends configured", "type": "server_error"}
            self._send_json(503, {"error": err})
            return

        status, resp_body, content_type, extra = route_request(body, backends, state, config)
        self._send_bytes(status, resp_body, content_type, extra)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        self._send_bytes(status, json.dumps(payload).encode(), "application/json", {})

    def _send_bytes(
        self, status: int, body: bytes, content_type: str, extra: dict[str, str]
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


class PoolServer(ThreadingHTTPServer):
    def __init__(self, config: dict[str, Any], state: PoolState):
        self.config = config
        self.state = state
        host = config.get("host", "127.0.0.1")
        port = config.get("port", 8000)
        super().__init__((host, port), PoolHandler)
