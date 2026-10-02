"""The standard-library transport, over a real socket.

Every other suite drives the client through `httpx.MockTransport`, which the
SDK now adapts. These tests drive the default path instead, the one a caller
gets with no `transport=`, against a real HTTP server on loopback. They check
that what goes over the wire, and what comes back, is what the httpx client
sent and returned.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from dmzagent import DMZAgent
from dmzagent._http import UrllibTransport, encode_json, resolve_transport
from dmzagent.concordia import ConcordiaClient
from dmzagent.errors import RateLimitError, ServerError, ValidationError

_KEY = "ck_test_xxxxxxxxxxxxxxxxxxxxx"


class _Server:
    """A loopback server that records each request and answers from a queue."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.replies: list[tuple[int, dict[str, str], bytes, float]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                outer.seen.append({"method": self.command, "path": self.path,
                                   "headers": {k.lower(): v for k, v in self.headers.items()},
                                   "body": self.rfile.read(n) if n else b""})
                status, headers, body, delay = (outer.replies.pop(0) if outer.replies
                                                else (200, {}, b"{}", 0.0))
                time.sleep(delay)
                self.send_response(status)
                for k, v in {"Content-Type": "application/json", **headers}.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = _serve

            def log_message(self, *a: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reply(self, status: int, body: Any, headers: dict[str, str] | None = None,
              delay: float = 0.0) -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.replies.append((status, headers or {}, raw, delay))

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = _Server()
    yield s
    s.close()


def test_no_transport_means_the_standard_library() -> None:
    assert isinstance(resolve_transport(None), UrllibTransport)


def test_a_post_goes_over_the_wire_as_httpx_sent_it(server: _Server) -> None:
    server.reply(200, {"interaction_id": "int_1"})
    client = DMZAgent(api_key=_KEY, base_url=server.url)
    payload = {"text": "café", "n": 1, "nested": {"a": [1, 2]}}
    out = client._post_json("/v1/agent-stream/event", payload,
                            extra_headers={"Idempotency-Key": "k-1"})
    assert out == {"interaction_id": "int_1"}
    req = server.seen[0]
    assert (req["method"], req["path"]) == ("POST", "/v1/agent-stream/event")
    # The exact bytes httpx 0.28 would have sent: compact, UTF-8, not escaped.
    assert req["body"] == encode_json(payload)
    assert req["body"] == '{"text":"café","n":1,"nested":{"a":[1,2]}}'.encode()
    assert req["headers"]["content-length"] == str(len(req["body"]))
    assert req["headers"]["authorization"] == f"Bearer {_KEY}"
    assert req["headers"]["content-type"] == "application/json"
    assert req["headers"]["idempotency-key"] == "k-1"
    assert req["headers"]["user-agent"].startswith("dmzagent-python/")


def test_query_parameters_are_encoded(server: _Server) -> None:
    server.reply(200, {"items": [], "next_cursor": None})
    client = DMZAgent(api_key=_KEY, base_url=server.url)
    client._get_json("/v1/approvals", {"status": "pending", "cursor": "a b&c"})
    assert server.seen[0]["path"] == "/v1/approvals?status=pending&cursor=a+b%26c"


def test_an_error_status_is_a_reply_not_a_transport_failure(server: _Server) -> None:
    server.reply(422, {"detail": "bad"})
    client = DMZAgent(api_key=_KEY, base_url=server.url)
    with pytest.raises(ValidationError) as e:
        client._post_json("/v1/x", {})
    assert e.value.status_code == 422 and e.value.body == {"detail": "bad"}


def test_retry_after_survives_the_transport(server: _Server) -> None:
    server.reply(429, {"detail": "slow down"}, headers={"Retry-After": "7"})
    client = DMZAgent(api_key=_KEY, base_url=server.url)
    with pytest.raises(RateLimitError) as e:
        client._get_json("/v1/x")
    assert e.value.retry_after == 7


def test_a_timeout_is_a_server_error(server: _Server) -> None:
    server.reply(200, {}, delay=1.0)
    client = DMZAgent(api_key=_KEY, base_url=server.url, timeout=0.2)
    with pytest.raises(ServerError, match="timeout"):
        client._get_json("/v1/slow")


def test_a_connect_timeout_wrapped_by_urllib_is_still_a_timeout(monkeypatch) -> None:
    """A connect timeout reaches us as URLError(reason=timeout), not as the bare
    TimeoutError a slow read raises, and loopback cannot produce one. The
    branch that unwraps it is driven directly."""
    import urllib.error
    import urllib.request

    def boom(*a: Any, **k: Any) -> Any:
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    client = DMZAgent(api_key=_KEY, base_url="http://127.0.0.1:9", timeout=2)
    with pytest.raises(ServerError, match="timeout"):
        client._get_json("/v1/connect")


def test_a_refused_connection_is_a_server_error() -> None:
    client = DMZAgent(api_key=_KEY, base_url="http://127.0.0.1:9", timeout=2)
    with pytest.raises(ServerError, match="network error"):
        client._get_json("/v1/nothing")


def test_concordia_speaks_json_rpc_over_the_same_layer(server: _Server) -> None:
    server.reply(200, {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
    cx = ConcordiaClient(api_key=_KEY, base_url=server.url)
    assert cx._rpc("tools/list") == {"tools": []}
    req = server.seen[0]
    assert req["path"] == "/mcp/v1"
    assert json.loads(req["body"]) == {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                       "params": {}}


def test_a_transport_that_is_neither_kind_is_refused() -> None:
    with pytest.raises(TypeError, match="transport must be"):
        DMZAgent(api_key=_KEY, transport=object())
