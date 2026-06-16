"""High-level conversation handle.

Most conversations involve multiple subjects together — the
Conversation class accepts a generic `participants` list and lets the
caller speak as any of them without rebuilding the roster every event.

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

The shape generalizes naturally: a multi-agent workflow, a group chat,
a video feed with detected persons — every variant just lists its
participants and their roles. No baked-in user/agent dichotomy.

The `agent_subject_id` the wire protocol still requires is derived
from the participant whose role is "agent" (first match wins). If
your conversation has no agent (two humans, sensor-only), pass
`agent_subject_id=` explicitly to override.

The interaction_id is created on the first event (the server returns
it; we cache it for subsequent calls). Callers can extend the roster
mid-conversation via `add_subject()` for late-joining observers.
"""
from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, Iterator

from .models import CheckResult, EmitResult

if TYPE_CHECKING:
    from .client import DMZAgent


# Roles the SDK treats as "this participant is an agent for the
# purposes of the wire protocol's agent_subject_id requirement."
# `service` and `system` also count — they're all non-human, non-customer
# subjects that carry workflow responsibility.
_AGENT_ROLES = frozenset({"agent", "service", "system"})


class Conversation:
    """Stateful handle for a single interaction. Use via
    `cx.conversation(...)` — direct construction is not part of the
    public API and may change."""

    def __init__(
        self,
        *,
        client: "DMZAgent",
        participants: list[dict],
        agent_subject_id: str | None = None,
        interaction_kind: str = "chat_session",
        metadata: dict | None = None,
    ) -> None:
        if not participants:
            raise ValueError("participants must be a non-empty list")
        # Normalize and validate participants. Each entry needs at
        # least subject_id; role/kind default to "other".
        roster: list[dict] = []
        for i, p in enumerate(participants):
            sid = (p.get("subject_id") or "").strip()
            if not sid:
                raise ValueError(f"participants[{i}] missing subject_id")
            roster.append({
                "subject_id": sid,
                "role":       p.get("role") or "other",
                "kind":       p.get("kind") or "other",
                **({"metadata": p["metadata"]} if "metadata" in p else {}),
            })

        # Derive the wire-level agent_subject_id when the caller didn't
        # set it explicitly. First participant whose role looks
        # agent-ish wins; otherwise the first participant — every
        # event needs SOMETHING anchored, and the SDK defaults
        # conservatively.
        if not agent_subject_id:
            agent_subject_id = next(
                (p["subject_id"] for p in roster
                 if (p.get("role") or "").lower() in _AGENT_ROLES),
                roster[0]["subject_id"],
            )

        self._client = client
        self._agent_subject_id = agent_subject_id
        self._interaction_kind = interaction_kind
        self._metadata = metadata or {}
        self._interaction_id: str | None = None
        self._subjects: list[dict] = roster

    # ===================================================================== #
    # Subject roster
    # ===================================================================== #

    @property
    def interaction_id(self) -> str | None:
        """The server-assigned id, available after the first event."""
        return self._interaction_id

    @property
    def subjects(self) -> list[dict]:
        """Snapshot of the current roster — read-only; mutate via
        `add_subject()`."""
        return list(self._subjects)

    def add_subject(
        self,
        subject_id: str,
        *,
        role: str = "other",
        kind: str = "other",
        metadata: dict | None = None,
    ) -> None:
        """Add another participant. Useful for multi-party chats:

            conv.add_subject("user:ws:supervisor", role="observer")

        Idempotent on subject_id: re-adding refreshes role/kind."""
        for i, s in enumerate(self._subjects):
            if s["subject_id"] == subject_id:
                self._subjects[i] = {
                    "subject_id": subject_id, "role": role, "kind": kind,
                    "metadata": metadata or {},
                }
                return
        self._subjects.append({
            "subject_id": subject_id, "role": role, "kind": kind,
            "metadata": metadata or {},
        })

    # ===================================================================== #
    # Event emission — all delegate to client.subject_says / etc.
    # ===================================================================== #

    def says(
        self,
        subject_id: str,
        subject_type: str,
        text: str,
        **kwargs: Any,
    ) -> EmitResult:
        """A subject in the conversation spoke. The SDK figures out
        their role from the roster."""
        r = self._client.subject_says(
            interaction_id=self._interaction_id,
            interaction_kind=self._interaction_kind if not self._interaction_id else "chat_session",
            agent_subject_id=self._agent_subject_id,
            subject_id=subject_id,
            subject_type=subject_type,
            text=text,
            subjects=self._subjects,
            **kwargs,
        )
        if not self._interaction_id and r.interaction_id:
            self._interaction_id = r.interaction_id
        return r

    def tool_call(
        self,
        subject_id: str,
        subject_type: str,
        tool: str,
        args: dict | None = None,
        **kwargs: Any,
    ) -> EmitResult:
        """A subject (typically the agent) invoked a tool."""
        r = self._client.tool_call(
            interaction_id=self._interaction_id,
            subject_id=subject_id,
            subject_type=subject_type,
            tool=tool,
            args=args,
            subjects=self._subjects,
            **kwargs,
        )
        if not self._interaction_id and r.interaction_id:
            self._interaction_id = r.interaction_id
        return r

    def tool_result(
        self,
        subject_id: str,
        subject_type: str,
        tool: str,
        result: Any,
        **kwargs: Any,
    ) -> EmitResult:
        """A tool returned a result."""
        r = self._client.tool_result(
            interaction_id=self._interaction_id,
            subject_id=subject_id,
            subject_type=subject_type,
            tool=tool,
            result=result,
            subjects=self._subjects,
            **kwargs,
        )
        if not self._interaction_id and r.interaction_id:
            self._interaction_id = r.interaction_id
        return r

    def observation(
        self,
        subject_type: str,
        payload: dict,
        **kwargs: Any,
    ) -> EmitResult:
        """Structured observation — video keyframe, IoT event, anything
        not utterance-shaped."""
        r = self._client.observation(
            interaction_id=self._interaction_id,
            agent_subject_id=self._agent_subject_id,
            subject_type=subject_type,
            subjects=self._subjects,
            payload=payload,
            **kwargs,
        )
        if not self._interaction_id and r.interaction_id:
            self._interaction_id = r.interaction_id
        return r

    # ===================================================================== #
    # Circuit breaker
    # ===================================================================== #

    def check(self, subject_id: str) -> CheckResult:
        """CB check scoped to a specific subject in the conversation."""
        return self._client.check(subject_id=subject_id)

    @contextlib.contextmanager
    def guard(
        self,
        subject_id: str,
        *,
        raise_on_open: bool = False,
    ) -> Iterator[CheckResult]:
        """Context-manager CB check. Defaults to returning the result;
        set `raise_on_open=True` to throw CBOpenError when the action
        should not proceed."""
        with self._client.guard(subject_id=subject_id, raise_on_open=raise_on_open) as g:
            yield g

    # ===================================================================== #
    # Lifecycle
    # ===================================================================== #

    def __enter__(self) -> "Conversation":
        return self

    def __exit__(self, *exc: Any) -> None:
        # Future: emit an `end_interaction` event so the server can
        # close the row and stop accepting events for this id.
        pass
