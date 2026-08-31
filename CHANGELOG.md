# Changelog

## [Unreleased]

### Added
- **Circuit-breaker state cache** (spec §4.4). `check()` is a network
  round trip in front of a sensitive action; `cb_cache_ttl` (seconds,
  like `timeout`) lets a repeat check on the same subject come from
  memory instead. Off at 0, which is the default.

  `cb_cache_max_entries` bounds it — the key is a subject id, so an
  agent seeing many subjects would otherwise hold an entry for each for
  the life of the process — and evicts least-recently-used first.
  `check(fresh=True)` skips the cache and refreshes it; `guard()` passes
  `fresh` through.
- `CheckResult.cached`, `.cache_age_ms`, `.stale`. A caller recording a
  denial has to be able to tell it read four-second-old state. The
  server's own `latency_ms`, `route_latency_ms`, `checked_at` and `raw`
  are left alone on a cached result — they describe the check that
  happened.
- `cb_cache_on_error="last_known"` serves the last known state for a
  subject, marked `.stale`, when the check cannot reach the server. It
  raises when nothing is known for that subject, and cannot be set
  without a TTL to fall back on. A `429` stays a `RateLimitError`: the
  server answered, and the `retry_after` is worth acting on.
- `ON_ERROR_RAISE` / `ON_ERROR_LAST_KNOWN` exported, so the policy is
  not a bare string at the call site.

### Fixed
- `__spec_version__` read `0.6.0` while `client._SPEC_VERSION` and
  `pyproject.toml` read `0.8.0`. The middle one is what goes out as the
  User-Agent on every request, so the version a server saw and the
  version a reader saw had drifted apart with nothing to catch it. All
  three now read `0.9.0`, and `tests/test_version_markers.py` holds
  them together.

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
