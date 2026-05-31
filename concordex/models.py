"""Dataclasses returned by the SDK's public methods.

Result types are deliberately small and dataclass-based — they
serialize cleanly, type-check well, and let consumers use attribute
access (`r.allow`) rather than dict-indexing (`r["allow"]`). The
server's full JSON response is available on `.raw` for anyone who
wants to peek at fields the SDK doesn't surface explicitly yet.

The synchronous fields on `EmitResult` (frame_id, subject_id, outcome,
triage_decision, tags_fired, scored_by_canons, soul_version,
ledger_index, follow_my_data) are populated when the server reasons
about the event inline (default). Setting `X-Concordex-Async: true` on
the request switches the server back to fire-and-forget mode, in
which case only `interaction_id`, `subjects`, and `queued=True` are
populated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CheckResult:
    """Return type of `Concordex.check()`."""

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

    Synchronous mode (default) populates the rich envelope: `frame_id`,
    `subject_id`, `outcome`, `triage_decision`, `tags_fired`,
    `scored_by_canons`, `soul_version`, `ledger_index`, and a
    `follow_my_data` dashboard path for the trace.

    Async mode (`X-Concordex-Async: true`) populates only
    `interaction_id`, `subjects`, and `queued=True`. Sync-only fields
    are `None` / empty in that case.
    """

    interaction_id:   str
    subjects:         list[str]  = field(default_factory=list)
    queued:           bool       = True
    # ---- sync envelope fields (None / empty in async mode) -----------
    frame_id:         str | None = None
    subject_id:       str | None = None
    outcome:          str | None = None    # scored | tagged | no_tags_fired | rejected | error
    triage_decision:  str | None = None    # deep | shallow
    tags_fired:       list[str]  = field(default_factory=list)
    scored_by_canons: list[str]  = field(default_factory=list)
    soul_version:     int | None = None
    ledger_index:     int | None = None
    follow_my_data:   str | None = None
    # ---- raw passthrough --------------------------------------------
    raw:              dict       = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "EmitResult":
        return cls(
            interaction_id   = data.get("interaction_id", ""),
            subjects         = data.get("subjects", []) or [],
            queued           = bool(data.get("queued", True)),
            frame_id         = data.get("frame_id"),
            subject_id       = data.get("subject_id"),
            outcome          = data.get("outcome"),
            triage_decision  = data.get("triage_decision"),
            tags_fired       = data.get("tags_fired", []) or [],
            scored_by_canons = data.get("scored_by_canons", []) or [],
            soul_version     = data.get("soul_version"),
            ledger_index     = data.get("ledger_index"),
            follow_my_data   = data.get("follow_my_data"),
            raw              = data,
        )
