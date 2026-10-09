# Evidence-file vectors

Two evidence files (`evidence-bundle/v2`, draft-mih-zhang-agent-disclosure-bundle-00) saved by an
independent producer on the two nodes of a live run: one exchange, each node's own record at log
position 1, under that node's signed checkpoint. Each producer pads its log to 32 leaves before a
checkpoint, so the checkpoint's last leaf is padding, not a record.

Each file carries:

- the record, unchanged;
- the checkpoint, with its COSE form (`checkpoint.cose`);
- the record's inclusion proof under the checkpoint;
- a range proof over the log as it stood just after the record (`range_root`, size 1), bound to the
  checkpoint (size 63) by a consistency proof (`completeness_certificate.consistency_proof`).

`python/tests/test_evidence_file_vectors.py` runs `check_evidence_file` on both:

- **Checks today:** the record, its producer signature, citation closure, and the signed checkpoint.
  `checkpointed-local-log`'s Python verifier accepts this producer's COSE checkpoint.
- **Coverage and membership:** these need an `agent-action-capsule` that accepts a range root bound
  to the checkpoint by a consistency proof, which the bundle draft allows. Until then they fail with
  `completeness_certificate_invalid`, and the file is INVALID. Once AAC accepts it, both pass, with
  `checkpoint_leaves_after_interval:31`, and the file is VALID. The test accepts exactly those two
  states and nothing else.

The files are byte-for-byte as the producer saved them.
