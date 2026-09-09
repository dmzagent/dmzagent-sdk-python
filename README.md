# dmzagent

The Python SDK for **DMZAgent** — the codex of trust between minds.

DMZAgent indexes how AI agents (and any other subject of study)
reveal themselves, predicts how they'll move under conditions, and
gates their actions through auditable circuit breakers. This SDK is
the customer-facing surface: emit conversation events, check whether
a subject is still in good standing, verify webhook signatures, and
let your operators write policy in one place.

This is the **Python** implementation of the DMZAgent SDK
specification. The canonical surface is defined in
[`dmzagent-sdk-spec`](https://github.com/praeceptor-thesis/dmzagent-sdk-spec)
and is implemented in lockstep across Python, TypeScript, C#, and Java.
A given version (e.g. `0.5.0`) means the same surface in every language.

## Install

```bash
pip install dmzagent
```

## Quickstart

```python
from dmzagent import DMZAgent, CBOpenError

cx = DMZAgent(api_key="ck_…")  # get one from your tenant_admin

with cx.conversation(participants=[
    {"subject_id": "user:ws_xxx:checkout-bot",  "role": "agent",    "kind": "agent"},
    {"subject_id": "user:ws_xxx:customer-anon", "role": "customer", "kind": "human"},
]) as conv:
    conv.says("user:ws_xxx:customer-anon", "I want a refund.")
    conv.says("user:ws_xxx:checkout-bot",  "I can help with that.")

    # Before doing something sensitive, check the breaker.
    with conv.guard("user:ws_xxx:checkout-bot", raise_on_open=True):
        conv.tool_call(
            "user:ws_xxx:checkout-bot",
            tool="refund.issue",
            args={"amount": 9900},
        )
```

A single `says()` call per utterance — the same method regardless of
who's speaking. `participants` is a list of `{subject_id, role, kind}`
dicts; the subject_id is the only discriminator.

## What the server returns

Synchronous mode (the default) returns the rich envelope:

```python
r = cx.subject_says(
    agent_subject_id="user:ws:bot",
    subject_id="user:ws:cust",
    text="I want a refund.",
)
r.frame_id          # "frame_xyz"
r.outcome           # "scored"
r.tags_fired        # ["risk.refund_pressure"]
r.scored_by_canons  # ["canon_market_abnormality_v3"]
r.soul_version      # 42
r.ledger_index      # 1234
r.follow_my_data    # "/w/ws_xxx/frames/frame_xyz"
```

Async mode keeps the response minimal — pass `async_mode=True` to
`emit_event` (or set the header `X-DMZAgent-Async: true` if you
construct requests manually):

```python
r = cx.emit_event("subject_says", ..., async_mode=True)
r.queued            # True
r.frame_id          # None — server is processing in the background
```

## Webhook verification

DMZAgent outbound webhooks are signed with HMAC-SHA256. Verify them
on your receiver:

```python
from dmzagent import verify_webhook_signature

ok = verify_webhook_signature(
    payload          = request.body,                 # raw bytes or str
    signature_header = request.headers["DMZAgent-Signature"],
    secret           = WEBHOOK_SUBSCRIPTION_SECRET,
    tolerance_seconds = 300,                          # max age, default 300
)
if not ok:
    return Response(status=400)
```

The helper returns `False` (never raises) for malformed headers,
expired timestamps, or signature mismatches.

## Concepts

**Subject.** An identifiable noun: an AI agent, a human customer, a
sensor, an institution. Each has a stable `subject_id` and a soul that
DMZAgent builds up from observed behavior.

**Interaction.** Anything that involves multiple subjects together —
a chat session, a transaction chain, a video feed. Events stamp an
`interaction_id` so the full participant list and timeline is recoverable.

**Circuit breaker.** A subject's current standing: `closed` (allow),
`half_open` (allow with warning), `open` (block). State is a function
of the subject's soul evaluated against your workspace's policies.
Recomputed after every reasoning step; cached for sub-50ms reads.

## Errors

| Exception          | When                                          |
|--------------------|-----------------------------------------------|
| `AuthError`        | API key missing / invalid / revoked           |
| `PermissionError`  | API key valid but scope insufficient          |
| `ValidationError`  | Server returned 400 — payload malformed       |
| `ServerError`      | Server returned 5xx — safe to retry           |
| `CBOpenError`      | Circuit breaker open — action must not proceed|

All inherit from `DMZAgentError`. Use `try/except CBOpenError` as a
control-flow seam around sensitive actions:

```python
try:
    with cx.guard(subject_id="user:ws_xxx:bot", raise_on_open=True):
        do_sensitive_thing()
except CBOpenError as e:
    log_blocked(e.reason, e.scope_ref, e.anchor)
```

## Configuration

```python
cx = DMZAgent(
    api_key="ck_…",
    base_url="https://api.dmzagent.com",   # override for staging / on-prem
    timeout=10.0,                            # per-request seconds
    user_agent="my-app/1.2.3",               # appears in server-side audit logs
)
```

### Circuit-breaker state cache

`check()` is a network round trip, and it usually sits in front of the
sensitive action. A per-client cache removes it for repeated checks on
the same subject. It is off unless you set a TTL:

```python
cx = DMZAgent(
    api_key="ck_…",
    cb_cache_ttl=5.0,                # seconds; 0 (the default) is off
    cb_cache_max_entries=1024,       # bounded, least-recently-used evicted
    cb_cache_on_error="last_known",  # or "raise" (default)
)

r = cx.check(subject_id="user:ws:bot")
r.cached          # served from memory?
r.cache_age_ms    # how old it was
r.stale           # served because the check itself failed

cx.check(subject_id="user:ws:bot", fresh=True)   # skip the cache, refresh it
```

Read the TTL as **the longest a newly-opened breaker can go unnoticed by
this client**. A cached `closed` is an allow the server might no longer
give, which is why the cache is opt-in and why every result says whether
it came from memory and how old it was.

One TTL covers every state. Holding a deny longer than an allow is a
safety policy, and it is yours to make with the number you pass.

`cb_cache_on_error="last_known"` serves the last state for that subject —
marked `.stale` — when the check cannot reach the server. With no entry
for that subject it raises, and it needs a TTL above zero to be set at
all. A `429` is not covered: that is the server answering, and it carries
a `retry_after` worth acting on.

## Human-in-the-loop approvals

A circuit-breaker policy can fire with action `require_approval`, which
**holds** the action instead of refusing it. `check()` then hands back a
denial that names what it is waiting on:

```python
g = cx.check(subject_id="user:ws:checkout-bot")
if g.awaiting_approval:
    show_my_own_approval_screen(g.pending_approval_id)   # asked
elif not g.allow:
    return refuse(g.reason)                              # refused
```

That is the whole difference between a breaker and a human-in-the-loop
control, and it is one field because you have to branch on it.

### You render it. All of it.

```python
for a in cx.iter_approvals(status="pending"):
    print(a.action["tool"], a.action["args"])   # the held call, verbatim
    print(a.reason)                             # your operator's policy words
    print(a.expires_at)                         # decide before this
```

Nothing in an `Approval` is display text we wrote. `reason` and each
`fired_policies[].name` are the words your operator typed when they
wrote the policy, and `action` is the call your agent was about to make.
There is no message for your end user, no copy of ours, and no branding —
because a sentence we wrote would read identically in every customer's
product, which is the thing this is designed to avoid.

### A decision records which human made it

```python
cx.approve_approval(
    "apr_7f3c9a1b",
    actor_id="acct_4471",              # your identifier, not ours
    actor_label="Dana R.",
    reason="verified the order by phone",
)
```

`actor_id` is required, never defaulted, and never derived from the API
key — the key identifies your integration, and an approval whose actor is
the integration that requested it has recorded nobody. We resolve it
against no directory, so your users never need an account here. An empty
one raises `ValueError` before any request goes out.

Two operators who click at the same moment produce one decision and one
`ConflictError`; `err.body["status"]` says what the approval had already
become. That is not a retry — the call did not fail, it lost.

**An approval that nobody answers declines.** `on_expiry` is always
`decline` and there is no setting that changes it: an approval that
becomes an allow because nobody looked at it is not a
human-in-the-loop control, it is a delay with extra steps.

## The incident and remediation ledger

`anchor` has been on `CheckResult` since 0.4.0, pointing into a ledger
nothing could open. Now it opens:

```python
g = cx.check(subject_id="user:ws:checkout-bot")
recorded = g.anchor           # {"ledger_index": 40197, "hash": "b1c4…"}

for inc in cx.iter_incidents(status="open", since="2026-09-01T00:00:00Z"):
    print(inc.kind, inc.reason, len(inc.remediations))
    if inc.anchor == recorded:
        ...                   # this is the entry your check was told about
```

Every breaker that opened, every approval decided, every remediation
that ran — newest ledger entry first, in the order the ledger recorded
them rather than by timestamp, because two entries written in the same
second still have an order.

The ledger is **append-only**. There is no `close_incident()` and no
method that edits an entry: an incident reaches `remediated` because a
remediation was appended to it, and `status` is a fold over what has
been appended. An incident with no remediations is the normal shape of
something nobody has answered yet.

### Paging

`list_approvals()` and `get_incidents()` return one page and do not
follow `next_cursor`. You asked for 25 and you get 25 — a method that
quietly walked every page would turn one bounded request into an
unbounded one against a record that only grows. `iter_approvals()` and
`iter_incidents()` do the walk, lazily: break out of the loop and the
next page is never requested.

## Spec version

This SDK implements the [DMZAgent SDK specification](https://github.com/praeceptor-thesis/dmzagent-sdk-spec)
at the version pinned in `pyproject.toml`'s `[tool.dmzagent]
spec-version`. The contract test corpus from that repo is what
guarantees parity with the TypeScript, C#, and Java SDKs at the same
version.

To run conformance locally:

```bash
git clone https://github.com/praeceptor-thesis/dmzagent-sdk-spec ../dmzagent-sdk-spec
DMZAGENT_SPEC_PATH=../dmzagent-sdk-spec pytest tests/test_contract.py
```

## Concordia MCP client (`dmzagent.concordia`)

Concordia is DMZAgent's governance MCP server — customer agents
speak [MCP 1.0](https://modelcontextprotocol.io/) to it to enforce
covenants, record audit decisions, query installed Canons, and read
accumulated soul state on subjects under observation.

The Python SDK ships a typed client for it alongside the agent-stream
surface, so you can do both in the same process without writing
JSON-RPC by hand:

```python
from dmzagent.concordia import ConcordiaClient

client = ConcordiaClient(api_key="ck_live_…")

# Pre-flight check — does policy allow this action?
v = client.enforce_covenant(
    subject_id="user:alice",
    action_kind="payment.issue",
    action_payload={"amount": 9900, "currency": "usd"},
    context={"session_id": "s_42", "model": "claude-sonnet-4-6"},
)
if v.verdict == "block":
    return generate_safe_fallback(v.rationale)

# Audit record after the fact
client.record_decision(
    subject_id="user:alice",
    decision_kind="payment_issued",
    payload={"refund_id": "re_123", "amount": 9900},
    outcome="completed",
)

# Read installed policy Canons (cacheable at session start)
for policy in client.workspace_policies():
    print(policy.name, policy.action, policy.enabled)

# Search Canons for relevant policy text
result = client.query_corpus(query="how do we handle refund pressure?")
for match in result.matches:
    print(f"{match.canon_id}/{match.section}: {match.excerpt}")

# Stream through the workspace's audit chain
for entry in client.iter_ledger(since=0, page_size=100):
    verify(entry.prev_hash, entry.hash, entry.payload)
```

Errors map 1:1 to the MCP spec §8 codes:
`ConcordiaAuthError`, `ConcordiaQuotaExceededError`,
`ConcordiaPolicyEngineUnavailableError`,
`ConcordiaCanonNotInstalledError`,
`ConcordiaSubjectNotFoundError`,
`ConcordiaCircuitOpenError`,
`ConcordiaPermissionDeniedError`. All inherit from
`ConcordiaError`, so `try: ... except ConcordiaError as e:` covers
every failure mode.

The client is thread-safe and supports context-manager usage:

```python
with ConcordiaClient(api_key=os.environ["DMZAGENT_API_KEY"]) as c:
    c.enforce_covenant(...)
# HTTP pool released on exit
```

See [`CONCORDIA_MCP.md`](https://github.com/praeceptor-thesis/dmzagent-sdk-spec/blob/main/CONCORDIA_MCP.md)
for the underlying protocol specification.

## License

Apache-2.0
