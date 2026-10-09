import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const consumer = mkdtempSync(join(tmpdir(), "emit-packed-consumer-"));
const run = (command, args, cwd = consumer) =>
  execFileSync(command, args, { cwd, stdio: "pipe" }).toString();
try {
  const [pack] = JSON.parse(
    run("npm", ["pack", "--json", "--pack-destination", consumer], root),
  );
  const paths = pack.files.map((file) => file.path);
  assert(paths.includes("LICENSE"));
  assert(!paths.some((path) => /^(src|test|node_modules)\//.test(path)));
  writeFileSync(
    join(consumer, "package.json"),
    '{"private":true,"type":"module"}',
  );
  run("npm", [
    "install",
    "--ignore-scripts",
    "--omit=optional",
    join(consumer, pack.filename),
  ]);
  writeFileSync(
    join(consumer, "core.mjs"),
    `import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
for (const driver of ['mysql2','better-sqlite3','@types/better-sqlite3']) assert.throws(() => require.resolve(driver), {code:'MODULE_NOT_FOUND'});
import { build, verifyCapsule } from '@action-state-group/capsule-emit';
import { jcs } from '@action-state-group/capsule-emit/aac';
import * as artifact from '@action-state-group/capsule-emit/artifact';
import * as jsonl from '@action-state-group/capsule-emit/artifact/jsonl';
const built = await build({actionId:'packed', actionType:'fyi', operator:'operator', developer:'developer', timestamp:'2026-09-09T00:00:00Z'});
assert.equal((await verifyCapsule(built.json)).capsuleId, built.capsuleId);
assert.equal(typeof jcs, 'function');
assert(Object.keys(artifact).length && Object.keys(jsonl).length);
`,
  );
  run(process.execPath, ["core.mjs"]);
  const metadata = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
  run("npm", [
    "install",
    "--save-exact",
    `better-sqlite3@${metadata.devDependencies["better-sqlite3"]}`,
    `mysql2@${metadata.devDependencies.mysql2}`,
  ]);
  writeFileSync(
    join(consumer, "exports.mjs"),
    `import assert from 'node:assert/strict';
const paths = ${JSON.stringify(Object.keys(metadata.exports))};
assert.equal(paths.length, 6);
for (const path of paths) {
 const module = await import('@action-state-group/capsule-emit' + (path === '.' ? '' : path.slice(1)));
 assert(Object.keys(module).length, path);
}
`,
  );
  run(process.execPath, ["exports.mjs"]);
  console.log(
    "Packed consumer: six exports load; core works without storage drivers.",
  );
} finally {
  rmSync(consumer, { recursive: true, force: true });
}
