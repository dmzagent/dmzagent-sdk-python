# concordex

The Python SDK for **Concordex** — the codex of trust between minds.

Concordex indexes how AI agents (and any other subject of study)
reveal themselves, predicts how they'll move under conditions, and
gates their actions through auditable circuit breakers. This SDK is
the customer-facing surface: emit conversation events, check whether
a subject is still in good standing, verify webhook signatures, and
let your operators write policy in one place.

This is the **Python** implementation of the Concordex SDK
specification. The canonical surface is defined in
[`concordex-sdk-spec`](https://github.com/praeceptor-thesis/concordex-sdk-spec)
and is implemented in lockstep across Python, TypeScript, C#, and Java.
A given version (e.g. `0.5.0`) means the same surface in every language.

## Install

```bash
pip install concordex
```

## Quickstart

```python
from concordex import Concordex, CBOpenError

cx = Concordex(api_key="ck_…")  # get one from your tenant_admin

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
`emit_event` (or set the header `X-Concordex-Async: true` if you
construct requests manually):

```python
r = cx.emit_event("subject_says", ..., async_mode=True)
r.queued            # True
r.frame_id          # None — server is processing in the background
```

## Webhook verification

Concordex outbound webhooks are signed with HMAC-SHA256. Verify them
on your receiver:

```python
from concordex import verify_webhook_signature

ok = verify_webhook_signature(
    payload          = request.body,                 # raw bytes or str
    signature_header = request.headers["Concordex-Signature"],
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
Concordex builds up from observed behavior.

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

All inherit from `ConcordexError`. Use `try/except CBOpenError` as a
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
cx = Concordex(
    api_key="ck_…",
    base_url="https://api.concordex.dev",   # override for staging / on-prem
    timeout=10.0,                            # per-request seconds
    user_agent="my-app/1.2.3",               # appears in server-side audit logs
)
```

## Spec version

This SDK implements the [Concordex SDK specification](https://github.com/praeceptor-thesis/concordex-sdk-spec)
at the version pinned in `pyproject.toml`'s `[tool.concordex]
spec-version`. The contract test corpus from that repo is what
guarantees parity with the TypeScript, C#, and Java SDKs at the same
version.

To run conformance locally:

```bash
git clone https://github.com/praeceptor-thesis/concordex-sdk-spec ../concordex-sdk-spec
CONCORDEX_SPEC_PATH=../concordex-sdk-spec pytest tests/test_contract.py
```

## Concordia MCP client (`concordex.concordia`)

Concordia is Concordex's governance MCP server — customer agents
speak [MCP 1.0](https://modelcontextprotocol.io/) to it to enforce
covenants, record audit decisions, query installed Canons, and read
accumulated soul state on subjects under observation.

The Python SDK ships a typed client for it alongside the agent-stream
surface, so you can do both in the same process without writing
JSON-RPC by hand:

```python
from concordex.concordia import ConcordiaClient

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
with ConcordiaClient(api_key=os.environ["CONCORDEX_API_KEY"]) as c:
    c.enforce_covenant(...)
# HTTP pool released on exit
```

See [`CONCORDIA_MCP.md`](https://github.com/praeceptor-thesis/concordex-sdk-spec/blob/main/CONCORDIA_MCP.md)
for the underlying protocol specification.

## License

Apache-2.0
