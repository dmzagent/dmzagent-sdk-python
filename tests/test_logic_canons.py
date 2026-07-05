"""Logic Canon surface — /v1/logic-canons + /v1/logic/events (spec Phase 14.7).

Drives every typed method through an httpx.MockTransport, asserting the wire
shape (method, path, snake_case body) and the dataclass mapping back.
"""
from __future__ import annotations

import json

import httpx
import pytest

from dmzagent import DMZAgent


class Recorder:
    def __init__(self, responses):
        self.calls: list[dict] = []
        self._responses = list(responses)

    def route(self, request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            body = json.loads(request.content.decode("utf-8"))
        self.calls.append({
            "method": request.method,
            "path": request.url.raw_path.decode("utf-8"),
            "body": body,
        })
        status, payload = self._responses.pop(0) if self._responses else (200, {})
        return httpx.Response(status, json=payload)


def client_with(responses):
    rec = Recorder(responses)
    cx = DMZAgent(api_key="ck_test_xxxxxxxxxxxxxxxxxxxxx",
                  transport=httpx.MockTransport(rec.route))
    return cx, rec


class TestAuthoring:
    def test_create_draft_canon(self):
        cx, rec = client_with([(200, {"logic_canon_id": "lc_1", "name": "Ops", "status": "draft"})])
        canon = cx.create_logic_canon(name="Ops", description="d")
        assert rec.calls[0]["method"] == "POST"
        assert rec.calls[0]["path"] == "/v1/logic-canons"
        assert rec.calls[0]["body"] == {"name": "Ops", "description": "d"}
        assert canon.logic_canon_id == "lc_1"
        assert canon.status == "draft"

    def test_create_requires_name(self):
        cx, _ = client_with([])
        with pytest.raises(ValueError):
            cx.create_logic_canon(name="  ")

    def test_publish_version(self):
        cx, rec = client_with([(200, {"logic_canon_id": "lc_1", "version": 3, "n_rules": 2})])
        v = cx.publish_logic_canon_version("lc_1", {"rules": []}, changelog="tighten")
        assert rec.calls[0]["path"] == "/v1/logic-canons/lc_1/versions"
        assert rec.calls[0]["body"] == {"rulebook": {"rules": []}, "changelog": "tighten"}
        assert v.version == 3 and v.n_rules == 2

    def test_list_with_status_filter(self):
        cx, rec = client_with([(200, {"logic_canons": [{"logic_canon_id": "lc_1"}], "total": 1})])
        rows = cx.list_logic_canons(status="published")
        assert rec.calls[0]["path"] == "/v1/logic-canons?status=published"
        assert rows[0].logic_canon_id == "lc_1"

    def test_validate_rulebook(self):
        cx, rec = client_with([(200, {"valid": True, "n_rules": 4, "n_stateful": 1})])
        res = cx.validate_rulebook({"rules": []})
        assert rec.calls[0]["path"] == "/v1/logic-canons/validate"
        assert res.valid and res.n_stateful == 1


class TestInstallSurface:
    def test_install_with_pinned_version(self):
        cx, rec = client_with([(200, {"logic_canon_id": "lc_1", "workspace_id": "ws_1", "version": 2})])
        inst = cx.install_logic_canon("lc_1", workspace_id="ws_1", version=2)
        assert rec.calls[0]["path"] == "/v1/logic-canons/lc_1/install"
        assert rec.calls[0]["body"] == {"workspace_id": "ws_1", "version": 2}
        assert inst.version == 2

    def test_uninstall_is_delete(self):
        cx, rec = client_with([(200, {"uninstalled": True})])
        cx.uninstall_logic_canon("lc_1", workspace_id="ws_1")
        assert rec.calls[0]["method"] == "DELETE"
        assert rec.calls[0]["path"] == "/v1/logic-canons/lc_1/install/ws_1"

    def test_workspace_installs_and_health(self):
        cx, rec = client_with([
            (200, {"installs": [{"logic_canon_id": "lc_1", "version": 1}], "total": 1}),
            (200, {"workspace_id": "ws_1", "ok": False, "broken": 1,
                   "installs": [{"logic_canon_id": "lc_1", "status": "compile_error", "detail": "x"}]}),
        ])
        installs = cx.list_workspace_logic_canons("ws_1")
        health = cx.workspace_logic_canon_health("ws_1")
        assert rec.calls[0]["path"] == "/v1/workspaces/ws_1/logic-canons"
        assert rec.calls[1]["path"] == "/v1/workspaces/ws_1/logic-canons/health"
        assert installs[0].logic_canon_id == "lc_1"
        assert health.ok is False
        assert health.installs[0].status == "compile_error"


class TestLogicDoor:
    def test_emit_event_maps_ack(self):
        cx, rec = client_with([(202, {
            "accepted": True, "workspace_id": "ws_1", "subject_id": "subject:d:s",
            "n_logic_pass": 3, "n_deferred": 0,
            "fired": [{"rule_id": "hard"}],
            "escalations": [{"rule_id": "creep", "band": "enforce", "lane": "enforce"}],
            "dispositions": 1, "emitted_frames": 0, "expected_loss_avoided": 4000,
        })])
        ack = cx.emit_logic_event(workspace_id="ws_1",
                                  event={"subject_id": "subject:d:s", "amount": 50000})
        assert rec.calls[0]["path"] == "/v1/logic/events"
        assert rec.calls[0]["body"] == {"workspace_id": "ws_1",
                                        "event": {"subject_id": "subject:d:s", "amount": 50000}}
        assert ack.accepted is True
        assert ack.fired[0]["rule_id"] == "hard"
        assert ack.expected_loss_avoided == 4000.0

    def test_event_requires_subject_id(self):
        cx, _ = client_with([])
        with pytest.raises(ValueError):
            cx.emit_logic_event(workspace_id="ws_1", event={"amount": 1})
