"""Exception hierarchy for the DMZAgent SDK.

The hierarchy is deliberately shallow — most consumers only need to
catch DMZAgentError to bail out gracefully, or CBOpenError specifically
when they want to handle a blocked subject differently from other
failures.

  DMZAgentError                base
    ├── AuthError               API key invalid, expired, revoked
    ├── PermissionError         API key valid but lacks the needed scope
    ├── ValidationError         server rejected the payload (400 or 422)
    ├── RateLimitError          429; carries retry_after when the server sent it
    ├── ServerError             5xx from DMZAgent; safe to retry
    └── CBOpenError             cb.check() returned open — action blocked

`CBOpenError` is intentionally an exception (not just a flag) so that
production code paths that wrap CB checks can use `try/except CBOpenError`
as a natural control-flow seam, with the same idiom as other
authorization-failure exceptions.
"""
from __future__ import annotations

from typing import Any


class DMZAgentError(Exception):
    """Base class for every error raised by the SDK."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 body: dict | str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class AuthError(DMZAgentError):
    """API key was rejected (missing / invalid / revoked)."""


class PermissionError(DMZAgentError):  # noqa: A001 — intentional shadowing
    """API key is valid but lacks the scope required for this operation."""


class ValidationError(DMZAgentError):
    """The server rejected the payload.

    400 means the request was malformed. 422 means it parsed but failed
    evaluation — the spec's error taxonomy maps both here, because the
    caller's remedy is the same: fix the request, do not retry it unchanged.
    `status_code` distinguishes them when that matters.
    """


class RateLimitError(DMZAgentError):
    """The server returned 429 — the caller is being rate limited.

    Attributes:
        retry_after  seconds to wait, from the `Retry-After` response header,
                     or None when the server did not send one. Callers should
                     handle None rather than assume a default: the spec has a
                     vector for each case precisely because both occur.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: dict[str, Any] | str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, body=body)
        self.retry_after = retry_after


class ConflictError(DMZAgentError):
    """The server returned 409 — an Idempotency-Key request is in flight.

    Distinct from ServerError because it is not a transient fault: the
    duplicate is the caller's *own* earlier request, still running. Retrying
    the same key after a short pause replays that request's stored response
    rather than producing a second side effect, so the caller can wait and
    retry safely — but the SDK never does so on its own (spec §1.8).
    """


class ServerError(DMZAgentError):
    """The server returned 5xx. Caller may safely retry with backoff."""


class CBOpenError(DMZAgentError):
    """The circuit breaker is open for this subject. Action MUST NOT proceed.

    Attributes:
        reason          human-readable rationale from the policy decision
        fired_policies  list of `{cb_policy_id, name, action}` that fired
        anchor          ledger anchor for the transition (auditable proof)
        scope_ref       the subject/interaction id that was blocked
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = "",
        fired_policies: list[dict] | None = None,
        anchor: dict | None = None,
        scope_ref: str = "",
    ) -> None:
        super().__init__(message)
        self.reason          = reason
        self.fired_policies  = fired_policies or []
        self.anchor          = anchor
        self.scope_ref       = scope_ref
