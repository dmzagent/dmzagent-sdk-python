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

import dataclasses
from dataclasses import dataclass, field
from typing import Any


def _opt_bool(raw: Any) -> bool | None:
    """A tri-state bool: True, False, or None when the server omitted it.

    `bool(raw)` would fold a missing key into False, which for `livemode`
    reads as "this is test data" — the opposite of the safe assumption when
    the truth is simply unknown. Only a real JSON boolean is accepted.
    """
    return raw if isinstance(raw, bool) else None


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
    # How the caller got this result (spec §4.4). No counterpart on the
    # wire: with the state cache off — the default — these are always
    # False / 0.0 / False.
    #
    # `cache_age_ms` is milliseconds, beside `latency_ms` above, while the
    # client's `cb_cache_ttl` is seconds beside `timeout`. Each sits in the
    # unit its neighbours use.
    cached:          bool = False      # served from the state cache
    cache_age_ms:    float = 0.0       # age of the entry when it was served
    stale:           bool = False      # served past its TTL: the check failed
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

    def as_cached(self, age_s: float, *, stale: bool = False) -> "CheckResult":
        """This result, marked as served from the cache at `age_s` old.

        `latency_ms`, `route_latency_ms`, `checked_at` and `raw` are left
        alone: they describe the check that actually happened, and
        rewriting them to describe the cache hit would erase the only
        record of when the server was last asked.
        """
        return dataclasses.replace(
            self, cached=True, cache_age_ms=max(0.0, age_s) * 1000.0, stale=stale)


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
    # True for a live key, False for a test key (ck_test_…), None when the
    # server omitted it. Never defaulted: guessing "live" on a test key, or
    # vice versa, is exactly the mistake this field exists to prevent.
    livemode:         bool | None = None
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
            livemode         = _opt_bool(data.get("livemode")),
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
    livemode:         bool | None = None
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
            livemode         = _opt_bool(data.get("livemode")),
            raw              = data,
        )


@dataclass(frozen=True)
class OutcomeResult:
    """Return type of `DMZAgent.await_outcome()` (sdk-spec.md §7.3).

    Per-workspace reasoning results for a captured frame. A frame is
    division-scoped: it fans out to every workspace in its division and
    produces one trace per workspace, each entry in `reasoning` naming the
    `workspace_id` that produced it.
    """

    frame_id:         str
    # Fold over `reasoning` computed server-side, with precedence
    # failed > held > applied > no_change > skipped (§2.7). None until at
    # least one trace exists — never guessed. This defaulted to
    # "no_change" when the key was absent, which reported a clean result
    # for a frame nothing had reasoned over yet.
    outcome:          str | None = None
    division_id:      str | None = None
    workspace_ids:    list[str] = field(default_factory=list)
    complete:         bool = False
    error:            dict | None = None
    tags_fired:       list[dict] = field(default_factory=list)
    reasoning:        list[dict] = field(default_factory=list)
    soul_version:     int | None = None
    finished_at:      str = ""
    raw:              dict = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "OutcomeResult":
        summary = data.get("summary") or {}
        return cls(
            frame_id      = data.get("frame_id", ""),
            outcome       = data.get("outcome"),
            division_id   = data.get("division_id"),
            workspace_ids = data.get("workspace_ids", []) or [],
            complete      = bool(summary.get("complete", False)),
            error         = data.get("error"),
            tags_fired    = data.get("tags_fired", []) or [],
            reasoning     = data.get("reasoning", []) or [],
            soul_version  = data.get("soul_version"),
            finished_at   = data.get("finished_at", ""),
            raw           = data,
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
