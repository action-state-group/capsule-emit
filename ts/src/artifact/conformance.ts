// The shared, language-neutral artifact store conformance suite
// (test-vectors/artifact-store, bundled as a byte-identical copy), specified by
// docs/artifact-store.md. It runs against any Store, built in or your own, and
// depends on no test framework: artifactConformanceTests returns one test per
// constructor vector and case for a runner to register, and
// runArtifactConformance runs them all and reports outcomes.
import vectorsJson from "./conformance-vectors.json" with { type: "json" };
import { decodeRecord } from "./jsonl.js";
import { ArtifactError, type Record, type Store } from "./record.js";

/** Adapts one backend to the suite. `B` is its storage handle. */
export interface ConformanceHarness<B> {
  /** Fresh, empty storage for one case. */
  newBacking(): B | Promise<B>;
  /**
   * Constructs a store over a backing without calling `init`; constructor
   * errors propagate so the suite can check configuration validation.
   */
  open(backing: B, namespace: string, trusted: Uint8Array[]): Store;
  /** Releases a backing after its case. */
  close?(backing: B): void | Promise<void>;
  /** A store reopened over the same backing sees earlier writes. */
  durable?: boolean;
  /** Stores with different namespaces can share one backing. */
  sharedBacking?: boolean;
}

interface StoreSpec {
  namespace: string;
  trusted: string[];
}

interface Step {
  op: string;
  store?: string;
  record?: string;
  id?: string;
  trusted?: string[];
  expect: string;
  result?: string;
}

interface Case {
  name: string;
  requires?: string[];
  stores: { [name: string]: StoreSpec };
  steps: Step[];
}

interface OpenVector {
  name: string;
  namespace: string;
  trusted: string[];
  expect: string;
}

/** The decoded vector file. See test-vectors/artifact-store/README.md. */
export interface ConformanceVectors {
  format: string;
  keys: { [name: string]: string };
  records: { [name: string]: unknown };
  checksums: { record: string; storage_checksum: string }[];
  lines: { record: string; line: string }[];
  open: OpenVector[];
  cases: Case[];
}

/** The bundled conformance vectors. */
export const conformanceVectors = vectorsJson as ConformanceVectors;

/** A named record, decoded with the JSONL wire codec. */
export function vectorRecord(name: string): Record {
  const value = conformanceVectors.records[name];
  if (value === undefined) throw new Error(`unknown record ${name}`);
  return decodeRecord(JSON.stringify(value));
}

function hexBytes(value: string): Uint8Array {
  return new Uint8Array(Buffer.from(value, "hex"));
}

function equalBytes(a: Uint8Array, b: Uint8Array): boolean {
  return Buffer.from(a).equals(Buffer.from(b));
}

/**
 * Compares records as the contract defines equality: artifact order is
 * ignored, an empty binding or checksum equals an absent one, and absent
 * content differs from zero-length content. Returns "" when equal.
 */
export function diffRecords(want: Record, got: Record): string {
  if (want.capsuleId !== got.capsuleId)
    return `capsule_id ${got.capsuleId} != ${want.capsuleId}`;
  if (!equalBytes(want.capsule, got.capsule)) return "capsule bytes differ";
  if (!equalBytes(want.producerEnvelope, got.producerEnvelope))
    return "producer_envelope bytes differ";
  if (want.artifacts.length !== got.artifacts.length)
    return `${got.artifacts.length} artifacts, want ${want.artifacts.length}`;
  for (const w of want.artifacts) {
    const g = got.artifacts.find((a) => a.name === w.name);
    if (g === undefined) return `missing artifact ${w.name}`;
    if ((g.binding ?? "") !== (w.binding ?? ""))
      return `${w.name} binding ${g.binding}, want ${w.binding}`;
    if (g.state !== w.state)
      return `${w.name} state ${g.state}, want ${w.state}`;
    if ((g.contentSha256 ?? "") !== (w.contentSha256 ?? ""))
      return `${w.name} content_sha256 differs`;
    if ((g.content === undefined) !== (w.content === undefined))
      return `${w.name} content presence ${g.content !== undefined}, want ${w.content !== undefined}`;
    if (
      g.content !== undefined &&
      w.content !== undefined &&
      !equalBytes(g.content, w.content)
    )
      return `${w.name} content differs`;
  }
  return "";
}

async function outcome(call: () => unknown): Promise<string> {
  try {
    await call();
    return "ok";
  } catch (error) {
    if (error instanceof ArtifactError) return error.code;
    return `error: ${String(error)}`;
  }
}

function expectCode(label: string, want: string, got: string): void {
  if (got !== want) throw new Error(`${label}: want ${want}, got ${got}`);
}

/** One conformance test: `run` throws on failure; `skip` explains a skip. */
export interface ConformanceTest {
  name: string;
  skip?: string;
  run(): Promise<void>;
}

function openTest<B>(
  h: ConformanceHarness<B>,
  v: OpenVector,
): () => Promise<void> {
  return async () => {
    const backing = await h.newBacking();
    try {
      let store: Store | undefined;
      const got = await outcome(() => {
        store = h.open(backing, v.namespace, v.trusted.map(hexBytes));
      });
      expectCode("open", v.expect, got);
      if (store !== undefined && store.namespace !== v.namespace)
        throw new Error(`namespace ${store.namespace}, want ${v.namespace}`);
    } finally {
      await h.close?.(backing);
    }
  };
}

function caseTest<B>(h: ConformanceHarness<B>, c: Case): () => Promise<void> {
  const keys = (names: string[]): Uint8Array[] =>
    names.map((n) => hexBytes(conformanceVectors.keys[n] ?? ""));
  return async () => {
    const backing = await h.newBacking();
    const stores = new Map<string, Store>();
    const open = async (name: string, trusted: string[]): Promise<void> => {
      const spec = c.stores[name];
      if (spec === undefined) throw new Error(`unknown store ${name}`);
      const store = h.open(backing, spec.namespace, keys(trusted));
      for (let i = 0; i < 2; i += 1) await store.init(); // init is idempotent
      if (store.namespace !== spec.namespace)
        throw new Error(`namespace ${store.namespace}, want ${spec.namespace}`);
      stores.set(name, store);
    };
    try {
      for (const name of Object.keys(c.stores).sort())
        await open(name, c.stores[name]?.trusted ?? []);
      for (const [i, s] of c.steps.entries()) {
        const name = s.store ?? "main";
        const store = stores.get(name);
        if (store === undefined) throw new Error(`unknown store ${name}`);
        const label = `step ${i} (${s.op} ${s.record ?? ""}${s.id ?? ""})`;
        const id = s.id ?? "";
        switch (s.op) {
          case "put": {
            const record = vectorRecord(s.record ?? "");
            expectCode(label, s.expect, await outcome(() => store.put(record)));
            break;
          }
          case "get": {
            let got: Record | undefined;
            const code = await outcome(async () => {
              got = await store.get(id);
            });
            expectCode(label, s.expect, code);
            if (got !== undefined) {
              const difference = diffRecords(vectorRecord(s.result ?? ""), got);
              if (difference !== "")
                throw new Error(
                  `${label}: differs from ${s.result}: ${difference}`,
                );
            }
            break;
          }
          case "purge":
            expectCode(label, s.expect, await outcome(() => store.purge(id)));
            break;
          case "reopen":
            await open(name, s.trusted ?? c.stores[name]?.trusted ?? []);
            break;
          default:
            throw new Error(`${label}: unknown op`);
        }
      }
    } finally {
      await h.close?.(backing);
    }
  };
}

/** One test per constructor vector (`open/<name>`) and per case. */
export function artifactConformanceTests<B>(
  h: ConformanceHarness<B>,
): ConformanceTest[] {
  const tests: ConformanceTest[] = conformanceVectors.open.map((v) => ({
    name: `open/${v.name}`,
    run: openTest(h, v),
  }));
  const capabilities: { [name: string]: boolean } = {
    durable: h.durable ?? false,
    shared_backing: h.sharedBacking ?? false,
  };
  for (const c of conformanceVectors.cases) {
    const missing = (c.requires ?? []).find((r) => !capabilities[r]);
    const test: ConformanceTest = { name: c.name, run: caseTest(h, c) };
    if (missing !== undefined)
      test.skip = `backend lacks capability ${missing}`;
    tests.push(test);
  }
  return tests;
}

/** The outcome of one conformance test. */
export interface ConformanceOutcome {
  name: string;
  status: "pass" | "fail" | "skip";
  detail: string;
}

/** Runs every conformance test and reports one outcome each. */
export async function runArtifactConformance<B>(
  h: ConformanceHarness<B>,
): Promise<ConformanceOutcome[]> {
  const outcomes: ConformanceOutcome[] = [];
  for (const test of artifactConformanceTests(h)) {
    if (test.skip !== undefined) {
      outcomes.push({ name: test.name, status: "skip", detail: test.skip });
      continue;
    }
    try {
      await test.run();
      outcomes.push({ name: test.name, status: "pass", detail: "" });
    } catch (error) {
      outcomes.push({ name: test.name, status: "fail", detail: String(error) });
    }
  }
  return outcomes;
}
