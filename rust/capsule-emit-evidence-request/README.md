# capsule-emit-evidence-request (deprecated)

**Renamed to [`evidence-request`](https://crates.io/crates/evidence-request).**
The Evidence Request protocol (draft-mih-agent-evidence-request-00) moved to
[agent-action-capsule](https://github.com/action-state-group/agent-action-capsule/tree/main/rust/evidence-request),
next to the draft and its conformance vectors.

This final release, 0.0.5, re-exports `evidence-request` 0.1, so code written
against 0.0.4 keeps compiling. To move:

```toml
evidence-request = "0.1"
```

and replace `capsule_emit_evidence_request::` with `evidence_request::` in
`use` paths. The API is the same as 0.0.4. No further versions of this crate
will be published.

## License

Apache-2.0.
