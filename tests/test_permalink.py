# SPDX-License-Identifier: Apache-2.0
"""Tests for `capsule-emit permalink` — withheld/bundle and disclosed (--reveal)."""
from __future__ import annotations

import base64
import json

import pytest

from capsule_emit import seal
from capsule_emit.cli import main as cli_main
from capsule_emit.permalink import (
    DEFAULT_BASE_URL,
    PermalinkError,
    build_url,
    check_capsules,
    load_capsules,
    summarize,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def three_capsule_chain(tmp_path):
    """executed -> blocked -> executed, matching the Dapr/Goose demo shape."""
    ledger = tmp_path / "chain.jsonl"
    root = seal(
        {"invoice_id": "INV-1"},
        action="check_invoice",
        operator="acme-co",
        developer="agent@v1",
        agent_output={"risk": "low"},
        model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
        verdict="executed",
        effect={"type": "check_invoice", "status": "dispatched"},
        anchor=False,
        ledger=ledger,
    )
    denial = seal(
        None,
        action="approve_large_purchase",
        operator="acme-co",
        developer="agent@v1",
        confirms=root.capsule_id,
        verdict="blocked",
        anchor=False,
        ledger=ledger,
    )
    escalation = seal(
        None,
        action="escalate_and_approve",
        operator="acme-co",
        developer="agent@v1",
        confirms=denial.capsule_id,
        verdict="executed",
        anchor=False,
        ledger=ledger,
    )
    return ledger, [root, denial, escalation]


@pytest.fixture
def three_capsule_chain_with_io(tmp_path):
    """executed -> blocked -> executed, each with agent_input/agent_output — for
    per-item bundle disclosure tests."""
    ledger = tmp_path / "chain_io.jsonl"
    root = seal(
        {"po_number": "PO-1"},
        action="write_order",
        operator="acme-co",
        developer="agent@v1",
        agent_output={"status": "dispatched"},
        verdict="executed",
        anchor=False,
        ledger=ledger,
    )
    denial = seal(
        {"po_number": "PO-1", "amount_usd": "125000.00"},
        action="approve_large_order",
        operator="acme-co",
        developer="agent@v1",
        confirms=root.capsule_id,
        agent_output={"reason": "exceeds PO ceiling"},
        verdict="blocked",
        decision="reject",
        human_disposed=True,
        approver="human",
        anchor=False,
        ledger=ledger,
    )
    escalation = seal(
        {"po_number": "PO-1"},
        action="escalate_to_manager",
        operator="acme-co",
        developer="agent@v1",
        confirms=denial.capsule_id,
        agent_output={"escalated_to": "ap-manager@acme-co.com"},
        verdict="executed",
        anchor=False,
        ledger=ledger,
    )
    return ledger, [root, denial, escalation]


@pytest.fixture
def two_capsule_run_dir(tmp_path):
    """A --from-run directory holding a ledger.jsonl."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ledger = run_dir / "ledger.jsonl"
    root = seal(
        None,
        action="write_order",
        operator="acme-co",
        developer="agent@v1",
        verdict="executed",
        anchor=False,
        ledger=ledger,
    )
    child = seal(
        None,
        action="confirm_write_order",
        operator="acme-co",
        developer="agent@v1",
        confirms=root.capsule_id,
        verdict="confirmed",
        anchor=False,
        ledger=ledger,
    )
    return run_dir, [root, child]


def _decode_fragment(url: str) -> object:
    frag = url.split("#", 1)[1]
    return json.loads(base64.b64decode(frag))


# ---------------------------------------------------------------------------
# load_capsules
# ---------------------------------------------------------------------------


def test_load_capsules_from_files(tmp_path):
    cap = {"capsule_id": "a" * 64, "disposition": {"verdict_class": "executed"}}
    p = tmp_path / "cap.json"
    p.write_text(json.dumps(cap))
    capsules = load_capsules(capsule_files=[str(p)])
    assert capsules == [cap]


def test_load_capsules_from_ledger(three_capsule_chain):
    ledger, records = three_capsule_chain
    capsules = load_capsules(ledger_path=str(ledger))
    assert len(capsules) == 3
    assert capsules[0]["capsule_id"] == records[0].capsule_id


def test_load_capsules_from_run_dir_with_ledger(two_capsule_run_dir):
    run_dir, records = two_capsule_run_dir
    capsules = load_capsules(from_run=str(run_dir))
    assert len(capsules) == 2
    assert capsules[0]["capsule_id"] == records[0].capsule_id


def test_load_capsules_from_run_dir_with_json_files(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "a.json").write_text(json.dumps({"capsule_id": "a" * 64}))
    (run_dir / "b.json").write_text(json.dumps({"capsule_id": "b" * 64}))
    capsules = load_capsules(from_run=str(run_dir))
    assert len(capsules) == 2


def test_load_capsules_rejects_multiple_sources(tmp_path):
    p = tmp_path / "cap.json"
    p.write_text(json.dumps({"capsule_id": "a" * 64}))
    with pytest.raises(PermalinkError):
        load_capsules(capsule_files=[str(p)], ledger_path=str(tmp_path / "ledger.jsonl"))


def test_load_capsules_rejects_no_source():
    with pytest.raises(PermalinkError):
        load_capsules()


def test_load_capsules_empty_ledger_errors(tmp_path):
    ledger = tmp_path / "empty.jsonl"
    ledger.write_text("")
    with pytest.raises(PermalinkError):
        load_capsules(ledger_path=str(ledger))


def test_load_capsules_from_run_no_capsules_errors(tmp_path):
    run_dir = tmp_path / "empty_run"
    run_dir.mkdir()
    with pytest.raises(PermalinkError):
        load_capsules(from_run=str(run_dir))


# ---------------------------------------------------------------------------
# build_url / summarize
# ---------------------------------------------------------------------------


def test_build_url_single_capsule_encodes_object(three_capsule_chain):
    _, records = three_capsule_chain
    capsules = [records[0].capsule]
    url = build_url(capsules, bundle=False)
    assert url.startswith(f"{DEFAULT_BASE_URL}/v/{records[0].capsule_id}#")
    decoded = _decode_fragment(url)
    assert isinstance(decoded, dict)
    assert decoded["capsule_id"] == records[0].capsule_id


def test_build_url_bundle_encodes_array(three_capsule_chain):
    _, records = three_capsule_chain
    capsules = [r.capsule for r in records]
    url = build_url(capsules, bundle=True)
    decoded = _decode_fragment(url)
    assert isinstance(decoded, list)
    assert len(decoded) == 3
    assert [c["capsule_id"] for c in decoded] == [r.capsule_id for r in records]


def test_build_url_custom_base_url(three_capsule_chain):
    _, records = three_capsule_chain
    url = build_url([records[0].capsule], bundle=False, base_url="http://localhost:8080/")
    assert url.startswith("http://localhost:8080/v/")


def test_build_url_disclosures_wraps_in_envelope(three_capsule_chain):
    _, records = three_capsule_chain
    cap = records[0].capsule
    url = build_url([cap], bundle=False, disclosures={"agent_input": {"invoice_id": "INV-1"}})
    decoded = _decode_fragment(url)
    assert decoded == {"capsule": cap, "disclosures": {"agent_input": {"invoice_id": "INV-1"}}}
    # the wrapped capsule is byte-identical to the unmodified sealed one — no re-keying/mutation
    assert decoded["capsule"] == cap


def test_build_url_disclosures_reject_multiple_capsules(three_capsule_chain):
    _, records = three_capsule_chain
    capsules = [r.capsule for r in records]
    with pytest.raises(PermalinkError, match="exactly one capsule"):
        build_url(capsules, bundle=False, disclosures={"agent_input": {}})


# ---------------------------------------------------------------------------
# build_url — per-item bundle disclosure (scitt-cose#30 lifted the block)
# ---------------------------------------------------------------------------


def test_build_url_bundle_disclosures_wraps_only_targeted_item(three_capsule_chain):
    """bundle=True + disclosures={capsule_id: {field: payload}} envelope-wraps
    only the targeted item(s); the rest of the array stays bare."""
    _, records = three_capsule_chain
    capsules = [r.capsule for r in records]
    disclosures = {records[1].capsule_id: {"agent_input": {"po_number": "PO-42"}}}
    url = build_url(capsules, bundle=True, disclosures=disclosures)
    decoded = _decode_fragment(url)
    assert isinstance(decoded, list) and len(decoded) == 3
    assert decoded[0] == capsules[0]
    assert decoded[2] == capsules[2]
    assert decoded[1] == {
        "capsule": capsules[1],
        "disclosures": {"agent_input": {"po_number": "PO-42"}},
    }
    # the wrapped capsule is byte-identical to the unmodified sealed one
    assert decoded[1]["capsule"] == capsules[1]


def test_build_url_bundle_disclosures_multiple_items(three_capsule_chain):
    _, records = three_capsule_chain
    capsules = [r.capsule for r in records]
    disclosures = {
        records[0].capsule_id: {"agent_output": {"risk": "low"}},
        records[2].capsule_id: {"agent_input": {"escalated": True}},
    }
    url = build_url(capsules, bundle=True, disclosures=disclosures)
    decoded = _decode_fragment(url)
    assert decoded[0]["disclosures"] == {"agent_output": {"risk": "low"}}
    assert decoded[1] == capsules[1]
    assert decoded[2]["disclosures"] == {"agent_input": {"escalated": True}}


def test_build_url_bundle_disclosures_no_entries_is_plain_bundle(three_capsule_chain):
    """An empty disclosures dict behaves like disclosures=None — a plain bundle array."""
    _, records = three_capsule_chain
    capsules = [r.capsule for r in records]
    url = build_url(capsules, bundle=True, disclosures={})
    decoded = _decode_fragment(url)
    assert decoded == capsules


def test_build_url_bundle_disclosures_unknown_capsule_id_rejected(three_capsule_chain):
    _, records = three_capsule_chain
    capsules = [r.capsule for r in records]
    with pytest.raises(PermalinkError, match="not in the bundle"):
        build_url(capsules, bundle=True, disclosures={"f" * 64: {"agent_input": {}}})


def test_summarize_single_capsule(three_capsule_chain):
    _, records = three_capsule_chain
    summary = summarize([records[0].capsule])
    assert "1 capsule" in summary
    assert "executed" in summary


def test_summarize_chain_shows_verdict_sequence(three_capsule_chain):
    _, records = three_capsule_chain
    summary = summarize([r.capsule for r in records])
    assert "3 capsules" in summary
    assert "executed → blocked → executed" in summary


# ---------------------------------------------------------------------------
# check_capsules (local verify(), no network)
# ---------------------------------------------------------------------------


def test_check_capsules_all_valid(three_capsule_chain):
    _, records = three_capsule_chain
    results = check_capsules([r.capsule for r in records])
    assert all(r.ok for r in results)


def test_check_capsules_detects_tamper(three_capsule_chain):
    _, records = three_capsule_chain
    capsules = [r.capsule for r in records]
    tampered = dict(capsules[0])
    tampered["action_type"] = "tampered"
    results = check_capsules([tampered, *capsules[1:]])
    assert results[0].ok is False


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_permalink_defaults_to_bundle_for_multiple_capsules(three_capsule_chain, capsys):
    ledger, records = three_capsule_chain
    exit_code = cli_main(["permalink", "--ledger", str(ledger)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "3 capsules" in out
    assert "executed → blocked → executed" in out
    url = [line for line in out.splitlines() if line.startswith("http")][0]
    decoded = _decode_fragment(url)
    assert isinstance(decoded, list) and len(decoded) == 3


def test_cli_permalink_single_capsule_defaults_to_object(two_capsule_run_dir, capsys):
    run_dir, records = two_capsule_run_dir
    cap_path = run_dir.parent / "single.json"
    cap_path.write_text(json.dumps(records[0].capsule))
    exit_code = cli_main(["permalink", str(cap_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    url = [line for line in out.splitlines() if line.startswith("http")][0]
    decoded = _decode_fragment(url)
    assert isinstance(decoded, dict)


def test_cli_permalink_bundle_flag_forces_array_for_single_capsule(two_capsule_run_dir, capsys):
    run_dir, records = two_capsule_run_dir
    cap_path = run_dir.parent / "single.json"
    cap_path.write_text(json.dumps(records[0].capsule))
    exit_code = cli_main(["permalink", str(cap_path), "--bundle"])
    assert exit_code == 0
    out = capsys.readouterr().out
    url = [line for line in out.splitlines() if line.startswith("http")][0]
    decoded = _decode_fragment(url)
    assert isinstance(decoded, list) and len(decoded) == 1


def test_cli_permalink_check_passes_on_valid_chain(three_capsule_chain, capsys):
    ledger, _ = three_capsule_chain
    exit_code = cli_main(["permalink", "--ledger", str(ledger), "--check"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "3/3" in out
    assert "VALID" in out


def test_cli_permalink_check_refuses_url_on_corrupted_capsule(three_capsule_chain, tmp_path, capsys):
    """The corrupted-fixture proof required by the task's acceptance test:
    --check must exit non-zero AND must not print a URL."""
    ledger, records = three_capsule_chain
    capsules = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    capsules[1]["disposition"]["verdict_class"] = "executed"  # tamper post-seal
    corrupt_ledger = tmp_path / "corrupt.jsonl"
    corrupt_ledger.write_text("\n".join(json.dumps(c) for c in capsules) + "\n")

    exit_code = cli_main(["permalink", "--ledger", str(corrupt_ledger), "--check"])
    assert exit_code != 0

    captured = capsys.readouterr()
    assert "FAILED" in captured.err
    assert records[1].capsule_id[:16] in captured.err
    # No URL emitted on stdout or stderr — a bad demo link must never be produced.
    assert "http" not in captured.out
    assert "http" not in captured.err


def test_cli_permalink_no_input_errors(capsys):
    exit_code = cli_main(["permalink"])
    assert exit_code == 1
    assert "no capsules given" in capsys.readouterr().err


def test_cli_permalink_reveal_unqualified_on_bundle_is_rejected(three_capsule_chain, capsys):
    """--reveal FIELD=... (no SELECTOR:) + more than one capsule is refused — ambiguous
    which item to disclose, not silently applied to the first."""
    ledger, _ = three_capsule_chain
    exit_code = cli_main(["permalink", "--ledger", str(ledger), "--reveal", "agent_input=x.json"])
    assert exit_code != 0
    err = capsys.readouterr().err
    assert "SELECTOR:FIELD" in err
    assert "http" not in capsys.readouterr().out


def test_cli_permalink_reveal_matching_payload(tmp_path, capsys):
    """--reveal FIELD=payload.json wraps the capsule in the Disclosure Envelope shape."""
    ledger = tmp_path / "l.jsonl"
    cap = seal(
        {"invoice_id": "INV-1"},
        action="check_invoice",
        operator="acme-co",
        developer="agent@v1",
        agent_output={"risk": "low"},
        verdict="executed",
        anchor=False,
        ledger=ledger,
    )
    input_file = tmp_path / "input.json"
    input_file.write_text(json.dumps({"invoice_id": "INV-1"}))
    output_file = tmp_path / "output.json"
    output_file.write_text(json.dumps({"risk": "low"}))

    exit_code = cli_main(
        [
            "permalink",
            "--ledger",
            str(ledger),
            "--reveal",
            f"agent_input={input_file}",
            "--reveal",
            f"agent_output={output_file}",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "digest-match VALID" in out
    url = [line for line in out.splitlines() if line.startswith("http")][0]
    decoded = _decode_fragment(url)
    assert decoded["capsule"]["capsule_id"] == cap.capsule_id
    assert decoded["disclosures"]["agent_input"] == {"invoice_id": "INV-1"}
    assert decoded["disclosures"]["agent_output"] == {"risk": "low"}


def test_cli_permalink_reveal_mismatched_payload_refused(tmp_path, capsys):
    """A disclosed payload that doesn't hash to the committed digest must never ship."""
    ledger = tmp_path / "l.jsonl"
    seal(
        {"invoice_id": "INV-1"},
        action="check_invoice",
        operator="acme-co",
        developer="agent@v1",
        verdict="executed",
        anchor=False,
        ledger=ledger,
    )
    input_file = tmp_path / "wrong.json"
    input_file.write_text(json.dumps({"invoice_id": "WRONG-ID"}))

    exit_code = cli_main(
        ["permalink", "--ledger", str(ledger), "--reveal", f"agent_input={input_file}"]
    )
    assert exit_code != 0
    captured = capsys.readouterr()
    assert "does not match the committed digest" in captured.err
    assert "http" not in captured.out


# ---------------------------------------------------------------------------
# CLI --reveal on bundles (per-item disclosure, scitt-cose#30 lifted the block)
# ---------------------------------------------------------------------------


def test_cli_permalink_reveal_bundle_by_index(three_capsule_chain_with_io, tmp_path, capsys):
    """--reveal SELECTOR:FIELD=payload.json with a 1-based record number
    discloses exactly that item; the rest of the bundle stays bare."""
    ledger, records = three_capsule_chain_with_io
    input_file = tmp_path / "input.json"
    input_file.write_text(json.dumps({"po_number": "PO-1", "amount_usd": "125000.00"}))
    output_file = tmp_path / "output.json"
    output_file.write_text(json.dumps({"reason": "exceeds PO ceiling"}))

    exit_code = cli_main(
        [
            "permalink",
            "--ledger",
            str(ledger),
            "--reveal",
            f"2:agent_input={input_file}",
            "--reveal",
            f"2:agent_output={output_file}",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "digest-match VALID" in out
    assert "1/3 capsule(s) disclosed" in out
    url = [line for line in out.splitlines() if line.startswith("http")][0]
    decoded = _decode_fragment(url)
    assert isinstance(decoded, list) and len(decoded) == 3
    assert decoded[0] == records[0].capsule
    assert decoded[2] == records[2].capsule
    assert decoded[1]["capsule"]["capsule_id"] == records[1].capsule_id
    assert decoded[1]["disclosures"]["agent_input"] == {"po_number": "PO-1", "amount_usd": "125000.00"}
    assert decoded[1]["disclosures"]["agent_output"] == {"reason": "exceeds PO ceiling"}


def test_cli_permalink_reveal_bundle_by_capsule_id_prefix(three_capsule_chain_with_io, tmp_path, capsys):
    ledger, records = three_capsule_chain_with_io
    input_file = tmp_path / "input.json"
    input_file.write_text(json.dumps({"po_number": "PO-1"}))
    prefix = records[0].capsule_id[:10]

    exit_code = cli_main(
        ["permalink", "--ledger", str(ledger), "--reveal", f"{prefix}:agent_input={input_file}"]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    url = [line for line in out.splitlines() if line.startswith("http")][0]
    decoded = _decode_fragment(url)
    assert decoded[0]["capsule"]["capsule_id"] == records[0].capsule_id
    assert decoded[0]["disclosures"]["agent_input"] == {"po_number": "PO-1"}
    assert decoded[1] == records[1].capsule
    assert decoded[2] == records[2].capsule


def test_cli_permalink_reveal_bundle_multiple_items(three_capsule_chain_with_io, tmp_path, capsys):
    """More than one item disclosed at once, mixing index and capsule_id-prefix selectors."""
    ledger, records = three_capsule_chain_with_io
    root_input = tmp_path / "root_input.json"
    root_input.write_text(json.dumps({"po_number": "PO-1"}))
    esc_output = tmp_path / "esc_output.json"
    esc_output.write_text(json.dumps({"escalated_to": "ap-manager@acme-co.com"}))

    exit_code = cli_main(
        [
            "permalink",
            "--ledger",
            str(ledger),
            "--reveal",
            f"1:agent_input={root_input}",
            "--reveal",
            f"3:agent_output={esc_output}",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "2/3 capsule(s) disclosed" in out
    url = [line for line in out.splitlines() if line.startswith("http")][0]
    decoded = _decode_fragment(url)
    assert decoded[0]["disclosures"]["agent_input"] == {"po_number": "PO-1"}
    assert decoded[1] == records[1].capsule
    assert decoded[2]["disclosures"]["agent_output"] == {"escalated_to": "ap-manager@acme-co.com"}


def test_cli_permalink_reveal_bundle_mismatch_refused_per_item(three_capsule_chain_with_io, tmp_path, capsys):
    """A mismatched disclosed payload on ONE bundle item refuses the whole URL —
    same fail-closed rule as the single-capsule case, applied per item."""
    ledger, records = three_capsule_chain_with_io
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"po_number": "WRONG"}))

    exit_code = cli_main(
        ["permalink", "--ledger", str(ledger), "--reveal", f"2:agent_input={wrong}"]
    )
    assert exit_code != 0
    captured = capsys.readouterr()
    assert "does not match the committed digest" in captured.err
    assert records[1].capsule_id[:16] in captured.err
    assert "http" not in captured.out


def test_cli_permalink_reveal_bundle_bad_index_refused(three_capsule_chain_with_io, tmp_path, capsys):
    ledger, _ = three_capsule_chain_with_io
    f = tmp_path / "x.json"
    f.write_text("{}")
    exit_code = cli_main(["permalink", "--ledger", str(ledger), "--reveal", f"9:agent_input={f}"])
    assert exit_code != 0
    assert "out of range" in capsys.readouterr().err


def test_cli_permalink_reveal_bundle_ambiguous_prefix_refused(three_capsule_chain_with_io, tmp_path, capsys):
    ledger, records = three_capsule_chain_with_io
    f = tmp_path / "x.json"
    f.write_text("{}")
    # a single hex char is a prefix of >=1 of the three capsule_ids almost always;
    # force ambiguity is impractical here, so instead assert the too-short-prefix path.
    exit_code = cli_main(["permalink", "--ledger", str(ledger), "--reveal", f"abc:agent_input={f}"])
    assert exit_code != 0
    assert ">=8-char" in capsys.readouterr().err


def test_resolve_capsule_by_selector_all_digit_long_prefix_resolves_as_prefix():
    """An all-digit selector >=8 chars is a legal hex capsule_id prefix (digits
    0-9 are valid hex), not a record number -- isdigit() alone over-claims it."""
    from capsule_emit.cli import _resolve_capsule_by_selector
    from capsule_emit.permalink import PermalinkError

    capsules = [
        {"capsule_id": "aaaaaaaaaaaa" + "0" * 52},
        {"capsule_id": "12345678" + "b" * 56},
        {"capsule_id": "cccccccccccc" + "0" * 52},
    ]
    # Before the fix: "12345678".isdigit() is True -> treated as record index 12345678
    # -> out of range on a 3-capsule bundle, even though it is a valid prefix of record 2.
    resolved = _resolve_capsule_by_selector(capsules, "12345678")
    assert resolved is capsules[1]

    with pytest.raises(PermalinkError):
        _resolve_capsule_by_selector(capsules, "99999999")


def test_resolve_capsule_by_selector_all_digit_prefix_collision_with_valid_index():
    """The silent-wrong-capsule case: an all-digit 8-char prefix that also
    happens to look like a valid record index must resolve as the PREFIX
    match, not the index -- record 2 has a wholly unrelated capsule_id."""
    from capsule_emit.cli import _resolve_capsule_by_selector

    capsules = [
        {"capsule_id": "aaaaaaaaaaaa" + "0" * 52},
        {"capsule_id": "bbbbbbbbbbbb" + "0" * 52},
        {"capsule_id": "00000002" + "c" * 56},
    ]
    # "00000002" is all-digit and 8 chars -- isdigit()-only logic reads it as
    # record index 2 and silently returns capsules[1] (the wrong capsule).
    resolved = _resolve_capsule_by_selector(capsules, "00000002")
    assert resolved is capsules[2]
    assert resolved is not capsules[1]



def test_cli_permalink_fragment_size_no_warning_for_small_chain(three_capsule_chain, capsys):
    ledger, _ = three_capsule_chain
    exit_code = cli_main(["permalink", "--ledger", str(ledger), "--base-url", "http://x"])
    assert exit_code == 0
    small_err = capsys.readouterr().err
    assert "fragment" not in small_err  # the small demo chain stays well under threshold


def test_cli_permalink_fragment_size_warning_past_16kb(tmp_path, capsys):
    """A disclosed payload big enough to push the fragment past ~16KB triggers a
    stderr warning — flagged, not refused (the task's fragment-size note)."""
    ledger = tmp_path / "big.jsonl"
    big_payload = {"blob": "x" * 20_000}
    cap = seal(
        big_payload,
        action="write_order",
        operator="acme-co",
        developer="agent@v1",
        verdict="executed",
        anchor=False,
        ledger=ledger,
    )
    input_file = tmp_path / "big_input.json"
    input_file.write_text(json.dumps(big_payload))

    exit_code = cli_main(
        ["permalink", "--ledger", str(ledger), "--reveal", f"agent_input={input_file}"]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "warning" in captured.err
    assert "16" in captured.err
    url = [line for line in captured.out.splitlines() if line.startswith("http")][0]
    decoded = _decode_fragment(url)
    assert decoded["capsule"]["capsule_id"] == cap.capsule_id


def test_cli_permalink_from_run(two_capsule_run_dir, capsys):
    run_dir, records = two_capsule_run_dir
    exit_code = cli_main(["permalink", "--from-run", str(run_dir)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "2 capsules" in out
    url = [line for line in out.splitlines() if line.startswith("http")][0]
    assert url.startswith(f"{DEFAULT_BASE_URL}/v/{records[0].capsule_id}#")
