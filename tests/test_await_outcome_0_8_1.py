"""Spec 0.8.1: `await_outcome()` against the division-scoped story endpoint.

Mirrors `contract-tests/outcome-vectors.json`. Kept here as well because the
shared corpus runner stubs transport at a coarser level than httpx's
MockTransport, and the request-shape assertion (that no `workspace_id` is
sent) is the whole point of the fix.

What was wrong at 0.8.0, specifically in this SDK: the loop terminated on a
top-level `outcome` key that the story endpoint never returned, so it ran to
`timeout` on every call — and it sent no `workspace_id`, which the endpoint
then required, so the first request 422'd anyway.
"""
from __future__ import annotations

import httpx
import pytest

from dmzagent import DMZAgent, ServerError

_KEY = "ck_test_xxxxxxxxxxxxxxxxxxxxx"


def _story(*, complete: bool, outcome: str = "applied", traces=None) -> dict:
    traces = traces if traces is not None else [
        {"trace_id": "trace_1", "workspace_id": "ws_1", "outcome": "applied"},
        {"trace_id": "trace_2", "workspace_id": "ws_2", "outcome": "no_change"},
    ]
    return {
        "frame_id": "frame_abc",
        "subject_id": "subject:dv_test:acme-bot",
        "division_id": "dv_test",
        "workspace_id": None,
        "workspace_ids": ["ws_1", "ws_2"],
        "outcome": outcome,
        "reasoning": traces,
        "summary": {
            "trace_count": len(traces),
            "workspace_count": 2,
            "complete": complete,
        },
    }


def _client(pages):
    """Serve `pages` in order; the last repeats. Records every request."""
    seen: list[httpx.Request] = []
    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = pages[min(state["i"], len(pages) - 1)]
        state["i"] += 1
        return httpx.Response(200, json=body)

    return DMZAgent(api_key=_KEY, transport=httpx.MockTransport(handler)), seen


class TestRequestShape:
    def test_no_workspace_id_is_sent(self):
        """The regression. The endpoint is division-scoped; the SDK holds no
        workspace to name, and naming one narrows to 1 of N perspectives."""
        cx, seen = _client([_story(complete=True)])
        cx.await_outcome("frame_abc", timeout=5.0)
        assert seen, "no request was made"
        for r in seen:
            assert "workspace_id" not in r.url.params, f"sent workspace_id: {r.url}"

    def test_polls_the_story_path(self):
        cx, seen = _client([_story(complete=True)])
        cx.await_outcome("frame_abc", timeout=5.0)
        assert seen[0].url.path == "/v1/frames/frame_abc/story"


class TestTermination:
    def test_returns_once_complete(self):
        cx, seen = _client([_story(complete=True)])
        res = cx.await_outcome("frame_abc", timeout=5.0)
        assert res.complete is True
        assert len(seen) == 1

    def test_keeps_polling_while_incomplete(self):
        """An incomplete story is not an answer: the endpoint returns 200
        throughout the fan-out, handing back traces as workspaces finish."""
        cx, seen = _client([_story(complete=False), _story(complete=True)])
        res = cx.await_outcome("frame_abc", timeout=5.0)
        assert len(seen) == 2
        assert res.complete is True
        assert len(res.reasoning) == 2

    def test_times_out_rather_than_returning_partial(self):
        cx, seen = _client([_story(complete=False)])
        with pytest.raises(ServerError, match="timed out"):
            cx.await_outcome("frame_abc", timeout=0.3)

    def test_timeout_is_capped_at_120s(self, monkeypatch):
        """§5.10 — MUST cap at 120.0.

        Driven by a fake clock rather than by actually waiting: asserting
        the cap by sleeping through it would put two minutes of wall clock
        into the suite to learn one number.
        """
        from dmzagent import client as client_mod

        now = {"t": 0.0}
        monkeypatch.setattr(client_mod.time, "monotonic", lambda: now["t"])
        monkeypatch.setattr(client_mod.time, "sleep", lambda s: now.__setitem__("t", now["t"] + s))

        cx, _ = _client([_story(complete=False)])
        with pytest.raises(ServerError, match=r"120(\.0)?s"):
            cx.await_outcome("frame_abc", timeout=9999.0)
        assert now["t"] >= 120.0, "gave up before the capped deadline"
        assert now["t"] < 9999.0, "did not cap the timeout at 120s"


class TestResultShape:
    def test_reports_the_server_fold_verbatim(self):
        """`outcome` is folded server-side so four SDKs cannot reach four
        answers. An SDK that recomputed it from `reasoning` — say by taking
        the first trace — would report `applied` here instead of `failed`."""
        page = _story(complete=True, outcome="failed", traces=[
            {"trace_id": "trace_1", "workspace_id": "ws_1", "outcome": "applied"},
            {"trace_id": "trace_2", "workspace_id": "ws_2", "outcome": "failed"},
        ])
        cx, _ = _client([page])
        assert cx.await_outcome("frame_abc", timeout=5.0).outcome == "failed"

    def test_per_trace_workspace_survives_parsing(self):
        cx, _ = _client([_story(complete=True)])
        res = cx.await_outcome("frame_abc", timeout=5.0)
        assert [t["workspace_id"] for t in res.reasoning] == ["ws_1", "ws_2"]

    def test_scope_fields_are_exposed(self):
        cx, _ = _client([_story(complete=True)])
        res = cx.await_outcome("frame_abc", timeout=5.0)
        assert res.division_id == "dv_test"
        assert res.workspace_ids == ["ws_1", "ws_2"]

    def test_held_is_carried_through(self):
        """`held` joined the enum in 0.8.1; the server has emitted it since
        ST-8 while the spec listed four of the five values."""
        page = _story(complete=True, outcome="held", traces=[
            {"trace_id": "trace_1", "workspace_id": "ws_1", "outcome": "held"},
        ])
        cx, _ = _client([page])
        res = cx.await_outcome("frame_abc", timeout=5.0)
        assert res.outcome == "held"
        assert res.reasoning[0]["outcome"] == "held"

    def test_absent_outcome_is_none_not_no_change(self):
        """It defaulted to `no_change`, reporting a clean result for a frame
        nothing had reasoned over. A missing value is not a benign one."""
        page = _story(complete=True)
        page.pop("outcome")
        cx, _ = _client([page])
        assert cx.await_outcome("frame_abc", timeout=5.0).outcome is None
