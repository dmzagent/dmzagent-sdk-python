# Changelog

## [0.6.0] — 2026-06-02

### Added
- `concordex.concordia` — Python client for the Concordia MCP 1.0
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
  surface (`Concordex` client, `Conversation`, `verify_webhook_signature`)
  remains at spec v0.5.0; `__spec_version__` is unchanged.

## [0.5.0] — 2026-05-30

First lockstep release — supersedes the unilateral `0.1.0` line.
This version aligns the Python SDK with the TypeScript, C#, and Java
implementations under `concordex-sdk-spec` v0.5.0.

### Added
- `EmitResult` surfaces the rich synchronous envelope: `frame_id`,
  `subject_id`, `outcome`, `triage_decision`, `tags_fired`,
  `scored_by_canons`, `soul_version`, `ledger_index`, `follow_my_data`.
- `verify_webhook_signature(payload, header, secret, tolerance_seconds=300)`
  helper for verifying Concordex outbound webhooks.
- `emit_event(..., async_mode=True)` opt-in to the legacy
  fire-and-forget envelope via `X-Concordex-Async: true`.
- `[tool.concordex] spec-version` pin in `pyproject.toml`; CI verifies
  it matches the checked-out spec tag.
- Contract test runner (`tests/test_contract.py`) that exercises every
  fixture in `concordex-sdk-spec/contract-tests/`.
- `__spec_version__` module attribute.
- Spec-conformance and publish GitHub Actions workflows aligned with
  the spec repo's `promote.yml`.

### Changed
- `User-Agent` default bumped to `concordex-python/0.5.0`.
- Repository moved out of the monorepo at `uni/sdks/python/` into its
  own repo `concordex-sdk-python` for ecosystem-native CI and
  contribution workflow.

### No breaking changes
- All existing method signatures preserved.
- Existing `EmitResult.interaction_id` / `subjects` / `queued` /
  `raw` fields unchanged.

## [0.1.0] — unilateral
Initial Python-only release. Superseded by 0.5.0 with full multi-language parity.
