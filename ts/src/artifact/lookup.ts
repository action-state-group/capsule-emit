// The optional index extension (docs/artifact-store.md, "Optional index
// extension"): the public interface and the parts every backend shares, which
// are path validation, value extraction from Capsule bytes, timestamp
// normalization, query checks, cursors, and the in-memory index the memory
// and jsonl backends keep. It imports no storage driver.
import { createHash } from "node:crypto";

import { ArtifactError, type Store } from "./record.js";

/** The most paths one namespace may declare. */
export const MAX_INDEX_PATHS = 32;
/** The page size `find` uses when no limit is given. */
export const DEFAULT_FIND_LIMIT = 100;
/** The largest page `find` returns. */
export const MAX_FIND_LIMIT = 1000;

/** Selects records by the namespace's declared index paths. */
export interface Query {
  /**
   * Declared paths and the value each must hold. A record matches a path whose
   * value is that string, or an array containing it. Every entry must match.
   */
  where?: { readonly [path: string]: string } | undefined;
  /** An RFC 3339 date-time; only records whose top-level timestamp is at or after it match. */
  since?: string | undefined;
  /** The `next` of a previous page. */
  after?: string | undefined;
  /** The page size, 1 to {@link MAX_FIND_LIMIT}; default {@link DEFAULT_FIND_LIMIT}. */
  limit?: number | undefined;
}

/** One page of `find` results. */
export interface Page {
  /** Matching Capsule IDs in the store's admission order, which is not any CLL log's order. */
  ids: string[];
  /** The cursor for the following page; empty when there is none. */
  next: string;
}

/**
 * The optional index extension: a namespace declares JSON paths into its
 * Capsules, the store extracts their values at `put`, and `find` returns
 * matching Capsule IDs without reading record bodies. Every built-in backend
 * implements it; detect it with {@link isIndexStore}.
 */
export interface IndexStore extends Store {
  /**
   * Provisions storage as `init` does, then declares `paths` and indexes the
   * records already stored. The same set again does nothing; a different set
   * is `conflict`.
   */
  initIndex(paths: readonly string[]): Promise<void>;
  /** Replaces the declared paths and rebuilds the index. */
  reindex(paths: readonly string[]): Promise<void>;
  /** Returns one page of matching Capsule IDs. */
  find(query?: Query): Promise<Page>;
}

/** Reports whether a store implements the index extension. */
export function isIndexStore(store: Store): store is IndexStore {
  const candidate = store as Partial<IndexStore>;
  return (
    typeof candidate.initIndex === "function" &&
    typeof candidate.reindex === "function" &&
    typeof candidate.find === "function"
  );
}

const SEGMENT_PATTERN = /^[A-Za-z0-9_-]{1,64}$/u;
const TIMESTAMP_PATTERN =
  /^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})$/u;

/** Validates a declared path list and returns it sorted (byte order). */
export function normalizePaths(paths: readonly string[]): string[] {
  if (paths.length > MAX_INDEX_PATHS)
    throw new ArtifactError(
      "invalid",
      `more than ${MAX_INDEX_PATHS} index paths`,
    );
  const sorted = [...paths].sort(compareBytes);
  sorted.forEach((path, i) => {
    if (!validPath(path))
      throw new ArtifactError("invalid", `index path ${JSON.stringify(path)}`);
    if (i > 0 && sorted[i - 1] === path)
      throw new ArtifactError(
        "invalid",
        `repeated index path ${JSON.stringify(path)}`,
      );
  });
  return sorted;
}

// Paths are ASCII, so code-unit order is byte order.
function compareBytes(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function validPath(path: unknown): boolean {
  if (typeof path !== "string" || path.length > 255) return false;
  const segments = path.split(".");
  return (
    segments.length <= 8 &&
    segments.every((segment) => SEGMENT_PATTERN.test(segment))
  );
}

/** The stored form of a normalized path list: a compact JSON array. */
export function encodePaths(paths: readonly string[]): string {
  return JSON.stringify(paths);
}

/** Reads {@link encodePaths} output back. */
export function decodePaths(stored: string): string[] {
  let paths: unknown;
  try {
    paths = JSON.parse(stored);
  } catch {
    throw new ArtifactError("corrupt", "stored index paths");
  }
  if (!Array.isArray(paths))
    throw new ArtifactError("corrupt", "stored index paths");
  try {
    return normalizePaths(paths as string[]);
  } catch {
    throw new ArtifactError("corrupt", "stored index paths");
  }
}

/** One indexed value of one path. */
export interface Entry {
  path: string;
  value: string;
}

/** What a record contributes to the index. */
export interface Extracted {
  entries: Entry[];
  /** UTC microseconds since the Unix epoch, or undefined without a timestamp. */
  ts: bigint | undefined;
}

/**
 * Reads the declared paths and the top-level timestamp from Capsule bytes. A
 * string value is one entry and each distinct string in an array is one entry;
 * any other value, or a missing member, gives none. The bytes were verified at
 * admission, so a decode failure is reported as corrupt.
 */
export function extract(
  capsule: Uint8Array,
  paths: readonly string[],
): Extracted {
  let root: unknown;
  try {
    root = JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(capsule),
    );
  } catch {
    throw new ArtifactError("corrupt", "capsule is not JSON");
  }
  const object = isObject(root) ? root : undefined;
  const timestamp = object?.timestamp;
  const out: Extracted = {
    entries: [],
    ts: typeof timestamp === "string" ? parseTimestamp(timestamp) : undefined,
  };
  for (const path of paths) {
    let value: unknown = object;
    for (const segment of path.split(".")) {
      value =
        isObject(value) && Object.hasOwn(value, segment)
          ? value[segment]
          : undefined;
    }
    if (typeof value === "string") out.entries.push({ path, value });
    else if (Array.isArray(value)) {
      const seen = new Set<string>();
      for (const element of value)
        if (typeof element === "string" && !seen.has(element)) {
          seen.add(element);
          out.entries.push({ path, value: element });
        }
    }
  }
  return out;
}

function isObject(value: unknown): value is { [key: string]: unknown } {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Reads an RFC 3339 date-time with an upper-case T and Z (or a numeric offset)
 * and at most nine fraction digits, in UTC microseconds since the Unix epoch,
 * truncating finer digits. Returns undefined when it is not one.
 */
export function parseTimestamp(s: string): bigint | undefined {
  const m = TIMESTAMP_PATTERN.exec(s);
  if (m === null) return undefined;
  const [year, month, day, hour, minute, second] = m
    .slice(1, 7)
    .map((part) => Number(part)) as [
    number,
    number,
    number,
    number,
    number,
    number,
  ];
  if (
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > daysIn(year, month) ||
    hour > 23 ||
    minute > 59 ||
    second > 59
  )
    return undefined;
  const micros =
    m[7] === undefined ? 0 : Number(`${m[7].slice(1)}00000`.slice(0, 6));
  let offset = 0;
  const zone = m[8] ?? "Z";
  if (zone !== "Z") {
    const oh = Number(zone.slice(1, 3));
    const om = Number(zone.slice(4, 6));
    if (oh > 23 || om > 59) return undefined;
    offset = (zone[0] === "-" ? -1 : 1) * (oh * 60 + om);
  }
  const days = daysFromCivil(year, month, day);
  const seconds =
    BigInt(days) * 86_400n +
    BigInt(hour * 3600 + minute * 60 + second) -
    BigInt(offset * 60);
  return seconds * 1_000_000n + BigInt(micros);
}

function isLeap(year: number): boolean {
  return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
}

function daysIn(year: number, month: number): number {
  return [31, isLeap(year) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][
    month - 1
  ] as number;
}

// Days since 1970-01-01 in the proleptic Gregorian calendar (Hinnant's
// days_from_civil).
function daysFromCivil(year: number, month: number, day: number): number {
  const y = month <= 2 ? year - 1 : year;
  const era = Math.floor(y / 400);
  const yoe = y - era * 400;
  const mp = (month + 9) % 12;
  const doy = Math.floor((153 * mp + 2) / 5) + day - 1;
  const doe = yoe * 365 + Math.floor(yoe / 4) - Math.floor(yoe / 100) + doy;
  return era * 146097 + doe - 719468;
}

/** The key SQL backends store for a value: lowercase hex SHA-256 of its UTF-8 bytes. */
export function valueKey(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

/** A validated query. */
export interface Checked {
  /** The equalities sorted by path. */
  where: Entry[];
  since: bigint | undefined;
  after: bigint;
  limit: number;
}

/** Validates a query against the declared paths. */
export function check(query: Query, declared: readonly string[]): Checked {
  const limit = query.limit ?? DEFAULT_FIND_LIMIT;
  if (!Number.isInteger(limit) || limit < 1 || limit > MAX_FIND_LIMIT)
    throw new ArtifactError("invalid", `limit ${String(limit)}`);
  const where: Entry[] = [];
  for (const [path, value] of Object.entries(query.where ?? {})) {
    if (!declared.includes(path))
      throw new ArtifactError(
        "invalid",
        `${JSON.stringify(path)} is not a declared index path`,
      );
    if (typeof value !== "string")
      throw new ArtifactError("invalid", `value of ${path}`);
    where.push({ path, value });
  }
  where.sort((a, b) => compareBytes(a.path, b.path));
  let since: bigint | undefined;
  if (query.since !== undefined && query.since !== "") {
    since = parseTimestamp(query.since);
    if (since === undefined)
      throw new ArtifactError(
        "invalid",
        `since ${JSON.stringify(query.since)} is not an RFC 3339 date-time`,
      );
  }
  let after = 0n;
  if (query.after !== undefined && query.after !== "") {
    if (!/^[1-9][0-9]{0,18}$/u.test(query.after))
      throw new ArtifactError("invalid", "cursor");
    after = BigInt(query.after);
    if (after > 9_223_372_036_854_775_807n)
      throw new ArtifactError("invalid", "cursor");
  }
  return { where, since, after, limit };
}

/** Formats an admission position as the opaque cursor `find` returns. */
export function cursor(position: bigint | number): string {
  return String(position);
}

/**
 * The in-memory index of the memory and jsonl backends. Positions start at 1
 * and follow admission order.
 */
export class MemoryIndex {
  private paths: string[] = [];
  private declared = false;
  private ids: string[] = [];
  private times: (bigint | undefined)[] = [];
  private values = new Map<string, Map<string, number[]>>();
  private positions = new Map<string, number>();

  /**
   * Records paths for a fresh index, or checks them against the existing
   * declaration: the same set is a no-op, another is a conflict. Returns
   * whether the caller must now add the existing records.
   */
  public declare(paths: readonly string[]): boolean {
    const normalized = normalizePaths(paths);
    if (this.declared) {
      if (encodePaths(this.paths) !== encodePaths(normalized))
        throw new ArtifactError(
          "conflict",
          `namespace already indexes ${encodePaths(this.paths)}`,
        );
      return false;
    }
    this.reset(normalized);
    return true;
  }

  /** Replaces the declaration with normalized paths and empties the index. */
  public reset(normalized: string[]): void {
    this.paths = normalized;
    this.declared = true;
    this.ids = [];
    this.times = [];
    this.values = new Map();
    this.positions = new Map();
  }

  /** Indexes one record at the next position; does nothing before a declaration or for a record already indexed. */
  public add(id: string, capsule: Uint8Array): void {
    if (!this.declared || this.positions.has(id)) return;
    const extracted = extract(capsule, this.paths);
    this.ids.push(id);
    const position = this.ids.length;
    this.positions.set(id, position);
    this.times.push(extracted.ts);
    for (const { path, value } of extracted.entries) {
      let byValue = this.values.get(path);
      if (byValue === undefined) {
        byValue = new Map();
        this.values.set(path, byValue);
      }
      const list = byValue.get(value);
      if (list === undefined) byValue.set(value, [position]);
      else list.push(position);
    }
  }

  /** Answers a query; before a declaration it is invalid. */
  public find(query: Query = {}): Page {
    if (!this.declared)
      throw new ArtifactError("invalid", "no index paths declared");
    const c = check(query, this.paths);
    const after = Number(c.after);
    const [first, ...rest] = c.where;
    let candidates: number[];
    if (first === undefined) {
      candidates = [];
      for (let p = after + 1; p <= this.ids.length; p++) candidates.push(p);
    } else {
      candidates = (this.values.get(first.path)?.get(first.value) ?? []).filter(
        (p) => p > after,
      );
    }
    const page: Page = { ids: [], next: "" };
    let last = 0;
    for (const p of candidates) {
      if (!this.matches(p, c.since, rest)) continue;
      if (page.ids.length === c.limit) {
        page.next = cursor(last);
        break;
      }
      page.ids.push(this.ids[p - 1] as string);
      last = p;
    }
    return page;
  }

  private matches(
    p: number,
    since: bigint | undefined,
    rest: Entry[],
  ): boolean {
    if (since !== undefined) {
      const ts = this.times[p - 1];
      if (ts === undefined || ts < since) return false;
    }
    return rest.every((w) =>
      sortedHas(this.values.get(w.path)?.get(w.value) ?? [], p),
    );
  }
}

function sortedHas(sorted: readonly number[], p: number): boolean {
  let lo = 0;
  let hi = sorted.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if ((sorted[mid] as number) < p) lo = mid + 1;
    else hi = mid;
  }
  return sorted[lo] === p;
}
