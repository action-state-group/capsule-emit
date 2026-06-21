#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""[final-flip-gate] Clean-room PyPI install acceptance gate.

Run this from OUTSIDE any existing capsule-emit/agent-action-capsule install.
The script creates a temporary venv, installs only from PyPI, then exercises
every documented path verbatim. Exit 0 = all green; exit 1 = something failed.

Usage (clean room):
    python3 tests/test_flip_gate.py

It does NOT require pytest — it's a standalone self-contained script so it can
run from a CI matrix that starts with only Python.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path


# ── helpers ───────────────────────────────────────────────────────────────────

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
_failures: list[str] = []


def ok(msg: str) -> None:
    print(f"  {PASS} {msg}")


def fail(msg: str, detail: str = "") -> None:
    tag = f"  {FAIL} FAIL: {msg}"
    if detail:
        tag += f"\n       {detail}"
    print(tag)
    _failures.append(msg)


def section(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


def run(venv_python: str, code: str, *, cwd: str | None = None,
        env: dict | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    env_ = {**os.environ, **(env or {})}
    return subprocess.run(
        [venv_python, "-c", code],
        capture_output=True, text=True, timeout=timeout,
        cwd=cwd, env=env_,
    )


def run_cmd(cmd: list[str], *, cwd: str | None = None,
            env: dict | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    env_ = {**os.environ, **(env or {})}
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        cwd=cwd, env=env_,
    )


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 60)
    print("  [final-flip-gate] capsule-emit clean-room PyPI gate")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        venv_dir = Path(tmpdir) / "gate_venv"
        workdir = Path(tmpdir) / "work"
        workdir.mkdir()

        # ── 0. Create venv and install from PyPI ──────────────────────────────
        section("0. Fresh venv — install from PyPI only")

        r = run_cmd([sys.executable, "-m", "venv", str(venv_dir)])
        if r.returncode != 0:
            print(f"venv creation failed: {r.stderr}")
            return 1

        venv_python = str(venv_dir / "bin" / "python")
        venv_pip = str(venv_dir / "bin" / "pip")

        # install capsule-emit (which depends on agent-action-capsule>=0.0.3)
        r = run_cmd([venv_pip, "install", "--quiet", "capsule-emit", "langchain-core"],
                    timeout=120)
        if r.returncode != 0:
            print(f"pip install failed:\n{r.stderr}")
            return 1

        # verify versions
        r = run_cmd([venv_pip, "show", "capsule-emit", "agent-action-capsule"])
        ce_ver = aac_ver = "unknown"
        for line in r.stdout.splitlines():
            if line.startswith("Name: capsule-emit"):
                pass
            if line.startswith("Version:") and ce_ver == "unknown" and "capsule-emit" in r.stdout[:r.stdout.find(line)].lower():
                ce_ver = line.split(":", 1)[1].strip()
            if line.startswith("Version:") and aac_ver == "unknown" and "agent-action-capsule" in r.stdout[:r.stdout.find(line)].lower():
                aac_ver = line.split(":", 1)[1].strip()

        # simpler version parse
        versions: dict[str, str] = {}
        current_pkg = ""
        for line in r.stdout.splitlines():
            if line.startswith("Name:"):
                current_pkg = line.split(":", 1)[1].strip().lower()
            elif line.startswith("Version:"):
                versions[current_pkg] = line.split(":", 1)[1].strip()

        ce_ver = versions.get("capsule-emit", "?")
        aac_ver = versions.get("agent-action-capsule", "?")
        print(f"  capsule-emit          : {ce_ver}")
        print(f"  agent-action-capsule  : {aac_ver}")

        if ce_ver != "0.1.1":
            fail(f"capsule-emit version should be 0.1.1, got {ce_ver}")
        else:
            ok(f"capsule-emit {ce_ver}")

        # parse aac version — >=0.0.3 required
        try:
            aac_parts = tuple(int(x) for x in aac_ver.split(".")[:3])
            if aac_parts < (0, 0, 3):
                fail(f"agent-action-capsule should be >=0.0.3, got {aac_ver}")
            else:
                ok(f"agent-action-capsule {aac_ver} (>=0.0.3)")
        except ValueError:
            fail(f"Could not parse aac version: {aac_ver}")

        ledger = str(workdir / "ledger.jsonl")

        # ── 1. README hero snippet ─────────────────────────────────────────────
        section("1. README hero emit() snippet")
        r = run(venv_python, textwrap.dedent(f"""
            from capsule_emit import emit
            result = {{"po_id": "PO-7781"}}
            cap = emit(
                action="write_order",
                operator="acme-co",
                developer="po-agent@v1",
                agent_input={{"vendor": "Frobozz Supply", "total": 1240.19}},
                agent_output=result,
                model={{"provider": "anthropic", "model_id": "claude-sonnet-4-6"}},
                verdict="executed",
                effect={{"type": "write_order", "status": "dispatched"}},
                anchor=False,
                ledger={ledger!r},
            )
            assert len(cap.capsule_id) == 64, f"bad capsule_id len: {{len(cap.capsule_id)}}"
            c = cap.capsule
            for field in ("capsule_id","action_id","action_type","operator","developer","timestamp","disposition"):
                assert field in c, f"missing field: {{field}}"
            assert c["operator"] == "acme-co"
            assert c["disposition"]["verdict_class"] == "executed"
            assert "effect" in c and c["effect"]["type"] == "write_order"
            assert "model_attestation" in c
            ca = c["model_attestation"].get("compute_attestation", {{}})
            assert "agent_input_digest" in ca, "I/O digests missing from capsule"
            assert "agent_output_digest" in ca, "I/O digests missing from capsule"
            print("OK capsule_id=" + cap.capsule_id[:12])
        """))
        if r.returncode == 0 and "OK" in r.stdout:
            ok("README hero snippet sealed; shape + I/O digests present")
        else:
            fail("README hero snippet", r.stderr or r.stdout)

        # ── 2. quickstart_demo.py ─────────────────────────────────────────────
        section("2. examples/quickstart_demo.py")
        demo_path = Path(__file__).parent.parent / "examples" / "quickstart_demo.py"
        if demo_path.exists():
            r = run_cmd([venv_python, str(demo_path)], timeout=60)
            if r.returncode == 0 and "All acceptance checks passed" in r.stdout:
                ok("quickstart_demo.py exit 0")
            else:
                fail("quickstart_demo.py", (r.stderr or r.stdout)[-500:])
        else:
            fail("examples/quickstart_demo.py not found")

        # ── 3. Tutorial 01 — emit ≥3, ledger view, verify --store, tamper ────
        section("3. Tutorial 01 — first capsule flow")
        ledger3 = str(workdir / "tut01.jsonl")
        r = run(venv_python, textwrap.dedent(f"""
            from capsule_emit import emit, ledger_view
            import json, pathlib
            ledger = pathlib.Path({ledger3!r})

            caps = []
            for i in range(3):
                c = emit(
                    action="write_order", operator="acme-co", developer="po-agent@v1",
                    agent_input={{"i": i}}, agent_output={{"ok": True}},
                    effect={{"type": "write_order", "status": "dispatched"}},
                    anchor=False, ledger=ledger,
                )
                caps.append(c)

            # ledger view
            ledger_view(ledger)

            # verify --store via library
            from agent_action_capsule import verify_store
            lines = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
            assert len(lines) == 3, f"expected 3 lines, got {{len(lines)}}"
            results = verify_store(lines)
            assert isinstance(results, list), f"expected list from verify_store, got {{type(results)}}"
            errors = [f.detail for r in results for f in r.findings if f.severity == "error"]
            assert all(r.ok for r in results), f"verify_store failed: {{errors}}"

            # check no unknown_registry_value at error/warning for write_order
            bad = [f for r in results for f in r.findings
                   if f.code == "unknown_registry_value" and f.severity in ("error","warning")]
            assert not bad, f"unexpected registry findings: {{bad}}"

            # tamper — must fail
            lines[0]["operator"] = "evil-corp"
            tamper = verify_store(lines)
            assert not all(r.ok for r in tamper), "tampered store should not all verify"
            print("OK lines=" + str(len(lines)))
        """))
        if r.returncode == 0 and "OK" in r.stdout:
            ok("Tutorial 01: emit×3, ledger view, verify_store green, no registry warnings, tamper caught")
        else:
            fail("Tutorial 01", r.stderr or r.stdout)

        # ── 4. Tutorial 02 — chaining ────────────────────────────────────────
        section("4. Tutorial 02 — confirming & chaining")
        ledger2 = str(workdir / "tut02.jsonl")
        r = run(venv_python, textwrap.dedent(f"""
            from capsule_emit import emit
            from agent_action_capsule import verify_store
            import json, pathlib
            ledger = pathlib.Path({ledger2!r})

            attempt = emit(
                action="write_order", operator="acme-co", developer="po-agent@v1",
                effect={{"type": "write_order", "status": "dispatched"}},
                anchor=False, ledger=ledger,
            )
            done = emit(
                action="write_order", operator="acme-co", developer="po-agent@v1",
                verdict="confirmed",
                effect={{"type": "write_order", "status": "confirmed"}},
                agent_output={{"confirmed": True}},
                confirms=attempt.capsule_id,
                anchor=False, ledger=ledger,
            )

            chain = done.capsule.get("chain", {{}})
            assert chain.get("parent_capsule_id") == attempt.capsule_id, f"wrong parent: {{chain}}"
            assert chain.get("relation") == "confirms", f"wrong relation: {{chain}}"

            # response_digest lives on effect, not compute_attestation
            eff = done.capsule.get("effect", {{}})
            assert "response_digest" in eff, \
                f"response_digest missing from effect: {{eff}}"

            assurance = done.capsule.get("assurance", {{}})
            assert assurance.get("effect_mode") == "confirmed", \
                f"expected effect_mode=confirmed: {{assurance}}"
            assert assurance.get("ledger_mode") == "chained", \
                f"expected ledger_mode=chained: {{assurance}}"

            lines = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
            results = verify_store(lines)
            errors = [f.detail for r in results for f in r.findings if f.severity == "error"]
            assert all(r.ok for r in results), f"chain verify failed: {{errors}}"
            print("OK parent=" + attempt.capsule_id[:8])
        """))
        if r.returncode == 0 and "OK" in r.stdout:
            ok("Tutorial 02: confirms chain, response_digest, ledger_mode, verify_store green")
        else:
            fail("Tutorial 02", r.stderr or r.stdout)

        # ── 5. Tutorial 03 — ledger view + --json ────────────────────────────
        section("5. Tutorial 03 — reading your ledger")
        ledger_t3 = str(workdir / "tut03.jsonl")
        r = run(venv_python, textwrap.dedent(f"""
            from capsule_emit import emit, ledger_view
            from capsule_emit.ledger import read_ledger
            import json, pathlib, io, sys
            ledger = pathlib.Path({ledger_t3!r})

            a = emit(action="write_order", operator="acme-co", developer="po-agent@v1",
                     effect={{"type": "write_order", "status": "dispatched"}},
                     anchor=False, ledger=ledger)
            emit(action="write_order", operator="acme-co", developer="po-agent@v1",
                 verdict="confirmed",
                 effect={{"type": "write_order", "status": "confirmed"}},
                 agent_output={{"ok": True}},
                 confirms=a.capsule_id,
                 anchor=False, ledger=ledger)

            # table view — must not crash and must include capsule ids
            buf = io.StringIO()
            old, sys.stdout = sys.stdout, buf
            ledger_view(ledger)
            sys.stdout = old
            table = buf.getvalue()
            assert "write_order" in table, f"table missing action: {{table!r}}"
            assert a.capsule_id[:8] in table, "first capsule_id missing from table"

            # --json equivalent: read_ledger
            records = list(read_ledger(ledger))
            assert len(records) == 2, f"expected 2 records, got {{len(records)}}"
            assert records[0]["capsule_id"] == a.capsule_id
            chain_rec = records[1]
            assert chain_rec.get("chain", {{}}).get("relation") == "confirms"
            print("OK table_len=" + str(len(table)))
        """))
        if r.returncode == 0 and "OK" in r.stdout:
            ok("Tutorial 03: ledger view table + json (read_ledger) both show chain")
        else:
            fail("Tutorial 03", r.stderr or r.stdout)

        # ── 6. Tutorial 04 — manifest ─────────────────────────────────────────
        section("6. Tutorial 04 — declaring constraints")
        manifest_src = Path(__file__).parent.parent / "flows" / "write-po" / "manifest.md"
        if manifest_src.exists():
            r = run(venv_python, textwrap.dedent(f"""
                from capsule_emit.manifest import load_manifest
                import pathlib
                m = load_manifest(pathlib.Path({str(manifest_src)!r}))
                assert m.wicket_id == "write-po", f"wicket_id={{m.wicket_id}}"
                assert m.autonomy is not None, "autonomy is None"
                assert m.effect_type is not None, "effect_type is None"
                assert isinstance(m.constraint_names, list), f"constraint_names not list: {{m.constraint_names}}"
                assert len(m.constraint_names) >= 1, "no constraints"
                print(f"OK wicket={{m.wicket_id}} autonomy={{m.autonomy}} effect={{m.effect_type}} constraints={{m.constraint_names}}")
            """))
            if r.returncode == 0 and "OK" in r.stdout:
                ok(f"Tutorial 04: manifest parsed — {r.stdout.strip()}")
            else:
                fail("Tutorial 04: manifest", r.stderr or r.stdout)
        else:
            fail("Tutorial 04: flows/write-po/manifest.md not found")

        # ── 7. Adapter pages — emit capsule with I/O digests ─────────────────
        section("7. Adapter pages — I/O digests present on all adapters")

        # MCP adapter
        r = run(venv_python, textwrap.dedent(f"""
            from capsule_emit.adapters.mcp import MCPCapsuleEmitter
            import tempfile, pathlib
            ledger = pathlib.Path(tempfile.mktemp(suffix=".jsonl"))
            emitter = MCPCapsuleEmitter(operator="acme-co", developer="po-agent@v1", ledger=ledger, anchor=False)

            @emitter.tool("write_order")
            def write_order(vendor: str, total: float) -> dict:
                return {{"po_id": "PO-001"}}

            result = write_order(vendor="Frobozz", total=1240.19)
            from capsule_emit.ledger import read_ledger
            caps = list(read_ledger(ledger))
            assert len(caps) == 1, f"expected 1 capsule, got {{len(caps)}}"
            ca = caps[0].get("model_attestation", {{}}).get("compute_attestation", {{}})
            assert "agent_input_digest" in ca, f"MCP: no input digest. ca={{ca}}"
            assert "agent_output_digest" in ca, f"MCP: no output digest. ca={{ca}}"
            print("OK mcp input=" + ca["agent_input_digest"][:8])
        """))
        if r.returncode == 0 and "OK" in r.stdout:
            ok("MCP adapter: @emitter.tool → capsule with I/O digests")
        else:
            fail("MCP adapter", r.stderr or r.stdout)

        # Hermes adapter
        r = run(venv_python, textwrap.dedent(f"""
            from capsule_emit.adapters.hermes import HermesCapsuleEmitter
            import tempfile, pathlib
            ledger = pathlib.Path(tempfile.mktemp(suffix=".jsonl"))
            emitter = HermesCapsuleEmitter(operator="acme-co", developer="hermes-agent@v1", ledger=ledger, anchor=False)

            result = {{"confirmed": True}}
            emitter.after_tool("write_order", {{"vendor": "X"}}, result)

            from capsule_emit.ledger import read_ledger
            caps = list(read_ledger(ledger))
            assert len(caps) == 1
            ca = caps[0].get("model_attestation", {{}}).get("compute_attestation", {{}})
            assert "agent_input_digest" in ca, f"Hermes: no input digest. ca={{ca}}"
            assert "agent_output_digest" in ca, f"Hermes: no output digest. ca={{ca}}"
            print("OK hermes input=" + ca["agent_input_digest"][:8])
        """))
        if r.returncode == 0 and "OK" in r.stdout:
            ok("Hermes adapter: after_tool → capsule with I/O digests")
        else:
            fail("Hermes adapter", r.stderr or r.stdout)

        # CrewAI adapter (no crewai dep — plain callable path)
        r = run(venv_python, textwrap.dedent(f"""
            from capsule_emit.adapters.crewai import CrewAICapsuleEmitter
            import tempfile, pathlib
            ledger = pathlib.Path(tempfile.mktemp(suffix=".jsonl"))
            emitter = CrewAICapsuleEmitter(operator="acme-co", developer="ops-agent@v1", ledger=ledger, anchor=False)

            def send_payment(amount: float) -> dict:
                return {{"tx_id": "TX-001"}}

            wrapped = emitter.wrap(send_payment)
            result = wrapped(amount=40.00)

            from capsule_emit.ledger import read_ledger
            caps = list(read_ledger(ledger))
            assert len(caps) == 1
            ca = caps[0].get("model_attestation", {{}}).get("compute_attestation", {{}})
            assert "agent_input_digest" in ca, f"CrewAI: no input digest. ca={{ca}}"
            assert "agent_output_digest" in ca, f"CrewAI: no output digest. ca={{ca}}"
            print("OK crewai input=" + ca["agent_input_digest"][:8])
        """))
        if r.returncode == 0 and "OK" in r.stdout:
            ok("CrewAI adapter: wrap(callable) → capsule with I/O digests")
        else:
            fail("CrewAI adapter", r.stderr or r.stdout)

        # LangChain adapter (needs langchain-core, already installed)
        r = run(venv_python, textwrap.dedent(f"""
            from capsule_emit.adapters.langchain import LangChainCapsuleEmitter
            import tempfile, pathlib, uuid
            ledger = pathlib.Path(tempfile.mktemp(suffix=".jsonl"))
            emitter = LangChainCapsuleEmitter(operator="acme-co", developer="research-agent@v1", ledger=ledger, anchor=False)

            run_id = uuid.uuid4()
            emitter.on_tool_start({{"name": "write_order"}}, "Frobozz Supply", run_id=run_id)
            emitter.on_tool_end("PO-001", run_id=run_id)

            from capsule_emit.ledger import read_ledger
            caps = list(read_ledger(ledger))
            assert len(caps) == 1
            ca = caps[0].get("model_attestation", {{}}).get("compute_attestation", {{}})
            assert "agent_input_digest" in ca, f"LangChain: no input digest. ca={{ca}}"
            assert "agent_output_digest" in ca, f"LangChain: no output digest. ca={{ca}}"
            print("OK langchain input=" + ca["agent_input_digest"][:8])
        """))
        if r.returncode == 0 and "OK" in r.stdout:
            ok("LangChain adapter: on_tool_start/end → capsule with I/O digests")
        else:
            fail("LangChain adapter", r.stderr or r.stdout)

        # ── 8. anchor=False, AAC_ANCHOR_URL override ──────────────────────────
        section("8. going-deeper / why-anchoring — offline + anchor_url override")
        r = run(venv_python, textwrap.dedent(f"""
            from capsule_emit import emit
            import tempfile, pathlib
            ledger = pathlib.Path(tempfile.mktemp(suffix=".jsonl"))

            # anchor=False — must not network, anchored=False
            cap = emit(action="test", operator="o", developer="d@v1",
                       anchor=False, ledger=ledger)
            assert not cap.anchored, f"expected anchored=False, got {{cap.anchored}}"

            # AAC_ANCHOR_URL override (bad URL → emit still completes, anchored=False or True with error)
            import os
            cap2 = emit(action="test2", operator="o", developer="d@v1",
                        anchor_url="http://127.0.0.1:0/v1/digest",
                        anchor=True, ledger=ledger)
            # anchored may be True (fire-and-forget) or False — either is fine; must not raise
            print(f"OK anchor_url_override anchored={{cap2.anchored}}")
        """))
        if r.returncode == 0 and "OK" in r.stdout:
            ok("anchor=False works; anchor_url= override does not crash emit()")
        else:
            fail("anchor=False / anchor_url override", r.stderr or r.stdout)

        # ── 9. CLI — verify --store + ledger view + nonzero on bad input ──────
        section("9. CLI — verify --store, ledger view, nonzero exit on error")

        # build a 2-capsule ledger to test CLI --store
        cli_ledger = str(workdir / "cli.jsonl")
        setup = run(venv_python, textwrap.dedent(f"""
            from capsule_emit import emit
            import pathlib
            ledger = pathlib.Path({cli_ledger!r})
            a = emit(action="write_order", operator="o", developer="d@v1",
                     anchor=False, ledger=ledger,
                     effect={{"type": "write_order", "status": "dispatched"}})
            emit(action="write_order", operator="o", developer="d@v1",
                 verdict="confirmed",
                 effect={{"type": "write_order", "status": "confirmed"}},
                 agent_output={{"ok": True}},
                 confirms=a.capsule_id,
                 anchor=False, ledger=ledger)
            print("OK")
        """))
        if setup.returncode != 0:
            fail("CLI setup", setup.stderr)
        else:
            aac_bin = str(venv_dir / "bin" / "agent-action-capsule")
            r = run_cmd([aac_bin, "verify", "--store", cli_ledger])
            if r.returncode == 0:
                out = r.stdout + r.stderr
                # "INVALID" in output combined with non-info finding = real failure
                if "capsule_id_mismatch" in out or ("INVALID" in out and "error:" in out.lower()):
                    fail("CLI verify --store: unexpected error finding", out[-300:])
                else:
                    ok("CLI `agent-action-capsule verify --store` exit 0")
            else:
                fail("CLI verify --store", r.stdout + r.stderr)

            # CLI ledger view
            ce_bin = str(venv_dir / "bin" / "capsule-emit")
            r = run_cmd([ce_bin, "ledger", "view", cli_ledger])
            if r.returncode == 0 and ("write_order" in r.stdout or "confirmed" in r.stdout):
                ok("CLI `capsule-emit ledger view` renders table")
            else:
                fail("CLI ledger view", r.stdout + r.stderr)

            # nonzero on bad input
            bad_file = str(workdir / "bad.jsonl")
            Path(bad_file).write_text("not json at all\n")
            r = run_cmd([aac_bin, "verify", "--store", bad_file])
            if r.returncode != 0:
                ok("CLI nonzero exit on malformed input")
            else:
                fail("CLI should exit nonzero on bad JSONL", r.stdout)

        # ── 10. No unknown_registry_value warnings on canonical write_order path
        section("10. No unknown_registry_value on write_order + confirms")
        r = run(venv_python, textwrap.dedent(f"""
            from capsule_emit import emit
            from agent_action_capsule import verify
            import tempfile, pathlib
            ledger = pathlib.Path(tempfile.mktemp(suffix=".jsonl"))

            a = emit(action="write_order", operator="acme-co", developer="po-agent@v1",
                     effect={{"type": "write_order", "status": "dispatched"}},
                     anchor=False, ledger=ledger)
            b = emit(action="write_order", operator="acme-co", developer="po-agent@v1",
                     verdict="confirmed",
                     effect={{"type": "write_order", "status": "confirmed"}},
                     agent_output={{"ok": True}},
                     confirms=a.capsule_id,
                     anchor=False, ledger=ledger)

            for cap in (a, b):
                res = verify(cap.capsule)
                bad = [f for f in res.findings
                       if f.code == "unknown_registry_value"
                       and f.severity in ("error", "warning")]
                assert not bad, f"unexpected registry findings on {{cap.capsule.get('action_id')}}: {{bad}}"

            print("OK no unknown_registry_value at error/warning severity")
        """))
        if r.returncode == 0 and "OK" in r.stdout:
            ok("No unknown_registry_value at error/warning severity on canonical write_order + confirms path")
        else:
            fail("unknown_registry_value check", r.stderr or r.stdout)

    # ── result ────────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    if _failures:
        print(f"  {FAIL} GATE FAILED — {len(_failures)} check(s) failed:")
        for f in _failures:
            print(f"     • {f}")
        print(f"{'='*60}")
        return 1
    else:
        print(f"  {PASS} ALL CHECKS PASSED — gate is green")
        print(f"  capsule-emit is ready to flip public.")
        print(f"{'='*60}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
