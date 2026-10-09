# Shared receipt proof-array vectors

Copied byte-for-byte from action-state-group/scitt-cose at
`e539fe4d72e6ad70fe443bf2079795322c69a711`,
`test-vectors/v1/{valid-eddsa-multi-proof,fail-zero-size-proof}`.
Source: https://github.com/action-state-group/scitt-cose/tree/e539fe4d72e6ad70fe443bf2079795322c69a711/test-vectors
License: Apache-2.0.

The donated corpus owns these cases. This consumer copy pins the exact bytes
so CI needs no network or unpublished crate. The multi-proof receipt puts a
decoy at index 0 and the target at index 1; the reordered receipt is a positive
control. Zero-size trees must return `Malformed` with and without a key.
