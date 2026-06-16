"""Dataclasses returned by the SDK's public methods.

Result types are deliberately small and dataclass-based — they
serialize cleanly, type-check well, and let consumers use attribute
access (`r.allow`) rather than dict-indexing (`r["allow"]`). The
server's full JSON response is available on `.raw` for anyone who
wants to peek at fields the SDK doesn't surface explicitly yet.

Event emission is accepted-only async by default: the server returns
interaction_id, subjects, frame_id, accepted, n_workspaces, and
follow_my_data. Per-workspace reasoning outcomes are retrieved
out-of-band via await_outcome(), webhooks, or the SDK stream.
Legacy sync envelope fields (outcome, triage_decision, tags_fired,
soul_version, ledger_index) are no longer populated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CheckResult:
    """Return type of `DMZAgent.check()`."""

    state:           str               # "closed" | "half_open" | "open"
    allow:           bool              # False only when state == "open"
    warning:         bool              # True when state == "half_open"
    reason:          str
    fired_policies:  list[dict] = field(default_factory=list)
    anchor:          dict | None = None
    checked_at:      str = ""
    latency_ms:      float = 0.0       # server-side cb.check() latency
    route_latency_ms: float = 0.0      # server-side route handler latency
    raw:             dict = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "CheckResult":
        return cls(
            state           = data.get("state", "closed"),
            allow           = bool(data.get("allow", True)),
            warning         = bool(data.get("warning", False)),
            reason          = data.get("reason", ""),
            fired_policies  = data.get("fired_policies", []) or [],
            anchor          = data.get("anchor"),
            checked_at      = data.get("checked_at", ""),
            latency_ms      = float(data.get("latency_ms", 0)),
            route_latency_ms = float(data.get("route_latency_ms", 0)),
            raw             = data,
        )


@dataclass(frozen=True)
class EmitResult:
    """Return type of all event-emit methods (`subject_says`, `tool_call`, etc.).

    Accepted-only async by default: `frame_id`, `accepted`, `n_workspaces`,
    and `follow_my_data` are available immediately. Per-workspace reasoning
    outcomes are retrieved via `await_outcome()`.
    """

    interaction_id:   str
    subjects:         list[str]  = field(default_factory=list)
    queued:           bool       = True
    accepted:         bool | None = None
    n_workspaces:     int | None  = None
    frame_id:         str | None = None
    follow_my_data:   str | None = None
    # ---- raw passthrough --------------------------------------------
    raw:              dict       = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "EmitResult":
        accepted_raw = data.get("accepted")
        accepted = bool(accepted_raw) if isinstance(accepted_raw, bool) else None
        return cls(
            interaction_id   = data.get("interaction_id", ""),
            subjects         = data.get("subjects", []) or [],
            queued           = bool(data.get("queued", True)),
            accepted         = accepted,
            n_workspaces     = data.get("n_workspaces"),
            frame_id         = data.get("frame_id"),
            follow_my_data   = data.get("follow_my_data"),
            raw              = data,
        )


@dataclass(frozen=True)
class CaptureResult:
    """Return type of `DMZAgent.capture()`.

    Lightweight accepted-only ack. Per-workspace reasoning outcomes
    are retrieved via `await_outcome()`.
    """

    frame_id:         str
    accepted:         bool
    n_workspaces:     int
    interaction_id:   str
    subjects:         list[str] = field(default_factory=list)
    follow_my_data:   str | None = None
    raw:              dict       = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "CaptureResult":
        return cls(
            frame_id         = data.get("frame_id", ""),
            accepted         = bool(data.get("accepted", False)),
            n_workspaces     = int(data.get("n_workspaces", 0)),
            interaction_id   = data.get("interaction_id", ""),
            subjects         = data.get("subjects", []) or [],
            follow_my_data   = data.get("follow_my_data"),
            raw              = data,
        )


@dataclass(frozen=True)
class OutcomeResult:
    """Return type of `DMZAgent.await_outcome()`.

    Per-workspace reasoning results for a captured frame.
    """

    frame_id:         str
    outcome:          str       # skipped | no_change | applied | failed
    error:            dict | None = None
    tags_fired:       list[dict] = field(default_factory=list)
    reasoning:        list[dict] = field(default_factory=list)
    soul_version:     int | None = None
    finished_at:      str = ""
    raw:              dict = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "OutcomeResult":
        return cls(
            frame_id     = data.get("frame_id", ""),
            outcome      = data.get("outcome", "no_change"),
            error        = data.get("error"),
            tags_fired   = data.get("tags_fired", []) or [],
            reasoning    = data.get("reasoning", []) or [],
            soul_version = data.get("soul_version"),
            finished_at  = data.get("finished_at", ""),
            raw          = data,
        )


@dataclass(frozen=True)
class NotificationPrefs:
    email_cadence: str = "off"
    email_paused_until: str | None = None
    push_enabled: bool = False
    phone: str | None = None
    sms_enabled: bool = False
    whatsapp_enabled: bool = False
    webhook_url: str | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "NotificationPrefs":
        return cls(
            email_cadence     = data.get("email_cadence", "off"),
            email_paused_until = data.get("email_paused_until"),
            push_enabled      = bool(data.get("push_enabled", False)),
            phone             = data.get("phone"),
            sms_enabled       = bool(data.get("sms_enabled", False)),
            whatsapp_enabled  = bool(data.get("whatsapp_enabled", False)),
            webhook_url       = data.get("webhook_url"),
            raw               = data,
        )


@dataclass(frozen=True)
class DivisionConfig:
    config: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "DivisionConfig":
        return cls(
            config = data.get("config", {}),
            raw    = data,
        )


@dataclass(frozen=True)
class ReviewEvent:
    event_id: str = ""
    type: str = ""
    review_id: str = ""
    subject_id: str = ""
    tag_id: str = ""
    level: str = "review"
    status: str = "open"
    tier: str = "workspace"
    decision: str | None = None
    workspace_id: str = ""
    division_id: str | None = None
    frame_id: str | None = None
    occurred_at: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "ReviewEvent":
        return cls(
            event_id     = data.get("event_id", ""),
            type         = data.get("type", ""),
            review_id    = data.get("review_id", ""),
            subject_id   = data.get("subject_id", ""),
            tag_id       = data.get("tag_id", ""),
            level        = data.get("level", "review"),
            status       = data.get("status", "open"),
            tier         = data.get("tier", "workspace"),
            decision     = data.get("decision"),
            workspace_id = data.get("workspace_id", ""),
            division_id  = data.get("division_id"),
            frame_id     = data.get("frame_id"),
            occurred_at  = data.get("occurred_at", ""),
            raw          = data,
        )
