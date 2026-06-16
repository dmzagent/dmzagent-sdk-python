"""Webhook signature verification helper.

DMZAgent outbound webhooks are signed with HMAC-SHA256 using the
subscription's secret. The signature header format is:

    t=<unix_seconds>,v1=<hex_hmac_sha256(secret, f"{t}.{payload}")>

This helper is what customers wire into their webhook receiver to
prove the request came from DMZAgent (and not a spoofed source) and
that the request is recent (not a replay).

Reference vectors live in dmzagent-sdk-spec/contract-tests/
signature-vectors.json. The contract test runner verifies this
implementation against every vector.
"""
from __future__ import annotations

import hashlib
import hmac
import time


DEFAULT_TOLERANCE_SECONDS = 300


def verify_webhook_signature(
    payload: str | bytes,
    signature_header: str,
    secret: str,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    *,
    now: int | None = None,
) -> bool:
    """Return True if the signature header is valid for the payload + secret.

    Returns False (never raises) for any of: malformed header, missing
    `t` or `v1`, non-numeric timestamp, signature mismatch, timestamp
    outside the tolerance window.

    Args:
        payload:           the raw request body. str → utf-8 encoded.
        signature_header:  the value of the DMZAgent-Signature header.
        secret:            the subscription's signing secret.
        tolerance_seconds: max age the timestamp may have, in seconds.
                           Defaults to 300 (5 minutes).
        now:               override the wall clock — for testing. Unix
                           seconds.
    """
    if not signature_header or not secret:
        return False

    fields: dict[str, str] = {}
    for part in signature_header.split(","):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        fields[k.strip()] = v.strip()

    t_raw = fields.get("t")
    v1    = fields.get("v1")
    if not t_raw or not v1:
        return False
    try:
        t_unix = int(t_raw)
    except ValueError:
        return False

    if tolerance_seconds is not None and tolerance_seconds >= 0:
        clock = int(time.time()) if now is None else int(now)
        if abs(clock - t_unix) > tolerance_seconds:
            return False

    if isinstance(payload, str):
        payload_bytes = payload.encode("utf-8")
    else:
        payload_bytes = payload

    signed = hmac.new(
        secret.encode("utf-8"),
        f"{t_unix}.".encode("utf-8") + payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signed, v1)
