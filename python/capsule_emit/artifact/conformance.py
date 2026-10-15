# SPDX-License-Identifier: Apache-2.0
"""The shared, language-neutral artifact store conformance suite.

Runs ``test-vectors/artifact-store`` (bundled as ``conformance_vectors.json``,
a byte-identical copy) against any store, built in or your own::

    from capsule_emit.artifact import conformance
    from capsule_emit.artifact.jsonl import JsonlArtifactStore

    harness = conformance.Harness(
        new_backing=lambda: tempfile.mkdtemp() + "/artifacts.jsonl",
        open=lambda path, namespace, trusted: JsonlArtifactStore(path, namespace, trusted),
        durable=True,
    )
    failures = [o for o in conformance.run(harness) if o.status == "fail"]

The runner depends on no test framework: :func:`tests` yields one callable per
constructor vector and case for a test runner to parametrize over, and
:func:`run` executes them all and reports outcomes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any, Callable

from .record import ArtifactError, Record, decode_record

__all__ = ["ConformanceTest", "Harness", "Outcome", "diff", "load_vectors", "run", "tests"]


@dataclass
class Harness:
    """Adapts one backend to the suite.

    ``new_backing()`` returns fresh, empty storage for one case (a path, a
    connection, ...). ``open(backing, namespace, trusted_keys)`` constructs a
    store over it without calling ``init`` and lets constructor errors raise.
    ``durable``: a store reopened over the same backing sees earlier writes.
    ``shared_backing``: stores with different namespaces can share a backing.
    ``close(backing)``, if given, releases a backing after its case.
    """

    new_backing: Callable[[], Any]
    open: Callable[[Any, str, list], Any]
    durable: bool = False
    shared_backing: bool = False
    close: Callable[[Any], None] | None = None


@dataclass
class Outcome:
    name: str
    status: str  # "pass", "fail" or "skip"
    detail: str = ""


@dataclass
class ConformanceTest:
    name: str
    skip: str | None
    run: Callable[[], None]


class ConformanceFailure(AssertionError):
    pass


def load_vectors() -> dict:
    """Decodes the bundled conformance vectors."""
    text = resources.files(__package__).joinpath("conformance_vectors.json").read_text(encoding="utf-8")
    return json.loads(text)


def _record(vectors: dict, name: str) -> Record:
    if name not in vectors["records"]:
        raise ConformanceFailure(f"unknown record {name!r}")
    return decode_record(json.dumps(vectors["records"][name]))


def _outcome(call: Callable[[], Any]) -> tuple[str, Any]:
    try:
        return "ok", call()
    except ArtifactError as exc:
        return exc.code or "unknown", exc


def _expect(label: str, expect: str, got: str, detail: Any) -> None:
    if got != expect:
        raise ConformanceFailure(f"{label}: want {expect}, got {got} ({detail!r})")


def diff(want: Record, got: Record) -> str:
    """Compares records as the contract defines equality: artifact order is
    ignored, and absent content differs from zero-length content. Returns ""
    when they are equal."""
    if want.capsule_id != got.capsule_id:
        return f"capsule_id {got.capsule_id!r} != {want.capsule_id!r}"
    if bytes(want.capsule) != bytes(got.capsule):
        return "capsule bytes differ"
    if bytes(want.producer_envelope) != bytes(got.producer_envelope):
        return "producer_envelope bytes differ"
    if len(want.artifacts) != len(got.artifacts):
        return f"{len(got.artifacts)} artifacts, want {len(want.artifacts)}"
    by_name = {a.name: a for a in got.artifacts}
    for w in want.artifacts:
        g = by_name.get(w.name)
        if g is None:
            return f"missing artifact {w.name}"
        for attribute in ("binding", "state", "content_sha256"):
            if getattr(g, attribute) != getattr(w, attribute):
                return f"{w.name} {attribute} {getattr(g, attribute)!r}, want {getattr(w, attribute)!r}"
        if (g.content is None) != (w.content is None):
            return f"{w.name} content presence {g.content is not None}, want {w.content is not None}"
        if g.content is not None and bytes(g.content) != bytes(w.content):
            return f"{w.name} content differs"
    return ""


def _open_test(harness: Harness, vector: dict) -> Callable[[], None]:
    def run() -> None:
        keys = [bytes.fromhex(k) for k in vector["trusted"]]
        backing = harness.new_backing()
        try:
            got, store = _outcome(lambda: harness.open(backing, vector["namespace"], keys))
            _expect("open", vector["expect"], got, store)
            if got == "ok" and store.namespace != vector["namespace"]:
                raise ConformanceFailure(f"namespace {store.namespace!r}, want {vector['namespace']!r}")
        finally:
            if harness.close is not None:
                harness.close(backing)

    return run


def _keys(vectors: dict, names: list) -> list:
    return [bytes.fromhex(vectors["keys"][name]) for name in names]


def _case_test(harness: Harness, vectors: dict, case: dict) -> Callable[[], None]:
    def run() -> None:
        backing = harness.new_backing()
        stores: dict = {}

        def open_store(name: str, trusted: list) -> None:
            spec = case["stores"][name]
            store = harness.open(backing, spec["namespace"], _keys(vectors, trusted))
            for _ in range(2):  # init is idempotent
                store.init()
            if store.namespace != spec["namespace"]:
                raise ConformanceFailure(f"namespace {store.namespace!r}, want {spec['namespace']!r}")
            stores[name] = store

        try:
            for name in sorted(case["stores"]):
                open_store(name, case["stores"][name]["trusted"])
            for i, step in enumerate(case["steps"]):
                name = step.get("store") or "main"
                store = stores[name]
                op = step["op"]
                label = f"step {i} ({op} {step.get('record', '')}{step.get('id', '')})"
                if op == "put":
                    record = _record(vectors, step["record"])
                    got, detail = _outcome(lambda s=store, r=record: s.put(r))
                    _expect(label, step["expect"], got, detail)
                elif op == "get":
                    got, result = _outcome(lambda s=store, i=step["id"]: s.get(i))
                    _expect(label, step["expect"], got, result)
                    if got == "ok":
                        difference = diff(_record(vectors, step["result"]), result)
                        if difference:
                            raise ConformanceFailure(f"{label}: differs from {step['result']}: {difference}")
                elif op == "purge":
                    got, detail = _outcome(lambda s=store, i=step["id"]: s.purge(i))
                    _expect(label, step["expect"], got, detail)
                elif op == "reopen":
                    open_store(name, step.get("trusted") or case["stores"][name]["trusted"])
                else:
                    raise ConformanceFailure(f"{label}: unknown op")
        finally:
            if harness.close is not None:
                harness.close(backing)

    return run


def tests(harness: Harness, vectors: dict | None = None) -> list:
    """One :class:`ConformanceTest` per constructor vector (``open/<name>``) and
    per case. A test whose required capability the harness lacks has ``skip``
    set; ``run()`` raises ``AssertionError`` on failure."""
    vectors = vectors if vectors is not None else load_vectors()
    result = [ConformanceTest("open/" + v["name"], None, _open_test(harness, v)) for v in vectors["open"]]
    capabilities = {"durable": harness.durable, "shared_backing": harness.shared_backing}
    for case in vectors["cases"]:
        missing = [r for r in case.get("requires", []) if not capabilities.get(r, False)]
        skip = f"backend lacks capability {missing[0]}" if missing else None
        result.append(ConformanceTest(case["name"], skip, _case_test(harness, vectors, case)))
    return result


def run(harness: Harness) -> list:
    """Runs every test and returns one :class:`Outcome` each."""
    outcomes = []
    for test in tests(harness):
        if test.skip:
            outcomes.append(Outcome(test.name, "skip", test.skip))
            continue
        try:
            test.run()
        except Exception as exc:  # report every failure, keep going
            outcomes.append(Outcome(test.name, "fail", f"{type(exc).__name__}: {exc}"))
        else:
            outcomes.append(Outcome(test.name, "pass"))
    return outcomes
