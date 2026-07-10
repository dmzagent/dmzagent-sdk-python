# Changelog

## [0.7.0] — 2026-07-10

DX-9 harmonization release — aligns the Python SDK with
`dmzagent-sdk-spec` v0.7.0 (error taxonomy) and the server's
LC-P3..P6 wire changes, per the Logic Canon spec-review §3 mandates.

### Added
- `RateLimitError` — new exception for HTTP 429 with a `retry_after`
  attribute parsed from the `Retry-After` header (delta-seconds;
  `None` when absent or unparseable). The SDK never sleeps or retries
  automatically — surface the value and let the caller decide.
- HTTP 422 now maps to `ValidationError` (well-formed but
  unprocessable — bad event / rulebook). Previously 422 and 429 both
  collapsed into the generic `DMZAgentError`.
- `FiredRule` and `Escalation` frozen dataclasses with the house
  `raw` wire-dict passthrough. `band`/`lane`/`status` fields remain
  open strings, never enums.
- `LogicEventAck` honest-ack fields (server LC-P3):
  `degraded: bool`, `responded: bool`, `installs_evaluated: int`,
  `installs_total: int` — defaulting to `False`/`0` when absent
  (older servers).
- `LogicCanon.vendor_id` (`str | None`) — parity with the TypeScript
  SDK.
- `LogicInstallHealthRow` — the health-surface row model
  (`status` = ok | missing_bytes | compile_error, plus `detail`).
- Contract-test runner passes fixture `headers` through to the
  stubbed response and asserts `expected_retry_after` against
  `RateLimitError.retry_after` (spec fixtures
  `422_unprocessable_validation`, `429_rate_limited_retry_after`,
  `429_rate_limited_no_header`).

### Changed
- **Breaking:** `LogicEventAck.fired` / `.escalations` are now tuples
  of `FiredRule` / `Escalation` DTOs instead of raw dicts — consumers
  indexing `ack.fired[0]["rule_id"]` must switch to
  `ack.fired[0].rule_id` (the wire dict remains on `.raw`).
- **Breaking:** `LogicInstallHealth.installs` now contains
  `LogicInstallHealthRow` items; `LogicCanonInstall` is a pure deploy
  record (lifecycle `status`, no `detail` field) — the two previously
  conflated enums are split per spec-review §3 / P6.
- Spec version pin bumped to `0.7.0` (`pyproject.toml`
  `[tool.dmzagent] spec-version`, `__spec_version__`, User-Agent).
- Server list responses renamed `total` → `count` (LC-P6); the SDK
  never read `total`, so this is test-fixture-only. Workspace
  install-list rows no longer carry `vendor_id`.

## [0.6.0] — 2026-06-02

### Added
- `dmzagent.concordia` — Python client for the Concordia MCP 1.0
  governance server at `/mcp/v1`. New `ConcordiaClient` class wraps
  the four MCP tools (`enforce_covenant`, `record_decision`,
  `query_corpus`, `get_subject_soul`) and the three resources
  (`concordia:/workspace/policies`, `concordia:/workspace/canons`,
  `concordia:/workspace/recent-ledger`) so callers don't write
  JSON-RPC envelopes by hand.
- Typed result dataclasses: `EnforceCovenantResult`,
  `RecordDecisionResult`, `QueryCorpusResult`, `SubjectSoul`,
  `PolicySummary`, `InstalledCanon`, `LedgerEntry`, `LedgerPage`.
- Typed exception hierarchy with 1:1 mapping to MCP spec §8 error
  codes: `ConcordiaAuthError`, `ConcordiaQuotaExceededError`,
  `ConcordiaPolicyEngineUnavailableError`,
  `ConcordiaCanonNotInstalledError`,
  `ConcordiaSubjectNotFoundError`, `ConcordiaCircuitOpenError`,
  `ConcordiaPermissionDeniedError`. Plus base `ConcordiaError` and
  transport-level `ConcordiaProtocolError`.
- `ConcordiaClient.iter_ledger()` generator for streaming through
  paginated ledger pages.

### Changed
- Package version bumped to `0.6.0` (minor — additive, no breaking
  changes to the existing 0.5.0 surface). The agent-stream
  surface (`DMZAgent` client, `Conversation`, `verify_webhook_signature`)
  remains at spec v0.5.0; `__spec_version__` is unchanged.

## [0.5.0] — 2026-05-30

First lockstep release — supersedes the unilateral `0.1.0` line.
This version aligns the Python SDK with the TypeScript, C#, and Java
implementations under `dmzagent-sdk-spec` v0.5.0.

### Added
- `EmitResult` surfaces the rich synchronous envelope: `frame_id`,
  `subject_id`, `outcome`, `triage_decision`, `tags_fired`,
  `scored_by_canons`, `soul_version`, `ledger_index`, `follow_my_data`.
- `verify_webhook_signature(payload, header, secret, tolerance_seconds=300)`
  helper for verifying DMZAgent outbound webhooks.
- `emit_event(..., async_mode=True)` opt-in to the legacy
  fire-and-forget envelope via `X-DMZAgent-Async: true`.
- `[tool.dmzagent] spec-version` pin in `pyproject.toml`; CI verifies
  it matches the checked-out spec tag.
- Contract test runner (`tests/test_contract.py`) that exercises every
  fixture in `dmzagent-sdk-spec/contract-tests/`.
- `__spec_version__` module attribute.
- Spec-conformance and publish GitHub Actions workflows aligned with
  the spec repo's `promote.yml`.

### Changed
- `User-Agent` default bumped to `dmzagent-python/0.5.0`.
- Repository moved out of the monorepo at `uni/sdks/python/` into its
  own repo `dmzagent-sdk-python` for ecosystem-native CI and
  contribution workflow.

### No breaking changes
- All existing method signatures preserved.
- Existing `EmitResult.interaction_id` / `subjects` / `queued` /
  `raw` fields unchanged.

## [0.1.0] — unilateral
Initial Python-only release. Superseded by 0.5.0 with full multi-language parity.
