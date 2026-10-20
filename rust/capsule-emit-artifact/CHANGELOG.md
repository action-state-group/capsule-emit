# Changelog

All notable changes to the `capsule-emit-artifact` crate are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this crate uses [Semantic Versioning](https://semver.org/) once it reaches 1.0.

## Unreleased

The first version: the artifact store contract (`docs/artifact-store.md` in
the capsule-emit repository) as the `Store` and `TxStore` traits, with `jsonl`
and `memory` backends and `sqlite` and `mysql` backends behind Cargo features,
and `conformance::run`, the shared conformance suite. Not published yet.

`ArtifactError::Unsupported` reports a store's capability limits, which only
the legacy ledgers have (`docs/legacy-ledger.md`). capsule-emit's legacy
ledger backend is `capsule_emit::legacy_ledger::LegacyLedger` in the
`capsule-emit` crate, which depends on this one. `check_config` and
`decode_strict_json` are public, for a backend of your own. `conformance::run_limited`
with `conformance::Limits` runs the suite for a store with limits, deriving
the `originals` and `purge` capabilities from each case's steps.

Stores admit padding records (`PADDING_RECORD_TYPE`, `is_padding`): checked by
their closed shape instead of AAC Class 1, with a trusted envelope and only
unbound artifacts. `LegacyLedger` stores a padding record through `put` and returns
it from `get`.
