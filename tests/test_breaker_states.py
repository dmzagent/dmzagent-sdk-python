"""The breaker the server runs (spec §2.2, Appendix B).

Four states — closed, half_open, hold, open — and a policy action
vocabulary of allow / review / block / require_approval. What these hold:

  * `allow` is read from the wire, and believed only under a state that
    allows. A "hold", an "open", a state this SDK has never heard of, or a
    response that never said `allow: true` is a denial — whatever else the
    body says. A new word from the server is not a yes.
  * A hold names its approval, and `guard(raise_on_open=True)` refuses it.
  * Policy actions and anchors are kept as sent; nothing here enumerates
    or rejects them.
"""
from __future__ import annotations

import httpx
import pytest

from dmzagent import CBOpenError, DMZAgent
from dmzagent.models import Approval, CheckResult, Incident

_KEY = "ck_test_breaker"


def _check(**over) -> dict:
    d = {"state": "closed", "allow": True, "warning": False, "reason": "",
         "fired_policies": [], "anchor": None, "pending_approval_id": None}
    d.update(over)
    return d


@pytest.mark.parametrize("state,warning", [("closed", False), ("half_open", True)])
def test_an_allowing_state_with_allow_true_allows(state, warning):
    r = CheckResult.from_response(_check(state=state, warning=warning))
    assert r.allow is True
    assert r.state == state
    assert r.warning is warning


def test_a_hold_denies_and_names_its_approval():
    r = CheckResult.from_response(_check(
        state="hold", allow=False, reason="refund above the reviewed ceiling",
        pending_approval_id="apr_7f3c9a1b",
        fired_policies=[{"cb_policy_id": "cbp_11", "name": "refund ceiling",
                         "action": "require_approval"}]))
    assert r.state == "hold"
    assert r.allow is False
    assert r.awaiting_approval
    assert r.pending_approval_id == "apr_7f3c9a1b"


@pytest.mark.parametrize("state", ["hold", "open", "quarantine", "CLOSED", "closed ", "", None])
def test_allow_true_is_not_believed_under_a_state_that_does_not_allow(state):
    """A server body that says allow under hold, open, or a state this SDK
    does not know reads as a denial (spec §2.2, Appendix B)."""
    r = CheckResult.from_response(_check(state=state, allow=True))
    assert r.allow is False


def test_an_unknown_state_is_kept_raw():
    assert CheckResult.from_response(_check(state="quarantine")).state == "quarantine"


@pytest.mark.parametrize("allow", [None, "true", 1, False])
def test_allow_is_only_a_json_true(allow):
    body = _check(allow=allow)
    if allow is None:
        del body["allow"]
    assert CheckResult.from_response(body).allow is False


def test_a_response_with_no_state_is_not_read_as_closed():
    body = _check()
    del body["state"]
    r = CheckResult.from_response(body)
    assert r.state == ""
    assert r.allow is False


@pytest.mark.parametrize("action", ["allow", "review", "block", "require_approval",
                                    "escalate"])
def test_policy_actions_are_kept_verbatim_everywhere(action):
    """No vocabulary is enforced on `action`: the server's words, as sent,
    on CheckResult, Approval and Incident alike."""
    policy = {"cb_policy_id": "cbp_1", "name": "the operator's words", "action": action}
    assert CheckResult.from_response(_check(fired_policies=[policy])).fired_policies == [policy]
    assert Approval.from_response({"approval_id": "a", "status": "pending",
                                   "fired_policies": [policy]}).fired_policies == [policy]
    assert Incident.from_response({"incident_id": "i", "status": "open", "kind": "cb_open",
                                   "fired_policies": [policy]}).fired_policies == [policy]


def test_the_anchor_is_kept_with_its_ledger_event_id():
    anchor = {"ledger_index": 40197, "hash": "b1c4", "ledger_event_id": "le_91"}
    assert CheckResult.from_response(_check(anchor=anchor)).anchor == anchor


def test_guard_refuses_a_hold():
    cx = DMZAgent(api_key=_KEY, transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=_check(
            state="hold", allow=False, pending_approval_id="apr_1"))))
    with pytest.raises(CBOpenError), cx.guard(subject_id="user:ws_test:bot",
                                              raise_on_open=True):
        pytest.fail("the guarded block ran under a hold")


def test_guard_refuses_an_unknown_state_that_claims_allow():
    cx = DMZAgent(api_key=_KEY, transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=_check(state="quarantine", allow=True))))
    with pytest.raises(CBOpenError), cx.guard(subject_id="user:ws_test:bot",
                                              raise_on_open=True):
        pytest.fail("the guarded block ran under an unknown state")
