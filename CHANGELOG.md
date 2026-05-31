# Changelog

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
