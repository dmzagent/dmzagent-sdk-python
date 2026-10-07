"""A handle bound to one agent session (spec §5.23).

    session = cx.agent_session("seat:agent-a", "sess_4b1e")

    session.intent("Add a trace id to every request.",
                   paths=["src/obs/"], tools=["Edit", "Bash"])

    r = session.call("call_7", "Bash", {"command": "git push origin HEAD"})
    if r.runs:
        out = run_bash("git push origin HEAD")
        session.result("call_7", "Bash", "ok", result=out)
    else:
        session.refused("call_7", "Bash", refused_by="governor", reason=r.reason)

Every method sends exactly one step through `DMZAgent.agent_step()` and
returns its `StepResult`. That is all the handle does.

It holds the two ids it was opened with and nothing else. It does not
remember refusals, count attempts, or infer `attempt_of`: a handle that
guessed which call retried which would put its guess on the record as
the caller's statement, and the server does not depend on it anyway
(spec §1.9). A caller that knows passes `attempt_of=`; one that does not
leaves it out.

**Refusals are reported, whoever refused.** When a call does not run —
because the governor said so, because your harness's own rules said so,
or because the tool or host refused — send `refused()` for it. A rule an
agent got around is recognisable only against the refusal it got around.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import StepResult

if TYPE_CHECKING:
    from .client import DMZAgent


class AgentSession:
    """Handle for one agent session. Use via `cx.agent_session(...)` —
    direct construction is not part of the public API and may change."""

    # The whole of the handle's state, fixed. A slot for anything else —
    # a list of refusals, a last call_id — would be the inference §5.23
    # forbids, so there is no attribute to put it in.
    __slots__ = ("_agent_subject_id", "_client", "_interaction_id")

    def __init__(
        self,
        *,
        client: DMZAgent,
        agent_subject_id: str,
        interaction_id: str,
    ) -> None:
        for name, value in (("agent_subject_id", agent_subject_id),
                            ("interaction_id", interaction_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required to open an agent session")
        self._client = client
        self._agent_subject_id = agent_subject_id
        self._interaction_id = interaction_id

    @property
    def agent_subject_id(self) -> str:
        return self._agent_subject_id

    @property
    def interaction_id(self) -> str:
        """Caller-assigned, and stable for the session's life."""
        return self._interaction_id

    # ===================================================================== #
    # Steps
    # ===================================================================== #

    def intent(
        self,
        text: str,
        *,
        paths: list[str] | None = None,
        tools: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> StepResult:
        """The agent states what it will do and touch (`phase: intent`)."""
        intent: dict[str, Any] = {"text": text}
        if paths is not None:
            intent["paths"] = paths
        if tools is not None:
            intent["tools"] = tools
        return self._client.agent_step(
            self._agent_subject_id, self._interaction_id, "intent",
            intent=intent, idempotency_key=idempotency_key)

    def call(
        self,
        call_id: str,
        tool: str,
        args: dict[str, Any] | None = None,
        *,
        attempt_of: str | None = None,
        idempotency_key: str | None = None,
    ) -> StepResult:
        """Ask before a tool runs (`phase: call`). Run it only if `.runs`."""
        return self._client.agent_step(
            self._agent_subject_id, self._interaction_id, "call",
            call_id=call_id, tool=tool, args=args, attempt_of=attempt_of,
            idempotency_key=idempotency_key)

    def result(
        self,
        call_id: str,
        tool: str,
        status: str,
        *,
        result: Any = None,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> StepResult:
        """Report a call that ran (`phase: result`), `status` "ok" or "error".

        A call that did not run is not reported here: use `refused()`,
        which requires saying who refused it.
        """
        if status not in ("ok", "error"):
            raise ValueError(
                f"status must be 'ok' or 'error' for a call that ran, got "
                f"{status!r}; report a call that did not run with refused()")
        return self._client.agent_step(
            self._agent_subject_id, self._interaction_id, "result",
            call_id=call_id, tool=tool, status=status, result=result,
            reason=reason, idempotency_key=idempotency_key)

    def refused(
        self,
        call_id: str,
        tool: str,
        refused_by: str,
        *,
        reason: str | None = None,
        attempt_of: str | None = None,
        idempotency_key: str | None = None,
    ) -> StepResult:
        """Report a call that did not run (`phase: result`, `status: refused`).

        `refused_by` is "governor" (DMZAgent's directive), "harness" (your
        runner's own rules) or "host" (the tool, sandbox or OS).
        """
        return self._client.agent_step(
            self._agent_subject_id, self._interaction_id, "result",
            call_id=call_id, tool=tool, status="refused",
            refused_by=refused_by, reason=reason, attempt_of=attempt_of,
            idempotency_key=idempotency_key)

    # ===================================================================== #
    # Lifecycle
    # ===================================================================== #

    def close(self) -> None:
        """A no-op, as on `Conversation` (spec §6.3). It ends nothing on
        the server: agent mode has no end-of-session step."""

    def __enter__(self) -> AgentSession:  # noqa: PYI034 — typing.Self is 3.11+
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
