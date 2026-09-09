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
    # The approval this denial is waiting on, or None (spec §2.2). Non-None
    # only alongside allow=False. It is a field rather than a fourth state
    # so that code reading `allow` alone still refuses: a client that has
    # never heard of approvals must not start allowing what it used to deny.
    pending_approval_id: str | None = None
    raw:             dict = field(default_factory=dict)

    @property
    def awaiting_approval(self) -> bool:
        """This is an ask, not a refusal — a human can still clear it.

        The difference `pending_approval_id` exists to express: branch on
        it to show your approval UI instead of telling the user no.
        """
        return self.pending_approval_id is not None

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
            pending_approval_id = data.get("pending_approval_id"),
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


# ---------------------------------------------------------------------------
# Human-in-the-loop approvals and the incident ledger (spec §2.8–§2.10, 0.10.0)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalDecision:
    """The human half of an `Approval` — who decided, and why.

    `actor_id` is the *caller's* identifier for a person, not ours. We
    resolve it against no directory and store it as given, which is what
    lets a customer's own users decide without ever holding an account
    here.
    """

    decision:    str               # "approve" | "decline"
    actor_id:    str
    actor_label: str | None = None
    reason:      str | None = None
    decided_at:  str = ""

    @classmethod
    def from_response(cls, data: dict) -> "ApprovalDecision":
        return cls(
            decision    = data.get("decision", ""),
            actor_id    = data.get("actor_id", ""),
            actor_label = data.get("actor_label"),
            reason      = data.get("reason"),
            decided_at  = data.get("decided_at", ""),
        )


@dataclass(frozen=True)
class Approval:
    """An action held pending a human decision (spec §7.12).

    Every field here is something *you* render. There is no message
    written for your end user, no copy of ours, and no display string:
    `reason` and each `fired_policies[].name` are the words your operator
    typed when they wrote the policy, and `action` is the call your agent
    was about to make, verbatim. Building display text out of them is
    your job precisely because a sentence we wrote would read the same in
    every customer's product.

    `expires_at` is left as the server's ISO-8601 string rather than a
    parsed countdown. Seconds-remaining computed at parse time is wrong
    by however long you held the object, and the caller rendering an
    approval deadline is exactly the caller who holds it.
    """

    approval_id:    str
    status:         str            # "pending" | "approved" | "declined" | "expired"
    subject_id:     str = ""
    interaction_id: str | None = None
    frame_id:       str | None = None
    action:         dict = field(default_factory=dict)   # {"tool": ..., "args": {...}}
    reason:         str = ""
    fired_policies: list[dict] = field(default_factory=list)
    requested_at:   str = ""
    expires_at:     str = ""
    on_expiry:      str = "decline"
    anchor:         dict | None = None
    decision:       ApprovalDecision | None = None
    raw:            dict = field(default_factory=dict)

    @property
    def is_pending(self) -> bool:
        return self.status == "pending"

    @classmethod
    def from_response(cls, data: dict) -> "Approval":
        raw_decision = data.get("decision")
        return cls(
            approval_id    = data.get("approval_id", ""),
            status         = data.get("status", ""),
            subject_id     = data.get("subject_id", ""),
            interaction_id = data.get("interaction_id"),
            frame_id       = data.get("frame_id"),
            action         = data.get("action") or {},
            reason         = data.get("reason", ""),
            fired_policies = data.get("fired_policies") or [],
            requested_at   = data.get("requested_at", ""),
            expires_at     = data.get("expires_at", ""),
            # Not defaulted from the server's value: expiry declines, and
            # a server that ever sent "approve" would be describing a
            # control this SDK does not implement (spec §2.9).
            on_expiry      = "decline",
            anchor         = data.get("anchor"),
            decision       = ApprovalDecision.from_response(raw_decision)
                             if isinstance(raw_decision, dict) else None,
            raw            = data,
        )


@dataclass(frozen=True)
class ApprovalPage:
    """One page of `list_approvals()` (spec §7.11).

    `next_cursor` is None on the last page. Nothing here follows it for
    you — see `iter_approvals()`.
    """

    approvals:   list[Approval] = field(default_factory=list)
    next_cursor: str | None = None
    raw:         dict = field(default_factory=dict)

    def __iter__(self):
        return iter(self.approvals)

    def __len__(self) -> int:
        return len(self.approvals)

    @classmethod
    def from_response(cls, data: dict) -> "ApprovalPage":
        return cls(
            approvals   = [Approval.from_response(a)
                           for a in (data.get("approvals") or [])],
            next_cursor = data.get("next_cursor"),
            raw         = data,
        )


@dataclass(frozen=True)
class Remediation:
    """One thing that was done about an incident (spec §7.14)."""

    remediation_id: str
    kind:           str            # "approval" | "policy_change" | "manual" | "auto"
    outcome:        str = ""
    approval_id:    str | None = None
    actor_id:       str | None = None
    reason:         str | None = None
    occurred_at:    str = ""
    anchor:         dict | None = None

    @classmethod
    def from_response(cls, data: dict) -> "Remediation":
        return cls(
            remediation_id = data.get("remediation_id", ""),
            kind           = data.get("kind", ""),
            outcome        = data.get("outcome", ""),
            approval_id    = data.get("approval_id"),
            actor_id       = data.get("actor_id"),
            reason         = data.get("reason"),
            occurred_at    = data.get("occurred_at", ""),
            anchor         = data.get("anchor"),
        )


@dataclass(frozen=True)
class Incident:
    """One entry of the append-only incident ledger (spec §7.14).

    `anchor` is the ledger entry that opened this incident, in the same
    `{ledger_index, hash}` shape `CheckResult.anchor` carries. A caller
    who recorded an anchor at check time can find that entry here and
    compare hashes; a mismatch is the one alarm the ledger exists to make
    possible.

    An incident with no remediations and status "open" is the normal
    shape of something nobody has answered yet — not an error, and not
    something to collapse to None.
    """

    incident_id:    str
    status:         str            # "open" | "remediated" | "accepted"
    kind:           str            # "cb_open" | "cb_half_open" | "policy_fired" | "approval_required"
    subject_id:     str = ""
    frame_id:       str | None = None
    opened_at:      str = ""
    closed_at:      str | None = None
    reason:         str = ""
    fired_policies: list[dict] = field(default_factory=list)
    remediations:   list[Remediation] = field(default_factory=list)
    anchor:         dict | None = None
    raw:            dict = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @classmethod
    def from_response(cls, data: dict) -> "Incident":
        return cls(
            incident_id    = data.get("incident_id", ""),
            status         = data.get("status", ""),
            kind           = data.get("kind", ""),
            subject_id     = data.get("subject_id", ""),
            frame_id       = data.get("frame_id"),
            opened_at      = data.get("opened_at", ""),
            closed_at      = data.get("closed_at"),
            reason         = data.get("reason", ""),
            fired_policies = data.get("fired_policies") or [],
            remediations   = [Remediation.from_response(r)
                              for r in (data.get("remediations") or [])],
            anchor         = data.get("anchor"),
            raw            = data,
        )


@dataclass(frozen=True)
class IncidentPage:
    """One page of `get_incidents()` (spec §7.13).

    Newest `ledger_index` first, as the server ordered it. The SDK does
    not re-sort: ordering by a timestamp cannot separate two entries
    written in the same second, and the ledger's own order is the one
    that means something.
    """

    incidents:   list[Incident] = field(default_factory=list)
    next_cursor: str | None = None
    raw:         dict = field(default_factory=dict)

    def __iter__(self):
        return iter(self.incidents)

    def __len__(self) -> int:
        return len(self.incidents)

    @classmethod
    def from_response(cls, data: dict) -> "IncidentPage":
        return cls(
            incidents   = [Incident.from_response(i)
                           for i in (data.get("incidents") or [])],
            next_cursor = data.get("next_cursor"),
            raw         = data,
        )
