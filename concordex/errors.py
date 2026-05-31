"""Exception hierarchy for the Concordex SDK.

The hierarchy is deliberately shallow — most consumers only need to
catch ConcordexError to bail out gracefully, or CBOpenError specifically
when they want to handle a blocked subject differently from other
failures.

  ConcordexError                base
    ├── AuthError               API key invalid, expired, revoked
    ├── PermissionError         API key valid but lacks the needed scope
    ├── ValidationError         server rejected the payload as malformed
    ├── ServerError             5xx from Concordex; safe to retry
    └── CBOpenError             cb.check() returned open — action blocked

`CBOpenError` is intentionally an exception (not just a flag) so that
production code paths that wrap CB checks can use `try/except CBOpenError`
as a natural control-flow seam, with the same idiom as other
authorization-failure exceptions.
"""
from __future__ import annotations


class ConcordexError(Exception):
    """Base class for every error raised by the SDK."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 body: dict | str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class AuthError(ConcordexError):
    """API key was rejected (missing / invalid / revoked)."""


class PermissionError(ConcordexError):  # noqa: A001 — intentional shadowing
    """API key is valid but lacks the scope required for this operation."""


class ValidationError(ConcordexError):
    """The server returned 400 — the payload was malformed."""


class ServerError(ConcordexError):
    """The server returned 5xx. Caller may safely retry with backoff."""


class CBOpenError(ConcordexError):
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
