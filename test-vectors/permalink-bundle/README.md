# Permalink fragment vectors

Pins what `capsule_emit.permalink` puts after the `#` in a permalink: an
`evidence-bundle/v2` Bundle encoded with the §9 Fragment Codec of
draft-mih-zhang-agent-disclosure-bundle-00 (unpadded RFC 4648 base64url over
the UTF-8 JCS bytes), and the pointer form an oversize permalink falls back to:

```json
{"bundle_ref": {"digest": "<bundle digest>", "root": "<capsule_id>", "locations": ["<URI>", ...]}}
```

Each case in `vectors.json` gives the input capsules, the `bundle` flag and
`disclosures` passed to `build_url`, the expected Bundle, and three values
computed by agent-action-capsule's Go reference (`../go-oracle/jcs_oracle.go`),
not by capsule-emit: `fragment`, `bundle_digest` and `pointer_fragment`.
`python/tests/test_permalink_bundle_codec.py` checks capsule-emit against them.

The cases cover a Bundle of one, a three-capsule chain, the chain with
non-ASCII and astral-plane disclosures (its fragment contains `-`/`_`), and a
partial chain whose missing parent is declared in `completeness.missing`.
Fragment lengths cover every unpadded remainder (0, 2 and 3 mod 4).

The capsules carry no `signature`/`key_id`: those are capsule-emit's ledger
bookkeeping, outside the `capsule_id` preimage.

Regenerate (fresh capsules, so every value changes):

```bash
CAPSULE_WITNESS=off AAC_JCS_ORACLE=/tmp/aac-jcs-oracle \
    python test-vectors/permalink-bundle/scripts/generate_vectors.py
```
