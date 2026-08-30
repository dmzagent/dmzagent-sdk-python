"""Spec 0.8.0 additions: livemode, Idempotency-Key, and 409 → ConflictError.

None of this is covered by the shared contract corpus — `golden-envelopes`
exercises 5 of the 15 methods in §5, and no fixture inspects a request
header or a response's `livemode` (see sdk-spec.md §11.1). These are
SDK-local tests so the three additions are pinned by something.
"""
from __future__ import annotations

import httpx
import pytest

from dmzagent import ConflictError, DMZAgent

_KEY = "ck_test_xxxxxxxxxxxxxxxxxxxxx"

_FULL_ACK = {
    "interaction_id": "int_1",
    "subjects": ["user:ws:bot", "user:ws:cust"],
    "frame_id": "frm_1",
    "accepted": True,
    "n_workspaces": 2,
    "follow_my_data": "/v1/frames/frm_1/story",
}


def _client(handler):
    return DMZAgent(api_key=_KEY, transport=httpx.MockTransport(handler))


def _say(client, **kw):
    return client.subject_says(
        subject_id="user:ws:cust",
        subject_type="chat",
        text="hi",
        agent_subject_id="user:ws:bot",
        **kw,
    )


# --- livemode (§1.2, §2.1, §7.1) ---------------------------------------- #


@pytest.mark.parametrize("wire,expected", [(True, True), (False, False)])
def test_livemode_round_trips(wire, expected):
    client = _client(lambda r: httpx.Response(200, json={**_FULL_ACK, "livemode": wire}))
    assert _say(client).livemode is expected


def test_livemode_absent_is_none_not_false():
    """Absent MUST NOT collapse to False.

    False means "this is test data" — a definite claim. Reading it off a
    response that never carried the field would tell a caller holding a
    live key that their production traffic was test traffic, which is the
    exact confusion the field exists to prevent.
    """
    client = _client(lambda r: httpx.Response(200, json=_FULL_ACK))
    assert _say(client).livemode is None


def test_livemode_non_boolean_is_none():
    client = _client(lambda r: httpx.Response(200, json={**_FULL_ACK, "livemode": "yes"}))
    assert _say(client).livemode is None


def test_capture_also_exposes_livemode():
    client = _client(lambda r: httpx.Response(200, json={**_FULL_ACK, "livemode": False}))
    result = client.capture(
        subject_id="user:ws:cust", kind="observation", subject_type="chat",
        payload={"x": 1},
    )
    assert result.livemode is False


# --- Idempotency-Key (§1.8) --------------------------------------------- #


def test_idempotency_key_is_sent_when_given():
    seen: dict = {}

    def route(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json=_FULL_ACK)

    _say(_client(route), idempotency_key="idem-abc-123")
    assert seen["headers"]["idempotency-key"] == "idem-abc-123"


def test_no_key_means_no_header():
    """The SDK MUST NOT invent one (§1.8).

    A key minted per call is unique per call and deduplicates nothing; a
    key derived from the payload would collapse two genuinely distinct
    events that happen to be identical. Absent is the only correct default.
    """
    seen: dict = {}

    def route(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json=_FULL_ACK)

    _say(_client(route))
    assert "idempotency-key" not in seen["headers"]


def test_capture_forwards_idempotency_key():
    seen: dict = {}

    def route(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json=_FULL_ACK)

    _client(route).capture(
        subject_id="user:ws:cust", kind="observation", subject_type="chat",
        idempotency_key="idem-cap-1",
    )
    assert seen["headers"]["idempotency-key"] == "idem-cap-1"


# --- 409 → ConflictError (§3) ------------------------------------------- #


def test_409_raises_conflict_error_not_server_error():
    from dmzagent import ServerError

    client = _client(lambda r: httpx.Response(409, json={"detail": "already processing"}))
    with pytest.raises(ConflictError) as ei:
        _say(client)
    assert ei.value.status_code == 409
    # Not a transient fault: the duplicate is the caller's own request.
    assert not isinstance(ei.value, ServerError)
