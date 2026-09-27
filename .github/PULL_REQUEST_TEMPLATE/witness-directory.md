## Add a witness to the directory

<!-- Open with ?template=witness-directory.md. One row per pull request. -->

**Operator:** <!-- name as you declare it publicly -->
**Endpoint:** <!-- base URL -->
**Binding:** <!-- cll | rekor | scrapi -->

### Checklist

- [ ] One object added to `witnesses/witnesses.json`, in alphabetical position by `operator`
- [ ] `grades_issued` lists only what your receipt actually attests (`countersigned-observed` = existence and time; `mmr-verified` = you checked consistency)
- [ ] `public_key_pem` is the key your receipts are signed with, and a receipt from `endpoint` verifies under it
- [ ] `independent_of` lists only operators you share no control, keys, or infrastructure with
- [ ] `pytest tests/test_witness_directory.py` passes
- [ ] DCO sign-off: `git commit -s`
