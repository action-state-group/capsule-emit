# Contributing to capsule-emit

`capsule-emit` is the **producer/emission layer** for the Agent Action Capsule
specification ([`agent-action-capsule`](https://github.com/action-state-group/agent-action-capsule)).
Contributions are welcome.

## License (Apache-2.0)

All contributions are licensed under the **Apache License 2.0** (see `LICENSE`).

### Developer Certificate of Origin (DCO)

This project uses the [Developer Certificate of Origin 1.1](https://developercertificate.org/).
Sign off every commit:

```bash
git commit -s -m "your message"
```

No CLA is required — the DCO is the whole agreement.

## Scope discipline (review gates, not preferences)

1. **Product-free.** This library carries the emission layer, framework adapters,
   ledger utilities, and a manifest *declaration* parser — nothing tenant-specific,
   product-specific, or internal. PRs that import application/product internals
   (enforcement engines, hosted services, billing, custody) will be declined —
   that belongs in a downstream engine, not in this neutral substrate.
2. **Neutrality is enforced.** A CI gate scans every PR for a reserved-vocabulary
   set (held in a repo secret, not listed here). Keep contributions vendor-neutral;
   describe *a compatible enforcement gateway* generically rather than naming one.
3. **The spec is the source of truth.** When `capsule-emit` and the
   [`agent-action-capsule`](https://github.com/action-state-group/agent-action-capsule)
   draft disagree, fix the implementation or open an issue against the spec —
   never let the two silently diverge. A capsule produced here MUST verify with
   the reference verifier.
4. **Standards honesty.** The underlying profile is an **individual IETF
   Internet-Draft**, not an RFC; never claim an RFC number or WG adoption it does
   not have.
5. **Digest-only stays digest-only.** The anchor path submits a digest and never
   business content. Any change that could put raw payload on the wire is a
   correctness (and security) regression — see `SECURITY.md`.

## Dev setup

```bash
pip install -e ".[dev]"            # editable install + dev tools
pip install agent-action-capsule  # the reference verifier (separate package)
pytest -q                          # run the suite
ruff check .                       # lint
python examples/quickstart_demo.py # the 5-minute acceptance demo
```

## Adding a witness or countersigner to the directory

`witnesses.json` at the repository root lists who runs a witness and who
issues countersignatures, so a verifier can put a name to a key. Anyone who
operates one can add a row; no row is privileged, the maintainers' own
included. Both arrays are alphabetical by `name`, one row per operator and
endpoint, and every row passes the same checks
(`python -m capsule_emit.witness_directory witnesses.json`; field shapes in
`docs/schemas/witnesses.schema.json`).

Open the pull request with the directory-row template
(`?template=directory-row.md` on the compare URL). A reviewer fetches your
key from your endpoint's own host before merging. The directory records who
holds a key, never how good they are: rows carry no rating, score, or tier.
It also never decides whether a particular countersignature is independent.
A verifier works that out for each entry from the signer key and the
producer key, and a countersignature made with the producer's own key is
shown as not independent whatever the row says.

## Where discussion happens

The underlying specification is discussed in the IETF **SCITT** Working Group
(`scitt@ietf.org`). Library issues (adapters, CLI, ledger, ergonomics) belong
here as GitHub issues.
