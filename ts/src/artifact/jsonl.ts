// JSONL artifact backend. One UTF-8 file scopes to exactly one namespace; the
// namespace is a construction-time parameter, not persisted per line. Each line
// is the JSON serialization of a Record terminated by a single \n, using the
// same wire shape as the Go artifact.Record: snake_case keys (capsule_id,
// producer_envelope, content_sha256), Uint8Array fields as standard base64, and
// an empty artifacts array as [] (not null). This makes files portable to the
// Go JSONL store in either direction. Records are immutable except via explicit
// purge. Assumes a single writer.
import { randomBytes } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";

import {
  ArtifactError,
  prepare,
  storageChecksum,
  verify,
  type Artifact,
  type Record,
  type RetentionState,
  type Store,
} from "./record.js";

const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/u;
const ID_PATTERN = /^[0-9a-f]{64}$/u;

/** Byte span of a single JSONL line (including its trailing \n). */
interface LineSpan {
  offset: number;
  length: number;
}

// ---- Serialization helpers --------------------------------------------------

interface StoredArtifact {
  name: string;
  // Explicit `undefined` is allowed so encodeRecord can build the fields in
  // Go's struct order; JSON.stringify drops undefined-valued keys (omitempty).
  binding?: string | undefined;
  content?: string | undefined;
  state: string;
  content_sha256?: string | undefined;
}

interface StoredRecord {
  capsule_id: string;
  capsule: string;
  producer_envelope: string;
  artifacts: StoredArtifact[];
}

/**
 * Serializes a Record to its one-line JSON wire form (no trailing newline). Keys
 * are emitted in the same order as the Go `artifact.Record` struct
 * (`name,binding,content,state,content_sha256`) so the bytes are byte-identical
 * across the Go and TypeScript stores. Exported for the shared golden-line test;
 * `JSON.stringify` drops `undefined` values, reproducing Go's `omitempty`.
 */
export function encodeRecord(record: Record): string {
  const stored: StoredRecord = {
    capsule_id: record.capsuleId,
    capsule: Buffer.from(record.capsule).toString("base64"),
    producer_envelope: Buffer.from(record.producerEnvelope).toString("base64"),
    artifacts: record.artifacts.map((a) => ({
      name: a.name,
      binding: a.binding || undefined,
      content:
        a.content !== undefined
          ? Buffer.from(a.content).toString("base64")
          : undefined,
      state: a.state,
      content_sha256: a.contentSha256 || undefined,
    })),
  };
  return JSON.stringify(stored);
}

/**
 * Parses a JSON line back to a Record. Throws ArtifactError("corrupt") on
 * failure. Exported alongside encodeRecord as the store's wire codec.
 */
export function decodeRecord(line: string): Record {
  let parsed: unknown;
  try {
    parsed = JSON.parse(line);
  } catch {
    throw new ArtifactError("corrupt", "invalid JSON in JSONL store");
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new ArtifactError("corrupt", "malformed record in JSONL store");
  }
  const stored = parsed as StoredRecord;
  if (
    typeof stored.capsule_id !== "string" ||
    typeof stored.capsule !== "string" ||
    typeof stored.producer_envelope !== "string" ||
    !Array.isArray(stored.artifacts)
  ) {
    throw new ArtifactError("corrupt", "malformed record in JSONL store");
  }
  const artifacts: Artifact[] = (stored.artifacts as unknown[]).map((raw) => {
    if (raw === null || typeof raw !== "object")
      throw new ArtifactError("corrupt", "malformed artifact in JSONL store");
    const sa = raw as StoredArtifact;
    if (typeof sa.name !== "string" || typeof sa.state !== "string")
      throw new ArtifactError("corrupt", "malformed artifact in JSONL store");
    const a: Artifact = {
      name: sa.name,
      state: sa.state as RetentionState,
    };
    if (sa.binding !== undefined) a.binding = sa.binding as Artifact["binding"];
    if (sa.content !== undefined)
      a.content = new Uint8Array(Buffer.from(sa.content, "base64"));
    if (sa.content_sha256) a.contentSha256 = sa.content_sha256;
    return a;
  });
  return {
    capsuleId: stored.capsule_id,
    capsule: new Uint8Array(Buffer.from(stored.capsule, "base64")),
    producerEnvelope: new Uint8Array(
      Buffer.from(stored.producer_envelope, "base64"),
    ),
    artifacts,
  };
}

// ---- Index helpers ----------------------------------------------------------

/**
 * Scans the file and builds capsuleId -> LineSpan. Last-occurrence-wins is a
 * defensive tie-break for duplicates; a well-formed file has exactly one line
 * per capsuleId. Unparseable lines are skipped silently.
 */
function indexLine(
  index: Map<string, LineSpan>,
  body: Buffer,
  offset: number,
): void {
  if (body.length === 0) return;
  try {
    const obj = JSON.parse(body.toString("utf8")) as { capsule_id?: unknown };
    if (typeof obj.capsule_id === "string") {
      // length includes the trailing \n, matching readSpan and put.
      index.set(obj.capsule_id, { offset, length: body.length + 1 });
    }
  } catch {
    // Skip lines that fail to parse during index construction.
  }
}

function scanIndex(filePath: string): Map<string, LineSpan> {
  const index = new Map<string, LineSpan>();
  if (!fs.existsSync(filePath)) return index;
  // Stream the file in fixed chunks so peak memory scales with one line, not
  // the whole store; only the id -> (offset,length) index is retained. A
  // trailing partial line (no final \n) is left unindexed.
  const fd = fs.openSync(filePath, "r");
  try {
    const CHUNK = 1 << 16;
    const chunk = Buffer.allocUnsafe(CHUNK);
    let carry = Buffer.alloc(0); // bytes of the current partial line
    let lineStart = 0; // file offset where carry began
    let filePos = 0; // file offset of the next unread byte
    for (;;) {
      const n = fs.readSync(fd, chunk, 0, CHUNK, filePos);
      if (n === 0) break;
      const base = filePos; // file offset of chunk[0]
      filePos += n;
      let from = 0;
      for (;;) {
        const nl = chunk.indexOf(0x0a, from);
        if (nl === -1 || nl >= n) break;
        const body = Buffer.concat([carry, chunk.subarray(from, nl)]);
        indexLine(index, body, lineStart);
        carry = Buffer.alloc(0);
        from = nl + 1;
        lineStart = base + nl + 1;
      }
      if (from < n) carry = Buffer.concat([carry, chunk.subarray(from, n)]);
    }
  } finally {
    fs.closeSync(fd);
  }
  return index;
}

/**
 * Reads the exact bytes described by span using a file descriptor seek, then
 * strips the trailing \n. Returns fewer bytes than span.length if the file is
 * shorter (e.g., external truncation); callers that receive a partial or empty
 * string will encounter a JSON parse error in decodeRecord.
 */
function readSpan(filePath: string, span: LineSpan): string {
  const buf = Buffer.alloc(span.length);
  const fd = fs.openSync(filePath, "r");
  try {
    const bytesRead = fs.readSync(fd, buf, 0, span.length, span.offset);
    const end =
      bytesRead > 0 && buf[bytesRead - 1] === 0x0a ? bytesRead - 1 : bytesRead;
    return buf.subarray(0, end).toString("utf8");
  } finally {
    fs.closeSync(fd);
  }
}

// ---- Store ------------------------------------------------------------------

/**
 * An application-importable JSONL artifact store. The caller owns the file
 * path and the trusted keys. A namespace isolates a collection; it must not
 * implicitly be a log ID.
 */
export class JsonlArtifactStore implements Store {
  private readonly trusted: Uint8Array[];
  private index: Map<string, LineSpan>;

  public constructor(
    private readonly filePath: string,
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
    this.index = scanIndex(filePath);
  }

  /** Creates an empty JSONL file if it does not already exist. */
  public async init(): Promise<void> {
    if (!fs.existsSync(this.filePath)) {
      fs.writeFileSync(this.filePath, "");
    }
  }

  /** Persists an immutable record atomically, accepting byte-identical retries. */
  public async put(record: Record): Promise<void> {
    const prepared = prepare(record);
    await verify(prepared, this.trusted);
    const hash = storageChecksum(prepared);

    const existing = this.index.get(prepared.capsuleId);
    if (existing !== undefined) {
      // Record already exists: check for idempotent retry or conflict.
      const line = readSpan(this.filePath, existing);
      const stored = decodeRecord(line);
      if (stored.capsuleId !== prepared.capsuleId)
        throw new ArtifactError(
          "corrupt",
          "index points to a different capsule",
        );
      await verify(stored, this.trusted);
      if (storageChecksum(stored) !== hash)
        throw new ArtifactError("conflict", "immutable record conflict");
      for (const a of stored.artifacts)
        if (a.state === "purged")
          throw new ArtifactError("purged", "originals were purged");
      return; // byte-identical retry with no purged artifacts
    }

    // New record: append to file and update in-memory index. The file must
    // already exist (created by init); a missing file is an error rather than
    // a silent provision, matching the Go and sqlite backends.
    const line = encodeRecord(prepared) + "\n";
    const lineBuf = Buffer.from(line, "utf8");
    const offset = fs.statSync(this.filePath).size;
    fs.appendFileSync(this.filePath, lineBuf);
    this.index.set(prepared.capsuleId, { offset, length: lineBuf.length });
  }

  /** Loads a coherent snapshot and verifies authenticity and original digests. */
  public async get(id: string): Promise<Record> {
    if (!ID_PATTERN.test(id)) throw new ArtifactError("invalid", "capsule id");
    const span = this.index.get(id);
    if (span === undefined)
      throw new ArtifactError("not_found", "capsule not found");
    const line = readSpan(this.filePath, span);
    const record = decodeRecord(line);
    if (record.capsuleId !== id)
      throw new ArtifactError("corrupt", "index points to a different capsule");
    await verify(record, this.trusted);
    return record;
  }

  /**
   * Deletes business originals, retaining the exact Capsule/envelope, inventory
   * and tombstones. Rewrites the file atomically via a temp file and rename.
   * Only present artifacts are erased; never_retained and already-purged
   * artifacts are unchanged (idempotent). Erasure requires only namespace-scoped
   * existence, not signer verification.
   */
  public async purge(id: string): Promise<void> {
    if (!ID_PATTERN.test(id)) throw new ArtifactError("invalid", "capsule id");
    if (!this.index.has(id))
      throw new ArtifactError("not_found", "capsule not found");

    const buf = fs.readFileSync(this.filePath);
    const srcMode = fs.statSync(this.filePath).mode & 0o777;
    const resolvedDir = path.dirname(path.resolve(this.filePath));
    const tmpPath = path.join(
      resolvedDir,
      `.tmp-${randomBytes(12).toString("hex")}`,
    );
    const newIndex = new Map<string, LineSpan>();
    let writeOffset = 0;

    try {
      // Exclusive create ("wx"): never follow a pre-existing path or symlink.
      const outFd = fs.openSync(tmpPath, "wx");
      try {
        // chmod after creation so the mode is not filtered by the process
        // umask; inside the try so the finally still closes outFd if it throws.
        fs.fchmodSync(outFd, srcMode);
        let readOffset = 0;
        while (readOffset < buf.length) {
          const nlPos = buf.indexOf(0x0a, readOffset);
          const lineEnd = nlPos === -1 ? buf.length : nlPos;
          const lineStr = buf.subarray(readOffset, lineEnd).toString("utf8");

          // Default: copy the line unchanged.
          let outLine = lineStr.length > 0 ? lineStr + "\n" : "\n";
          let lineCapsuleId: string | undefined;

          if (lineStr.length > 0) {
            // Parse just enough to find capsuleId; skip unparseable lines.
            let obj: { capsule_id?: unknown } | undefined;
            try {
              obj = JSON.parse(lineStr) as { capsule_id?: unknown };
            } catch {
              obj = undefined;
            }
            if (obj !== undefined && typeof obj.capsule_id === "string") {
              lineCapsuleId = obj.capsule_id;
              if (lineCapsuleId === id) {
                // May throw ArtifactError("corrupt") if the line is malformed;
                // the outer catch will clean up the temp file.
                const existing = decodeRecord(lineStr);
                const purgedRecord: Record = {
                  ...existing,
                  artifacts: existing.artifacts.map((a) => {
                    if (a.state !== "present") return a;
                    return {
                      ...a,
                      content: undefined,
                      state: "purged" as RetentionState,
                    };
                  }),
                };
                outLine = encodeRecord(purgedRecord) + "\n";
              }
            }
          }

          const outBuf = Buffer.from(outLine, "utf8");
          if (lineCapsuleId !== undefined) {
            newIndex.set(lineCapsuleId, {
              offset: writeOffset,
              length: outBuf.length,
            });
          }
          fs.writeSync(outFd, outBuf);
          writeOffset += outBuf.length;
          readOffset = nlPos === -1 ? buf.length : nlPos + 1;
        }
      } finally {
        fs.closeSync(outFd);
      }

      fs.renameSync(tmpPath, this.filePath);
      this.index = newIndex;
    } catch (error) {
      try {
        fs.unlinkSync(tmpPath);
      } catch {
        // Ignore temp-file cleanup errors.
      }
      throw error;
    }
  }
}
