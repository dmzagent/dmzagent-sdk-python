"""DMZAgent — Python SDK.

DMZAgent is the codex of trust between minds: a reference work that
indexes how agents reveal themselves (Anima), how they move under
conditions (Augur), and how trust between them is sustained
(Concordia). This SDK is the customer-facing surface for streaming
agent events and gating actions through circuit breakers.

Quick start:

    from dmzagent import DMZAgent

    cx = DMZAgent(api_key="ck_...")

    cx.subject_says(
        agent_subject_id="user:ws:bot",
        subject_id="user:ws:cust",
        text="I want a refund.",
    )

    g = cx.check(subject_id="user:ws:bot")
    if not g.allow:
        return refuse(g.reason)

Or with the high-level Conversation handle:

    with cx.conversation(participants=[
        {"subject_id": "user:ws:bot",  "role": "agent",    "kind": "agent"},
        {"subject_id": "user:ws:cust", "role": "customer", "kind": "human"},
    ]) as conv:
        conv.says("user:ws:cust", "I want a refund.")
        conv.says("user:ws:bot",  "I can help.")
        with conv.guard("user:ws:bot", raise_on_open=True):
            conv.tool_call("user:ws:bot", "refund.issue", {"amount": 99})

This SDK implements the surface defined in dmzagent-sdk-spec at the
version recorded in `__spec_version__`. See sdk-spec.md for the
language-agnostic contract.
"""
from .client import DMZAgent, EVENT_KINDS
from .conversation import Conversation
from .errors import (
    AuthError,
    CBOpenError,
    DMZAgentError,
    PermissionError,
    ConflictError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from .models import (
    CaptureResult,
    CheckResult,
    DivisionConfig,
    EmitResult,
    NotificationPrefs,
    OutcomeResult,
    ReviewEvent,
)
from .subjects import (
    is_canonical_subject_id,
    slugify_subject,
    subject_for_division,
    subject_id_for_division,
    subject_type_from_subject_id,
)
from .webhook import verify_webhook_signature

# dmzagent.concordia — MCP 1.0 governance client (#218 / MCP-1.4).
# Surfaced as a submodule attribute so callers write
# `from dmzagent.concordia import ConcordiaClient`. Top-level import
# stays cheap — the submodule imports lazily on attribute access.
from . import concordia  # noqa: F401

__version__      = "0.6.0"
__spec_version__ = "0.6.0"

__all__ = [
    "DMZAgent",
    "Conversation",
    "CaptureResult",
    "CheckResult",
    "DivisionConfig",
    "EmitResult",
    "NotificationPrefs",
    "OutcomeResult",
    "ReviewEvent",
    "EVENT_KINDS",
    "DMZAgentError",
    "AuthError",
    "PermissionError",
    "ValidationError",
    "ConflictError",
    "RateLimitError",
    "ServerError",
    "CBOpenError",
    "verify_webhook_signature",
    "concordia",
    "__version__",
    "__spec_version__",
]
