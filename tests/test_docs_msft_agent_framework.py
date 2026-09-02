# SPDX-License-Identifier: Apache-2.0
"""Docs snippet gate for the Microsoft Agent Framework guide.

The guide at ``docs/adapters/msft-agent-framework.md`` is the artifact an upstream
snippet-plus-link block points at. A page that a stranger lands on and cannot run is
worse than no page, so this gate holds every Python block in it to one of two states:

1. **executed** — the block is a complete program; it is run in a subprocess with a
   temp working directory and must exit 0.
2. **excerpt** — the block is a registration fragment that cannot stand alone (it needs
   a caller-supplied chat client, or it is a single keyword argument). An excerpt is not
   executed, so it is checked structurally instead: it must parse, every name it imports
   must actually exist in the module it imports from, and every keyword it passes to a
   ``capsule_emit`` callable must be one that callable really accepts.

There is no third state. A block that is neither runnable nor a registered excerpt fails
the gate — absent is never pass. The gate also asserts the guide contains at least one
executed block, so deleting them all cannot turn the gate green, and that the excerpt
registry has no stale entries.

The structural check is what catches the rot that actually happens to a docs page whose
whole job is to be copy-pasted: a renamed keyword, a moved import, a function that no
longer exists. It does not claim the fragment was run — the guide says which block is
the executed one.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

GUIDE = Path(__file__).parent.parent / "docs" / "adapters" / "msft-agent-framework.md"

pytest.importorskip("agent_framework", reason="needs capsule-emit[msft-agent-framework]")

_FENCE = re.compile(r"^```(\w*)[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)

# Fragments: illustrative excerpts that are not standalone programs. Each must be
# provable against an executed block (see _lines_of / test_every_excerpt_is_real).
# The reason is recorded here so a future reader sees why it is not executed.
EXCERPT_REASONS = {
    0: "registration shape — needs a caller-supplied chat_client and tools",
    1: "per-run registration — one expression against an existing agent",
    3: "seal_runs=False switch — one expression",
    4: "ordering example — one keyword argument",
}


def _blocks() -> list[str]:
    """Every fenced ``python`` block in the guide, in document order."""
    assert GUIDE.exists(), f"guide missing: {GUIDE}"
    return [body for lang, body in _FENCE.findall(GUIDE.read_text()) if lang == "python"]


def _is_program(block: str) -> bool:
    """A block is a program if it does real work at module level, not just define a shape."""
    return "import " in block and ("asyncio.run" in block or "print(" in block)


def _split() -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    programs: list[tuple[int, str]] = []
    excerpts: list[tuple[int, str]] = []
    for index, block in enumerate(_blocks()):
        (programs if _is_program(block) else excerpts).append((index, block))
    return programs, excerpts


PROGRAMS, EXCERPTS = _split()


def test_the_guide_has_something_to_execute():
    # Guards the gate itself: deleting every runnable block must not make this pass.
    assert PROGRAMS, "the guide has no executable Python block"


@pytest.mark.parametrize("index,block", PROGRAMS, ids=[f"block-{i}" for i, _ in PROGRAMS])
def test_every_program_block_runs_clean(index, block):
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / f"guide_block_{index}.py"
        script.write_text(block)
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=td,
        )
    assert proc.returncode == 0, (
        f"docs block {index} in {GUIDE.name} failed:\n{proc.stdout}\n{proc.stderr}"
    )


# Keywords an excerpt may hand a capsule_emit callable: capsule_middleware forwards
# **core_kw to AgentFrameworkCore, which forwards **base_kw to CapsuleEmitterBase.
# Reading them off the real signatures is the point — a rename breaks the gate.
def _accepted_keywords() -> set[str]:
    import inspect

    from capsule_emit.adapters._base import CapsuleEmitterBase
    from capsule_emit.adapters.msft_agent_framework import AgentFrameworkCore, capsule_middleware

    accepted: set[str] = set()
    for func in (capsule_middleware, AgentFrameworkCore.__init__, CapsuleEmitterBase.__init__):
        for name, param in inspect.signature(func).parameters.items():
            if param.kind is not param.VAR_KEYWORD and name not in ("self", "core"):
                accepted.add(name)
    return accepted


CAPSULE_CALLABLES = {"capsule_middleware", "CapsuleFunctionMiddleware", "CapsuleRunMiddleware"}


def _parse(block: str) -> ast.AST:
    # Excerpts may contain a top-level `await` (the per-run registration example).
    return compile(block, "<docs>", "exec", ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)


@pytest.mark.parametrize("index,block", EXCERPTS, ids=[f"block-{i}" for i, _ in EXCERPTS])
def test_every_excerpt_is_registered_and_structurally_real(index, block):
    import importlib

    assert index in EXCERPT_REASONS, (
        f"docs block {index} is neither a runnable program nor a registered excerpt; "
        "make it runnable or add it to EXCERPT_REASONS with a reason"
    )
    tree = _parse(block)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            module = importlib.import_module(node.module)
            for alias in node.names:
                assert hasattr(module, alias.name), (
                    f"docs block {index} imports {alias.name} from {node.module}, "
                    "which does not export it"
                )

    accepted = _accepted_keywords()
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name not in CAPSULE_CALLABLES:
            continue
        checked += 1
        for keyword in node.keywords:
            assert keyword.arg in accepted, (
                f"docs block {index} passes {name}({keyword.arg}=...), which no capsule-emit "
                f"signature accepts; accepted: {sorted(accepted)}"
            )
    assert checked, f"docs block {index} is registered as a registration excerpt but calls nothing"


def test_excerpt_reasons_do_not_rot():
    # A stale entry means a block was made runnable (or deleted) and the reason stayed.
    assert set(EXCERPT_REASONS) == {index for index, _ in EXCERPTS}
