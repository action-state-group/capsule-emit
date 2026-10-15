// The TypeScript adapter of the cross-language artifact store interlock
// (.github/scripts/artifact-store-interlock.sh). It runs one phase of
// test-vectors/artifact-store/interlock.json against one store, built from
// dist/ (run `npm run build` first), and prints one report line per step;
// every language's adapter must print the same bytes.
//
//   node scripts/artifact-interlock.mjs <write|read> <jsonl|sqlite|mysql> <target> <namespace> <vectors-dir>
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { ArtifactError, storageChecksum } from "../dist/artifact/index.js";
import {
  decodeRecord,
  encodeRecord,
  JsonlArtifactStore,
} from "../dist/artifact/jsonl.js";

const [phase, format, target, namespace, dir] = process.argv.slice(2);
const plan = JSON.parse(readFileSync(join(dir, "interlock.json"), "utf8"));
const vectors = JSON.parse(readFileSync(join(dir, "vectors.json"), "utf8"));
const keys = plan.trusted.map(
  (name) => new Uint8Array(Buffer.from(vectors.keys[name], "hex")),
);

let store;
let close = async () => {};
if (format === "jsonl") {
  store = new JsonlArtifactStore(target, namespace, keys);
} else if (format === "sqlite") {
  const { default: Database } = await import("better-sqlite3");
  const { SqliteArtifactStore } = await import("../dist/artifact/sqlite.js");
  const db = new Database(target);
  db.pragma("foreign_keys = ON");
  store = new SqliteArtifactStore(db, namespace, keys);
  close = async () => db.close();
} else if (format === "mysql") {
  const { default: mysql } = await import("mysql2/promise");
  const { MysqlArtifactStore } = await import("../dist/artifact/mysql.js");
  const pool = mysql.createPool(`mysql://root@${target}/capsule_storage_test`);
  store = new MysqlArtifactStore(pool, namespace, keys);
  close = async () => pool.end();
} else {
  throw new Error(`unknown format ${format}`);
}

async function outcome(call) {
  try {
    return ["ok", await call()];
  } catch (error) {
    if (error instanceof ArtifactError) return [error.code, undefined];
    process.stderr.write(`storage error: ${String(error)}\n`);
    return ["storage_error", undefined];
  }
}

if (phase === "write") await store.init();
else if (phase !== "read") throw new Error(`unknown phase ${phase}`);
const steps = phase === "write" ? plan.write : plan.read;
const lines = [];
for (const [i, step] of steps.entries()) {
  const id = step.id ?? vectors.records[step.record]?.capsule_id ?? "";
  const subject = step.id !== undefined ? JSON.stringify(step.id) : step.record;
  let line = `${i}\t${step.op} ${subject}\t`;
  if (step.op === "put") {
    const record = decodeRecord(JSON.stringify(vectors.records[step.record]));
    line += (await outcome(() => store.put(record)))[0];
  } else if (step.op === "purge") {
    line += (await outcome(() => store.purge(id)))[0];
  } else if (step.op === "get") {
    const [code, got] = await outcome(() => store.get(id));
    line += code;
    if (got !== undefined) {
      const sorted = {
        ...got,
        artifacts: [...got.artifacts].sort((a, b) =>
          a.name < b.name ? -1 : a.name > b.name ? 1 : 0,
        ),
      };
      line += `\t${storageChecksum(got)}\t${encodeRecord(sorted)}`;
    }
  } else {
    throw new Error(`unknown op ${step.op}`);
  }
  lines.push(line);
}
await close();
process.stdout.write(lines.map((l) => `${l}\n`).join(""));
