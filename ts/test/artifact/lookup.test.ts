import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import Database from "better-sqlite3";
import { describe, expect, it } from "vitest";
import { isIndexStore } from "../../src/artifact/index.js";
import { JsonlArtifactStore } from "../../src/artifact/jsonl.js";
import {
  decodePaths,
  encodePaths,
  extract,
  MemoryIndex,
  normalizePaths,
  parseTimestamp,
  valueKey,
} from "../../src/artifact/lookup.js";
import { MemoryArtifactStore } from "../../src/artifact/memory.js";
import { SqliteArtifactStore } from "../../src/artifact/sqlite.js";
import { makeRecord, utf8 } from "./fixture.js";

describe("index helpers", () => {
  it("normalizes RFC 3339 date-times to UTC microseconds", () => {
    const cases: [string, bigint][] = [
      ["1970-01-01T00:00:00Z", 0n],
      ["1970-01-01T00:00:00.000001Z", 1n],
      ["1970-01-01T00:00:00.0000019Z", 1n],
      ["1970-01-01T01:00:00+01:00", 0n],
      ["1969-12-31T23:00:00-01:00", 0n],
      ["2024-02-29T00:00:00Z", 1709164800_000000n],
      ["0000-01-01T00:00:00Z", -62167219200_000000n],
      ["9999-12-31T23:59:59.999999+00:00", 253402300799_999999n],
    ];
    for (const [input, want] of cases) expect(parseTimestamp(input)).toBe(want);
    for (const input of [
      "",
      "2023-02-29T00:00:00Z",
      "2026-13-01T00:00:00Z",
      "2026-00-01T00:00:00Z",
      "2026-01-00T00:00:00Z",
      "2026-01-01T00:60:00Z",
      "2026-01-01T00:00:00+00:60",
      "2026-01-01T00:00:00",
    ])
      expect(parseTimestamp(input)).toBeUndefined();
  });

  it("extracts string values and distinct array strings only", () => {
    const got = extract(
      utf8(`{"a":{"b":"x","c":["y",1,"y","z"],"d":{"e":1}},"timestamp":"bad"}`),
      ["a.b", "a.c", "a.d", "a.b.x", "missing"],
    );
    expect(got.ts).toBeUndefined();
    expect(got.entries).toEqual([
      { path: "a.b", value: "x" },
      { path: "a.c", value: "y" },
      { path: "a.c", value: "z" },
    ]);
    expect(extract(utf8(`["x"]`), ["a"]).entries).toEqual([]);
    expect(() => extract(utf8("{"), [])).toThrow(
      expect.objectContaining({ code: "corrupt" }),
    );
  });

  it("round-trips declared paths and rejects damaged ones as corrupt", () => {
    const paths = normalizePaths(["b", "a.c"]);
    expect(encodePaths(paths)).toBe(`["a.c","b"]`);
    expect(decodePaths(encodePaths(paths))).toEqual(paths);
    for (const stored of ["{", `"a"`, `["a","a"]`, `[" "]`])
      expect(() => decodePaths(stored)).toThrow(
        expect.objectContaining({ code: "corrupt" }),
      );
    expect(valueKey("")).toBe(
      "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    );
  });

  it("ignores records before a declaration", () => {
    const index = new MemoryIndex();
    index.add("id", utf8("{"));
    expect(() => index.find()).toThrow(
      expect.objectContaining({ code: "invalid" }),
    );
    expect(index.declare(["a"])).toBe(true);
    expect(index.declare(["a"])).toBe(false);
  });

  it("detects the extension", async () => {
    const { trusted } = await makeRecord();
    expect(isIndexStore(new MemoryArtifactStore("a", [trusted]))).toBe(true);
    expect(
      isIndexStore({
        namespace: "a",
        init: async () => {},
        put: async () => {},
        get: async () => {
          throw new Error("unused");
        },
        purge: async () => {},
      }),
    ).toBe(false);
  });
});

describe("index backends", () => {
  it("jsonl leaves a damaged line out of the index", async () => {
    const { record, trusted } = await makeRecord("jsonl-index");
    const path = join(
      mkdtempSync(join(tmpdir(), "artifact-index-")),
      "a.jsonl",
    );
    writeFileSync(path, `{"capsule_id":"${"ab".repeat(32)}","capsule":1}\n`);
    const store = new JsonlArtifactStore(path, "a", [trusted]);
    await store.put(record);
    await store.initIndex(["operator"]);
    expect((await store.find()).ids).toEqual([record.capsuleId]);
    await store.reindex(["developer"]);
    expect(
      (await store.find({ where: { developer: "test-developer" } })).ids,
    ).toEqual([record.capsuleId]);
  });

  it("sqlite keeps accepting puts on an older schema and indexes them on initIndex", async () => {
    const { record, trusted } = await makeRecord("sqlite-older");
    const db = new Database(
      join(mkdtempSync(join(tmpdir(), "artifact-index-")), "a.db"),
    );
    db.exec(`CREATE TABLE capsule_store_capsules (namespace TEXT NOT NULL, capsule_id TEXT NOT NULL, capsule_bytes BLOB NOT NULL,
      producer_envelope BLOB NOT NULL, record_sha256 TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), PRIMARY KEY (namespace, capsule_id));
      CREATE TABLE capsule_store_artifacts (namespace TEXT NOT NULL, capsule_id TEXT NOT NULL, name TEXT NOT NULL,
      digest_field TEXT NOT NULL, content_bytes BLOB NULL, content_sha256 TEXT NOT NULL, retention_state TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), purged_at TEXT NULL,
      PRIMARY KEY (namespace, capsule_id, name));`);
    const store = new SqliteArtifactStore(db, "older", [trusted]);
    await store.put(record);
    await store.initIndex(["operator"]);
    expect(
      (await store.find({ where: { operator: "test-operator" } })).ids,
    ).toEqual([record.capsuleId]);

    db.exec(`UPDATE capsule_store_capsules SET capsule_bytes=X'7B'`);
    await store.reindex(["operator"]);
    expect((await store.find()).ids).toEqual([]);
    db.exec(`UPDATE capsule_store_index_paths SET paths='{'`);
    await expect(store.find()).rejects.toMatchObject({ code: "corrupt" });
    await expect(store.initIndex(["a", "a"])).rejects.toMatchObject({
      code: "invalid",
    });
    db.close();
  });
});
