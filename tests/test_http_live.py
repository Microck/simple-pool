"""Real loopback HTTP journeys; no model provider or credentials are used."""

import http.client
import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from simple_pool.server import PoolServer
from simple_pool.state import PoolState


@contextmanager
def running(server):
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize("body", [b"null", b"[]", b'"text"', b"42"])
def test_non_object_json_is_a_client_error(tmp_path, body):
    server = PoolServer(
        {"host": "127.0.0.1", "port": 0, "backends": []}, PoolState(tmp_path / "state")
    )
    with running(server) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("POST", "/v1/chat/completions", body, {"Content-Type": "application/json"})
        response = conn.getresponse()
        assert response.status == 400
        assert "invalid JSON" in json.loads(response.read())["error"]["message"]
        conn.close()


def test_negative_length_is_rejected_without_waiting_for_eof(tmp_path):
    server = PoolServer(
        {"host": "127.0.0.1", "port": 0, "backends": []}, PoolState(tmp_path / "state")
    )
    with running(server) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("POST", "/v1/chat/completions", headers={"Content-Length": "-1"})
        try:
            response = conn.getresponse()
            assert response.status == 400
            response.read()
        finally:
            conn.close()


def test_live_failover_stays_sticky_and_preserves_request_data(tmp_path):
    received = []

    class Backend(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.server.label, body))
            data = json.dumps(
                {"choices": [{"message": {"content": "synthetic completion"}}]}
            ).encode()
            self.send_response(self.server.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    first = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    first.label, first.status = "first", 503
    second = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    second.label, second.status = "second", 200
    with running(first) as port1, running(second) as port2:
        config = {
            "host": "127.0.0.1",
            "port": 0,
            "backends": [
                {"name": "first", "base_url": f"http://127.0.0.1:{port1}", "model": "fixture-a"},
                {"name": "second", "base_url": f"http://127.0.0.1:{port2}", "model": "fixture-b"},
            ],
            "cooldown": 60,
            "request_timeout": 2,
        }
        state = PoolState(tmp_path / "state")
        with running(PoolServer(config, state)) as port:
            for _ in range(2):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                conn.request(
                    "POST",
                    "/v1/chat/completions",
                    json.dumps({"messages": [{"role": "user", "content": "synthetic"}]}),
                )
                response = conn.getresponse()
                assert response.status == 200
                assert response.getheader("X-Simple-Pool-Backend") == "second"
                assert (
                    json.loads(response.read())["choices"][0]["message"]["content"]
                    == "synthetic completion"
                )
                conn.close()
        assert [name for name, _ in received] == ["first", "second", "second"]
        assert [body["model"] for _, body in received] == ["fixture-a", "fixture-b", "fixture-b"]
        assert all(body["messages"][0]["content"] == "synthetic" for _, body in received)
