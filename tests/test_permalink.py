# SPDX-License-Identifier: Apache-2.0
"""Tests for `capsule-emit permalink` — withheld/bundle and disclosed (--reveal)."""
from __future__ import annotations

import base64
import json

import pytest

from capsule_emit import emit
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
    root = emit(
        action="check_invoice",
        operator="acme-co",
        developer="agent@v1",
        agent_input={"invoice_id": "INV-1"},
        agent_output={"risk": "low"},
        model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
        verdict="executed",
        effect={"type": "check_invoice", "status": "dispatched"},
        anchor=False,
        ledger=ledger,
    )
    denial = emit(
        action="approve_large_purchase",
        operator="acme-co",
        developer="agent@v1",
        confirms=root.capsule_id,
        verdict="blocked",
        anchor=False,
        ledger=ledger,
    )
    escalation = emit(
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
def two_capsule_run_dir(tmp_path):
    """A --from-run directory holding a ledger.jsonl."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ledger = run_dir / "ledger.jsonl"
    root = emit(
        action="write_order",
        operator="acme-co",
        developer="agent@v1",
        verdict="executed",
        anchor=False,
        ledger=ledger,
    )
    child = emit(
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


def test_build_url_disclosures_reject_bundle(three_capsule_chain):
    _, records = three_capsule_chain
    with pytest.raises(PermalinkError, match="bundle=False"):
        build_url([records[0].capsule], bundle=True, disclosures={"agent_input": {}})


def test_build_url_disclosures_reject_multiple_capsules(three_capsule_chain):
    _, records = three_capsule_chain
    capsules = [r.capsule for r in records]
    with pytest.raises(PermalinkError, match="exactly one capsule"):
        build_url(capsules, bundle=False, disclosures={"agent_input": {}})


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


def test_cli_permalink_reveal_on_bundle_is_rejected(three_capsule_chain, capsys):
    """--reveal + more than one capsule (implicit bundle) is refused, not silently degraded."""
    ledger, _ = three_capsule_chain
    exit_code = cli_main(["permalink", "--ledger", str(ledger), "--reveal", "agent_input=x.json"])
    assert exit_code != 0
    err = capsys.readouterr().err
    assert "exactly one capsule" in err
    assert "http" not in capsys.readouterr().out


def test_cli_permalink_reveal_matching_payload(tmp_path, capsys):
    """--reveal FIELD=payload.json wraps the capsule in the Disclosure Envelope shape."""
    ledger = tmp_path / "l.jsonl"
    cap = emit(
        action="check_invoice",
        operator="acme-co",
        developer="agent@v1",
        agent_input={"invoice_id": "INV-1"},
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
    emit(
        action="check_invoice",
        operator="acme-co",
        developer="agent@v1",
        agent_input={"invoice_id": "INV-1"},
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


def test_cli_permalink_from_run(two_capsule_run_dir, capsys):
    run_dir, records = two_capsule_run_dir
    exit_code = cli_main(["permalink", "--from-run", str(run_dir)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "2 capsules" in out
    url = [line for line in out.splitlines() if line.startswith("http")][0]
    assert url.startswith(f"{DEFAULT_BASE_URL}/v/{records[0].capsule_id}#")
