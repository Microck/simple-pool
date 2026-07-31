"""Core failover routing logic."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .state import PoolState

logger = logging.getLogger("simple_pool")

QUOTA_PHRASES = ("usage limit", "rate limit", "quota", "reached", "exceeded")


def is_quota_failure(status: int, body: bytes) -> bool:
    """Detect quota/rate-limit failures from status and response body."""
    if status == 429:
        try:
            text = body.decode("utf-8", errors="replace").lower()
            return any(phrase in text for phrase in QUOTA_PHRASES)
        except Exception:
            return True
    return False


def upstream_request(
    backend: dict[str, Any],
    body: bytes,
    model_override: str | None,
    timeout: int,
) -> tuple[int, bytes, str, str]:
    """Send a chat-completions request to a single backend.

    Returns (status, body, content_type, backend_name).
    """
    request_data = json.loads(body)
    request_data["model"] = backend.get("model") or model_override or request_data.get("model", "")
    upstream_body = json.dumps(request_data).encode("utf-8")

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if backend.get("api_key"):
        headers["Authorization"] = f"Bearer {backend['api_key']}"

    url = f"{backend['base_url']}/v1/chat/completions"
    req = urllib.request.Request(url, data=upstream_body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (
                resp.status,
                resp.read(),
                resp.headers.get("Content-Type", "application/json"),
                backend["name"],
            )
    except urllib.error.HTTPError as e:
        return (
            e.code,
            e.read(),
            e.headers.get("Content-Type", "application/json"),
            backend["name"],
        )
    except (TimeoutError, OSError, urllib.error.URLError):
        error_body = json.dumps({"error": {"message": "upstream connection failed"}}).encode()
        return 503, error_body, "application/json", backend["name"]


def route_request(
    body: bytes,
    backends: list[dict[str, Any]],
    state: PoolState,
    config: dict[str, Any],
) -> tuple[int, bytes, str, dict[str, str]]:
    """Try backends in order until one succeeds or all fail.

    Returns (status, body, content_type, extra_headers).
    """
    failure_codes: set[int] = config.get("failure_codes", set())
    timeout: int = config.get("request_timeout", 120)
    cooldown: float = config.get("cooldown", 1200)
    model_override: str | None = config.get("model")

    available = state.available_order(backends)
    if not available:
        return (
            503,
            json.dumps(
                {
                    "error": {
                        "message": "all backends are unavailable",
                        "type": "server_error",
                        "reason": "cooldown active on all configured backends",
                    }
                }
            ).encode(),
            "application/json",
            {},
        )

    last_status = 503
    last_body = json.dumps({"error": {"message": "all backends failed"}}).encode()
    last_ct = "application/json"

    for idx, backend in available:
        status, resp_body, content_type, name = upstream_request(
            backend, body, model_override, timeout
        )

        if 200 <= status < 300:
            state.mark_success(name, idx)
            logger.info("success via %s", name)
            return status, resp_body, content_type, {"X-Simple-Pool-Backend": name}

        last_status = status
        last_body = resp_body
        last_ct = content_type

        should_fail = status in failure_codes or status >= 500
        if status == 429:
            should_fail = should_fail or is_quota_failure(status, resp_body)

        if should_fail:
            logger.warning("backend %s failed with %d, cooling down", name, status)
            state.mark_failure(name, cooldown)
            continue

        break

    return last_status, last_body, last_ct, {}
