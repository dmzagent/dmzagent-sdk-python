"""Exception hierarchy for the DMZAgent SDK.

The hierarchy is deliberately shallow — most consumers only need to
catch DMZAgentError to bail out gracefully, or CBOpenError specifically
when they want to handle a blocked subject differently from other
failures.

  DMZAgentError                base
    ├── AuthError               API key invalid, expired, revoked
    ├── PermissionError         API key valid but lacks the needed scope
    ├── ValidationError         server rejected the payload (400 malformed,
    │                           422 well-formed but unprocessable)
    ├── RateLimitError          429 — rate cap reached; retry after
    │                           `retry_after` seconds (caller's decision)
    ├── ServerError             5xx from DMZAgent; safe to retry
    └── CBOpenError             cb.check() returned open — action blocked

`CBOpenError` is intentionally an exception (not just a flag) so that
production code paths that wrap CB checks can use `try/except CBOpenError`
as a natural control-flow seam, with the same idiom as other
authorization-failure exceptions.
"""
from __future__ import annotations


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
    """The server rejected the payload — 400 (malformed) or 422
    (well-formed but unprocessable, e.g. a bad event or rulebook)."""


class RateLimitError(DMZAgentError):
    """The server returned 429 — a rate cap was reached.

    Attributes:
        retry_after  seconds until retrying can succeed, parsed from the
                     response's ``Retry-After`` header (delta-seconds
                     form); ``None`` when the header is absent or
                     unparseable. The SDK never sleeps or retries
                     automatically — surface the value and let the
                     caller decide.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: dict | str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, body=body)
        self.retry_after = retry_after


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
