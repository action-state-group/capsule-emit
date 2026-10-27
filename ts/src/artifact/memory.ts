// In-memory artifact backend, for tests and short-lived tools. It follows the
// same contract as the persistent backends (docs/artifact-store.md) but keeps
// nothing across process restarts and has no transactional extension. It
// imports no storage driver.
import {
  ArtifactError,
  prepare,
  storageChecksum,
  verify,
  type Record,
} from "./record.js";
import {
  MemoryIndex,
  normalizePaths,
  type IndexStore,
  type Page,
  type Query,
} from "./lookup.js";

const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/u;
const ID_PATTERN = /^[0-9a-f]{64}$/u;

function clone(record: Record): Record {
  return {
    capsuleId: record.capsuleId,
    capsule: Uint8Array.from(record.capsule),
    producerEnvelope: Uint8Array.from(record.producerEnvelope),
    artifacts: record.artifacts.map((a) => ({
      ...a,
      content: a.content === undefined ? undefined : Uint8Array.from(a.content),
    })),
  };
}

/** Holds prepared records by Capsule ID in process memory. */
export class MemoryArtifactStore implements IndexStore {
  private readonly trusted: Uint8Array[];
  private readonly records = new Map<string, Record>();
  private readonly lookup = new MemoryIndex();

  public constructor(
    public readonly namespace: string,
    trusted: readonly Uint8Array[],
  ) {
    if (!NAME_PATTERN.test(namespace) || trusted.length === 0)
      throw new ArtifactError("invalid", "store configuration");
    this.trusted = [];
    for (const key of trusted) {
      if (key.length !== 32)
        throw new ArtifactError("invalid", "public key size");
      this.trusted.push(Uint8Array.from(key));
    }
  }

  /** Has nothing to provision. */
  public async init(): Promise<void> {}

  /** Admits an immutable record, accepting byte-identical retries. */
  public async put(record: Record): Promise<void> {
    const prepared = prepare(record);
    await verify(prepared, this.trusted);
    const hash = storageChecksum(prepared);
    const existing = this.records.get(prepared.capsuleId);
    if (existing === undefined) {
      this.records.set(prepared.capsuleId, prepared);
      this.lookup.add(prepared.capsuleId, prepared.capsule);
      return;
    }
    // existing was verified with these same keys when it was admitted, and
    // memory cannot be altered behind the store, so it needs no re-verification.
    if (storageChecksum(existing) !== hash)
      throw new ArtifactError("conflict", "immutable record conflict");
    for (const a of existing.artifacts)
      if (a.state === "purged")
        throw new ArtifactError("purged", "originals were purged");
  }

  /** Returns a copy of the stored record after verifying it. */
  public async get(id: string): Promise<Record> {
    if (!ID_PATTERN.test(id)) throw new ArtifactError("invalid", "capsule id");
    const stored = this.records.get(id);
    if (stored === undefined)
      throw new ArtifactError("not_found", "capsule not found");
    const record = clone(stored);
    await verify(record, this.trusted);
    return record;
  }

  /**
   * Replaces each present original with a tombstone that keeps its SHA-256.
   * Requires only the record's existence, not its verification.
   */
  public async purge(id: string): Promise<void> {
    if (!ID_PATTERN.test(id)) throw new ArtifactError("invalid", "capsule id");
    const stored = this.records.get(id);
    if (stored === undefined)
      throw new ArtifactError("not_found", "capsule not found");
    const purged = clone(stored);
    purged.artifacts = purged.artifacts.map((a) =>
      a.state === "present" ? { ...a, content: undefined, state: "purged" } : a,
    );
    this.records.set(id, purged);
  }

  /** Declares index paths and indexes the records already stored. */
  public async initIndex(paths: readonly string[]): Promise<void> {
    if (this.lookup.declare(paths)) this.indexAll();
  }

  /** Replaces the declared index paths and rebuilds the index. */
  public async reindex(paths: readonly string[]): Promise<void> {
    this.lookup.reset(normalizePaths(paths));
    this.indexAll();
  }

  /** Returns one page of IDs matching the query, in admission order. */
  public async find(query?: Query): Promise<Page> {
    return this.lookup.find(query);
  }

  // Map iteration follows insertion, which is admission order.
  private indexAll(): void {
    for (const [id, record] of this.records)
      this.lookup.add(id, record.capsule);
  }
}
