"""The white-label approval control and the readable ledger (spec §2.8–§2.10).

What these hold, and why each one is here rather than an assertion that
merely passes:

  * `actor_id` is refused locally and before any round trip. The point of
    a human-in-the-loop control is that a person is on the other end, and
    an approval whose actor is the integration that requested it records
    nobody. The test asserts *no request was made*, because a server-side
    rejection would also raise and would tell us nothing about where the
    check lives.
  * A settled or expired approval is `ConflictError`, not a retry. The
    call did not fail, it lost.
  * Expiry declines, and `on_expiry` cannot be talked into anything else
    by a server that sends something else.
  * Neither list method follows a cursor on its own; the iterators do,
    and only when the consumer asks for the next item.
  * There is no method that closes an incident, because the ledger has no
    endpoint for one.
"""
from __future__ import annotations

import httpx
import pytest

from dmzagent import DMZAgent
from dmzagent.errors import ConflictError
from dmzagent.models import Approval, CheckResult

_KEY = "ck_test_approvals"


def _approval(status: str = "pending", **over) -> dict:
    d = {
        "approval_id": "apr_7f3c9a1b",
        "status": status,
        "subject_id": "user:ws_test:checkout-bot",
        "interaction_id": "ix_2b8e",
        "frame_id": "fr_91ac",
        "action": {"tool": "refund.issue", "args": {"amount": 9900}},
        "reason": "refund above the reviewed ceiling",
        "fired_policies": [
            {"cb_policy_id": "cbp_11", "name": "refund ceiling",
             "action": "require_approval"}],
        "requested_at": "2026-09-09T12:00:00Z",
        "expires_at": "2026-09-09T12:15:00Z",
        "on_expiry": "decline",
        "anchor": {"ledger_index": 40197, "hash": "b1c4"},
        "decision": None,
    }
    d.update(over)
    return d


def _incident(**over) -> dict:
    d = {
        "incident_id": "inc_5d2a70",
        "status": "remediated",
        "kind": "cb_open",
        "subject_id": "user:ws_test:checkout-bot",
        "frame_id": "fr_91ac",
        "opened_at": "2026-09-09T11:58:02Z",
        "closed_at": "2026-09-09T12:04:31Z",
        "reason": "refund above the reviewed ceiling",
        "fired_policies": [],
        "remediations": [{
            "remediation_id": "rem_88fe", "kind": "approval",
            "approval_id": "apr_7f3c9a1b", "outcome": "approved",
            "actor_id": "acct_4471", "reason": "verified by phone",
            "occurred_at": "2026-09-09T12:04:31Z",
            "anchor": {"ledger_index": 40202, "hash": "9ee0"}}],
        "anchor": {"ledger_index": 40197, "hash": "b1c4"},
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
#: before it calls the walk unbounded. Only a client that keeps following
#: a cursor nobody advanced ever reaches it.
_RUNAWAY_AFTER = 5


def _serve(*bodies, status: int = 200):
    """Serve each body in turn, repeating the last — then refuse.

    The repeat matters: a page body carrying `next_cursor` is served again
    and again, which is exactly what a client that auto-paginates will keep
    asking for. Left unbounded, the fake would let that client *hang*, and
    a hang is not a failing assertion — CI reports a timeout with no test
    named, which is the least useful red there is.

    So the fake stops. Five requests past the script it raises, and the
    unbounded walk becomes a named failure in the test that provoked it.
    """
    box = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        box["n"] += 1
        if box["n"] > len(bodies) + _RUNAWAY_AFTER:
            raise AssertionError(
                f"unbounded pagination: {box['n']} requests for "
                f"{len(bodies)} scripted page(s) — the caller asked for one "
                f"page and the SDK kept following the cursor")
        return httpx.Response(status, json=bodies[min(box["n"] - 1, len(bodies) - 1)])

    return handler


# --------------------------------------------------------------------- #
# The decision records a human, or it does not happen
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("actor", ["", "   ", None, 4471])
def test_decide_approval_refuses_a_decision_with_no_human_before_any_request(actor):
    cx, seen = _client(_serve(_approval("approved")))
    with pytest.raises(ValueError) as e:
        cx.decide_approval("apr_7f3c9a1b", "approve", actor_id=actor)
    assert "actor_id" in str(e.value)
    # The assertion that matters. A server-side rejection would raise too,
    # and would not tell us the check is where the mistake is.
    assert seen == [], "a decision with no human must not reach the wire"


def test_decide_approval_refuses_an_unknown_decision_before_any_request():
    cx, seen = _client(_serve(_approval("approved")))
    with pytest.raises(ValueError) as e:
        cx.decide_approval("apr_7f3c9a1b", "maybe", actor_id="acct_4471")
    assert "decision" in str(e.value)
    assert seen == []


def test_decide_approval_sends_the_actor_and_returns_the_decision():
    decided = _approval("approved", decision={
        "decision": "approve", "actor_id": "acct_4471", "actor_label": "Dana R.",
        "reason": "verified the order by phone", "decided_at": "2026-09-09T12:04:31Z"})
    cx, seen = _client(_serve(decided))

    a = cx.decide_approval("apr_7f3c9a1b", "approve", actor_id="acct_4471",
                           actor_label="Dana R.", reason="verified the order by phone")

    assert len(seen) == 1
    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == "/v1/approvals/apr_7f3c9a1b/decision"
    import json
    body = json.loads(req.content)
    assert body == {"decision": "approve", "actor_id": "acct_4471",
                    "actor_label": "Dana R.", "reason": "verified the order by phone"}
    assert a.status == "approved"
    assert a.decision is not None
    assert a.decision.actor_id == "acct_4471"
    assert a.decision.actor_label == "Dana R."
    assert not a.is_pending


def test_the_convenience_wrappers_still_require_a_human():
    cx, seen = _client(_serve(_approval("declined")))
    for call in (cx.approve_approval, cx.decline_approval):
        with pytest.raises(ValueError):
            call("apr_7f3c9a1b", actor_id="")
    assert seen == []


def test_approve_and_decline_send_their_own_verb():
    import json
    cx, seen = _client(_serve(_approval("approved"), _approval("declined")))
    cx.approve_approval("apr_1", actor_id="acct_1")
    cx.decline_approval("apr_2", actor_id="acct_2")
    assert [json.loads(r.content)["decision"] for r in seen] == ["approve", "decline"]


# --------------------------------------------------------------------- #
# A settled approval is lost, not failed
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("settled", ["approved", "declined", "expired"])
def test_deciding_a_settled_approval_is_a_conflict_naming_its_state(settled):
    cx, _ = _client(_serve({"detail": "already settled", "status": settled}, status=409))
    with pytest.raises(ConflictError) as e:
        cx.decide_approval("apr_7f3c9a1b", "approve", actor_id="acct_9002")
    assert settled in str(e.value)
    assert e.value.status_code == 409
    assert e.value.body["status"] == settled


def test_a_409_without_an_approval_status_still_reads_as_the_idempotency_conflict():
    """The other 409. Same type, and the message must not assert the wrong cause."""
    cx, _ = _client(_serve({"detail": "in flight"}, status=409))
    with pytest.raises(ConflictError) as e:
        cx.decide_approval("apr_7f3c9a1b", "approve", actor_id="acct_4471")
    assert "Idempotency-Key" in str(e.value)


# --------------------------------------------------------------------- #
# Expiry fails closed
# --------------------------------------------------------------------- #

def test_on_expiry_is_decline_even_if_the_server_says_otherwise():
    """An approval that becomes an allow because nobody looked at it is
    not a human-in-the-loop control. There is no path — server-sent or
    otherwise — by which this SDK reports one."""
    a = Approval.from_response(_approval(on_expiry="approve"))
    assert a.on_expiry == "decline"


def test_an_expired_approval_carries_no_decision():
    a = Approval.from_response(_approval("expired"))
    assert a.decision is None
    assert not a.is_pending


# --------------------------------------------------------------------- #
# Paging: bounded by default, lazy on request
# --------------------------------------------------------------------- #

def test_list_approvals_defaults_to_pending_and_does_not_follow_the_cursor():
    cx, seen = _client(_serve({"approvals": [_approval()], "next_cursor": "c2"}))
    page = cx.list_approvals()
    assert len(seen) == 1, "one page means one request"
    assert dict(seen[0].url.params) == {"status": "pending"}
    assert page.next_cursor == "c2"
    assert len(page) == 1


def test_list_approvals_passes_every_filter():
    cx, seen = _client(_serve({"approvals": [], "next_cursor": None}))
    cx.list_approvals(status="approved", subject_id="user:ws_test:bot",
                      limit=50, cursor="eyJpIjo0MH0")
    assert dict(seen[0].url.params) == {
        "status": "approved", "subject_id": "user:ws_test:bot",
        "limit": "50", "cursor": "eyJpIjo0MH0"}


@pytest.mark.parametrize("bad", [0, -1, 101, 1000, "50", 2.5, True])
def test_a_page_limit_the_server_would_reject_is_refused_before_the_round_trip(bad):
    cx, seen = _client(_serve({"approvals": []}))
    with pytest.raises(ValueError) as e:
        cx.list_approvals(limit=bad)
    assert "limit" in str(e.value)
    assert seen == []


def test_iter_approvals_fetches_a_page_only_when_asked_past_the_one_it_holds():
    pages = [
        {"approvals": [_approval(), _approval()], "next_cursor": "c2"},
        {"approvals": [_approval()], "next_cursor": None},
    ]
    cx, seen = _client(_serve(*pages))

    it = cx.iter_approvals()
    next(it)
    assert len(seen) == 1, "the first item must not have fetched page two"
    next(it)
    assert len(seen) == 1
    next(it)                       # exhausts page one, fetches page two
    assert len(seen) == 2
    with pytest.raises(StopIteration):
        next(it)
    assert len(seen) == 2, "a null cursor must end the walk, not fetch again"


def test_breaking_out_of_iter_approvals_never_requests_the_next_page():
    cx, seen = _client(_serve({"approvals": [_approval()], "next_cursor": "c2"}))
    for _ in cx.iter_approvals():
        break
    assert len(seen) == 1


# --------------------------------------------------------------------- #
# The ledger
# --------------------------------------------------------------------- #

def test_get_incidents_defaults_to_all_and_parses_remediations():
    cx, seen = _client(_serve({"incidents": [_incident()], "next_cursor": None}))
    page = cx.get_incidents()
    assert dict(seen[0].url.params) == {"status": "all"}
    inc = page.incidents[0]
    assert inc.status == "remediated"
    assert not inc.is_open
    assert len(inc.remediations) == 1
    r = inc.remediations[0]
    assert r.kind == "approval"
    assert r.approval_id == "apr_7f3c9a1b"
    assert r.anchor == {"ledger_index": 40202, "hash": "9ee0"}


def test_get_incidents_passes_the_whole_window():
    cx, seen = _client(_serve({"incidents": []}))
    cx.get_incidents(status="open", subject_id="user:ws_test:bot",
                     since="2026-09-01T00:00:00Z", until="2026-09-09T00:00:00Z",
                     limit=100)
    assert dict(seen[0].url.params) == {
        "status": "open", "subject_id": "user:ws_test:bot",
        "since": "2026-09-01T00:00:00Z", "until": "2026-09-09T00:00:00Z",
        "limit": "100"}


def test_an_unanswered_incident_is_an_incident_with_no_remediations():
    """Not an error, not an empty result, and not collapsed to None."""
    cx, _ = _client(_serve({"incidents": [
        _incident(status="open", closed_at=None, remediations=[])]}))
    inc = cx.get_incidents(status="open").incidents[0]
    assert inc.is_open
    assert inc.remediations == []
    assert inc.closed_at is None


def test_the_incident_anchor_is_the_one_the_check_handed_back():
    """The whole point of making the ledger readable: an anchor recorded
    at check time finds exactly this entry, and the hashes compare."""
    checked = CheckResult.from_response({
        "state": "open", "allow": False, "warning": False, "reason": "ceiling",
        "anchor": {"ledger_index": 40197, "hash": "b1c4"}})
    cx, _ = _client(_serve({"incidents": [_incident()]}))
    inc = cx.get_incidents().incidents[0]
    assert inc.anchor == checked.anchor


def test_iter_incidents_walks_pages_lazily():
    cx, seen = _client(_serve(
        {"incidents": [_incident()], "next_cursor": "c2"},
        {"incidents": [_incident()], "next_cursor": None}))
    got = list(cx.iter_incidents())
    assert len(got) == 2
    assert len(seen) == 2


def test_the_ledger_is_append_only_in_the_surface_too():
    """A convenience that reads as closing an incident would describe a
    ledger this is not — there is no endpoint behind one (spec §5.21)."""
    for forbidden in ("close_incident", "resolve_incident", "delete_incident",
                      "update_incident"):
        assert not hasattr(DMZAgent, forbidden), forbidden


# --------------------------------------------------------------------- #
# The link back to check(): a denial that names an approval is an ask
# --------------------------------------------------------------------- #

def test_a_denial_naming_an_approval_is_an_ask_not_a_refusal():
    r = CheckResult.from_response({
        "state": "open", "allow": False, "warning": False,
        "reason": "refund above the reviewed ceiling",
        "pending_approval_id": "apr_7f3c9a1b"})
    assert r.allow is False
    assert r.awaiting_approval
    assert r.pending_approval_id == "apr_7f3c9a1b"


def test_a_plain_denial_is_not_awaiting_anything():
    r = CheckResult.from_response({"state": "open", "allow": False, "warning": False})
    assert r.allow is False
    assert not r.awaiting_approval
    assert r.pending_approval_id is None


def test_an_older_response_without_the_field_still_refuses():
    """The field is additive on purpose: a client reading `allow` alone
    must not start allowing what it used to deny."""
    r = CheckResult.from_response({"state": "open", "allow": False, "warning": False,
                                   "reason": "policy fired"})
    assert r.allow is False
    assert r.pending_approval_id is None
