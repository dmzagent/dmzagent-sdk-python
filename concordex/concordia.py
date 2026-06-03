"""Concordex Concordia — Python client for the governance MCP.

This module wraps the Concordia MCP 1.0 server (`/mcp/v1` on a Concordex
tenant) so customer agents can enforce covenants, record decisions,
query installed Canons, and read accumulated soul state without
writing JSON-RPC envelopes by hand.

Companion to the existing `concordex.Concordex` client (which handles
agent-stream events and circuit-breaker checks at the workspace level).
ConcordiaClient targets a different protocol — MCP over HTTPS+JSON-RPC
— and a different audience: customer agents that need to interact with
governance infrastructure as part of their own decision loop.

Quick start:

    from concordex.concordia import ConcordiaClient

    client = ConcordiaClient(api_key="ck_live_…")

    v = client.enforce_covenant(
        subject_id="user:alice",
        action_kind="respond_to_user",
        action_payload={"text": "Sure, here's how to..."},
        context={"session_id": "s_42", "model": "claude-sonnet-4-6"},
    )
    if v.verdict == "block":
        return generate_safe_fallback(v.rationale)

    client.record_decision(
        subject_id="user:alice",
        decision_kind="content_generated",
        payload={"final_text": response_text},
        outcome="completed",
    )

The MCP wire protocol is JSON-RPC 2.0 over HTTPS. This client wraps
the envelope so caller code looks like ordinary method calls.

Spec: CONCORDIA_MCP.md (server side) and concordex-sdk-spec §8 (Python
naming conventions). This module implements MCP 1.0 — covered by
spec §10 milestones MCP-1.1 through MCP-1.3 on the server.
"""
from __future__ import annotations

import json
import os
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

# --------------------------------------------------------------------------- #
# Result dataclasses — typed wrappers around the four tool responses.
# --------------------------------------------------------------------------- #


@dataclass
class CBStateChange:
    """The circuit-breaker state at the moment of an enforce_covenant
    call. `state` is one of `closed | half_open | open`; `warning` is
    True iff state == half_open."""
    state:        str
    warning:      bool = False
    soul_version: int | None = None


@dataclass
class EnforceCovenantResult:
    """Result of `enforce_covenant`. The agent decides what to do
    based on `verdict`:

      - "allow"   — proceed
      - "review"  — proceed but escalate to a human reviewer
      - "block"   — abstain, surface the rationale
      - "escalate" — reserved; treat as "review" for MCP 1.0
    """
    verdict:           str
    policy_ids:        list[str]
    rationale:         str
    cb_state_change:   CBStateChange
    ledger_entry_id:   str

    @property
    def allow(self) -> bool:
        """True iff verdict is 'allow'. Convenience for `if v.allow:`."""
        return self.verdict == "allow"

    @property
    def blocked(self) -> bool:
        """True iff verdict is 'block'."""
        return self.verdict == "block"


@dataclass
class RecordDecisionResult:
    """Result of `record_decision`. The `chain_head_hash` is the hash
    of the entry just written, which equals the new chain head — so
    callers can hash-walk afterward to verify their record landed."""
    ledger_entry_id: str
    chain_head_hash: str
    index:           int | None = None


@dataclass
class CorpusMatch:
    """One match from `query_corpus`. `section` looks like
    `vocabulary/<tag_id>`, `prompts/<target>`, `policies/<name>`,
    or `description` — enough to re-locate the excerpt inside the
    canon manifest."""
    canon_id:      str
    canon_version: str
    section:       str
    excerpt:       str
    relevance:     float


@dataclass
class QueryCorpusResult:
    """Result of `query_corpus`."""
    matches:     list[CorpusMatch]
    scope:       str = "installed"
    canon_count: int = 0


@dataclass
class SoulTag:
    """One tag on a subject's soul snapshot."""
    tag_id:             str
    score:              float
    raw_strength:       float | None
    evidence_count:     int
    first_observed_at:  str
    last_reinforced_at: str


@dataclass
class SoulTrace:
    """One recent reasoning trace on a subject."""
    trace_id:    str
    frame_id:    str | None
    verdict:     str | None
    outcome:     str
    started_at:  str
    finished_at: str | None = None


@dataclass
class SubjectSoul:
    """Result of `get_subject_soul`."""
    subject_id:    str
    snapshot_at:   str
    soul_version:  int
    tags:          list[SoulTag]
    recent_traces: list[SoulTrace]
    retired_count: int = 0


# --------------------------------------------------------------------------- #
# Resource result dataclasses
# --------------------------------------------------------------------------- #


@dataclass
class PolicySummary:
    """One CB policy from `concordia:/workspace/policies`."""
    cb_policy_id: str
    name:         str
    description:  str
    scope:        str
    action:       str
    enabled:      bool
    rules:        list[dict]
    updated_at:   str | None = None


@dataclass
class InstalledCanon:
    """One installed Canon from `concordia:/workspace/canons`."""
    canon_id:          str
    name:              str
    category:          str | None
    author_name:       str | None
    latest_version:    str | None
    short_description: str
    icon_url:          str | None
    enabled:           bool
    installed_at:      str | None


@dataclass
class LedgerEntry:
    """One ledger row from `concordia:/workspace/recent-ledger`."""
    event_id:     str
    index:        int
    prev_hash:    str
    payload_hash: str
    hash:         str
    payload:      dict
    recorded_at:  str


@dataclass
class LedgerPage:
    """Paginated result from `recent_ledger`. If `next_since` is not
    None, the caller should re-call `recent_ledger(since=next_since)`
    to fetch the next page."""
    entries:    list[LedgerEntry]
    since:      int
    limit:      int
    count:      int
    next_since: int | None = None


# --------------------------------------------------------------------------- #
# Exception hierarchy. Maps 1:1 to MCP spec §8 error codes.
# --------------------------------------------------------------------------- #


class ConcordiaError(Exception):
    """Base class for every Concordia client error."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        error_id: str | None = None,
        data: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code     = code
        self.error_id = error_id
        self.data     = data or {}


class ConcordiaProtocolError(ConcordiaError):
    """JSON-RPC envelope was malformed, or transport-level failure.
    Includes parse errors, timeouts, and unexpected HTTP statuses."""


class ConcordiaAuthError(ConcordiaError):
    """API key rejected — error_id=auth_expired (-32001)."""


class ConcordiaQuotaExceededError(ConcordiaError):
    """Workspace hit a billing cap — error_id=tenant_quota_exceeded (-32002)."""


class ConcordiaPolicyEngineUnavailableError(ConcordiaError):
    """CB substrate degraded — error_id=policy_engine_unavailable (-32003).
    Per spec §8 the customer agent should default to `review` and
    defer the action when this fires."""


class ConcordiaCanonNotInstalledError(ConcordiaError):
    """A canon_filter referenced a Canon not in the workspace's corpus —
    error_id=canon_not_installed (-32004)."""


class ConcordiaSubjectNotFoundError(ConcordiaError):
    """get_subject_soul against an unknown subject —
    error_id=subject_not_found (-32005). Agent should treat as a new
    subject (no accumulated tags)."""


class ConcordiaCircuitOpenError(ConcordiaError):
    """Per-tool failures triggered a rate limit —
    error_id=circuit_open (-32006). Retry with exponential backoff."""


class ConcordiaPermissionDeniedError(ConcordiaError):
    """The API key's role doesn't allow the requested method/tool —
    error_id=permission_denied (-32007)."""


# JSON-RPC code → exception class. Used by the dispatcher to raise
# the most specific exception possible.
_ERROR_MAP: dict[int, type[ConcordiaError]] = {
    -32001: ConcordiaAuthError,
    -32002: ConcordiaQuotaExceededError,
    -32003: ConcordiaPolicyEngineUnavailableError,
    -32004: ConcordiaCanonNotInstalledError,
    -32005: ConcordiaSubjectNotFoundError,
    -32006: ConcordiaCircuitOpenError,
    -32007: ConcordiaPermissionDeniedError,
}


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

DEFAULT_BASE_URL = "https://api.concordex.dev"
DEFAULT_TIMEOUT  = 10.0
_USER_AGENT      = "concordex-concordia-python/0.6.0"
_MCP_PATH        = "/mcp/v1"
_PROTOCOL        = "1.0"


class ConcordiaClient:
    """Concordia MCP 1.0 client.

    Thread-safe (the underlying httpx.Client is). Implements
    `__enter__`/`__exit__` so it can be used as a context manager;
    call `.close()` explicitly otherwise to release the HTTP pool.

    Parameters mirror the agent-stream `Concordex` client for
    consistency: `api_key` (required, must start with `ck_`),
    `base_url`, `timeout`, `user_agent`. A `transport` kwarg is
    accepted for testing (pass a fake httpx transport to bypass the
    network).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        key = api_key or os.environ.get("CONCORDEX_API_KEY", "")
        if not key:
            raise ConcordiaError(
                "api_key required (pass api_key=… or set CONCORDEX_API_KEY)"
            )
        if not key.startswith("ck_"):
            raise ConcordiaError(
                "api_key must start with 'ck_' — got something else; "
                "double-check you copied a Concordex key, not a different "
                "service's token"
            )
        self._api_key  = key
        self._base_url = base_url.rstrip("/")
        self._timeout  = timeout
        self._ua       = user_agent or _USER_AGENT
        self._id_seq   = 0

        client_kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "timeout":  self._timeout,
            "headers":  {
                "User-Agent":    self._ua,
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type":  "application/json",
            },
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._http = httpx.Client(**client_kwargs)

    # --- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "ConcordiaClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """Release the underlying HTTP connection pool. Safe to call
        more than once."""
        try:
            self._http.close()
        except Exception:
            pass

    # --- MCP dispatch helpers ---------------------------------------------

    def _next_id(self) -> int:
        self._id_seq += 1
        return self._id_seq

    def _rpc(self, method: str, params: dict | None = None) -> Any:
        """Issue one JSON-RPC 2.0 request. Returns the `result` payload
        on success; raises a typed exception on app errors; raises
        ConcordiaProtocolError on transport / envelope failures.
        """
        body = {
            "jsonrpc": "2.0",
            "id":      self._next_id(),
            "method":  method,
            "params":  params or {},
        }
        try:
            resp = self._http.post(_MCP_PATH, json=body)
        except httpx.TimeoutException as e:
            raise ConcordiaProtocolError(
                f"Concordia request timed out after {self._timeout}s"
            ) from e
        except httpx.RequestError as e:
            raise ConcordiaProtocolError(
                f"Concordia transport error: {e}"
            ) from e

        # Concordia returns 200 with a JSON-RPC envelope even on app
        # errors. Anything else is a transport-level surprise.
        if resp.status_code != 200:
            raise ConcordiaProtocolError(
                f"Concordia returned HTTP {resp.status_code}",
                data={"status": resp.status_code, "body": resp.text[:1000]},
            )
        try:
            envelope = resp.json()
        except json.JSONDecodeError as e:
            raise ConcordiaProtocolError(
                "Concordia returned non-JSON body",
                data={"body": resp.text[:1000]},
            ) from e

        if not isinstance(envelope, dict):
            raise ConcordiaProtocolError("Envelope is not a JSON object")
        if envelope.get("jsonrpc") != "2.0":
            raise ConcordiaProtocolError(
                f"Envelope jsonrpc != '2.0' (got {envelope.get('jsonrpc')!r})"
            )

        if "error" in envelope:
            err = envelope["error"] or {}
            code = err.get("code")
            msg  = err.get("message") or ""
            data = err.get("data") or {}
            exc_cls = _ERROR_MAP.get(code, ConcordiaError)
            raise exc_cls(
                msg,
                code=code,
                error_id=data.get("error_id"),
                data=data,
            )
        if "result" not in envelope:
            raise ConcordiaProtocolError(
                "Envelope missing both `result` and `error`"
            )
        return envelope["result"]

    def _call_tool(self, name: str, arguments: dict) -> dict:
        """Wrapper for `tools/call`. Returns the unwrapped `_data`
        block from the tool's content envelope."""
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise ConcordiaProtocolError(
                f"tools/call {name} returned non-object: {type(result).__name__}"
            )
        # Prefer the typed `_data` field (Concordex extension); fall
        # back to parsing the canonical `content[].text` JSON if it's
        # missing (defensive — should always be present on a Concordex
        # server).
        if "_data" in result and isinstance(result["_data"], dict):
            return result["_data"]
        content = result.get("content") or []
        for block in content:
            if (isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)):
                try:
                    return json.loads(block["text"])
                except json.JSONDecodeError:
                    continue
        raise ConcordiaProtocolError(
            f"tools/call {name} response had no decodable content"
        )

    # --- discovery ---------------------------------------------------------

    def initialize(self) -> dict:
        """MCP initialize handshake. Returns the raw server response
        including `protocolVersion`, `serverInfo`, `capabilities`,
        and `principalHint`."""
        return self._rpc("initialize")

    def ping(self) -> bool:
        """Liveness check. Returns True on success."""
        r = self._rpc("ping")
        return bool(r.get("ok"))

    def tools_list(self) -> list[dict]:
        """Return the raw tool descriptors. Useful for code that
        wants to introspect the available tools at runtime."""
        r = self._rpc("tools/list")
        return list(r.get("tools") or [])

    def resources_list(self) -> list[dict]:
        """Return the raw resource descriptors."""
        r = self._rpc("resources/list")
        return list(r.get("resources") or [])

    # --- the four tools ----------------------------------------------------

    def enforce_covenant(
        self,
        *,
        subject_id: str,
        action_kind: str,
        action_payload: dict | None = None,
        context: dict | None = None,
    ) -> EnforceCovenantResult:
        """Score a proposed action against installed policy Canons.
        Spec §4.1.

        The agent calls this BEFORE executing any consequential
        action. The result's `verdict` is one of `allow`, `review`,
        `block`, or `escalate`. Every call writes a ledger entry —
        the action plus the decision is part of the audit chain.
        """
        d = self._call_tool("enforce_covenant", {
            "subject_id":     subject_id,
            "action_kind":    action_kind,
            "action_payload": action_payload or {},
            "context":        context or {},
        })
        cb = d.get("cb_state_change") or {}
        return EnforceCovenantResult(
            verdict=d.get("verdict", ""),
            policy_ids=list(d.get("policy_ids") or []),
            rationale=d.get("rationale", ""),
            cb_state_change=CBStateChange(
                state=cb.get("state", ""),
                warning=bool(cb.get("warning", False)),
                soul_version=cb.get("soul_version"),
            ),
            ledger_entry_id=d.get("ledger_entry_id", ""),
        )

    def record_decision(
        self,
        *,
        subject_id: str,
        decision_kind: str,
        payload: dict | None = None,
        actor: str = "agent",
        outcome: str = "completed",
    ) -> RecordDecisionResult:
        """Append an audit-grade decision record. Spec §4.2.

        Use this for actions the agent took without first calling
        enforce_covenant — retrospective auditing, content
        generation, tool invocations that didn't need pre-flight
        approval. The returned `chain_head_hash` is the new ledger
        head; verify offline by walking the chain.
        """
        d = self._call_tool("record_decision", {
            "subject_id":    subject_id,
            "decision_kind": decision_kind,
            "payload":       payload or {},
            "actor":         actor,
            "outcome":       outcome,
        })
        return RecordDecisionResult(
            ledger_entry_id=d.get("ledger_entry_id", ""),
            chain_head_hash=d.get("chain_head_hash", ""),
            index=d.get("index"),
        )

    def query_corpus(
        self,
        *,
        query: str,
        scope: str = "installed",
        canon_filter: Iterable[str] | None = None,
        limit: int = 20,
    ) -> QueryCorpusResult:
        """Search installed Canons for matching policy text,
        vocabulary, prompts, or descriptions. Spec §4.3."""
        args: dict[str, Any] = {"query": query, "scope": scope, "limit": limit}
        if canon_filter:
            args["canon_filter"] = list(canon_filter)
        d = self._call_tool("query_corpus", args)
        matches = [
            CorpusMatch(
                canon_id=m.get("canon_id", ""),
                canon_version=m.get("canon_version", ""),
                section=m.get("section", ""),
                excerpt=m.get("excerpt", ""),
                relevance=float(m.get("relevance", 0.0)),
            )
            for m in (d.get("matches") or [])
        ]
        return QueryCorpusResult(
            matches=matches,
            scope=d.get("scope", scope),
            canon_count=int(d.get("canon_count") or 0),
        )

    def get_subject_soul(
        self,
        *,
        subject_id: str,
        depth: str = "latest",
        tag_filter: Iterable[str] | None = None,
    ) -> SubjectSoul:
        """Return the latest soul snapshot for a subject. Spec §4.4.

        Raises ConcordiaSubjectNotFoundError if the subject has no
        soul in this workspace's division DB — the customer agent
        should treat that as a new subject with no accumulated state.
        """
        args: dict[str, Any] = {"subject_id": subject_id, "depth": depth}
        if tag_filter:
            args["tag_filter"] = list(tag_filter)
        d = self._call_tool("get_subject_soul", args)
        tags = [
            SoulTag(
                tag_id=t.get("tag_id", ""),
                score=float(t.get("score", 0.0)),
                raw_strength=t.get("raw_strength"),
                evidence_count=int(t.get("evidence_count", 0)),
                first_observed_at=t.get("first_observed_at", ""),
                last_reinforced_at=t.get("last_reinforced_at", ""),
            )
            for t in (d.get("tags") or [])
        ]
        traces = [
            SoulTrace(
                trace_id=tr.get("trace_id", ""),
                frame_id=tr.get("frame_id"),
                verdict=tr.get("verdict"),
                outcome=tr.get("outcome", ""),
                started_at=tr.get("started_at", ""),
                finished_at=tr.get("finished_at"),
            )
            for tr in (d.get("recent_traces") or [])
        ]
        return SubjectSoul(
            subject_id=d.get("subject_id", subject_id),
            snapshot_at=d.get("snapshot_at", ""),
            soul_version=int(d.get("soul_version") or 0),
            tags=tags,
            recent_traces=traces,
            retired_count=int(d.get("retired_count") or 0),
        )

    # --- the three resources (typed wrappers) -----------------------------

    def _read_resource(self, uri: str) -> dict:
        """Issue resources/read and decode the JSON content block."""
        r = self._rpc("resources/read", {"uri": uri})
        contents = r.get("contents") or []
        for block in contents:
            if (isinstance(block, dict)
                    and block.get("mimeType") == "application/json"
                    and isinstance(block.get("text"), str)):
                try:
                    return json.loads(block["text"])
                except json.JSONDecodeError as e:
                    raise ConcordiaProtocolError(
                        f"resources/read {uri} returned non-JSON content"
                    ) from e
        raise ConcordiaProtocolError(
            f"resources/read {uri} returned no JSON content block"
        )

    def workspace_policies(self) -> list[PolicySummary]:
        """concordia:/workspace/policies — every CB policy in this
        workspace. Read once at session start to know which covenant
        categories exist."""
        d = self._read_resource("concordia:/workspace/policies")
        return [
            PolicySummary(
                cb_policy_id=p.get("cb_policy_id", ""),
                name=p.get("name", ""),
                description=p.get("description", ""),
                scope=p.get("scope", ""),
                action=p.get("action", ""),
                enabled=bool(p.get("enabled", False)),
                rules=list(p.get("rules") or []),
                updated_at=p.get("updated_at"),
            )
            for p in (d.get("policies") or [])
        ]

    def workspace_canons(self) -> list[InstalledCanon]:
        """concordia:/workspace/canons — every installed Canon's
        metadata."""
        d = self._read_resource("concordia:/workspace/canons")
        return [
            InstalledCanon(
                canon_id=c.get("canon_id", ""),
                name=c.get("name", ""),
                category=c.get("category"),
                author_name=c.get("author_name"),
                latest_version=c.get("latest_version"),
                short_description=c.get("short_description", ""),
                icon_url=c.get("icon_url"),
                enabled=bool(c.get("enabled", False)),
                installed_at=c.get("installed_at"),
            )
            for c in (d.get("canons") or [])
        ]

    def recent_ledger(
        self,
        *,
        since: int = 0,
        limit: int = 100,
    ) -> LedgerPage:
        """concordia:/workspace/recent-ledger — paginated ledger
        reads. To page forward, call again with
        `since=page.next_since` until `next_since is None`."""
        params = urllib.parse.urlencode({"since": since, "limit": limit})
        uri = f"concordia:/workspace/recent-ledger?{params}"
        d = self._read_resource(uri)
        entries = [
            LedgerEntry(
                event_id=e.get("event_id", ""),
                index=int(e.get("index") or 0),
                prev_hash=e.get("prev_hash", ""),
                payload_hash=e.get("payload_hash", ""),
                hash=e.get("hash", ""),
                payload=e.get("payload") or {},
                recorded_at=e.get("recorded_at", ""),
            )
            for e in (d.get("entries") or [])
        ]
        ns = d.get("next_since")
        return LedgerPage(
            entries=entries,
            since=int(d.get("since") or since),
            limit=int(d.get("limit") or limit),
            count=int(d.get("count") or len(entries)),
            next_since=int(ns) if ns is not None else None,
        )

    def iter_ledger(
        self,
        *,
        since: int = 0,
        page_size: int = 100,
    ) -> Iterable[LedgerEntry]:
        """Yield every ledger entry from `since` onwards, paginating
        under the hood. Stops when a partial page (or empty page) is
        returned."""
        cursor: int | None = since
        while cursor is not None:
            page = self.recent_ledger(since=cursor, limit=page_size)
            for entry in page.entries:
                yield entry
            cursor = page.next_since


__all__ = [
    # Client
    "ConcordiaClient",
    # Result types
    "EnforceCovenantResult",
    "CBStateChange",
    "RecordDecisionResult",
    "QueryCorpusResult",
    "CorpusMatch",
    "SubjectSoul",
    "SoulTag",
    "SoulTrace",
    # Resource types
    "PolicySummary",
    "InstalledCanon",
    "LedgerEntry",
    "LedgerPage",
    # Exceptions
    "ConcordiaError",
    "ConcordiaProtocolError",
    "ConcordiaAuthError",
    "ConcordiaQuotaExceededError",
    "ConcordiaPolicyEngineUnavailableError",
    "ConcordiaCanonNotInstalledError",
    "ConcordiaSubjectNotFoundError",
    "ConcordiaCircuitOpenError",
    "ConcordiaPermissionDeniedError",
]
