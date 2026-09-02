# capsule-emit interoperability fixtures

`format4-interop/` was generated with the unmodified Python library at
`action-state-group/capsule-emit` release `v0.6.1`, commit
`004f8ca14372829dfa93a8816949e6713fb14596`. The Go repository owns this
frozen pack; the Python repository does not need a matching test-vector change.
The Go-owned `format4-interop/scripts/generate_vectors.py` regenerates the pack
through that release's public `seal`, `received`, and slot-composition API.
It freezes authored sealing with model/runtime
metadata, opaque receive/carry, WHO and DID members, WHO/CAN/DID slot
composition, signature-free canonical Capsule bytes, Python's stored
representation, and detached Producer Envelopes. Go reconstructs every record
from `input.json` and compares Capsule and Envelope bytes exactly.
The input's six-digit fractional timestamp and deliberately non-canonical
`did`/`can`/`who` member order are regression traps, not arbitrary examples.

`producer-envelope-valid-capsule.json` is copied without modification from
`action-state-group/capsule-emit` commit
`63dbb282236b184ae6ea6653c122a2125dd2976f`, path
`test-vectors/producer-envelope/valid/capsule.json`.

Its SHA-256 digest is
`cddbb90695c73ec12265089df554defba2a1f6e391492d02eaab15c5850cb51b`,
matching that upstream commit's
`test-vectors/producer-envelope/SHA256SUMS` manifest.

`slot-composition-valid-capsule.json` is the signature-free form of
`test-vectors/slot-composition/valid/slot_form_composition.json` from
`action-state-group/capsule-emit` commit
`d9347009adbd8fa45a97d79a103cc92329a48dc0`. Only the local Producer Envelope
fields `signature` and `key_id` were removed; both are excluded from the
format-4 Capsule-ID preimage. Its Capsule ID and slot-bearing
`composed_members` are unchanged. The local fixture SHA-256 is
`db5cf337051352478a8a834aa91d248f114a3167fcf2f8349ca016e618fb2bed`.

The source repository is licensed under Apache-2.0.
