# Concurrency — one log, one writer

Two processes writing to the same `ledger.jsonl` at once is not a crash bug —
it produces exactly the inconsistent-history artifact this whole design
treats as an attack. A torn or interleaved log manufactures fork evidence:
your own checkpoint/witness stream would refuse it later, so the failure
just gets deferred and harder to diagnose. `capsule-emit` refuses to let it
happen at all.

## The rule

**One log, one writer, enforced.** `append_to_ledger()` (and therefore every
`seal()` / `received()` call — standalone or composed via a slot wrapper —
plus the checkpoint stream's stamp writes) takes an OS-level `flock` for the
duration of each append. It is mandatory — there is no configuration flag to
turn it off.

- **In-process** (two threads): serialized on an internal lock, as before.
  Threads never see this behavior at all.
- **Cross-process** (two OS processes writing the same ledger): the second
  writer finds the lock held and fails **immediately**, with a
  `capsule_emit.ledger.LedgerLockedError` that names the holder:

  ```
  ledger /path/to/ledger.jsonl is locked by another writer (pid 8421 on
  my-host (writer since 2026-08-24T18:02:11Z)). capsule-emit enforces one
  writer per log -- a torn log manufactures fork evidence, so a second
  writer is never allowed to interleave silently. Route writes through the
  existing writer, or opt in to waiting with
  append_to_ledger(..., wait=True[, timeout=seconds]). See
  docs/concurrency.md ("One log, one writer").
  ```

Waiting is **opt-in, never silent.** Pass `wait=True` to
`append_to_ledger()` to block until the lock frees instead of failing; add
`timeout=<seconds>` to bound how long you're willing to wait before it still
raises `LedgerLockedError`.

## If you hit this error

You have two writer processes racing the same ledger file. Both are honest
shapes — pick one:

1. **Route writes through one writer.** Give each writer process its own
   log file (e.g. one ledger per worker, per pod, per signing identity), or
   have every caller hand its capsules to a single process that owns the
   append.
2. **Opt in to waiting**, if brief contention is expected and acceptable
   for your workload: `append_to_ledger(capsule, ledger, wait=True,
   timeout=5)`.

What is never an option is a silent interleave — that's the one shape this
lock exists to rule out.

## Mechanism

A sidecar lock file, `<ledger>.lock`, sits beside the ledger and is
`flock`'d for the duration of each append (POSIX `fcntl.flock`; CI and the
supported deployment targets are Linux/macOS). The lock file's contents
(`pid`, `hostname`, `acquired_at`) exist only so a blocked writer can name
who's holding it — they carry no other meaning and are not part of the
ledger's own record stream.
