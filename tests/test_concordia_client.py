"""Tests for `concordex.concordia` — the MCP 1.0 governance client.

Uses httpx.MockTransport to stub the Concordia server. We don't
spin up the real prothinker-server here; we verify the client's
JSON-RPC envelope construction, response parsing, error mapping,
and typed dataclass conversion against a controlled fake.

The server-side end-to-end is covered by the prothinker-server
in-process tests (#212 / #213 / #217). This file covers the client
side: same wire shape from the customer's perspective.
"""
from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from concordex.concordia import (
    ConcordiaClient,
    ConcordiaError,
    ConcordiaAuthError,
    ConcordiaProtocolError,
    ConcordiaCanonNotInstalledError,
    ConcordiaSubjectNotFoundError,
    ConcordiaPermissionDeniedError,
    ConcordiaQuotaExceededError,
    EnforceCovenantResult,
    RecordDecisionResult,
    QueryCorpusResult,
    SubjectSoul,
    PolicySummary,
    InstalledCanon,
    LedgerPage,
)


# --------------------------------------------------------------------------- #
# Fake server — a tiny dispatch table that mirrors prothinker-server's
# wire shape. Each fake handler takes the parsed params, returns the
# `result` payload (or raises HTTPException-equivalent).
# --------------------------------------------------------------------------- #


def _wrap_tool_result(d: dict) -> dict:
    return {
        "content":  [{"type": "text", "text": json.dumps(d)}],
        "_data":    d,
        "isError":  False,
    }


class FakeConcordia:
    """Fake JSON-RPC server. Each test installs its own handlers."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_with: tuple[int, str, dict | None] | None = None
        self.method_handlers: dict[str, Callable[[dict], Any]] = {}

    def fail(self, code: int, message: str, data: dict | None = None) -> None:
        self.fail_with = (code, message, data)

    def handle(self, method: str, fn: Callable[[dict], Any]) -> None:
        self.method_handlers[method] = fn

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/mcp/v1":
            return httpx.Response(404, text="not found")
        if not request.headers.get("authorization", "").startswith("Bearer ck_"):
            envelope = {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32001, "message": "auth_expired",
                          "data": {"error_id": "auth_expired"}},
            }
            return httpx.Response(200, json=envelope)
        try:
            body = json.loads(request.content)
        except Exception:
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "parse error"},
            })
        self.calls.append(body)
        method = body.get("method")
        params = body.get("params") or {}
        jid    = body.get("id")
        if self.fail_with is not None:
            code, msg, data = self.fail_with
            err: dict[str, Any] = {"code": code, "message": msg}
            if data is not None:
                err["data"] = data
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": jid, "error": err,
            })
        handler = self.method_handlers.get(method)
        if handler is None:
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": jid,
                "error": {"code": -32601, "message": f"unknown method: {method}"},
            })
        try:
            result = handler(params)
        except _AppError as e:
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": jid,
                "error": {"code": e.code, "message": e.msg,
                          "data": {"error_id": e.error_id}},
            })
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": jid, "result": result,
        })


class _AppError(Exception):
    def __init__(self, code: int, error_id: str, msg: str = "") -> None:
        self.code     = code
        self.error_id = error_id
        self.msg      = msg or error_id


@pytest.fixture
def fake() -> FakeConcordia:
    return FakeConcordia()


@pytest.fixture
def client(fake: FakeConcordia) -> ConcordiaClient:
    transport = httpx.MockTransport(fake)
    return ConcordiaClient(api_key="ck_test_xyz", transport=transport)


# --------------------------------------------------------------------------- #
# Construction + auth
# --------------------------------------------------------------------------- #


def test_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONCORDEX_API_KEY", raising=False)
    with pytest.raises(ConcordiaError) as ei:
        ConcordiaClient()
    assert "api_key required" in str(ei.value)


def test_rejects_non_ck_key() -> None:
    with pytest.raises(ConcordiaError) as ei:
        ConcordiaClient(api_key="sk_oops_wrong_service")
    assert "ck_" in str(ei.value)


def test_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONCORDEX_API_KEY", "ck_from_env")
    c = ConcordiaClient(transport=httpx.MockTransport(FakeConcordia()))
    assert c._api_key == "ck_from_env"


def test_bearer_header_sent(fake: FakeConcordia, client: ConcordiaClient) -> None:
    fake.handle("ping", lambda p: {"ok": True})
    client.ping()
    # The MockTransport callback runs inside FakeConcordia; calls list
    # captures parsed bodies, but the header check is implicit — if it
    # missed, FakeConcordia would have returned a -32001 envelope and
    # ping() would raise. Verify by switching the key to something invalid.
    bad = ConcordiaClient(
        api_key="ck_evil_route_around",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={
            "jsonrpc": "2.0", "id": 1, "error": {
                "code": -32001, "message": "auth_expired",
                "data": {"error_id": "auth_expired"},
            },
        })),
    )
    with pytest.raises(ConcordiaAuthError):
        bad.ping()


# --------------------------------------------------------------------------- #
# Tool: enforce_covenant
# --------------------------------------------------------------------------- #


def test_enforce_covenant_happy_path(
    fake: FakeConcordia, client: ConcordiaClient,
) -> None:
    captured: dict = {}

    def _tools_call(params: dict) -> dict:
        captured.update(params)
        return _wrap_tool_result({
            "verdict":          "review",
            "policy_ids":       ["pol_a", "pol_b"],
            "rationale":        "policies refund-cap + churn-risk fired",
            "cb_state_change":  {"state": "half_open", "warning": True, "soul_version": 42},
            "ledger_entry_id":  "lid_abc",
        })

    fake.handle("tools/call", _tools_call)

    r = client.enforce_covenant(
        subject_id="user:alice",
        action_kind="refund.issue",
        action_payload={"amount": 9900},
        context={"session_id": "s_42"},
    )
    assert isinstance(r, EnforceCovenantResult)
    assert r.verdict == "review"
    assert r.policy_ids == ["pol_a", "pol_b"]
    assert r.rationale.startswith("policies refund-cap")
    assert r.cb_state_change.state == "half_open"
    assert r.cb_state_change.warning is True
    assert r.cb_state_change.soul_version == 42
    assert r.ledger_entry_id == "lid_abc"
    assert r.allow is False
    assert r.blocked is False

    # Wire-shape: tools/call with the right name + arguments
    assert captured["name"] == "enforce_covenant"
    assert captured["arguments"]["subject_id"] == "user:alice"
    assert captured["arguments"]["action_kind"] == "refund.issue"
    assert captured["arguments"]["action_payload"] == {"amount": 9900}
    assert captured["arguments"]["context"] == {"session_id": "s_42"}


def test_enforce_covenant_block_path(
    fake: FakeConcordia, client: ConcordiaClient,
) -> None:
    fake.handle("tools/call", lambda p: _wrap_tool_result({
        "verdict": "block",
        "policy_ids": ["pol_block"],
        "rationale": "absolute block by canon-of-no",
        "cb_state_change": {"state": "open", "warning": False},
        "ledger_entry_id": "lid_xyz",
    }))
    r = client.enforce_covenant(subject_id="user:bob", action_kind="payment.issue")
    assert r.blocked is True
    assert r.allow is False


def test_enforce_covenant_propagates_quota_exceeded(
    fake: FakeConcordia, client: ConcordiaClient,
) -> None:
    def _tools_call(params: dict) -> dict:
        raise _AppError(-32002, "tenant_quota_exceeded", "monthly cap hit")
    fake.handle("tools/call", _tools_call)
    with pytest.raises(ConcordiaQuotaExceededError) as ei:
        client.enforce_covenant(subject_id="user:c", action_kind="x")
    assert ei.value.code == -32002
    assert ei.value.error_id == "tenant_quota_exceeded"


# --------------------------------------------------------------------------- #
# Tool: record_decision
# --------------------------------------------------------------------------- #


def test_record_decision_happy_path(
    fake: FakeConcordia, client: ConcordiaClient,
) -> None:
    captured: dict = {}

    def _tools_call(params: dict) -> dict:
        captured.update(params)
        return _wrap_tool_result({
            "ledger_entry_id": "lid_001",
            "chain_head_hash": "0xdeadbeef",
            "index":           1234,
        })

    fake.handle("tools/call", _tools_call)
    r = client.record_decision(
        subject_id="user:alice",
        decision_kind="content_generated",
        payload={"text": "ok"},
        outcome="completed",
    )
    assert isinstance(r, RecordDecisionResult)
    assert r.ledger_entry_id == "lid_001"
    assert r.chain_head_hash == "0xdeadbeef"
    assert r.index == 1234
    assert captured["name"] == "record_decision"
    assert captured["arguments"]["actor"] == "agent"  # default
    assert captured["arguments"]["outcome"] == "completed"


# --------------------------------------------------------------------------- #
# Tool: query_corpus
# --------------------------------------------------------------------------- #


def test_query_corpus(fake: FakeConcordia, client: ConcordiaClient) -> None:
    fake.handle("tools/call", lambda p: _wrap_tool_result({
        "matches": [
            {"canon_id": "cn_a", "canon_version": "1.0.0",
             "section": "policies/refund-cap", "excerpt": "Block refunds over $500.",
             "relevance": 1.15},
            {"canon_id": "cn_b", "canon_version": "2.0.0",
             "section": "vocabulary/risk:churn", "excerpt": "Customer churn signals.",
             "relevance": 0.8},
        ],
        "scope":       "installed",
        "canon_count": 2,
    }))
    r = client.query_corpus(query="refund", canon_filter=["cn_a", "cn_b"])
    assert isinstance(r, QueryCorpusResult)
    assert r.canon_count == 2
    assert len(r.matches) == 2
    assert r.matches[0].canon_id == "cn_a"
    assert r.matches[0].relevance == pytest.approx(1.15)


def test_query_corpus_canon_not_installed(
    fake: FakeConcordia, client: ConcordiaClient,
) -> None:
    def _tools_call(params: dict) -> dict:
        raise _AppError(-32004, "canon_not_installed", "missing cn_bogus")
    fake.handle("tools/call", _tools_call)
    with pytest.raises(ConcordiaCanonNotInstalledError):
        client.query_corpus(query="x", canon_filter=["cn_bogus"])


# --------------------------------------------------------------------------- #
# Tool: get_subject_soul
# --------------------------------------------------------------------------- #


def test_get_subject_soul(fake: FakeConcordia, client: ConcordiaClient) -> None:
    fake.handle("tools/call", lambda p: _wrap_tool_result({
        "subject_id":    "user:alice",
        "snapshot_at":   "2026-05-30T00:00:00Z",
        "soul_version":  42,
        "tags": [
            {"tag_id": "risk:moderate", "score": 0.6, "raw_strength": 0.55,
             "evidence_count": 12,
             "first_observed_at": "2026-01-01T00:00:00Z",
             "last_reinforced_at": "2026-05-30T00:00:00Z"},
        ],
        "retired_count": 3,
        "recent_traces": [
            {"trace_id": "tr_1", "frame_id": "fr_1", "verdict": "review",
             "outcome": "applied", "started_at": "2026-05-30T00:00:00Z",
             "finished_at": "2026-05-30T00:00:01Z"},
        ],
    }))
    s = client.get_subject_soul(subject_id="user:alice")
    assert isinstance(s, SubjectSoul)
    assert s.soul_version == 42
    assert s.retired_count == 3
    assert len(s.tags) == 1
    assert s.tags[0].tag_id == "risk:moderate"
    assert s.tags[0].score == pytest.approx(0.6)
    assert len(s.recent_traces) == 1
    assert s.recent_traces[0].verdict == "review"


def test_get_subject_soul_not_found(
    fake: FakeConcordia, client: ConcordiaClient,
) -> None:
    def _tools_call(params: dict) -> dict:
        raise _AppError(-32005, "subject_not_found")
    fake.handle("tools/call", _tools_call)
    with pytest.raises(ConcordiaSubjectNotFoundError) as ei:
        client.get_subject_soul(subject_id="user:ghost")
    assert ei.value.error_id == "subject_not_found"


# --------------------------------------------------------------------------- #
# Resources
# --------------------------------------------------------------------------- #


def _resource_response(payload: dict, uri: str) -> dict:
    return {
        "contents": [
            {"uri": uri, "mimeType": "application/json", "text": json.dumps(payload)},
        ],
    }


def test_workspace_policies(fake: FakeConcordia, client: ConcordiaClient) -> None:
    fake.handle("resources/read", lambda p: _resource_response({
        "workspace_id": "ws_test",
        "count":        2,
        "policies": [
            {"cb_policy_id": "pol_a", "name": "refund-cap", "description": "x",
             "scope": "subject", "action": "block", "enabled": True,
             "rules": [], "updated_at": "2026-05-01T00:00:00Z"},
            {"cb_policy_id": "pol_b", "name": "pii-leak",  "description": "y",
             "scope": "interaction", "action": "review", "enabled": False,
             "rules": [], "updated_at": "2026-05-02T00:00:00Z"},
        ],
    }, p["uri"]))
    pols = client.workspace_policies()
    assert len(pols) == 2
    assert all(isinstance(p, PolicySummary) for p in pols)
    assert pols[0].name == "refund-cap"
    assert pols[0].enabled is True
    assert pols[1].enabled is False


def test_workspace_canons(fake: FakeConcordia, client: ConcordiaClient) -> None:
    fake.handle("resources/read", lambda p: _resource_response({
        "workspace_id": "ws_test",
        "count": 1,
        "canons": [{
            "canon_id": "cn_compliance", "name": "Compliance Canon",
            "category": "compliance", "author_name": "Concordex",
            "latest_version": "1.2.0", "short_description": "SOC 2 + EU AI Act",
            "icon_url": None, "enabled": True, "installed_at": "2026-04-01T00:00:00Z",
        }],
    }, p["uri"]))
    canons = client.workspace_canons()
    assert len(canons) == 1
    assert isinstance(canons[0], InstalledCanon)
    assert canons[0].canon_id == "cn_compliance"
    assert canons[0].latest_version == "1.2.0"


def test_recent_ledger_pagination(
    fake: FakeConcordia, client: ConcordiaClient,
) -> None:
    """Fake server returns 3 entries per page across 2 pages.
    iter_ledger should yield all 6 entries and stop cleanly."""

    pages = [
        {  # since=0, limit=3 → entries 0..2, next_since=3
            "workspace_id": "ws", "since": 0, "limit": 3, "count": 3,
            "next_since":   3,
            "entries": [
                {"event_id": f"e{i}", "index": i, "prev_hash": f"h{i-1}",
                 "payload_hash": "p", "hash": f"h{i}",
                 "payload": {"i": i}, "recorded_at": "t"} for i in range(3)
            ],
        },
        {  # since=3, limit=3 → entries 3..5, next_since=None
            "workspace_id": "ws", "since": 3, "limit": 3, "count": 3,
            "next_since":   None,
            "entries": [
                {"event_id": f"e{i}", "index": i, "prev_hash": f"h{i-1}",
                 "payload_hash": "p", "hash": f"h{i}",
                 "payload": {"i": i}, "recorded_at": "t"} for i in range(3, 6)
            ],
        },
    ]
    call_log: list[str] = []

    def _resources_read(params: dict) -> dict:
        uri = params["uri"]
        call_log.append(uri)
        page = pages.pop(0) if pages else None
        if page is None:
            raise AssertionError("too many resources/read calls")
        return _resource_response(page, uri)

    fake.handle("resources/read", _resources_read)
    out = list(client.iter_ledger(since=0, page_size=3))
    assert len(out) == 6
    assert [e.index for e in out] == [0, 1, 2, 3, 4, 5]
    # Two calls: since=0 then since=3
    assert len(call_log) == 2
    assert "since=0" in call_log[0]
    assert "since=3" in call_log[1]


def test_recent_ledger_single_page(
    fake: FakeConcordia, client: ConcordiaClient,
) -> None:
    fake.handle("resources/read", lambda p: _resource_response({
        "workspace_id": "ws", "since": 0, "limit": 100, "count": 2,
        "next_since":   None,
        "entries": [
            {"event_id": "e0", "index": 0, "prev_hash": "G",
             "payload_hash": "p", "hash": "h0", "payload": {}, "recorded_at": "t"},
            {"event_id": "e1", "index": 1, "prev_hash": "h0",
             "payload_hash": "p", "hash": "h1", "payload": {}, "recorded_at": "t"},
        ],
    }, p["uri"]))
    page = client.recent_ledger(since=0, limit=100)
    assert isinstance(page, LedgerPage)
    assert page.next_since is None
    assert page.count == 2
    assert page.entries[1].hash == "h1"


# --------------------------------------------------------------------------- #
# Error mapping / transport
# --------------------------------------------------------------------------- #


def test_permission_denied_maps(
    fake: FakeConcordia, client: ConcordiaClient,
) -> None:
    def _tools_call(params: dict) -> dict:
        raise _AppError(-32007, "permission_denied", "viewer cannot write")
    fake.handle("tools/call", _tools_call)
    with pytest.raises(ConcordiaPermissionDeniedError) as ei:
        client.enforce_covenant(subject_id="x", action_kind="y")
    assert ei.value.code == -32007


def test_unknown_app_code_falls_back_to_base(
    fake: FakeConcordia, client: ConcordiaClient,
) -> None:
    def _tools_call(params: dict) -> dict:
        raise _AppError(-32099, "unknown_concordex_error", "made up")
    fake.handle("tools/call", _tools_call)
    with pytest.raises(ConcordiaError) as ei:
        client.enforce_covenant(subject_id="x", action_kind="y")
    # Not one of the seven typed exceptions, so it surfaces as the base.
    assert type(ei.value) is ConcordiaError
    assert ei.value.code == -32099


def test_transport_error_wrapped() -> None:
    def _bad_transport(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")
    transport = httpx.MockTransport(_bad_transport)
    client = ConcordiaClient(api_key="ck_t", transport=transport)
    with pytest.raises(ConcordiaProtocolError) as ei:
        client.ping()
    assert "transport error" in str(ei.value).lower()


def test_initialize(fake: FakeConcordia, client: ConcordiaClient) -> None:
    fake.handle("initialize", lambda p: {
        "protocolVersion": "1.0",
        "serverInfo": {"name": "concordia", "version": "1.0.0"},
        "capabilities": {"tools": {"listChanged": False}},
        "principalHint": {"workspace_id": "ws_t", "role": "analyst"},
    })
    r = client.initialize()
    assert r["protocolVersion"] == "1.0"
    assert r["principalHint"]["workspace_id"] == "ws_t"


def test_context_manager_closes(fake: FakeConcordia) -> None:
    transport = httpx.MockTransport(fake)
    with ConcordiaClient(api_key="ck_test", transport=transport) as c:
        fake.handle("ping", lambda p: {"ok": True})
        assert c.ping() is True
    # close() is idempotent — call again to ensure no errors
    c.close()
