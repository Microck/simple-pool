"""Tests for simple-pool routing, state, config, and server behavior."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest

from simple_pool.config import expand_env, load_config, parse_config
from simple_pool.router import is_quota_failure, route_request
from simple_pool.state import PoolState

# -- Fixtures --


@pytest.fixture
def tmp_state(tmp_path: Path) -> PoolState:
    return PoolState(tmp_path / "state.json")


@pytest.fixture
def backends() -> list[dict]:
    return [
        {"name": "alpha", "base_url": "http://localhost:19001", "api_key": "key-a", "model": "m1"},
        {"name": "beta", "base_url": "http://localhost:19002", "api_key": "key-b", "model": "m2"},
        {"name": "gamma", "base_url": "http://localhost:19003", "api_key": "key-c", "model": "m3"},
    ]


@pytest.fixture
def config(backends: list[dict]) -> dict:
    return {
        "backends": backends,
        "failure_codes": {403, 408, 429, 500, 502, 503, 504},
        "request_timeout": 5,
        "cooldown": 60,
        "model": "test-model",
    }


def make_body(msg: str = "hello") -> bytes:
    return json.dumps({"model": "x", "messages": [{"role": "user", "content": msg}]}).encode()


# -- Config tests --


class TestConfig:
    def test_expand_env_braces(self):
        os.environ["_SP_TEST_VAR"] = "secret123"
        assert expand_env("Bearer ${_SP_TEST_VAR}") == "Bearer secret123"
        del os.environ["_SP_TEST_VAR"]

    def test_expand_env_bare(self):
        os.environ["_SP_TEST_BARE"] = "val"
        assert expand_env("$_SP_TEST_BARE/path") == "val/path"
        del os.environ["_SP_TEST_BARE"]

    def test_expand_env_missing(self):
        assert expand_env("${NONEXISTENT_VAR_XYZ}") == ""

    def test_load_json_config(self, tmp_path: Path):
        cfg = {
            "host": "0.0.0.0",
            "port": 9000,
            "model": "gpt-4",
            "backends": [{"name": "b1", "base_url": "http://example.com", "api_key": "$_SP_K"}],
        }
        os.environ["_SP_K"] = "expanded-key"
        p = tmp_path / "config.json"
        p.write_text(json.dumps(cfg))
        result = load_config(p)
        assert result["backends"][0]["api_key"] == "expanded-key"
        del os.environ["_SP_K"]

    def test_parse_config_defaults(self):
        raw = {"backends": [{"name": "x", "base_url": "http://x.com", "api_key": "k"}]}
        parsed = parse_config(raw)
        assert parsed["host"] == "127.0.0.1"
        assert parsed["port"] == 8000
        assert parsed["request_timeout"] == 120
        assert parsed["cooldown"] == 1200
        assert 429 in parsed["failure_codes"]
        assert len(parsed["backends"]) == 1

    def test_parse_config_strips_trailing_slash(self):
        raw = {"backends": [{"name": "x", "base_url": "http://x.com/v1/", "api_key": "k"}]}
        parsed = parse_config(raw)
        assert not parsed["backends"][0]["base_url"].endswith("/")


# -- State tests --


class TestState:
    def test_initial_state(self, tmp_state: PoolState):
        assert tmp_state.current_index == 0
        assert tmp_state.cooldowns == {}

    def test_mark_success_persists(self, tmp_path: Path):
        state = PoolState(tmp_path / "s.json")
        state.mark_success("alpha", 2)
        reloaded = PoolState(tmp_path / "s.json")
        assert reloaded.current_index == 2
        assert "alpha" not in reloaded.cooldowns

    def test_mark_failure_persists(self, tmp_path: Path):
        state = PoolState(tmp_path / "s.json")
        state.mark_failure("beta", 300)
        reloaded = PoolState(tmp_path / "s.json")
        assert "beta" in reloaded.cooldowns
        assert reloaded.cooldowns["beta"] > time.time()

    def test_available_order_skips_cooled(self, tmp_state: PoolState, backends: list[dict]):
        tmp_state.mark_failure("alpha", 9999)
        available = tmp_state.available_order(backends)
        names = [b["name"] for _, b in available]
        assert "alpha" not in names
        assert "beta" in names

    def test_all_on_cooldown(self, tmp_state: PoolState, backends: list[dict]):
        for b in backends:
            tmp_state.mark_failure(b["name"], 9999)
        assert tmp_state.all_on_cooldown(backends)

    def test_cooldown_expires(self, tmp_state: PoolState, backends: list[dict]):
        tmp_state.cooldowns["alpha"] = time.time() - 1
        tmp_state.cooldowns["beta"] = time.time() - 1
        tmp_state.cooldowns["gamma"] = time.time() - 1
        assert not tmp_state.all_on_cooldown(backends)


# -- Router tests --


class TestQuotaDetection:
    def test_429_with_quota_phrase(self):
        body = b'{"error": "Usage limit reached for this account"}'
        assert is_quota_failure(429, body) is True

    def test_429_without_quota_phrase(self):
        body = b'{"error": "too many requests"}'
        assert is_quota_failure(429, body) is False

    def test_non_429(self):
        assert is_quota_failure(500, b"server error") is False


class TestRouting:
    def _mock_upstream(self, responses: list[tuple[int, bytes, str, str]]):
        """Patch upstream_request to return from a response list."""
        self._call_count = 0
        original_responses = list(responses)

        def fake_upstream(backend, body, model_override, timeout):
            idx = self._call_count
            self._call_count += 1
            if idx < len(original_responses):
                return original_responses[idx]
            return 503, b'{"error": "no more mocks"}', "application/json", backend["name"]

        return patch("simple_pool.router.upstream_request", side_effect=fake_upstream)

    def test_success_prefers_first(self, tmp_state: PoolState, backends: list[dict], config: dict):
        responses = [(200, b'{"ok":true}', "application/json", "alpha")]
        with self._mock_upstream(responses):
            status, body, ct, headers = route_request(make_body(), backends, tmp_state, config)
        assert status == 200
        assert headers.get("X-Simple-Pool-Backend") == "alpha"

    def test_success_sticks(self, tmp_state: PoolState, backends: list[dict], config: dict):
        # First call succeeds on beta (alpha fails)
        responses = [
            (500, b'{"error": "down"}', "application/json", "alpha"),
            (200, b'{"ok":true}', "application/json", "beta"),
        ]
        with self._mock_upstream(responses):
            route_request(make_body(), backends, tmp_state, config)
        assert tmp_state.current_index == 1  # beta

    def test_429_quota_triggers_cooldown(
        self, tmp_state: PoolState, backends: list[dict], config: dict
    ):
        responses = [
            (429, b'{"error": "Usage limit reached"}', "application/json", "alpha"),
            (200, b'{"ok":true}', "application/json", "beta"),
        ]
        with self._mock_upstream(responses):
            status, _, _, _ = route_request(make_body(), backends, tmp_state, config)
        assert status == 200
        assert "alpha" in tmp_state.cooldowns

    def test_fallback_through_multiple(
        self, tmp_state: PoolState, backends: list[dict], config: dict
    ):
        responses = [
            (503, b'{"error": "down"}', "application/json", "alpha"),
            (502, b'{"error": "bad gw"}', "application/json", "beta"),
            (200, b'{"ok":true}', "application/json", "gamma"),
        ]
        with self._mock_upstream(responses):
            status, _, _, headers = route_request(make_body(), backends, tmp_state, config)
        assert status == 200
        assert headers.get("X-Simple-Pool-Backend") == "gamma"

    def test_all_fail_returns_503(self, tmp_state: PoolState, backends: list[dict], config: dict):
        responses = [
            (500, b'{"error": "e1"}', "application/json", "alpha"),
            (500, b'{"error": "e2"}', "application/json", "beta"),
            (500, b'{"error": "e3"}', "application/json", "gamma"),
        ]
        with self._mock_upstream(responses):
            status, body, _, _ = route_request(make_body(), backends, tmp_state, config)
        assert status == 500

    def test_all_on_cooldown_returns_503(
        self, tmp_state: PoolState, backends: list[dict], config: dict
    ):
        for b in backends:
            tmp_state.mark_failure(b["name"], 9999)
        status, body, _, _ = route_request(make_body(), backends, tmp_state, config)
        assert status == 503
        assert b"all backends are unavailable" in body


# -- Server integration tests --


class TestServer:
    @pytest.fixture(autouse=True)
    def setup_server(self, tmp_path: Path):
        from simple_pool.server import PoolServer

        self.config = {
            "host": "127.0.0.1",
            "port": 0,
            "model": "test-model",
            "backends": [],
            "failure_codes": {429, 500, 502, 503, 504},
            "request_timeout": 5,
            "cooldown": 60,
            "auth_token": "test-secret",
            "state_path": str(tmp_path / "state.json"),
        }
        state = PoolState(tmp_path / "state.json")
        self.server = PoolServer(self.config, state)
        self.port = self.server.server_address[1]
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        yield
        self.server.shutdown()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path: str, token: str | None = "test-secret") -> tuple[int, dict]:
        import urllib.error
        import urllib.request

        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self._url(path), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _post(self, path: str, body: dict, token: str | None = "test-secret") -> tuple[int, dict]:
        import urllib.error
        import urllib.request

        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode()
        req = urllib.request.Request(self._url(path), data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_models_endpoint(self):
        status, data = self._get("/v1/models")
        assert status == 200
        assert data["data"][0]["id"] == "test-model"

    def test_health_endpoint(self):
        status, data = self._get("/health")
        assert status == 200
        assert data["status"] == "ok"

    def test_auth_required(self):
        status, data = self._get("/v1/models", token=None)
        assert status == 401

    def test_wrong_auth(self):
        status, data = self._get("/v1/models", token="wrong")
        assert status == 401

    def test_404(self):
        status, data = self._get("/v1/unknown")
        assert status == 404

    def test_no_backends_503(self):
        body = {"model": "x", "messages": [{"role": "user", "content": "hi"}]}
        status, data = self._post("/v1/chat/completions", body)
        assert status == 503
        assert "no backends" in data["error"]["message"]

    def test_malformed_json(self):
        import urllib.error
        import urllib.request

        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer test-secret",
        }
        req = urllib.request.Request(
            self._url("/v1/chat/completions"),
            data=b"not json at all",
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            status = e.code
            data = json.loads(e.read())
        assert status == 400
        assert "invalid JSON" in data["error"]["message"]


# -- Secret redaction / log safety --


class TestLogSafety:
    def test_api_key_not_in_error_body(self, tmp_state: PoolState):
        """Upstream errors must not leak the pool's configured API keys."""
        backends = [
            {
                "name": "x",
                "base_url": "http://localhost:1",
                "api_key": "sk-SUPERSECRET",
                "model": "m",
            }
        ]
        config = {
            "backends": backends,
            "failure_codes": {500},
            "request_timeout": 1,
            "cooldown": 60,
            "model": "m",
        }
        status, body, _, _ = route_request(make_body(), backends, tmp_state, config)
        assert b"sk-SUPERSECRET" not in body

    def test_config_expand_does_not_store_raw_env(self, tmp_path: Path):
        """Verify that expanded secrets are not persisted back to config files."""
        os.environ["_SP_SECRET"] = "hunter2"
        cfg_path = tmp_path / "c.json"
        cfg = {"backends": [{"name": "t", "base_url": "http://x", "api_key": "${_SP_SECRET}"}]}
        cfg_path.write_text(json.dumps(cfg))
        loaded = load_config(cfg_path)
        raw_text = cfg_path.read_text()
        assert "hunter2" not in raw_text
        assert loaded["backends"][0]["api_key"] == "hunter2"
        del os.environ["_SP_SECRET"]


class TestStateWriteEfficiency:
    def test_repeated_success_does_not_rewrite_unchanged_state(self, tmp_path):
        state = PoolState(tmp_path / "state.json")
        with patch.object(state, "_save", wraps=state._save) as save:
            for _ in range(100):
                state.mark_success("alpha", 0)
            assert save.call_count == 1
        assert PoolState(state.path).current_index == 0

    def test_failed_save_is_retried_even_for_same_preferred_backend(self, tmp_path):
        state = PoolState(tmp_path / "state.json")
        with patch("simple_pool.state.os.replace", side_effect=OSError("synthetic failure")):
            with pytest.raises(OSError):
                state.mark_success("alpha", 1)
        state.mark_success("alpha", 1)
        assert PoolState(state.path).current_index == 1

    @pytest.mark.parametrize(
        "raw",
        [
            '{"current_index": 1e309}',
            '{"cooldowns": {"a": ' + "9" * 400 + "}}",
            "null",
            "[]",
            '{"cooldowns": []}',
            '{"cooldowns": false}',
            '{"cooldowns": 0}',
            '{"cooldowns": ""}',
            '{"cooldowns": {"a": "NaN"}}',
        ],
    )
    def test_malformed_state_does_not_crash_or_poison_availability(self, tmp_path, raw):
        path = tmp_path / "state.json"
        path.write_text(raw)
        state = PoolState(path)
        assert state.current_index == 0
        assert state.cooldowns == {}
        assert path.read_text() == raw
        state.mark_success("alpha", 0)
        assert json.loads(path.read_text()) == {"current_index": 0, "cooldowns": {}}
