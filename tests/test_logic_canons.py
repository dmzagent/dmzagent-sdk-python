"""Logic Canon surface — /v1/logic-canons + /v1/logic/events (spec Phase 14.7).

Drives every typed method through an httpx.MockTransport, asserting the wire
shape (method, path, snake_case body) and the dataclass mapping back.
"""
from __future__ import annotations

import json

import httpx
import pytest

from dmzagent import (
    DMZAgent,
    Escalation,
    FiredRule,
    LogicCanonInstall,
    LogicInstallHealthRow,
    RateLimitError,
    ValidationError,
)


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
        entry = self._responses.pop(0) if self._responses else (200, {})
        status, payload = entry[0], entry[1]
        headers = entry[2] if len(entry) > 2 else {}
        return httpx.Response(status, json=payload, headers=headers)


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
        # Server LC-P6: list responses carry `count` (renamed from `total`).
        cx, rec = client_with([(200, {"logic_canons": [{"logic_canon_id": "lc_1"}], "count": 1})])
        rows = cx.list_logic_canons(status="published")
        assert rec.calls[0]["path"] == "/v1/logic-canons?status=published"
        assert rows[0].logic_canon_id == "lc_1"

    def test_canon_surfaces_vendor_id(self):
        cx, _ = client_with([(200, {
            "logic_canon_id": "lc_1", "vendor_id": "ven_9",
            "name": "Ops", "status": "published",
        })])
        canon = cx.get_logic_canon("lc_1")
        assert canon.vendor_id == "ven_9"
        # Older servers omit it — must default to None, not blow up.
        cx2, _ = client_with([(200, {"logic_canon_id": "lc_2"})])
        assert cx2.get_logic_canon("lc_2").vendor_id is None

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
            (200, {"installs": [{"logic_canon_id": "lc_1", "version": 1}], "count": 1}),
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

    def test_install_vs_health_row_model_split(self):
        """Deploy records and health rows are distinct models: the deploy
        record's `status` is the lifecycle enum, the health row's is the
        health enum, and only the health row carries `detail`."""
        cx, _ = client_with([
            (200, {"logic_canon_id": "lc_1", "workspace_id": "ws_1", "version": 2,
                   "status": "published", "installed_by": "u_1",
                   "installed_at": "2026-07-10T00:00:00Z"}),
            (200, {"workspace_id": "ws_1", "ok": False, "broken": 1,
                   "installs": [{"logic_canon_id": "lc_1", "version": 2,
                                 "status": "missing_bytes", "detail": "bytes gone"}]}),
        ])
        inst = cx.install_logic_canon("lc_1", workspace_id="ws_1", version=2)
        health = cx.workspace_logic_canon_health("ws_1")

        assert isinstance(inst, LogicCanonInstall)
        assert inst.status == "published"          # lifecycle enum, open str
        assert inst.installed_by == "u_1"
        assert not hasattr(inst, "detail")         # health-only field

        row = health.installs[0]
        assert isinstance(row, LogicInstallHealthRow)
        assert row.status == "missing_bytes"       # health enum, open str
        assert row.detail == "bytes gone"
        assert row.raw["logic_canon_id"] == "lc_1"

    def test_install_list_rows_have_no_vendor_id_field(self):
        """Server LC-P6 removed `vendor_id` from workspace install-list
        rows; the deploy-record model never surfaces one."""
        cx, _ = client_with([
            (200, {"installs": [{"logic_canon_id": "lc_1", "version": 1}], "count": 1}),
        ])
        rows = cx.list_workspace_logic_canons("ws_1")
        assert not hasattr(rows[0], "vendor_id")


class TestLogicDoor:
    def test_emit_event_maps_ack(self):
        cx, rec = client_with([(202, {
            "accepted": True, "workspace_id": "ws_1", "subject_id": "subject:d:s",
            "n_logic_pass": 3, "n_deferred": 0,
            "fired": [{"rule_id": "hard"}],
            "escalations": [{"rule_id": "creep", "band": "enforce", "lane": "enforce"}],
            "dispositions": 1, "emitted_frames": 0, "expected_loss_avoided": 4000,
            "degraded": False, "responded": True,
            "installs_evaluated": 2, "installs_total": 2,
        })])
        ack = cx.emit_logic_event(workspace_id="ws_1",
                                  event={"subject_id": "subject:d:s", "amount": 50000})
        assert rec.calls[0]["path"] == "/v1/logic/events"
        assert rec.calls[0]["body"] == {"workspace_id": "ws_1",
                                        "event": {"subject_id": "subject:d:s", "amount": 50000}}
        assert ack.accepted is True
        assert ack.fired[0].rule_id == "hard"
        assert ack.expected_loss_avoided == 4000.0
        assert ack.degraded is False
        assert ack.responded is True
        assert ack.installs_evaluated == 2
        assert ack.installs_total == 2

    def test_ack_fired_and_escalations_are_typed_with_raw_passthrough(self):
        fired_wire = {"rule_id": "hard", "weight": 0.9, "future_field": "x"}
        esc_wire = {"rule_id": "creep", "band": "enforce", "lane": "review",
                    "undocumented": True}
        cx, _ = client_with([(202, {
            "accepted": True, "workspace_id": "ws_1", "subject_id": "s",
            "fired": [fired_wire], "escalations": [esc_wire],
        })])
        ack = cx.emit_logic_event(workspace_id="ws_1", event={"subject_id": "s"})

        assert isinstance(ack.fired[0], FiredRule)
        assert ack.fired[0].rule_id == "hard"
        assert ack.fired[0].raw == fired_wire        # untouched wire dict

        assert isinstance(ack.escalations[0], Escalation)
        assert ack.escalations[0].rule_id == "creep"
        assert ack.escalations[0].band == "enforce"
        assert ack.escalations[0].lane == "review"
        assert ack.escalations[0].raw == esc_wire

    def test_escalation_band_lane_optional(self):
        cx, _ = client_with([(202, {
            "accepted": True, "workspace_id": "ws_1", "subject_id": "s",
            "escalations": [{"rule_id": "creep"}],
        })])
        ack = cx.emit_logic_event(workspace_id="ws_1", event={"subject_id": "s"})
        assert ack.escalations[0].band is None
        assert ack.escalations[0].lane is None

    def test_ack_degraded_fields_default_for_older_servers(self):
        """Servers predating LC-P3 omit the honest-ack fields; the SDK
        must default them rather than KeyError."""
        cx, _ = client_with([(202, {
            "accepted": True, "workspace_id": "ws_1", "subject_id": "s",
        })])
        ack = cx.emit_logic_event(workspace_id="ws_1", event={"subject_id": "s"})
        assert ack.degraded is False
        assert ack.responded is False
        assert ack.installs_evaluated == 0
        assert ack.installs_total == 0
        assert ack.fired == ()
        assert ack.escalations == ()

    def test_ack_degraded_true_maps_through(self):
        cx, _ = client_with([(202, {
            "accepted": True, "workspace_id": "ws_1", "subject_id": "s",
            "degraded": True, "responded": False,
            "installs_evaluated": 1, "installs_total": 3,
        })])
        ack = cx.emit_logic_event(workspace_id="ws_1", event={"subject_id": "s"})
        assert ack.degraded is True
        assert ack.responded is False
        assert ack.installs_evaluated == 1
        assert ack.installs_total == 3

    def test_event_requires_subject_id(self):
        cx, _ = client_with([])
        with pytest.raises(ValueError):
            cx.emit_logic_event(workspace_id="ws_1", event={"amount": 1})


class TestErrorTaxonomy:
    """Spec 0.7.0 §3 — 422 → ValidationError, 429 → RateLimitError."""

    def test_422_maps_to_validation_error(self):
        cx, _ = client_with([(422, {"detail": "logic evaluation failed",
                                    "error": "validation_error"})])
        with pytest.raises(ValidationError) as ei:
            cx.emit_logic_event(workspace_id="ws_1", event={"subject_id": "s"})
        assert ei.value.status_code == 422

    def test_429_maps_to_rate_limit_error_with_retry_after(self):
        cx, _ = client_with([
            (429, {"detail": "rate cap reached", "error": "rate_limited"},
             {"Retry-After": "30"}),
        ])
        with pytest.raises(RateLimitError) as ei:
            cx.emit_logic_event(workspace_id="ws_1", event={"subject_id": "s"})
        assert ei.value.status_code == 429
        assert ei.value.retry_after == 30

    def test_429_without_retry_after_header_is_none(self):
        cx, _ = client_with([(429, {"detail": "rate cap reached",
                                    "error": "rate_limited"})])
        with pytest.raises(RateLimitError) as ei:
            cx.emit_logic_event(workspace_id="ws_1", event={"subject_id": "s"})
        assert ei.value.retry_after is None

    def test_429_unparseable_retry_after_is_none(self):
        # HTTP-date form (or garbage) is not delta-seconds — never guess.
        cx, _ = client_with([
            (429, {"detail": "rate cap reached"},
             {"Retry-After": "Fri, 10 Jul 2026 12:00:00 GMT"}),
        ])
        with pytest.raises(RateLimitError) as ei:
            cx.emit_logic_event(workspace_id="ws_1", event={"subject_id": "s"})
        assert ei.value.retry_after is None
