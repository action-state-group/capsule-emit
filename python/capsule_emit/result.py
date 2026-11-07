# SPDX-License-Identifier: Apache-2.0
"""Append a sealed Evidence Result capsule to a ledger.

A Result is built and sealed elsewhere (``capsulectl result build``, which
writes the sealed capsule with ``--capsule-out`` as an artifact record);
capsule-emit never builds or seals one (issue #355, Ethan's append-only
ruling of 2026-10-21). :func:`append_result` takes that record, checks it
and appends it to a ledger, so that ``capsule-emit bundle --record <result id>
--closure-depth 1`` can root an Evidence Bundle on it. The Result page is built
from that bundle.

What is checked, before anything is written:

- the seal: the capsule id recomputes, the capsule verifies as Class 1 and the
  Producer Envelope verifies over it (the ledger's own admission check), and the
  record's ``payload`` original is bound by the capsule's ``agent_input_digest``;
- the identity: that original is an ``evidence-result-v0`` document;
- the citations: every ``acted_on`` reference the capsule makes is a record
  this ledger already holds, so a bundle rooted on the Result carries them.

Where the original goes depends on the target. A ledger store directory (the
flat ``ledger.jsonl`` layout) keeps no originals, so the ``payload`` original is
checked and then left out of the stored line, as for every record such a ledger
holds; the capsule (its references, its payload digest) is what is appended.
A store-backed :class:`capsule_emit.store_backed_ledger.Ledger` (a CLL entry log
and an artifact store: jsonl, memory, sqlite, mysql) retains the original beside
the capsule, so :func:`capsule_emit.evidence_bundle.build_store_bundle` can
carry it as the root's disclosed ``agent_input`` and the Result page renders
from the bundle (capsule-emit#443). Appending the same Result again writes
nothing.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .artifact.record import (
    PAYLOAD_DIGEST,
    PRESENT,
    ArtifactError,
    Record,
    _StrictJSONError,
    decode_strict_json,
    valid_id,
)
from .artifact.record import verify as verify_record

if TYPE_CHECKING:
    from .store_backed_ledger import Ledger

__all__ = ["ResultAppend", "ResultRefused", "append_result", "append_result_file"]

#: The ``result_version`` of the Result document (Evidence Result v0).
RESULT_VERSION = "evidence-result-v0"

#: The record original that holds the sealed document.
PAYLOAD = "payload"


class ResultRefused(ValueError):
    """The record is not an appendable sealed Result; the message says why.
    Nothing was written."""


@dataclass(frozen=True)
class ResultAppend:
    """What :func:`append_result` did: the Result's ``capsule_id``, its 1-based
    position in the ledger, and whether this call wrote it (``False``: the
    ledger already held it)."""

    capsule_id: str
    seq: int
    appended: bool


def _payload(record: Record) -> dict:
    """The Result document the record carries, checked against the capsule."""
    originals = [a for a in record.artifacts if a.name == PAYLOAD]
    if len(originals) != 1 or originals[0].state != PRESENT or originals[0].content is None:
        raise ResultRefused(f"a Result record carries its document as one retained {PAYLOAD!r} original")
    if originals[0].binding != PAYLOAD_DIGEST:
        raise ResultRefused(f"the {PAYLOAD!r} original is not bound by the capsule's agent_input_digest")
    try:
        document = decode_strict_json(bytes(originals[0].content))
    except _StrictJSONError as exc:
        raise ResultRefused(f"the {PAYLOAD!r} original is not JSON: {exc}") from exc
    if not isinstance(document, dict) or document.get("result_version") != RESULT_VERSION:
        raise ResultRefused(f"the {PAYLOAD!r} original is not an {RESULT_VERSION} document")
    return document


def _missing(missing: list, where: str) -> str:
    return (
        f"{where} does not hold {len(missing)} record(s) the Result cites (acted_on): "
        + ", ".join(missing)
        + "; append them first"
    )


def _acted_on(capsule: dict) -> list:
    cited = []
    for ref in capsule.get("references") or []:
        if isinstance(ref, dict) and ref.get("citation_purpose") == "acted_on":
            digest = ref.get("digest")
            if not isinstance(digest, str) or not valid_id(digest.lower()):
                raise ResultRefused(f"an acted_on reference has no SHA-256 digest: {digest!r}")
            cited.append(digest.lower())
    return cited


def append_result(
    record: Record,
    ledger: str | os.PathLike | Ledger | None = None,
) -> ResultAppend:
    """Append the sealed Result ``record`` to ``ledger``: a ledger store
    directory (the default ledger when ``None``, as :func:`capsule_emit.seal`
    resolves it; its own trust and lock settings apply; the original is not
    kept), or an open store-backed ``Ledger`` (the original is retained; see
    :meth:`Ledger.append_record` for the chain and statement rules the ledger
    was opened with). Raises :class:`ResultRefused` for a record that is not a
    sealed Result or cites a record the ledger lacks; the ledger errors
    (``LedgerLockedError``, ...) propagate. Idempotent."""
    from . import ledger_layout as _layout
    from .legacy_ledger import shared

    try:
        capsule = decode_strict_json(bytes(record.capsule))
    except _StrictJSONError as exc:
        raise ResultRefused(f"the capsule is not JSON: {exc}") from exc
    if not isinstance(capsule, dict):
        raise ResultRefused("the capsule is not a JSON object")

    from agent_action_capsule.producer_envelope import verify_producer_envelope

    envelope = verify_producer_envelope(record.capsule_id, bytes(record.producer_envelope))
    if not envelope.ok or envelope.public_key is None:
        raise ResultRefused("the Producer Envelope does not verify over the capsule id")
    try:
        # Trust is the ledger's own policy at write time (put_entry); here the
        # signer is only needed to run the record check that binds the document.
        verify_record(record, [envelope.public_key])
    except ArtifactError as exc:
        raise ResultRefused(f"the record does not verify: {exc}") from exc
    _payload(record)
    cited = _acted_on(capsule)
    if not cited:
        raise ResultRefused("a Result cites no record (no acted_on reference)")

    from .store_backed_ledger import (
        AlreadyInLogError,
        AppendChainMismatchError,
        EnvelopeError,
        Ledger,
        MissingCapsuleIdError,
        PaddingMisroutedError,
        StatementInvalidError,
    )

    if isinstance(ledger, Ledger):
        missing = [digest for digest in cited if not ledger.contains(digest)]
        if missing:
            raise ResultRefused(_missing(missing, "the ledger"))
        try:
            seq, appended = ledger.append_record(record)
        except (
            AlreadyInLogError,
            AppendChainMismatchError,
            EnvelopeError,
            MissingCapsuleIdError,
            PaddingMisroutedError,
            StatementInvalidError,
            ArtifactError,  # the record's own admission checks (prepare): limits, a purged original
        ) as exc:
            # The ledger refused this record. StaleLedgerError, StoreError and
            # LogError (a second writer, a failing backend) propagate, as the
            # flat ledger's lock errors do.
            raise ResultRefused(f"the ledger refused the Result: {exc}") from exc
        return ResultAppend(record.capsule_id, seq, appended)

    path = _layout.resolve_ledger(ledger).path
    if not Path(path).exists():
        raise ResultRefused(f"no ledger at {path}; the records the Result cites must be in it")
    target = shared(path)
    missing = [digest for digest in cited if not target._holds(bytes.fromhex(digest))]
    if missing:
        raise ResultRefused(_missing(missing, f"the ledger {path}"))

    # The id is over the capsule's value, not its bytes: capsulectl writes raw
    # UTF-8 and escapes < > &, which the ledger's line form does not.
    from .legacy_ledger import _line_form

    stored = dataclasses.replace(record, artifacts=[], capsule=_line_form(capsule).encode("ascii"))
    try:
        result = target.put_entry(stored)
    except ArtifactError as exc:
        raise ResultRefused(f"the ledger refused the Result: {exc}") from exc
    return ResultAppend(record.capsule_id, result.entry.seq, result.outcome == "inserted")


def _load(path: str | os.PathLike) -> Record:
    from .artifact.record import decode_record

    with open(path, encoding="utf-8") as fh:
        return decode_record(fh.read())


def append_result_file(path: str | os.PathLike, ledger: str | os.PathLike | None = None) -> ResultAppend:
    """:func:`append_result` for the record file ``capsulectl result build
    --capsule-out`` writes."""
    try:
        record = _load(path)
    except (OSError, ArtifactError, ValueError, json.JSONDecodeError) as exc:
        raise ResultRefused(f"{path}: not an artifact record file: {exc}") from exc
    return append_result(record, ledger)
