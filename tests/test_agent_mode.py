"""Agent mode: a session governed one step at a time (spec §1.9, §2.11–§2.13).

What these hold, and why each one is here rather than an assertion that
merely passes:

  * A malformed step is refused locally and before any round trip. Each
    rule of §5.22 has its own test, and each asserts *no request was
    made*: a server-side rejection would also raise, and would tell us
    nothing about where the check lives.
  * `runs` is True for "proceed" and "warn" and for nothing else. An
    unknown directive is kept raw and does not run: an unknown word from
    the governor is not a yes.
  * An unanswered step raises. A directive-less 200 is not an answer.
  * The SDK never invents an Idempotency-Key, and sends the caller's.
  * The session handle holds its two ids and nothing else, and infers no
    `attempt_of`.
  * Neither list method follows a cursor on its own; the iterator does,
    and only when asked.
  * `get_approval` on an unknown id is `DMZAgentError` itself.
"""
from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from dmzagent import (
    DIRECTIVES,
    EVENT_KINDS,
    STEP_PHASES,
    AgentSession,
    DMZAgent,
    DMZAgentError,
    ServerError,
    StepResult,
)
from dmzagent.models import Behavior, BehaviorPage

_KEY = "ck_test_agent_mode"
_AGENT = "seat:agent-a"
_SESSION = "sess_4b1e"


def _answer(directive: str = "proceed", **over) -> dict:
    d = {
        "frame_id": "fr_7c21",
        "interaction_id": _SESSION,
        "directive": directive,
        "scope": None if directive == "proceed" else "interaction",
        "reason": "",
        "approval_id": "apr_9" if directive == "hold" else None,
        "settled": True,
        "behaviors": [],
        "anchor": {"ledger_index": 40311, "hash": "c0d9"},
        "livemode": False,
    }
    d.update(over)
    return d


def _behavior(**over) -> dict:
    d = {
        "behavior_id": "bhv_19ac",
        "subject_id": _AGENT,
        "interaction_id": _SESSION,
        "tag": "circumvention",
        "polarity": "negative",
        "strength": 0.82,
        "source": "reasoning",
        "evidence": ["fr_7b90", "fr_7c21"],
        "calls": ["call_12", "call_14"],
        "observed_at": "2026-10-07T15:02:11Z",
        "anchor": {"ledger_index": 40312, "hash": "77ab"},
    }
    d.update(over)
    return d


def _client(handler):
    """A client over MockTransport, plus the list of requests it saw."""
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return DMZAgent(api_key=_KEY, transport=httpx.MockTransport(wrapped)), seen


#: How many requests past the last scripted body the fake will tolerate
#: before it calls the walk unbounded (see test_approvals_and_ledger.py).
_RUNAWAY_AFTER = 5


def _serve(*bodies, status: int = 200):
    """Serve each body in turn, repeating the last — then refuse."""
    box = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        box["n"] += 1
        if box["n"] > len(bodies) + _RUNAWAY_AFTER:
            raise AssertionError(
                f"unbounded pagination: {box['n']} requests for "
                f"{len(bodies)} scripted page(s)")
        return httpx.Response(status, json=bodies[min(box["n"] - 1, len(bodies) - 1)])

    return handler


def _body(req: httpx.Request) -> dict:
    return json.loads(req.content)


_CALL = {"call_id": "call_7", "tool": "Bash", "args": {"command": "ls"}}


# --------------------------------------------------------------------- #
# A malformed step is the harness's mistake, and fails in the harness
# --------------------------------------------------------------------- #

def _refused_locally(match: str, *args, **kwargs) -> None:
    cx, seen = _client(_serve(_answer()))
    with pytest.raises(ValueError) as e:
        cx.agent_step(*args, **kwargs)
    assert match in str(e.value)
    # The assertion that matters. A server-side rejection would raise too,
    # and would not tell us the check is where the mistake is.
    assert seen == [], "a malformed step must not reach the wire"


@pytest.mark.parametrize("phase", ["plan", "", None, "CALL", "tool_call"])
def test_a_step_with_an_unknown_phase_is_refused_before_any_request(phase):
    _refused_locally("phase", _AGENT, _SESSION, phase, **_CALL)


@pytest.mark.parametrize("missing", ["agent_subject_id", "interaction_id"])
@pytest.mark.parametrize("value", [None, "", "  "])
def test_a_step_without_its_ids_is_refused_before_any_request(missing, value):
    ids = {"agent_subject_id": _AGENT, "interaction_id": _SESSION, missing: value}
    _refused_locally(missing, phase="call", **ids, **_CALL)


@pytest.mark.parametrize("phase", ["call", "result"])
@pytest.mark.parametrize("missing", ["call_id", "tool"])
def test_call_and_result_steps_need_a_call_id_and_a_tool(phase, missing):
    kw = {"call_id": "call_7", "tool": "Bash", "status": "ok", missing: None}
    _refused_locally(missing, _AGENT, _SESSION, phase, **kw)


def test_a_result_step_needs_a_status():
    _refused_locally("status", _AGENT, _SESSION, "result", call_id="call_7", tool="Bash")


def test_a_refusal_that_does_not_say_who_refused_is_refused():
    _refused_locally("refused_by", _AGENT, _SESSION, "result",
                     call_id="call_7", tool="Bash", status="refused")


@pytest.mark.parametrize("status", ["ok", "error"])
def test_a_refuser_on_a_call_that_ran_is_refused(status):
    _refused_locally("refused", _AGENT, _SESSION, "result",
                     call_id="call_7", tool="Bash", status=status, refused_by="host")


def test_a_refuser_on_a_call_step_is_refused():
    _refused_locally("refused", _AGENT, _SESSION, "call", refused_by="harness", **_CALL)


@pytest.mark.parametrize("intent", [None, {}, {"paths": ["src/"]}, "do the thing"])
def test_an_intent_step_needs_an_intent_with_text(intent):
    _refused_locally("intent", _AGENT, _SESSION, "intent", intent=intent)


# --------------------------------------------------------------------- #
# What goes on the wire
# --------------------------------------------------------------------- #

def test_a_call_step_sends_only_what_was_given():
    cx, seen = _client(_serve(_answer()))
    cx.agent_step(_AGENT, _SESSION, "call", attempt_of="call_6", **_CALL)
    assert len(seen) == 1
    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == "/v1/agent-stream/step"
    assert _body(req) == {
        "agent_subject_id": _AGENT, "interaction_id": _SESSION, "phase": "call",
        "call_id": "call_7", "tool": "Bash", "args": {"command": "ls"},
        "attempt_of": "call_6"}
    # A step is an agent_session by definition; nothing says so on the wire.
    assert "interaction_kind" not in _body(req)


@pytest.mark.parametrize("returned", [0, "", [], False, {}])
def test_a_falsy_tool_result_is_still_sent(returned):
    """A tool that returned 0 returned something."""
    cx, seen = _client(_serve(_answer()))
    cx.agent_step(_AGENT, _SESSION, "result", call_id="c", tool="t",
                  status="ok", result=returned)
    assert _body(seen[0])["result"] == returned


def test_no_idempotency_key_is_invented():
    cx, seen = _client(_serve(_answer()))
    cx.agent_step(_AGENT, _SESSION, "call", **_CALL)
    assert "idempotency-key" not in seen[0].headers


def test_the_callers_idempotency_key_is_sent_verbatim():
    cx, seen = _client(_serve(_answer()))
    cx.agent_step(_AGENT, _SESSION, "call", idempotency_key="retry-call_7", **_CALL)
    assert seen[0].headers["Idempotency-Key"] == "retry-call_7"
    assert "idempotency_key" not in _body(seen[0])


# --------------------------------------------------------------------- #
# Reading the answer: may this call run?
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("directive,runs", [
    ("proceed", True), ("warn", True),
    ("hold", False), ("block", False), ("shutdown", False)])
def test_runs_is_true_exactly_for_proceed_and_warn(directive, runs):
    cx, _ = _client(_serve(_answer(directive)))
    r = cx.agent_step(_AGENT, _SESSION, "call", **_CALL)
    assert r.directive == directive
    assert r.runs is runs


@pytest.mark.parametrize("unknown", ["quarantine", "", "PROCEED", "proceed ", "allow"])
def test_an_unknown_directive_is_kept_raw_and_does_not_run(unknown):
    """Appendix B lets the server add a directive. An SDK that does not know
    it must not let the call run (spec §1.9)."""
    cx, _ = _client(_serve(_answer(unknown)))
    r = cx.agent_step(_AGENT, _SESSION, "call", **_CALL)
    assert r.directive == unknown
    assert r.runs is False


def test_runs_cannot_be_told_to_disagree_with_the_directive():
    """Derived, not stored: there is no field a constructor could set."""
    assert "runs" not in {f.name for f in dataclasses.fields(StepResult)}
    assert StepResult.from_response(_answer("block")).runs is False


def test_a_hold_names_the_approval_it_waits_on():
    cx, _ = _client(_serve(_answer("hold")))
    r = cx.agent_step(_AGENT, _SESSION, "call", **_CALL)
    assert r.runs is False
    assert r.approval_id == "apr_9"


def test_the_answer_carries_its_behaviors_unrenamed():
    cx, _ = _client(_serve(_answer("block", settled=False, behaviors=[
        {"tag": "circumvention", "polarity": "negative", "strength": 0.82,
         "source": "reasoning", "evidence": ["fr_0", "fr_1"],
         "calls": ["call_0", "call_1"]},
        {"tag": "kept-its-word", "polarity": "neutral", "strength": 0.4,
         "source": "logic", "evidence": ["fr_1"]}])))
    r = cx.agent_step(_AGENT, _SESSION, "call", **_CALL)
    assert r.settled is False
    assert [b.tag for b in r.behaviors] == ["circumvention", "kept-its-word"]
    assert r.behaviors[0].calls == ["call_0", "call_1"]
    assert r.behaviors[1].polarity == "neutral", "an unknown polarity is kept raw"
    assert r.behaviors[1].calls == []
    assert r.behaviors[0].behavior_id is None, "a step's behavior has no record id"
    assert r.livemode is False
    assert r.scope == "interaction"


@pytest.mark.parametrize("body", [{}, {"frame_id": "fr_1"}, {"directive": None}, [1, 2]])
def test_an_answer_with_no_directive_raises_rather_than_returning(body):
    """An unanswered step is not a yes. A result with an empty directive
    would read as 'does not run' — but the caller would also have no idea
    the server never answered, so this raises."""
    cx, _ = _client(_serve(body))
    with pytest.raises(DMZAgentError) as e:
        cx.agent_step(_AGENT, _SESSION, "call", **_CALL)
    assert "directive" in str(e.value)


def test_an_unreachable_governor_raises_server_error():
    cx, _ = _client(_serve({"detail": "unavailable"}, status=503))
    with pytest.raises(ServerError):
        cx.agent_step(_AGENT, _SESSION, "call", **_CALL)


def test_the_constants():
    assert STEP_PHASES == ("intent", "call", "result")
    assert DIRECTIVES == ("proceed", "warn", "hold", "block", "shutdown")
    assert EVENT_KINDS == ("subject_says", "tool_call", "tool_result", "observation"), (
        "a step is not an event kind")


@pytest.mark.parametrize("obj", [
    StepResult.from_response(_answer()),
    Behavior.from_response(_behavior()),
    BehaviorPage.from_response({"behaviors": []}),
])
def test_result_types_are_immutable(obj):
    field = dataclasses.fields(obj)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(obj, field, "x")


# --------------------------------------------------------------------- #
# The session handle
# --------------------------------------------------------------------- #

def test_the_session_sends_each_phase_with_its_two_ids():
    cx, seen = _client(_serve(_answer()))
    s = cx.agent_session(_AGENT, _SESSION)
    s.intent("Add a trace id.", paths=["src/obs/"], tools=["Edit"])
    s.call("call_7", "Bash", {"command": "git push"})
    s.result("call_8", "Read", "ok", result="def main(): ...")
    s.refused("call_9", "Bash", "harness", reason="remote writes are the runner's",
              attempt_of="call_7")

    bodies = [_body(r) for r in seen]
    assert all(b["agent_subject_id"] == _AGENT and b["interaction_id"] == _SESSION
               for b in bodies)
    assert bodies[0] == {"agent_subject_id": _AGENT, "interaction_id": _SESSION,
                         "phase": "intent",
                         "intent": {"text": "Add a trace id.", "paths": ["src/obs/"],
                                    "tools": ["Edit"]}}
    assert bodies[1]["phase"] == "call" and bodies[1]["args"] == {"command": "git push"}
    assert bodies[2]["status"] == "ok" and "refused_by" not in bodies[2]
    assert bodies[3] == {"agent_subject_id": _AGENT, "interaction_id": _SESSION,
                         "phase": "result", "call_id": "call_9", "tool": "Bash",
                         "status": "refused", "refused_by": "harness",
                         "reason": "remote writes are the runner's",
                         "attempt_of": "call_7"}


def test_the_session_infers_no_attempt_of():
    """A second call to the same tool after a refusal is not marked as a
    retry by the handle. Only the caller knows that."""
    cx, seen = _client(_serve(_answer("block"), _answer()))
    s = cx.agent_session(_AGENT, _SESSION)
    s.call("call_7", "Bash", {"command": "git push"})
    s.refused("call_7", "Bash", "governor")
    s.call("call_8", "Bash", {"command": "git push"})
    assert "attempt_of" not in _body(seen[-1])


def test_the_session_holds_its_two_ids_and_nothing_else():
    cx, _ = _client(_serve(_answer()))
    s = cx.agent_session(_AGENT, _SESSION)
    assert isinstance(s, AgentSession)
    assert (s.agent_subject_id, s.interaction_id) == (_AGENT, _SESSION)
    assert not hasattr(s, "__dict__")
    with pytest.raises(AttributeError):
        s.refusals = []  # type: ignore[attr-defined]


def test_each_session_method_passes_its_idempotency_key():
    cx, seen = _client(_serve(_answer()))
    s = cx.agent_session(_AGENT, _SESSION)
    s.intent("x", idempotency_key="k1")
    s.call("c", "t", idempotency_key="k2")
    s.result("c", "t", "error", reason="boom", idempotency_key="k3")
    s.refused("c", "t", "host", idempotency_key="k4")
    s.call("c2", "t")
    assert [r.headers.get("Idempotency-Key") for r in seen] == ["k1", "k2", "k3", "k4", None]


def test_session_result_refuses_a_refusal_and_points_at_refused():
    cx, seen = _client(_serve(_answer()))
    s = cx.agent_session(_AGENT, _SESSION)
    with pytest.raises(ValueError) as e:
        s.result("call_7", "Bash", "refused")
    assert "refused()" in str(e.value)
    assert seen == []


def test_session_refused_still_requires_a_refuser():
    cx, seen = _client(_serve(_answer()))
    s = cx.agent_session(_AGENT, _SESSION)
    with pytest.raises(ValueError):
        s.refused("call_7", "Bash", "")
    assert seen == []


@pytest.mark.parametrize("ids", [("", _SESSION), (_AGENT, ""), (_AGENT, None)])
def test_a_session_cannot_be_opened_without_its_ids(ids):
    cx, _ = _client(_serve(_answer()))
    with pytest.raises(ValueError):
        cx.agent_session(*ids)


# --------------------------------------------------------------------- #
# The conduct record: bounded by default, lazy on request
# --------------------------------------------------------------------- #

def test_list_behaviors_sends_no_filter_it_was_not_given_and_does_not_follow_the_cursor():
    cx, seen = _client(_serve({"behaviors": [_behavior()], "next_cursor": "c2"}))
    page = cx.list_behaviors(_AGENT)
    assert len(seen) == 1, "one page means one request"
    assert seen[0].method == "GET"
    assert seen[0].url.path == f"/v1/subjects/{_AGENT}/behaviors"
    assert dict(seen[0].url.params) == {}
    assert page.next_cursor == "c2"
    assert len(page) == 1
    b = page.behaviors[0]
    assert (b.behavior_id, b.subject_id, b.observed_at) == (
        "bhv_19ac", _AGENT, "2026-10-07T15:02:11Z")
    assert b.anchor == {"ledger_index": 40312, "hash": "77ab"}


def test_list_behaviors_passes_every_filter_and_the_cursor():
    cx, seen = _client(_serve({"behaviors": [], "next_cursor": None}))
    cx.list_behaviors(_AGENT, polarity="negative", interaction_id=_SESSION,
                      since="2026-10-01T00:00:00Z", until="2026-10-07T00:00:00Z",
                      limit=10, cursor="eyJpIjo0MH0")
    assert dict(seen[0].url.params) == {
        "polarity": "negative", "interaction_id": _SESSION,
        "since": "2026-10-01T00:00:00Z", "until": "2026-10-07T00:00:00Z",
        "limit": "10", "cursor": "eyJpIjo0MH0"}


@pytest.mark.parametrize("bad", [0, 101, 500, "10", True])
def test_list_behaviors_refuses_a_page_limit_before_the_round_trip(bad):
    cx, seen = _client(_serve({"behaviors": []}))
    with pytest.raises(ValueError) as e:
        cx.list_behaviors(_AGENT, limit=bad)
    assert "limit" in str(e.value)
    assert seen == []


def test_list_behaviors_keeps_the_servers_order():
    """Newest observed_at first, and not re-sorted by ledger_index: logic
    and reasoning anchor on different chains whose indexes do not compare."""
    cx, _ = _client(_serve({"behaviors": [
        _behavior(behavior_id="b2", anchor={"ledger_index": 5, "hash": "a"}),
        _behavior(behavior_id="b1", anchor={"ledger_index": 900, "hash": "b"})]}))
    assert [b.behavior_id for b in cx.list_behaviors(_AGENT)] == ["b2", "b1"]


def test_iter_behaviors_fetches_a_page_only_when_asked_past_the_one_it_holds():
    cx, seen = _client(_serve(
        {"behaviors": [_behavior(), _behavior()], "next_cursor": "c2"},
        {"behaviors": [_behavior()], "next_cursor": None}))
    it = cx.iter_behaviors(_AGENT, polarity="positive")
    next(it)
    next(it)
    assert len(seen) == 1, "page one's items must not have fetched page two"
    next(it)
    assert len(seen) == 2
    assert dict(seen[1].url.params) == {"polarity": "positive", "cursor": "c2"}
    with pytest.raises(StopIteration):
        next(it)
    assert len(seen) == 2, "a null cursor must end the walk, not fetch again"


def test_breaking_out_of_iter_behaviors_never_requests_the_next_page():
    cx, seen = _client(_serve({"behaviors": [_behavior()], "next_cursor": "c2"}))
    for _ in cx.iter_behaviors(_AGENT):
        break
    assert len(seen) == 1


def test_the_conduct_record_is_read_only_in_the_surface_too():
    """Corrected by correcting the soul, never by editing a behavior (§2.12)."""
    for forbidden in ("delete_behavior", "remove_behavior", "amend_behavior",
                      "update_behavior"):
        assert not hasattr(DMZAgent, forbidden), forbidden


# --------------------------------------------------------------------- #
# One approval, by id
# --------------------------------------------------------------------- #

def test_get_approval_reads_one_approval():
    cx, seen = _client(_serve({
        "approval_id": "apr_9", "status": "approved", "subject_id": _AGENT,
        "action": {"tool": "Bash", "args": {"command": "git push"}},
        "decision": {"decision": "approve", "actor_id": "acct_4471",
                     "decided_at": "2026-10-07T15:04:00Z"}}))
    a = cx.get_approval("apr_9")
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/v1/approvals/apr_9"
    assert a.status == "approved"
    assert a.decision is not None and a.decision.actor_id == "acct_4471"


def test_get_approval_on_an_unknown_id_is_the_base_error():
    """404 maps to DMZAgentError itself; a not-found subtype is deferred (§2.13)."""
    cx, _ = _client(_serve({"detail": "not found"}, status=404))
    with pytest.raises(DMZAgentError) as e:
        cx.get_approval("apr_missing")
    assert type(e.value) is DMZAgentError
    assert e.value.status_code == 404


@pytest.mark.parametrize("bad", ["", "  ", None])
def test_get_approval_without_an_id_is_refused_before_it_reads_the_list(bad):
    """GET /v1/approvals/ is not one approval; it must not be parsed as one."""
    cx, seen = _client(_serve({"approvals": []}))
    with pytest.raises(ValueError):
        cx.get_approval(bad)
    assert seen == []
