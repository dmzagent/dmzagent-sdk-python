"""The DMZAgent client — the SDK's main entry point.

Usage:

    from dmzagent import DMZAgent

    cx = DMZAgent(api_key="ck_…")

    cx.subject_says(
        agent_subject_id="user:ws_xxx:checkout-bot",
        subject_id="user:ws_xxx:customer-anon",
        subject_type="chat",
        text="I want a refund.",
        subjects=[
            {"subject_id": "user:ws_xxx:checkout-bot",  "role": "agent"},
            {"subject_id": "user:ws_xxx:customer-anon", "role": "customer"},
        ],
    )

    result = cx.check(subject_id="user:ws_xxx:checkout-bot")
    if not result.allow:
        return refuse(result.reason)

    cx.tool_call(
        interaction_id="int_abc",
        subject_id="user:ws_xxx:checkout-bot",
        subject_type="chat",
        tool="refund.issue",
        args={"amount": 9900},
    )

The client is sync-first. Most agent runtimes already manage their own
event loop, so adding asyncio at every layer of the SDK obscures the
hot path; consumers who want async simply wrap calls in their
executor of choice. (An async client may land in a future release when
the patterns in customer code make it worth the surface-area cost.)

Auth: every request carries `Authorization: Bearer ck_…`. The key
resolves server-side to the workspace_id, so we never need to pass
workspace_id on the wire.

Default base_url: `https://api.dmzagent.com`. Customers running
against staging override with `DMZAgent(api_key=…, base_url="https://staging.api.eastern-shore-solutions.com")`.

This module implements spec version 0.8.0 — see sdk-spec.md in
dmzagent-sdk-spec for the canonical surface.
"""
from __future__ import annotations

import contextlib
import logging
import time
from typing import Any, Iterator

import httpx

from .errors import (
    AuthError,
    CBOpenError,
    ConflictError,
    DMZAgentError,
    PermissionError,
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
)


def _parse_retry_after(raw: str | None) -> int | None:
    """Seconds from a `Retry-After` header, or None.

    Only the delta-seconds form is understood. RFC 9110 also permits an
    HTTP-date, and a caller that got one would be worse served by a wrong
    integer than by None — so anything non-numeric returns None rather than
    guessing. The spec carries a vector for the header-absent case, which
    lands here too.
    """
    if raw is None:
        return None
    try:
        seconds = int(raw.strip())
    except (ValueError, AttributeError):
        return None
    return seconds if seconds >= 0 else None


logger = logging.getLogger("dmzagent")


_DEFAULT_BASE_URL = "https://api.dmzagent.com"
_DEFAULT_TIMEOUT_S = 10.0
_SPEC_VERSION = "0.8.0"


# Event kinds the agent_stream endpoint accepts. Mirrors
# prothinker/connectors/agent_stream_connector.py — keep in sync via
# the spec.
EVENT_KINDS = ("subject_says", "tool_call", "tool_result", "observation")

VALID_SUBJECT_TYPES = ("chat", "lead", "journey", "sensor", "ticket")


class DMZAgent:
    """Synchronous DMZAgent client. Thread-safe (httpx.Client is)."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT_S,
        user_agent: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key or not api_key.startswith("ck_"):
            raise ValueError("api_key must start with 'ck_' — get one from your tenant_admin")
        self._api_key  = api_key
        self._base_url = base_url.rstrip("/")
        # Authorization: Bearer <ck_...> is the auth contract the server
        # honors via auth._extract_bearer / auth.current_user.
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization":   f"Bearer {api_key}",
                "Content-Type":    "application/json",
                "User-Agent":      user_agent or f"dmzagent-python/{_SPEC_VERSION}",
            },
            transport=transport,
        )

    # ===================================================================== #
    # Event emission — /v1/agent-stream/event
    # ===================================================================== #

    def emit_event(
        self,
        kind: str,
        *,
        agent_subject_id: str,
        subject_type: str,
        payload: dict[str, Any] | None = None,
        interaction_id: str | None = None,
        interaction_kind: str = "chat_session",
        subjects: list[dict] | None = None,
        speaker_subject_id: str | None = None,
        speaker_role: str | None = None,
        occurred_at: str | None = None,
        metadata: dict | None = None,
        async_mode: bool = False,
        idempotency_key: str | None = None,
    ) -> EmitResult:
        """Low-level event emitter — every higher-level helper lands here.

        `kind` ∈ {"subject_says", "tool_call", "tool_result", "observation"}.
        `agent_subject_id` is required because the wire protocol grounds
        every event against an agent identity (the conversation anchor);
        the speaker is named separately via `speaker_subject_id`.

        `async_mode=True` sets `X-DMZAgent-Async: true`, switching the
        server to fire-and-forget — the result envelope will only
        contain `interaction_id`, `subjects`, `queued=True`. Sync-only
        fields (frame_id, tags_fired, etc.) will be None / empty.
        """
        if kind not in EVENT_KINDS:
            raise ValueError(f"kind must be one of {EVENT_KINDS}, got {kind!r}")
        if subject_type not in VALID_SUBJECT_TYPES:
            raise ValueError(f"subject_type must be one of {VALID_SUBJECT_TYPES}, got {subject_type!r}")
        body: dict[str, Any] = {
            "kind":             kind,
            "agent_subject_id": agent_subject_id,
            "subject_type":     subject_type,
            "payload":          payload or {},
        }
        if interaction_id:    body["interaction_id"]   = interaction_id
        if interaction_kind:  body["interaction_kind"] = interaction_kind
        if subjects:          body["subjects"]         = subjects
        if speaker_subject_id: body["speaker_subject_id"] = speaker_subject_id
        if speaker_role:      body["speaker_role"]     = speaker_role
        if occurred_at:       body["occurred_at"]      = occurred_at
        if metadata:          body["metadata"]         = metadata

        extra_headers: dict[str, str] = {}
        if async_mode:
            extra_headers["X-DMZAgent-Async"] = "true"
        # Caller-supplied only (spec 1.8). The SDK deliberately does not
        # generate one: a key minted per call is unique per call and so
        # deduplicates nothing, and a key derived from the payload would
        # collapse two genuinely distinct but identical events.
        if idempotency_key:
            extra_headers["Idempotency-Key"] = idempotency_key
        data = self._post_json(
            "/v1/agent-stream/event", body,
            extra_headers=extra_headers or None,
        )
        return EmitResult.from_response(data)

    # ----- convenience wrappers around emit_event ----- #

    def subject_says(
        self,
        *,
        subject_id: str,
        subject_type: str,
        text: str,
        interaction_id: str | None = None,
        agent_subject_id: str | None = None,
        subjects: list[dict] | None = None,
        **kwargs: Any,
    ) -> EmitResult:
        """A subject in the conversation said something.

        `subject_id` is the speaker — could be an agent, a human
        customer, a sensor, anything DMZAgent has registered as a
        subject. The SDK doesn't care; it passes the speaker explicitly
        via `speaker_subject_id` and lets the server attribute the
        utterance based on the subjects roster.

        `agent_subject_id` is required because the wire protocol grounds
        every event against an agent identity (the conversation anchor).
        In single-agent conversations, that's the AI agent. In
        multi-agent conversations, the SDK caller picks one — typically
        the agent that owns the workflow. The Conversation helper
        derives this from the participants list automatically.
        """
        if not agent_subject_id:
            raise ValueError(
                "agent_subject_id is required — every event is grounded "
                "against an agent identity (use the Conversation helper "
                "to avoid passing this on every call)"
            )
        return self.emit_event(
            "subject_says",
            interaction_id=interaction_id,
            agent_subject_id=agent_subject_id,
            subject_type=subject_type,
            payload={"text": text, **kwargs.pop("payload_extra", {})},
            subjects=subjects,
            speaker_subject_id=subject_id,
            **kwargs,
        )

    def tool_call(
        self,
        *,
        interaction_id: str | None = None,
        subject_id: str,
        subject_type: str,
        tool: str,
        args: dict | None = None,
        subjects: list[dict] | None = None,
        **kwargs: Any,
    ) -> EmitResult:
        """The agent invoked a tool. Use BEFORE the tool runs — emit the
        intent. The result lands separately via `tool_result`."""
        return self.emit_event(
            "tool_call",
            interaction_id=interaction_id,
            agent_subject_id=subject_id,
            subject_type=subject_type,
            payload={"tool": tool, "args": args or {}},
            subjects=subjects,
            speaker_subject_id=subject_id,
            speaker_role="agent",
            **kwargs,
        )

    def tool_result(
        self,
        *,
        interaction_id: str | None = None,
        subject_id: str,
        subject_type: str,
        tool: str,
        result: Any,
        subjects: list[dict] | None = None,
        **kwargs: Any,
    ) -> EmitResult:
        """A tool returned a result. Pair with the prior `tool_call`."""
        return self.emit_event(
            "tool_result",
            interaction_id=interaction_id,
            agent_subject_id=subject_id,
            subject_type=subject_type,
            payload={"tool": tool, "result": result},
            subjects=subjects,
            speaker_subject_id=subject_id,
            **kwargs,
        )

    def observation(
        self,
        *,
        interaction_id: str | None = None,
        agent_subject_id: str,
        subject_type: str,
        subjects: list[dict],
        payload: dict,
        **kwargs: Any,
    ) -> EmitResult:
        """Generic structured observation — video keyframes, IoT events,
        anything that doesn't fit the speech-bubble shape."""
        return self.emit_event(
            "observation",
            interaction_id=interaction_id,
            agent_subject_id=agent_subject_id,
            subject_type=subject_type,
            payload=payload,
            subjects=subjects,
            **kwargs,
        )

    # ===================================================================== #
    # Capture — /v1/agent-stream/event (capture shape, different from emit)
    # ===================================================================== #

    def capture(
        self,
        *,
        subject_id: str,
        kind: str,
        subject_type: str,
        payload: dict[str, Any] | None = None,
        agent_subject_id: str | None = None,
        interaction_id: str | None = None,
        interaction_kind: str | None = None,
        subjects: list[dict] | None = None,
        speaker_subject_id: str | None = None,
        speaker_role: str | None = None,
        occurred_at: str | None = None,
        metadata: dict | None = None,
        idempotency_key: str | None = None,
    ) -> CaptureResult:
        """Capture an event in the agent stream.

        `kind` ∈ {"subject_says", "tool_call", "tool_result", "observation"}.
        Unlike `emit_event`, `agent_subject_id` is optional here.
        """
        if kind not in EVENT_KINDS:
            raise ValueError(f"kind must be one of {EVENT_KINDS}, got {kind!r}")
        if subject_type not in VALID_SUBJECT_TYPES:
            raise ValueError(f"subject_type must be one of {VALID_SUBJECT_TYPES}, got {subject_type!r}")
        body: dict[str, Any] = {
            "kind":             kind,
            "subject_id":       subject_id,
            "subject_type":     subject_type,
            "payload":          payload or {},
        }
        if agent_subject_id:  body["agent_subject_id"]  = agent_subject_id
        if interaction_id:    body["interaction_id"]    = interaction_id
        if interaction_kind:  body["interaction_kind"]  = interaction_kind
        if subjects:          body["subjects"]          = subjects
        if speaker_subject_id: body["speaker_subject_id"] = speaker_subject_id
        if speaker_role:      body["speaker_role"]      = speaker_role
        if occurred_at:       body["occurred_at"]       = occurred_at
        if metadata:          body["metadata"]          = metadata

        data = self._post_json(
            "/v1/agent-stream/event", body,
            extra_headers={"Idempotency-Key": idempotency_key} if idempotency_key else None,
        )
        return CaptureResult.from_response(data)

    # ===================================================================== #
    # Await outcome — GET /v1/frames/{frame_id}/story
    # ===================================================================== #

    def await_outcome(
        self,
        frame_id: str,
        timeout: float = 30.0,
    ) -> OutcomeResult:
        """Poll the frame story endpoint until an outcome is available.

        Backoff: start 100ms, double to max 2s, cap at `timeout`.
        Raises ServerError with "timeout" message if exceeded.
        """
        deadline = time.monotonic() + timeout
        delay = 0.1
        while True:
            data = self._get_json(f"/v1/frames/{frame_id}/story")
            outcome = data.get("outcome")
            if outcome and outcome != "pending":
                return OutcomeResult.from_response(data)
            if time.monotonic() >= deadline:
                raise ServerError("timeout")
            time.sleep(delay)
            delay = min(delay * 2, 2.0)

    # ===================================================================== #
    # Circuit breaker — /v1/cb/check
    # ===================================================================== #

    def check(
        self,
        *,
        subject_id: str | None = None,
        interaction_id: str | None = None,
    ) -> CheckResult:
        """Synchronous CB check. Pass exactly one of subject_id or
        interaction_id.

        Returns CheckResult — `.allow` is the binary the caller cares
        about. `.warning` is set when state is half-open (review).
        """
        if (subject_id is None) == (interaction_id is None):
            raise ValueError("pass exactly one of subject_id or interaction_id")
        scope     = "subject" if subject_id else "interaction"
        scope_ref = subject_id or interaction_id
        data = self._post_json(
            "/v1/cb/check",
            {"scope": scope, "scope_ref": scope_ref},
        )
        return CheckResult.from_response(data)

    @contextlib.contextmanager
    def guard(
        self,
        *,
        subject_id: str | None = None,
        interaction_id: str | None = None,
        raise_on_open: bool = False,
    ) -> Iterator[CheckResult]:
        """Context-manager form of `check()`.

            with cx.guard(subject_id="user:ws:bot") as g:
                if not g.allow:
                    return refuse(g.reason)
                # ...sensitive action

        Set `raise_on_open=True` for a try/except control-flow seam:

            try:
                with cx.guard(subject_id="user:ws:bot", raise_on_open=True):
                    do_sensitive_thing()
            except CBOpenError as e:
                log_blocked(e.reason, e.fired_policies)
        """
        result = self.check(subject_id=subject_id, interaction_id=interaction_id)
        if raise_on_open and not result.allow:
            raise CBOpenError(
                f"circuit breaker open: {result.reason}",
                reason=result.reason,
                fired_policies=result.fired_policies,
                anchor=result.anchor,
                scope_ref=subject_id or interaction_id or "",
            )
        yield result

    # ===================================================================== #
    # Conversation — high-level wrapper for the single-agent-one-customer
    # case that covers ~80% of customer code paths.
    # ===================================================================== #

    def conversation(
        self,
        *,
        participants: list[dict],
        agent_subject_id: str | None = None,
        kind: str = "chat_session",
        metadata: dict | None = None,
    ) -> "Conversation":
        """Open a Conversation handle bound to this client.

            with cx.conversation(participants=[
                {"subject_id": "user:ws:bot",  "role": "agent",    "kind": "agent"},
                {"subject_id": "user:ws:cust", "role": "customer", "kind": "human"},
            ]) as conv:
                conv.says("user:ws:cust", "I want a refund.")
                conv.says("user:ws:bot",  "I can help.")
                with conv.guard("user:ws:bot") as g:
                    if not g.allow:
                        raise CBOpenError(g.reason)
                conv.tool_call("user:ws:bot", "refund.issue", {"amount": 99})

        `participants` is a list of {subject_id, role, kind} dicts —
        the same shape that lands in the wire protocol's `subjects`
        array. No fixed agent/customer dichotomy; supply whatever
        roles fit the conversation (agent, customer, observer,
        detected_person, counterparty, mentioned, ...).

        `agent_subject_id` is derived from the first participant whose
        role is agent-ish (agent / service / system); pass it
        explicitly when your conversation has no agent-shaped
        participant.
        """
        from .conversation import Conversation
        return Conversation(
            client=self,
            participants=participants,
            agent_subject_id=agent_subject_id,
            interaction_kind=kind,
            metadata=metadata,
        )

    # ===================================================================== #
    # Notification preferences — /v1/settings/notifications
    # ===================================================================== #

    def get_notification_prefs(self) -> NotificationPrefs:
        """Fetch the current user's notification preferences."""
        data = self._get_json("/v1/settings/notifications")
        return NotificationPrefs.from_response(data)

    def update_notification_prefs(self, **prefs: dict | bool | str | None) -> NotificationPrefs:
        """Update notification preferences. Only supplied fields are touched.

        Supported keyword args: email_cadence (off|daily|weekly),
        push_enabled (bool), phone (str), sms_enabled (bool),
        whatsapp_enabled (bool).
        """
        data = self._put_json("/v1/settings/notifications", prefs)
        return NotificationPrefs.from_response(data)

    # ===================================================================== #
    # Division config — /v1/divisions/{id}/config
    # ===================================================================== #

    def get_division_config(self, division_id: str) -> DivisionConfig:
        """Read a division's JSON config blob.

        Contains settings like ``reasoning_mode``
        ("per_frame" | "per_trace").
        """
        data = self._get_json(f"/v1/divisions/{division_id}/config")
        return DivisionConfig.from_response(data)

    def update_division_config(self, division_id: str, config: dict) -> DivisionConfig:
        """Replace a division's JSON config blob."""
        data = self._put_json(f"/v1/divisions/{division_id}/config", config)
        return DivisionConfig.from_response(data)

    # ===================================================================== #
    # Resource management
    # ===================================================================== #

    def close(self) -> None:
        """Close the underlying httpx.Client. Safe to call multiple times."""
        self._client.close()

    def __enter__(self) -> "DMZAgent":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ===================================================================== #
    # Internal — HTTP plumbing
    # ===================================================================== #

    def _get_json(self, path: str) -> dict:
        url = f"{self._base_url}{path}"
        try:
            resp = self._client.get(url)
        except httpx.TimeoutException as e:
            raise ServerError(f"timeout calling {path}", body=str(e)) from e
        except httpx.RequestError as e:
            raise ServerError(f"network error calling {path}: {e}") from e
        return self._handle(resp, path)

    def _put_json(self, path: str, body: dict) -> dict:
        url = f"{self._base_url}{path}"
        try:
            resp = self._client.put(url, json=body)
        except httpx.TimeoutException as e:
            raise ServerError(f"timeout calling {path}", body=str(e)) from e
        except httpx.RequestError as e:
            raise ServerError(f"network error calling {path}: {e}") from e
        return self._handle(resp, path)

    def _post_json(
        self,
        path: str,
        body: dict,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        url = f"{self._base_url}{path}"
        try:
            resp = self._client.post(url, json=body, headers=extra_headers)
        except httpx.TimeoutException as e:
            raise ServerError(f"timeout calling {path}", body=str(e)) from e
        except httpx.RequestError as e:
            raise ServerError(f"network error calling {path}: {e}") from e
        return self._handle(resp, path)

    def _handle(self, resp: httpx.Response, path: str) -> dict:
        if 200 <= resp.status_code < 300:
            try:
                return resp.json()
            except Exception:
                return {}
        body: Any
        try:
            body = resp.json()
        except Exception:
            body = resp.text

        if resp.status_code == 401:
            raise AuthError(
                "invalid or revoked API key",
                status_code=resp.status_code, body=body,
            )
        if resp.status_code == 403:
            raise PermissionError(
                "API key lacks required scope for this operation",
                status_code=resp.status_code, body=body,
            )
        # 400 and 422 both mean "fix the request" — malformed vs parsed-but-
        # rejected. The spec taxonomy maps both to ValidationError; status_code
        # tells them apart for callers that care.
        if resp.status_code in (400, 422):
            raise ValidationError(
                f"server rejected request to {path}: {body!r}",
                status_code=resp.status_code, body=body,
            )
        # 409 is the Idempotency-Key in-flight conflict (spec §1.8). Kept
        # separate from ServerError: the duplicate is the caller's own
        # earlier request, so retrying the same key replays its response
        # instead of causing a second side effect.
        if resp.status_code == 409:
            raise ConflictError(
                f"a request with this Idempotency-Key is already in flight on {path}",
                status_code=resp.status_code, body=body,
            )
        if resp.status_code == 429:
            raise RateLimitError(
                f"rate limited on {path}",
                status_code=resp.status_code, body=body,
                retry_after=_parse_retry_after(resp.headers.get("Retry-After")),
            )
        if resp.status_code >= 500:
            raise ServerError(
                f"server error from {path} ({resp.status_code})",
                status_code=resp.status_code, body=body,
            )
        raise DMZAgentError(
            f"unexpected status {resp.status_code} from {path}",
            status_code=resp.status_code, body=body,
        )
