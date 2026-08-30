"""Contract-test runner against dmzagent-sdk-spec.

Reads the JSON corpora from dmzagent-sdk-spec/contract-tests/ and
drives this SDK to prove parity. The spec path defaults to a sibling
directory; CI sets DMZAGENT_SPEC_PATH to the checked-out tag.

Run with:

    DMZAGENT_SPEC_PATH=../dmzagent-sdk-spec pytest tests/test_contract.py

This is the same corpus every other SDK runs against. Failures here
mean the Python SDK has drifted from the spec.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

import httpx
import pytest

from dmzagent import (
    AuthError,
    CBOpenError,
    DMZAgent,
    DMZAgentError,
    PermissionError,
    RateLimitError,
    ServerError,
    ValidationError,
    verify_webhook_signature,
)


# Resolve the spec path. CI sets DMZAGENT_SPEC_PATH; locally we fall
# back to the sibling sdks-new/dmzagent-sdk-spec/ directory.
_SPEC_PATH_ENV = os.environ.get("DMZAGENT_SPEC_PATH")
_HERE          = Path(__file__).resolve().parent
_DEFAULT_SPEC  = _HERE.parent.parent / "dmzagent-sdk-spec"

SPEC_PATH = Path(_SPEC_PATH_ENV) if _SPEC_PATH_ENV else _DEFAULT_SPEC


def _load(name: str) -> dict:
    return json.loads((SPEC_PATH / "contract-tests" / name).read_text())


def _normalize(obj):
    """Sort keys recursively for byte-stable JSON comparison."""
    if isinstance(obj, dict):
        return {k: _normalize(obj[k]) for k in sorted(obj.keys())}
    if isinstance(obj, list):
        return [_normalize(x) for x in obj]
    return obj


# Map canonical exception names from the spec → Python classes.
EXC_MAP = {
    "ValidationError":                ValidationError,
    "AuthError":                      AuthError,
    "PermissionError":                PermissionError,
    "ServerError":                    ServerError,
    "RateLimitError":                 RateLimitError,
    "DMZAgentError":                 DMZAgentError,
    "CBOpenError":                    CBOpenError,
    # The spec corpus marks client-side validation as
    # "ValidationError_or_ArgumentError". Python uses ValueError for
    # client-side argument validation (idiomatic), so the runner
    # accepts ValueError in that slot.
    "ValidationError_or_ArgumentError": ValueError,
}


@pytest.fixture
def captured():
    """Container the stub transport writes to and tests assert against."""
    return {"requests": []}


@pytest.fixture
def stub_transport(captured):
    """An httpx mock transport that captures every request and replies
    200 with an empty body unless the test installs a custom handler."""
    handler = {"fn": lambda req: httpx.Response(200, json={"interaction_id": "int_stub"})}

    def _route(request: httpx.Request) -> httpx.Response:
        captured["requests"].append({
            "method": request.method,
            "url":    str(request.url),
            "path":   request.url.path,
            "body":   json.loads(request.content) if request.content else {},
            "headers": dict(request.headers),
        })
        return handler["fn"](request)

    transport = httpx.MockTransport(_route)
    transport.set_handler = lambda fn: handler.__setitem__("fn", fn)  # type: ignore[attr-defined]
    return transport


@pytest.fixture
def client(stub_transport):
    return DMZAgent(api_key="ck_test_xxxxxxxxxxxxxxxxxxxxx", transport=stub_transport)


# ===================================================================== #
# Golden envelopes — serialization parity
# ===================================================================== #

def _call_method(client: DMZAgent, method: str, args: dict):
    if method == "subject_says":
        return client.subject_says(**args)
    if method == "tool_call":
        return client.tool_call(**args)
    if method == "tool_result":
        return client.tool_result(**args)
    if method == "observation":
        return client.observation(**args)
    if method == "check":
        return client.check(**args)
    if method == "emit_event":
        return client.emit_event(args.pop("kind"), **args)
    raise AssertionError(f"unknown method: {method}")


@pytest.mark.parametrize("fx", _load("golden-envelopes.json")["fixtures"],
                          ids=lambda f: f["name"])
def test_golden_envelope(fx, client, captured):
    _call_method(client, fx["method"], dict(fx["args"]))
    req = captured["requests"][-1]
    assert req["path"] == fx["expected_path"], (
        f"{fx['name']}: expected {fx['expected_path']!r}, got {req['path']!r}"
    )
    assert _normalize(req["body"]) == _normalize(fx["expected_body"]), (
        f"{fx['name']}: body mismatch\n"
        f"expected: {json.dumps(_normalize(fx['expected_body']), sort_keys=True)}\n"
        f"got:      {json.dumps(_normalize(req['body']), sort_keys=True)}"
    )


@pytest.mark.parametrize("fx", _load("golden-envelopes.json")["validation_failures"],
                          ids=lambda f: f["name"])
def test_validation_failure(fx, stub_transport):
    expected = EXC_MAP[fx["expected_exception"]]
    if fx["method"] == "construct":
        with pytest.raises(expected) as ei:
            DMZAgent(transport=stub_transport, **fx["args"])
    else:
        client = DMZAgent(api_key="ck_test_xxxxxxxxxxxxxxxxxxxxx", transport=stub_transport)
        with pytest.raises(expected) as ei:
            _call_method(client, fx["method"], dict(fx["args"]))
    expected_msg = fx.get("expected_message_contains", "")
    assert expected_msg in str(ei.value), (
        f"expected message to contain {expected_msg!r}, got {str(ei.value)!r}"
    )


# ===================================================================== #
# Signature vectors — webhook helper parity
# ===================================================================== #

def _sign(secret: str, t: int, payload: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"{t}.{payload}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _resolve_header(fx: dict) -> str:
    header = fx["header"]
    if "<COMPUTE>" in header:
        t = int(header.split("t=")[1].split(",")[0])
        v1 = _sign(fx["secret"], t, fx["payload"])
        return header.replace("<COMPUTE>", v1)
    if "<COMPUTE_WITH_OTHER>" in header:
        t = int(header.split("t=")[1].split(",")[0])
        v1 = _sign(fx["header_signed_with"], t, fx["payload"])
        return header.replace("<COMPUTE_WITH_OTHER>", v1)
    return header


@pytest.mark.parametrize("fx", _load("signature-vectors.json")["fixtures"],
                          ids=lambda f: f["name"])
def test_signature_vector(fx):
    header = _resolve_header(fx)
    result = verify_webhook_signature(
        payload=fx["payload"],
        signature_header=header,
        secret=fx["secret"],
        tolerance_seconds=fx["tolerance_seconds"],
        now=fx["now_unix"],
    )
    assert result is fx["valid"], (
        f"{fx['name']}: expected valid={fx['valid']}, got {result}"
    )


# ===================================================================== #
# Error mapping — HTTP status → exception parity
# ===================================================================== #

@pytest.mark.parametrize("fx", _load("error-mapping.json")["fixtures"],
                          ids=lambda f: f["name"])
def test_error_mapping(fx, stub_transport, captured):
    body = fx["body"]
    response_body = body if isinstance(body, dict) else {"detail": body}
    # The corpus attaches response headers to some fixtures (429 carries
    # Retry-After); forward them, or the SDK never sees what it is meant to
    # parse and the vector passes for the wrong reason.
    stub_transport.set_handler(
        lambda req: httpx.Response(
            fx["status"], json=response_body, headers=fx.get("headers") or {}
        )
    )

    client = DMZAgent(api_key="ck_test_xxxxxxxxxxxxxxxxxxxxx", transport=stub_transport)

    if fx["method"] == "guard_with_raise_on_open":
        expected = EXC_MAP[fx["expected_exception"]] if fx.get("expected_exception") else None
        if expected:
            with pytest.raises(expected) as ei:
                with client.guard(**fx["args"]):
                    pass
            for k, v in (fx.get("expected_fields") or {}).items():
                assert getattr(ei.value, k) == v
        else:
            with client.guard(**fx["args"]) as g:
                for k, v in (fx.get("expected_result_fields") or {}).items():
                    assert getattr(g, k) == v
        return

    expected = EXC_MAP[fx["expected_exception"]]
    with pytest.raises(expected) as ei:
        _call_method(client, fx["method"], dict(fx["args"]))
    if "expected_status_code" in fx:
        assert ei.value.status_code == fx["expected_status_code"]
    # Membership, not .get(): the corpus carries an explicit null case for a
    # 429 sent without a Retry-After header, and .get() would make that
    # indistinguishable from the field being absent.
    if "expected_retry_after" in fx:
        assert ei.value.retry_after == fx["expected_retry_after"], (
            f"expected retry_after={fx['expected_retry_after']!r}, "
            f"got {ei.value.retry_after!r}"
        )
