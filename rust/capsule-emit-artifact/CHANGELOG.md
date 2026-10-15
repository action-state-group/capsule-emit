# Changelog

All notable changes to the `capsule-emit-artifact` crate are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this crate uses [Semantic Versioning](https://semver.org/) once it reaches 1.0.

## Unreleased

The first version: the artifact store contract (`docs/artifact-store.md` in
the capsule-emit repository) as the `Store` and `TxStore` traits, with `jsonl`
and `memory` backends and `sqlite` and `mysql` backends behind Cargo features,
and `conformance::run`, the shared conformance suite. Not published yet.
