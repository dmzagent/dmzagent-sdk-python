"""The Concordex client — the SDK's main entry point.

Usage:

    from concordex import Concordex

    cx = Concordex(api_key="ck_…")

    cx.subject_says(
        agent_subject_id="user:ws_xxx:checkout-bot",
        subject_id="user:ws_xxx:customer-anon",
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

Default base_url: `https://api.concordex.dev`. Customers running
against staging override with `Concordex(api_key=…, base_url="https://staging.api.praeceptor-thesis.com")`.

This module implements spec version 0.5.0 — see sdk-spec.md in
concordex-sdk-spec for the canonical surface.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any, Iterator

import httpx

from .errors import (
    AuthError,
    CBOpenError,
    ConcordexError,
    PermissionError,
    ServerError,
    ValidationError,
)
from .models import CheckResult, EmitResult

logger = logging.getLogger("concordex")


_DEFAULT_BASE_URL = "https://api.concordex.dev"
_DEFAULT_TIMEOUT_S = 10.0
_SPEC_VERSION = "0.5.0"


# Event kinds the agent_stream endpoint accepts. Mirrors
# prothinker/connectors/agent_stream_connector.py — keep in sync via
# the spec.
EVENT_KINDS = ("subject_says", "tool_call", "tool_result", "observation")


class Concordex:
    """Synchronous Concordex client. Thread-safe (httpx.Client is)."""

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
                "User-Agent":      user_agent or f"concordex-python/{_SPEC_VERSION}",
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
        payload: dict[str, Any] | None = None,
        interaction_id: str | None = None,
        interaction_kind: str = "chat_session",
        subjects: list[dict] | None = None,
        speaker_subject_id: str | None = None,
        speaker_role: str | None = None,
        occurred_at: str | None = None,
        metadata: dict | None = None,
        async_mode: bool = False,
    ) -> EmitResult:
        """Low-level event emitter — every higher-level helper lands here.

        `kind` ∈ {"subject_says", "tool_call", "tool_result", "observation"}.
        `agent_subject_id` is required because the wire protocol grounds
        every event against an agent identity (the conversation anchor);
        the speaker is named separately via `speaker_subject_id`.

        `async_mode=True` sets `X-Concordex-Async: true`, switching the
        server to fire-and-forget — the result envelope will only
        contain `interaction_id`, `subjects`, `queued=True`. Sync-only
        fields (frame_id, tags_fired, etc.) will be None / empty.
        """
        if kind not in EVENT_KINDS:
            raise ValueError(f"kind must be one of {EVENT_KINDS}, got {kind!r}")
        body: dict[str, Any] = {
            "kind":             kind,
            "agent_subject_id": agent_subject_id,
            "payload":          payload or {},
        }
        if interaction_id:    body["interaction_id"]   = interaction_id
        if interaction_kind:  body["interaction_kind"] = interaction_kind
        if subjects:          body["subjects"]         = subjects
        if speaker_subject_id: body["speaker_subject_id"] = speaker_subject_id
        if speaker_role:      body["speaker_role"]     = speaker_role
        if occurred_at:       body["occurred_at"]      = occurred_at
        if metadata:          body["metadata"]         = metadata

        extra_headers = {"X-Concordex-Async": "true"} if async_mode else None
        data = self._post_json("/v1/agent-stream/event", body, extra_headers=extra_headers)
        return EmitResult.from_response(data)

    # ----- convenience wrappers around emit_event ----- #

    def subject_says(
        self,
        *,
        subject_id: str,
        text: str,
        interaction_id: str | None = None,
        agent_subject_id: str | None = None,
        subjects: list[dict] | None = None,
        **kwargs: Any,
    ) -> EmitResult:
        """A subject in the conversation said something.

        `subject_id` is the speaker — could be an agent, a human
        customer, a sensor, anything Concordex has registered as a
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
            payload=payload,
            subjects=subjects,
            **kwargs,
        )

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
    # Resource management
    # ===================================================================== #

    def close(self) -> None:
        """Close the underlying httpx.Client. Safe to call multiple times."""
        self._client.close()

    def __enter__(self) -> "Concordex":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ===================================================================== #
    # Internal — HTTP plumbing
    # ===================================================================== #

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
        if resp.status_code == 400:
            raise ValidationError(
                f"server rejected request to {path}: {body!r}",
                status_code=resp.status_code, body=body,
            )
        if resp.status_code >= 500:
            raise ServerError(
                f"server error from {path} ({resp.status_code})",
                status_code=resp.status_code, body=body,
            )
        raise ConcordexError(
            f"unexpected status {resp.status_code} from {path}",
            status_code=resp.status_code, body=body,
        )
