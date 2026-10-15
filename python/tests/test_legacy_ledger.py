"""The flat ledger.jsonl as a backend for the CLL entry log and the artifact
store (capsule-emit#308 item 5): the declared conformance subsets, the
legacy cases, root invariance and every CLI command."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cll.checkpoint import core as mmr_core
from cll.checkpoint.index import MmrLedger
from cll.log import (
    IDEMPOTENT,
    INSERTED,
    AppendInput,
    ClosedError,
    CorruptError,
    NotFoundError,
    UnsupportedError,
)
from cll.log import conformance as cll_conformance

from capsule_emit import cli, core, ledger, seal, witness
from capsule_emit.artifact import ConflictError, InvalidError, Record, UntrustedSignerError
from capsule_emit.artifact import NotFoundError as ArtifactNotFoundError
from capsule_emit.artifact import UnsupportedError as ArtifactUnsupportedError
from capsule_emit.artifact import conformance as artifact_conformance
from capsule_emit.artifact.record import Artifact
from capsule_emit.legacy_ledger import CAPABILITIES, LegacyLedger

FIXTURE = Path(__file__).parent / "fixtures" / "legacy_ledger" / "ledger.jsonl"
# The root origin/main's checkpoint index computed for FIXTURE (see
# fixtures/legacy_ledger/make_fixture.py): 11 leaves, MMR size 19.
FIXTURE_ROOT = "35bebcfecf0733675e56a7185cdc2bcbeb5a86c3923a40913cbbf3227be23849"


def _lines():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines()]


@pytest.fixture
def fixture_path(tmp_path):
    path = tmp_path / "ledger.jsonl"
    shutil.copy(FIXTURE, path)
    return path


# -- declared conformance subsets ---------------------------------------------


def _fresh_ledger():
    return LegacyLedger(Path(tempfile.mkdtemp()) / "ledger.jsonl")


def _reopen(current):
    current.close()
    return LegacyLedger(current._path)


CLL_CASES = cll_conformance.cases(_fresh_ledger, _reopen, digest_append=False, interfaces=("entries", "close"))


@pytest.mark.parametrize("case", CLL_CASES, ids=[c.name for c in CLL_CASES])
def test_cll_conformance_subset(case):
    if case.skip:
        pytest.skip(case.skip)
    case.run()


def test_cll_subset_is_declared():
    assert CAPABILITIES["digest-append"] is False
    ran = {c.name for c in CLL_CASES if not c.skip}
    assert {"entries/empty-log", "entries/invalid-values", "entries/digest-append-unsupported"} <= ran


HARNESS = artifact_conformance.Harness(
    new_backing=lambda: Path(tempfile.mkdtemp()) / "ledger.jsonl",
    open=lambda path, namespace, trusted: LegacyLedger(path, namespace, trusted),
    durable=True,
    originals=False,
    purge=False,
)
ARTIFACT_TESTS = artifact_conformance.tests(HARNESS)


@pytest.mark.parametrize("test", ARTIFACT_TESTS, ids=[t.name for t in ARTIFACT_TESTS])
def test_artifact_conformance_subset(test):
    if test.skip:
        pytest.skip(test.skip)
    test.run()


def test_artifact_subset_runs_the_bare_cases():
    ran = {t.name for t in ARTIFACT_TESTS if not t.skip}
    assert {"bare_record", "bare_rejections", "bare_untrusted_on_read", "malformed_ids"} <= ran
    assert "purge_lifecycle" not in ran and "put_get_roundtrip" not in ran


# -- historical ledger ---------------------------------------------------------


def test_root_invariance_with_stamp_disclosure_backfill_and_padding_lines(fixture_path):
    kinds = [line.get("kind") or line.get("record_type") for line in _lines()]
    for kind in (ledger.CHECKPOINT_STAMP_KIND, ledger.DISCLOSURE_RECORD_KIND, ledger.WITNESS_BACKFILL_KIND, "padding"):
        assert kind in kinds
    mmr = MmrLedger(witness._JsonlLogSource(str(fixture_path)))
    mmr.sync()
    assert (mmr.leaf_count(), mmr.size(), mmr.root().hex()) == (11, 19, FIXTURE_ROOT)

    # The entry log holds exactly the leaves the index folded.
    entries = LegacyLedger(fixture_path).scan_entries(0, 1000)
    assert [e.value for e in entries] == [mmr.body_digest(seq) for seq in range(1, 12)]


def test_scan_keeps_repeated_capsule_ids_and_lookups_return_the_first(fixture_path):
    store = LegacyLedger(fixture_path)
    entries = store.scan_entries(0, 100)
    assert [e.seq for e in entries] == list(range(1, 12))
    assert entries[5].value == entries[0].value
    assert store.get_entry(entries[0].value).seq == 1
    result = store.append(AppendInput(entries[0].value, datetime.now(timezone.utc)))
    assert (result.outcome, result.entry.seq) == (IDEMPOTENT, 1)
    with pytest.raises(UnsupportedError):
        store.append(AppendInput(b"\x07" * 32, datetime.now(timezone.utc)))
    with pytest.raises(NotFoundError):
        store.get_entry(b"\x07" * 32)
    assert store.scan_entries(4, 2) == entries[4:6]
    assert store.scan_entries(11, 5) == []


def test_appended_at_is_derived_per_line_kind(fixture_path):
    entries = LegacyLedger(fixture_path).scan_entries(0, 100)

    def iso(entry):
        return entry.appended_at.isoformat(timespec="milliseconds")

    assert iso(entries[3]) == "2025-01-02T03:04:05.678+00:00"  # jcs-n capsule, offset timestamp
    assert iso(entries[6]) == "2026-10-15T00:00:01.000+00:00"  # stamp: checkpoint.timestamp
    assert iso(entries[7]) == "2026-10-15T00:00:02.500+00:00"  # disclosure: timestamp
    assert iso(entries[8]) == iso(entries[7])  # backfill: inherits
    assert iso(entries[9]) == iso(entries[7])  # padding: inherits
    assert all(e.appended_at.tzinfo is not None for e in entries)


def test_first_line_without_time_is_the_unix_epoch(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps({"capsule_id": "ab" * 32, "record_type": "padding"}) + "\n")
    (entry,) = LegacyLedger(path).scan_entries(0, 10)
    assert entry.appended_at == datetime(1970, 1, 1, tzinfo=timezone.utc)


def test_get_returns_capsule_bytes_and_inline_envelope(fixture_path):
    store = LegacyLedger(fixture_path)
    lines = _lines()
    for index in (0, 1, 3, 4, 10):  # jcs, jcs chained, jcs-n, absent canonicalization_id, jcs
        line = dict(lines[index])
        record = store.get(line["capsule_id"])
        envelope = bytes.fromhex(line.pop("signature"))
        line.pop("key_id")
        assert record.capsule == json.dumps(line, separators=(",", ":")).encode()
        assert record.producer_envelope == envelope
        assert record.artifacts == []
    unsigned = store.get(lines[2]["capsule_id"])  # a log() entry
    assert unsigned.producer_envelope == b""
    for index in (6, 7, 8, 9):  # stamp, disclosure, backfill, padding: log entries only
        with pytest.raises(ArtifactNotFoundError):
            store.get(lines[index]["capsule_id"])


def test_get_authorizes_signers_only_when_trusted_keys_are_given(fixture_path):
    key = bytes.fromhex(_lines()[0]["key_id"])
    capsule_id = _lines()[0]["capsule_id"]
    assert LegacyLedger(fixture_path, "ledger", [key]).get(capsule_id).capsule_id == capsule_id
    with pytest.raises(UntrustedSignerError):
        LegacyLedger(fixture_path, "ledger", [b"\x01" * 32]).get(capsule_id)
    with pytest.raises(InvalidError):
        LegacyLedger(fixture_path, "ledger", [])


def test_put_of_a_historical_record_reproduces_its_line(fixture_path, tmp_path):
    source = LegacyLedger(fixture_path)
    target = LegacyLedger(tmp_path / "copy.jsonl")
    raw = FIXTURE.read_text().splitlines()
    for index in (0, 3, 4):
        record = source.get(_lines()[index]["capsule_id"])
        assert target.put_entry(record).outcome == INSERTED
        assert target.put_entry(record).outcome == IDEMPOTENT
    assert (tmp_path / "copy.jsonl").read_text().splitlines() == [raw[0], raw[3], raw[4]]


def test_capability_limits_are_refused_as_unsupported(fixture_path):
    store = LegacyLedger(fixture_path)
    capsule_id = _lines()[0]["capsule_id"]
    record = store.get(capsule_id)
    record.artifacts = [Artifact(name="payload", state="never_retained")]
    with pytest.raises(ArtifactUnsupportedError):
        store.put(record)
    with pytest.raises(ArtifactUnsupportedError):
        store.purge(capsule_id)
    with pytest.raises(InvalidError):
        store.purge(capsule_id.upper())
    assert CAPABILITIES == {
        "digest-append": False,
        "originals": False,
        "purge": False,
        "transactions": False,
        "multiple-writers": False,
    }


def test_put_refuses_bytes_outside_the_line_form_and_envelope_members(fixture_path):
    store = LegacyLedger(fixture_path)
    record = store.get(_lines()[0]["capsule_id"])
    spaced = Record(record.capsule_id, json.dumps(json.loads(record.capsule)).encode(), record.producer_envelope)
    with pytest.raises(InvalidError):
        store.put(spaced)
    member = json.loads(record.capsule)
    member["key_id"] = "00"
    with pytest.raises(InvalidError):
        store.put(Record(record.capsule_id, json.dumps(member, separators=(",", ":")).encode(), b""))


def test_a_different_line_under_an_existing_capsule_id_conflicts(fixture_path):
    store = LegacyLedger(fixture_path)
    signed = _lines()[0]
    unsigned = {k: v for k, v in signed.items() if k not in ("signature", "key_id")}
    with pytest.raises(ConflictError):
        store.append_line(unsigned)
    stamp = _lines()[6]
    with pytest.raises(ConflictError):
        store.append_line({"capsule_id": stamp["capsule_id"], "action_id": "x"})


def test_seal_re_append_is_idempotent_and_new_capsules_keep_their_bytes(fixture_path, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS", "off")
    before = fixture_path.read_bytes()
    assert core._append_capsule(dict(_lines()[1]), fixture_path) == 2
    assert fixture_path.read_bytes() == before

    result = seal({"n": 9}, action="new", operator="acme", anchor=False, witness=False, ledger=fixture_path)
    assert result.seq == 12
    assert fixture_path.read_bytes()[len(before) :] == (json.dumps(result.capsule, separators=(",", ":")) + "\n").encode()


# -- the file -------------------------------------------------------------------


def test_torn_last_line_is_never_merged_into_a_new_line(tmp_path, fixture_path):
    store = LegacyLedger(fixture_path)
    record = store.get(_lines()[0]["capsule_id"])
    path = tmp_path / "torn.jsonl"
    path.write_bytes(b'{"capsule_id":"')
    torn = LegacyLedger(path)
    assert torn.scan_entries(0, 10) == []
    torn.put(record)
    assert path.read_bytes().split(b"\n")[1] == FIXTURE.read_bytes().split(b"\n")[0]
    assert [e.seq for e in torn.scan_entries(0, 10)] == [1]


def test_a_complete_last_line_without_newline_still_counts(tmp_path):
    path = tmp_path / "ledger.jsonl"
    raw = FIXTURE.read_bytes().split(b"\n")
    path.write_bytes(raw[0] + b"\n" + raw[1])
    store = LegacyLedger(path)
    assert [e.seq for e in store.scan_entries(0, 10)] == [1, 2]
    assert store.get_entry(bytes.fromhex(_lines()[1]["capsule_id"])).seq == 2
    store.put(LegacyLedger(FIXTURE).get(_lines()[3]["capsule_id"]))
    assert [e.seq for e in store.scan_entries(0, 10)] == [1, 2, 3]
    assert path.read_bytes().endswith(b"\n")


def test_a_malformed_capsule_id_is_corrupt(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"capsule_id":"zz"}\n')
    with pytest.raises(CorruptError):
        LegacyLedger(path).scan_entries(0, 10)
    path.write_text("[1]\n")
    with pytest.raises(CorruptError):
        LegacyLedger(path).scan_entries(0, 10)


def test_a_corrupt_line_leaves_the_index_unchanged(tmp_path):
    raw = FIXTURE.read_bytes().split(b"\n")
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(raw[0] + b"\n")
    store = LegacyLedger(path)
    assert len(store.scan_entries(0, 10)) == 1
    with open(path, "ab") as fh:
        fh.write(raw[1] + b'\n{"capsule_id":"zz"}\n')
    for _ in range(2):
        with pytest.raises(CorruptError):
            store.scan_entries(0, 10)
    path.write_bytes(raw[0] + b"\n" + raw[1] + b"\n")
    assert [e.seq for e in store.scan_entries(0, 10)] == [1, 2]


def test_unparseable_and_blank_lines_are_skipped_as_before(tmp_path):
    raw = FIXTURE.read_bytes().split(b"\n")
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(raw[0] + b"\n\nnot json\n" + raw[1] + b"\n")
    assert [e.seq for e in LegacyLedger(path).scan_entries(0, 10)] == [1, 2]
    assert len(ledger.read_ledger_entries(path)) == 2


def test_handles_see_each_others_writes_and_a_shrunk_file_is_reread(fixture_path, tmp_path):
    path = tmp_path / "other.jsonl"
    first, second = LegacyLedger(path), LegacyLedger(path)
    record = LegacyLedger(fixture_path).get(_lines()[0]["capsule_id"])
    first.put(record)
    assert second.get(record.capsule_id).capsule == record.capsule
    ledger.append_to_ledger(_lines()[6], path)
    assert [e.seq for e in second.scan_entries(0, 10)] == [1, 2]
    path.write_text("")
    assert second.scan_entries(0, 10) == []


def test_concurrent_puts_write_each_capsule_once(fixture_path, tmp_path):
    path = tmp_path / "other.jsonl"
    record = LegacyLedger(fixture_path).get(_lines()[0]["capsule_id"])
    stores = [LegacyLedger(path, wait=True) for _ in range(4)]
    threads = [threading.Thread(target=s.put, args=(record,)) for s in stores for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(path.read_text().splitlines()) == 1


def test_a_second_writer_is_excluded(fixture_path, tmp_path):
    path = tmp_path / "ledger.jsonl"
    record = LegacyLedger(fixture_path).get(_lines()[0]["capsule_id"])
    with ledger._writer_lock(path):
        with pytest.raises(ledger.LedgerLockedError):
            LegacyLedger(path).put(record)
    LegacyLedger(path).put(record)


def test_closed_operations_fail(fixture_path):
    store = LegacyLedger(fixture_path)
    store.close()
    store.close()
    with pytest.raises(ClosedError):
        store.scan_entries(0, 1)
    with pytest.raises(ClosedError):
        store.get(_lines()[0]["capsule_id"])


def test_init_creates_the_file_and_namespace_is_reported(tmp_path):
    path = tmp_path / "new.jsonl"
    store = LegacyLedger(path, "tenant-1")
    store.init()
    store.init()
    assert path.exists() and path.read_bytes() == b""
    assert store.namespace == "tenant-1"
    with pytest.raises(InvalidError):
        LegacyLedger(path, "bad/namespace")


def test_a_ledger_store_directory_is_refused(tmp_path):
    (tmp_path / ledger.LEDGER_STORE_MANIFEST_FILENAME).write_text("{}")
    with pytest.raises(InvalidError):
        LegacyLedger(tmp_path)


def test_new_stamp_lines_are_covered_by_the_next_checkpoint(fixture_path):
    mmr = MmrLedger(witness._JsonlLogSource(str(fixture_path)))
    mmr.sync()
    stamp = {
        "kind": ledger.CHECKPOINT_STAMP_KIND,
        "v": 1,
        "capsule_id": "cd" * 32,
        "checkpoint": {"mmr_size": mmr.size(), "root": mmr.root().hex(), "timestamp": "2026-10-15T01:00:00Z"},
    }
    ledger.append_to_ledger(stamp, fixture_path)
    seq = LegacyLedger(fixture_path).get_entry(bytes.fromhex(stamp["capsule_id"])).seq
    assert seq == 12
    mmr.sync()
    proof = mmr.inclusion_proof(seq)
    assert mmr_core.verify_inclusion(mmr.root(), mmr.size(), seq - 1, bytes.fromhex(stamp["capsule_id"]), proof)


# -- every CLI command over a ledger written through the backend ------------------

# ``export`` reads Rust's ledger directory (capsules.jsonl + checkpoints.jsonl),
# not a ledger.jsonl; tests/test_scoped_export.py covers it.
NOT_LEDGER_JSONL = {"export"}


def _subcommands():
    names = set()
    for action in cli._build_parser()._subparsers._group_actions:
        for name, sub in action.choices.items():
            nested = [a for a in sub._actions if isinstance(a, argparse._SubParsersAction)]
            names.update(f"{name} {n}" for a in nested for n in a.choices) if nested else names.add(name)
    return names


from test_disclose import _clean_witness_state, stub_ts, three_record_ledger  # noqa: E402,F401


def test_every_cli_command_through_the_legacy_backend(three_record_ledger, tmp_path, capsys):  # noqa: F811
    path, caps, payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    inputs = tmp_path / "in.json"
    outputs = tmp_path / "out.json"
    inputs.write_text(json.dumps(payloads[0][0]))
    outputs.write_text(json.dumps(payloads[0][1]))
    bundle = tmp_path / "bundle.json"
    base = "https://verify.example"
    runs = {
        "ledger view": [["ledger", "view", str(path)], ["ledger", "view", str(path), "--chains"],
                        ["ledger", "view", str(path), "--json"]],
        "ledger show": [["ledger", "show", str(path), cid]],
        "verify": [["verify", "--store", str(path)]],
        "status": [["status", str(path), "--offline"]],
        "permalink": [["permalink", "--base-url", base, "--ledger", str(path)],
                      ["permalink", "--base-url", base, "--ledger", str(path), "--bundle-out", str(bundle), "--all"]],
        "evidence": [["evidence", "--base-url", base, "--ledger", str(path), "--out", str(tmp_path / "evidence.md")]],
        "disclose": [["disclose", str(path), cid, "--audience", "auditor", "--reveal", f"{cid}:agent_input={inputs}",
                      "--reveal", f"{cid}:agent_output={outputs}"]],
    }
    runs["verify"].append(["verify", "--bundle", str(bundle)])
    assert _subcommands() - NOT_LEDGER_JSONL == set(runs), "a CLI command is missing from this test"
    # An evidence file checked without a witness directory is INCOMPLETE
    # (exit 2): its receipts are not checked. Never INVALID (exit 1).
    incomplete = {("verify", "--bundle")}
    order = ["ledger view", "ledger show", "permalink", "verify", "status", "evidence", "disclose"]
    for name in order:
        for argv in runs[name]:
            expected = 2 if tuple(argv[:2]) in incomplete else 0
            assert cli.main(argv) == expected, (argv, capsys.readouterr())

    # The disclosure record went through the shared writer; both interfaces
    # still read every line.
    store = LegacyLedger(path)
    entries = store.scan_entries(0, 1000)
    assert [e.seq for e in entries] == list(range(1, len(ledger.read_ledger_entries(path)) + 1))
    for capsule in caps:
        assert store.get(capsule["capsule_id"]).producer_envelope == bytes.fromhex(capsule["signature"])
    assert any(e.get("kind") == ledger.DISCLOSURE_RECORD_KIND for e in ledger.read_ledger_entries(path))


# -- review regressions --------------------------------------------------------------


def _with_line(tmp_path, text):
    path = tmp_path / "edited.jsonl"
    path.write_text(text + "\n")
    return LegacyLedger(path)


def test_a_present_but_empty_or_one_sided_signature_is_invalid(tmp_path):
    line = dict(_lines()[0])
    for signature, key_id in (("", line["key_id"]), (None, None), ("", "")):
        edited = dict(line, signature=signature, key_id=key_id)
        store = _with_line(tmp_path, json.dumps(edited, separators=(",", ":")))
        with pytest.raises(InvalidError):
            store.get(line["capsule_id"])
    one_sided = {k: v for k, v in line.items() if k != "key_id"}
    with pytest.raises(InvalidError):
        _with_line(tmp_path, json.dumps(one_sided, separators=(",", ":"))).get(line["capsule_id"])


def test_artifact_reads_reject_duplicate_json_keys(tmp_path):
    raw = FIXTURE.read_text().splitlines()[0]
    duplicated = raw.replace('{"spec_version":', '{"action_id":"forged","spec_version":', 1)
    assert duplicated != raw
    store = _with_line(tmp_path, duplicated)
    assert store.get_entry(bytes.fromhex(_lines()[0]["capsule_id"])).seq == 1  # still a leaf
    with pytest.raises(InvalidError):
        store.get(_lines()[0]["capsule_id"])


def test_jcs_capsules_get_class_1_verification(tmp_path):
    line = {k: v for k, v in _lines()[2].items()}  # the unsigned log() entry
    del line["operator"]
    line["capsule_id"] = core.compute_capsule_id(line)
    store = LegacyLedger(tmp_path / "x.jsonl")
    capsule = json.dumps(line, separators=(",", ":")).encode()
    with pytest.raises(InvalidError, match="capsule verification"):
        store.put(Record(line["capsule_id"], capsule, b""))
    store.append_line(line)  # seal()'s writer does not verify
    with pytest.raises(InvalidError, match="capsule verification"):
        store.get(line["capsule_id"])


def test_arguments_are_validated_before_the_originals_refusal(fixture_path):
    record = LegacyLedger(fixture_path).get(_lines()[0]["capsule_id"])
    record.artifacts = [Artifact(name="payload", state="never_retained")]
    bad_id = Record("NOT-AN-ID", record.capsule, record.producer_envelope, record.artifacts)
    with pytest.raises(InvalidError):
        LegacyLedger(fixture_path).put(bad_id)
    tampered = Record(record.capsule_id, record.capsule.replace(b"acme", b"emca"), record.producer_envelope, record.artifacts)
    with pytest.raises(InvalidError):
        LegacyLedger(fixture_path).put(tampered)


def test_shared_handles_resolve_relative_paths_once(tmp_path, monkeypatch):
    from capsule_emit import legacy_ledger

    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    shutil.copy(FIXTURE, first / "ledger.jsonl")
    monkeypatch.chdir(first)
    store = legacy_ledger.shared("ledger.jsonl")
    monkeypatch.chdir(second)
    assert legacy_ledger.shared(first / "ledger.jsonl") is store
    assert len(store.scan_entries(0, 100)) == 11


def test_a_replaced_file_of_the_same_size_is_reread(tmp_path):
    raw = FIXTURE.read_bytes().split(b"\n")
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(raw[0] + b"\n")
    store = LegacyLedger(path)
    assert store.scan_entries(0, 10)[0].value.hex() == _lines()[0]["capsule_id"]
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(raw[0] + b"\n" + raw[1] + b"\n")
    os.replace(replacement, path)
    assert [e.value.hex() for e in store.scan_entries(0, 10)] == [_lines()[0]["capsule_id"], _lines()[1]["capsule_id"]]


def test_corrupt_lines_carry_the_artifact_code_on_artifact_operations(tmp_path):
    from capsule_emit.artifact import CorruptError as ArtifactCorruptError

    store = _with_line(tmp_path, '{"capsule_id":"zz"}')
    with pytest.raises(ArtifactCorruptError):
        store.get(_lines()[0]["capsule_id"])
    with pytest.raises(ArtifactCorruptError):
        store.append_line(_lines()[0])
    with pytest.raises(CorruptError):
        store.scan_entries(0, 1)


def test_shared_replaces_a_closed_handle(fixture_path):
    from capsule_emit import legacy_ledger

    store = legacy_ledger.shared(fixture_path)
    store.close()
    assert legacy_ledger.shared(fixture_path) is not store
    assert len(legacy_ledger.shared(fixture_path).scan_entries(0, 100)) == 11
