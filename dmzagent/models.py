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


# ---------- Logic Canons (rulebook-as-code, spec Phase 14.7) ----------


@dataclass(frozen=True)
class LogicCanonVersion:
    """One immutable, integer-versioned rulebook publication."""

    version: int
    logic_canon_id: str | None = None
    n_rules: int | None = None
    changelog: str | None = None
    published_at: str | None = None
    rulebook: dict | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "LogicCanonVersion":
        return cls(
            version=int(data.get("version") or 0),
            logic_canon_id=data.get("logic_canon_id"),
            n_rules=data.get("n_rules"),
            changelog=data.get("changelog"),
            published_at=data.get("published_at"),
            rulebook=data.get("rulebook"),
            raw=data,
        )


@dataclass(frozen=True)
class LogicCanon:
    """A private, vendor-scoped Logic Canon (rulebook artifact)."""

    logic_canon_id: str
    vendor_id: str | None = None
    slug: str | None = None
    name: str | None = None
    description: str | None = None
    status: str | None = None          # open string: draft | published | unpublished
    latest_version: int | None = None
    versions: tuple[LogicCanonVersion, ...] | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "LogicCanon":
        versions = None
        if isinstance(data.get("versions"), list):
            versions = tuple(
                LogicCanonVersion.from_response(v) for v in data["versions"]
            )
        return cls(
            logic_canon_id=data.get("logic_canon_id") or "",
            vendor_id=data.get("vendor_id"),
            slug=data.get("slug"),
            name=data.get("name"),
            description=data.get("description"),
            status=data.get("status"),
            latest_version=data.get("latest_version"),
            versions=versions,
            raw=data,
        )


@dataclass(frozen=True)
class LogicCanonInstall:
    """A Logic Canon pinned into a workspace at one immutable version.

    Pure deploy record. ``status`` is the canon's *lifecycle* state
    (open string: draft | published | unpublished). Health of the
    install (is it actually evaluating?) lives on the health surface —
    see :class:`LogicInstallHealthRow`.
    """

    logic_canon_id: str
    version: int | None = None
    workspace_id: str | None = None
    slug: str | None = None
    name: str | None = None
    installed_by: str | None = None
    installed_at: str | None = None
    status: str | None = None          # lifecycle: draft | published | unpublished
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "LogicCanonInstall":
        return cls(
            logic_canon_id=data.get("logic_canon_id") or "",
            version=data.get("version"),
            workspace_id=data.get("workspace_id"),
            slug=data.get("slug"),
            name=data.get("name"),
            installed_by=data.get("installed_by"),
            installed_at=data.get("installed_at"),
            status=data.get("status"),
            raw=data,
        )


@dataclass(frozen=True)
class LogicInstallHealthRow:
    """One install's health on the fail-open alert surface.

    ``status`` is the *health* state (open string:
    ok | missing_bytes | compile_error); ``detail`` carries the
    compile error when present. Distinct from the deploy record
    (:class:`LogicCanonInstall`), whose ``status`` is a lifecycle enum.
    """

    logic_canon_id: str
    version: int | None = None
    slug: str | None = None
    name: str | None = None
    status: str | None = None          # health: ok | missing_bytes | compile_error
    detail: str | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "LogicInstallHealthRow":
        return cls(
            logic_canon_id=data.get("logic_canon_id") or "",
            version=data.get("version"),
            slug=data.get("slug"),
            name=data.get("name"),
            status=data.get("status"),
            detail=data.get("detail"),
            raw=data,
        )


@dataclass(frozen=True)
class LogicInstallHealth:
    """Install health for one workspace — the fail-open alert surface.

    ``ok is False`` means at least one installed control is NOT
    evaluating (published bytes missing or no longer compiling); treat
    it as an operational alarm.
    """

    workspace_id: str
    ok: bool
    broken: int
    installs: tuple[LogicInstallHealthRow, ...]
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "LogicInstallHealth":
        installs = tuple(
            LogicInstallHealthRow.from_response(i)
            for i in (data.get("installs") or [])
        )
        return cls(
            workspace_id=data.get("workspace_id") or "",
            ok=bool(data.get("ok")),
            broken=int(data.get("broken") or 0),
            installs=installs,
            raw=data,
        )


@dataclass(frozen=True)
class RulebookValidation:
    """Compile-only rulebook validation (CI / pre-publish lint)."""

    valid: bool
    error: str | None = None
    n_rules: int | None = None
    n_stateless: int | None = None
    n_stateful: int | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "RulebookValidation":
        return cls(
            valid=bool(data.get("valid")),
            error=data.get("error"),
            n_rules=data.get("n_rules"),
            n_stateless=data.get("n_stateless"),
            n_stateful=data.get("n_stateful"),
            raw=data,
        )


@dataclass(frozen=True)
class FiredRule:
    """A rule that fired during logic-door evaluation."""

    rule_id: str
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "FiredRule":
        return cls(
            rule_id=data.get("rule_id") or "",
            raw=data,
        )


@dataclass(frozen=True)
class Escalation:
    """An escalation raised during logic-door evaluation.

    ``band`` / ``lane`` are open strings — never enums — so new server
    values pass through without breaking consumers.
    """

    rule_id: str
    band: str | None = None
    lane: str | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "Escalation":
        return cls(
            rule_id=data.get("rule_id") or "",
            band=data.get("band"),
            lane=data.get("lane"),
            raw=data,
        )


@dataclass(frozen=True)
class LogicEventAck:
    """Ack from the live logic door — one evaluated event (202).

    Honest-ack fields (spec 0.7.0 / server LC-P3):

    - ``degraded`` — True when at least one installed control did NOT
      evaluate (fail-open). ``fired == ()`` with ``degraded is True``
      is an alarm, not a clean pass.
    - ``responded`` — True when the best-effort respond step ran; when
      False, ``dispositions`` / ``emitted_frames`` /
      ``expected_loss_avoided`` read 0 because responding failed, not
      because nothing happened.
    - ``installs_evaluated`` / ``installs_total`` — evaluated-vs-installed
      counts backing ``degraded``.

    All four default to False/0 when absent (older servers).
    """

    accepted: bool
    workspace_id: str
    subject_id: str
    n_logic_pass: int = 0
    n_deferred: int = 0
    fired: tuple[FiredRule, ...] = ()
    escalations: tuple[Escalation, ...] = ()
    dispositions: int = 0
    emitted_frames: int = 0
    expected_loss_avoided: float = 0.0
    degraded: bool = False
    responded: bool = False
    installs_evaluated: int = 0
    installs_total: int = 0
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "LogicEventAck":
        return cls(
            accepted=bool(data.get("accepted")),
            workspace_id=data.get("workspace_id") or "",
            subject_id=data.get("subject_id") or "",
            n_logic_pass=int(data.get("n_logic_pass") or 0),
            n_deferred=int(data.get("n_deferred") or 0),
            fired=tuple(
                FiredRule.from_response(f) for f in (data.get("fired") or ())
            ),
            escalations=tuple(
                Escalation.from_response(e) for e in (data.get("escalations") or ())
            ),
            dispositions=int(data.get("dispositions") or 0),
            emitted_frames=int(data.get("emitted_frames") or 0),
            expected_loss_avoided=float(data.get("expected_loss_avoided") or 0.0),
            degraded=bool(data.get("degraded", False)),
            responded=bool(data.get("responded", False)),
            installs_evaluated=int(data.get("installs_evaluated") or 0),
            installs_total=int(data.get("installs_total") or 0),
            raw=data,
        )
