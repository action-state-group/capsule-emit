# capsule-emit-ts

TypeScript-native AAC format-4 producer and verifier. The package is ESM-first,
uses strict TypeScript, preserves signer-independent Capsule IDs, and emits the
same attached-payload COSE Producer Envelopes as `capsule-emit-go`.

The root entry point builds records only. It does not execute actions, generate
business IDs or timestamps, persist Capsules, retry effects, contact witnesses,
or authorize signers. Optional verified persistence is available behind the
[`./artifact`](#artifact-storage) subpath.

## Install

Node.js 22 or newer is required.

```sh
npm install @action-state-group/capsule-emit
```

## Build, sign, and verify

Digest-dependent production and verification APIs are async. Await `build`,
`received`, `carry`, `buildComposition`, `digestJSON`, `seal`, `sign`,
`verifyCapsule`, `verifyEnvelope`, and artifact `verify`. Decoding, JCS bytes,
slot wrappers, identity constructors and raw `signCapsuleId` remain synchronous.
The compatibility `./aac` entry delegates to AAC's authoritative async core;
there is no private verifier or registry snapshot.

`seal` is the recommended application-facing API. It digests caller-owned JSON,
builds and verifies the format-4 Capsule, then signs its raw 32-byte Capsule ID
with an independent Producer Envelope.

```ts
import { randomBytes } from "node:crypto";
import {
  createEd25519Identity,
  seal,
  verifyCapsule,
  verifyEnvelope,
} from "@action-state-group/capsule-emit";

const identity = createEd25519Identity(randomBytes(32));
const result = await seal({
  capsule: {
    actionId: "example/1",
    actionType: "decide",
    operator: "example-org",
    developer: "example-agent@v1",
    timestamp: new Date("2026-09-02T12:00:00Z"),
    disposition: {
      decision: "accept",
      approver: "policy",
      humanDisposed: false,
      verdictClass: "executed",
    },
  },
  payload: { task: "publish", issue: 123 },
  agentOutput: { accepted: true },
  model: { provider: "example", modelId: "model-v1" },
  runtime: "example-runtime@1",
  identity,
});

const capsule = await verifyCapsule(result.payload);
if (capsule.capsuleId !== result.capsuleId) {
  throw new Error("Capsule ID mismatch");
}
const envelope = await verifyEnvelope(result.capsuleId, result.envelope);
if (!envelope.ok) {
  throw new Error(
    `Producer Envelope failed: ${JSON.stringify(envelope.findings)}`,
  );
}
if (
  !envelope.publicKey ||
  !Buffer.from(envelope.publicKey).equals(Buffer.from(identity.publicKey))
) {
  throw new Error("Producer Envelope signer is not authorized");
}
```

`verifyCapsule` validates Capsule identity and Class 1 structure. It does not
authenticate local-only `signature` or `key_id` fields. `verifyEnvelope`
authenticates the public key carried by the Producer Envelope. Whether that key
is authorized for an operator, developer, or action remains caller policy.

## Typed construction

Use `build` when the application already owns all typed Capsule fields and wants
construction separate from signing. `received` binds exact opaque bytes under a
caller-declared CPB type. `carry` is the same operation with the generic
`foreign-artifact` type. `who`, `can`, `did`, and `audit` reference already-built
Capsules in a typed composition without minting or persisting those members.

```ts
import { randomBytes } from "node:crypto";
import {
  build,
  buildComposition,
  can,
  createEd25519Identity,
  did,
  received,
  seal,
  sign,
  who,
} from "@action-state-group/capsule-emit";

const identity = createEd25519Identity(randomBytes(32));
const common = {
  actionType: "fyi" as const,
  operator: "example-org",
  developer: "example-agent@v1",
  timestamp: "2026-09-02T12:00:00Z",
};

const identityCapsule = await build({
  ...common,
  actionId: "identity/1",
  domain: "identity",
});
const providerAck = await received(
  { ...common, actionId: "provider-ack/1" },
  new TextEncoder().encode("opaque provider acknowledgement"),
  "provider-ack",
);
const actionCapsule = await build({
  ...common,
  actionId: "action/1",
  effect: {
    type: "example.publish",
    status: "planned",
    irreversibilityClass: "two_way",
  },
});
const composition = await buildComposition(
  { ...common, actionId: "composition/1" },
  [who(identityCapsule), can(providerAck), did(actionCapsule)],
);
const envelope = await sign(composition, identity);

console.log(providerAck.capsuleId, composition.capsuleId, envelope.length);
```

The same composition can use the high-level signing path:

```ts
const signedComposition = await seal({
  capsule: { ...common, actionId: "composition/2" },
  members: [who(identityCapsule), can(providerAck), did(actionCapsule)],
  identity,
});
```

Composition members must occupy distinct slots and refer to distinct verified
format-4 Capsules. Carried and composed construction rejects explicit agent
input/output digests because those records already own their construction
commitments.

## Compose with CLL

This package constructs and verifies AAC records. It does not persist them.
Applications that also need checkpointed inclusion install the independent CLL
package, store the full Capsule and Producer Envelope in application storage,
and append only the verified 32-byte Capsule ID to CLL.

```ts
import { build, verifyCapsule } from "@action-state-group/capsule-emit";
import { MysqlStore } from "@action-state-group/cll/mysql";

const built = await build({
  actionId: "deploy-42",
  actionType: "fyi",
  operator: "example-org",
  developer: "example-agent@v1",
  timestamp: new Date(),
});
await verifyCapsule(built.json); // returns verified metadata or throws

// Persist built.json and any Producer Envelope in application storage.
const mysqlUrl = process.env.MYSQL_URL;
if (!mysqlUrl) throw new Error("MYSQL_URL is required");
const cll = await MysqlStore.open(mysqlUrl, "application-log");
try {
  await cll.append({
    value: Buffer.from(built.capsuleId, "hex"),
    appendedAt: new Date(),
  });
} finally {
  await cll.close();
}
```

Neither package depends on the other. An application that uses both declares
both dependencies explicitly.

`capsule-emit` creates no database tables. `MysqlStore.open()` and
`SqliteStore.open()` create only CLL's internal tables, documented in the
[`@action-state-group/cll` backend guide](https://github.com/action-state-group/cll-ts#sqlite-and-mysql-tables).
Full Capsules and Producer Envelopes remain in application-owned storage.

## JSON digests

`await digestJSON(value)` returns the lowercase SHA-256 of RFC 8785 JCS bytes. It
rejects duplicate object names, excessive depth, floats, unsafe integers,
invalid UTF-8, and trailing JSON data on strict decoding paths.

```ts
import { digestJSON } from "@action-state-group/capsule-emit";

const requestDigest = await digestJSON({ issue: 123, operation: "publish" });
const responseDigest = await digestJSON({ accepted: true });
```

Callers own the JSON shape and assign these values to effect request/response
fields where appropriate. Raw payload values never enter the Capsule.

## Verification and compatibility

The `@action-state-group/capsule-emit/aac` subpath exposes strict JSON decoding, current and
vintage Capsule-ID computation, Class 1 verification, and store verification
for persistence adapters. Top-level construction and verification remain
format-4-only.

`isV4IrreversibilityClass(value)` tests membership in the four
irreversibility-class values seeded by AAC draft-04. It deliberately returns
false for future registry extensions without claiming that an extension is
invalid.

Tests replay the complete upstream AAC corpus, all Producer Envelope vectors,
and Go/Python authored, received, WHO, DID, and composition fixtures.

The runtime dependency is pinned to
`@action-state-group/agent-action-capsule@0.1.0`. AAC remains external in the
build and carries its BSD-3-Clause license; this package remains Apache-2.0.
The AAC corpus is pinned by `AAC_COMMIT` in `test/aac-pin.ts` to the source of
that reviewed npm artifact. Tests require a clean AAC checkout at that exact
revision; CI and publication checks read the same pin. Bump the npm dependency
and corpus pin together after validating the new artifact and vectors.

## Cross-record references

`Input.references` accepts `{ type, digestAlg, digest, citationPurpose?,
logCoordinates? }`. Use the registered type `agent-action-capsule` with
`SHA-256` and its Capsule ID for an AAC citation. `acted_on` and `responds_to`
are seeded citation purposes; unknown purposes remain informational. References
cannot duplicate the same Capsule's chain parent. Foreign digest representations
belong to the referenced CPB type and are not restricted to AAC's hex encoding.

`logCoordinates` carries the wire members `log_id`, `leaf_index` and
`inclusion_proof` together as opaque claims. Class 1 does not authenticate the
proof or resolve external targets. Undefined references are omitted; `[]` is
preserved, including its effect on the format-4 Capsule ID.

```ts
import { build, verifyCapsule } from "@action-state-group/capsule-emit";

const common = {
  actionType: "fyi" as const,
  operator: "example-org",
  developer: "example-agent@v1",
  timestamp: "2026-09-02T12:00:00Z",
};
const request = await build({ ...common, actionId: "request/1" });
const response = await build({
  ...common,
  actionId: "response/1",
  references: [
    {
      type: "agent-action-capsule",
      digestAlg: "SHA-256",
      digest: request.capsuleId,
      citationPurpose: "responds_to",
    },
  ],
});
await verifyCapsule(response.json);
console.log(request.capsuleId, response.capsuleId);
```

References enter through `Input`, including `seal({ capsule: { ... } })`.
There is no separate reference builder or closed purpose enum.

## Artifact storage

The optional `./artifact` subpath persists exact sealed Capsules, Producer
Envelopes, and business originals. The root entry point stays storage-free:
`better-sqlite3` and `mysql2` are optional peer dependencies, and an application
installs only the backend it imports. TypeScript users of the SQLite backend
also install `@types/better-sqlite3` (an optional peer dependency), because
`better-sqlite3` ships no bundled type declarations; `mysql2` bundles its own. Reads verify Capsule identity, the
Producer Envelope against caller-owned trusted keys, and every retained bound
original before returning. The SQL backends additionally verify a stored
inventory checksum; the JSONL backend has none, so it verifies the retained
contents but not inventory completeness. Records are immutable,
byte-identical retries are idempotent, and a divergent write for the same
Capsule ID throws an `ArtifactError` with `code: "conflict"`.

```ts
import Database from "better-sqlite3";
import { PAYLOAD_DIGEST } from "@action-state-group/capsule-emit/artifact";
import { SqliteArtifactStore } from "@action-state-group/capsule-emit/artifact/sqlite";

const db = new Database("artifacts.db");
db.pragma("foreign_keys = ON");
const store = new SqliteArtifactStore(db, "my-namespace", [trustedPublicKey]);
await store.init(); // provision v1 tables once during deployment

await store.put({
  capsuleId: sealed.capsuleId,
  capsule: sealed.payload,
  producerEnvelope: sealed.envelope,
  artifacts: [
    {
      name: "payload",
      binding: PAYLOAD_DIGEST,
      content: payloadBytes,
      state: "present",
    },
  ],
});
const record = await store.get(sealed.capsuleId);
```

`./artifact/mysql` exposes the same API over a `mysql2` pool. The inventory
checksum is byte-compatible with `capsule-emit-go`, so a Go writer and a
TypeScript reader interoperate over a shared database. See
[DESIGN.md](DESIGN.md#artifact-storage) for the full contract.

`./artifact/jsonl` (`JsonlArtifactStore`) exposes the same
`init`/`put`/`get`/`purge` API over a single flat JSONL file and needs no peer
dependency — it uses Node's built-in `fs`. Each record is one line in the same
`snake_case` wire shape as the other backends, so its files are byte-compatible
with the `capsule-emit-go` `artifact/jsonl` store in either direction. `put`
appends a line and `get` seeks to an in-memory `capsuleId`-to-offset index built
on open; `purge` rewrites the file through a temp file and atomic rename. It
assumes a single writer and provides no file locking, no crash-atomicity beyond
that rename, and no transaction-join API. `init` creates the file, and `put`
requires it to already exist.

## Development

The embedded provisional registry snapshot mirrors
`agent-action-capsule/python/agent_action_capsule/data/cpb_provisional.json`.
Known provisional values change informational diagnostics without increasing
assurance. Refresh the snapshot from that source; the test suite checks its
provenance and content. Raw JCS still normalizes `-0` to `0`; Python's optional
strict input verification tier is a separate acceptance policy.

Shared Class 1, reference and Producer Envelope vectors live in the AAC source
checkout under `vectors/`; vocabulary vectors under `go/verify/testdata/`. The producer-to-CLL check is maintained in
`capsule-emit-go/scripts/check-producer-cll-interop.sh` and runs in both emitters'
CI. It covers in-memory append/checkpoint interoperability without witness I/O.

```sh
npm install
npm run check
npm run build
```

Interop tests expect `agent-action-capsule` and `capsule-emit-go` as sibling
checkouts. Override those paths with `AAC_ROOT` and `CAPSULE_EMIT_GO_ROOT`.

## Release

Releases are published from `main` with the manual
[Publish npm package](https://github.com/action-state-group/capsule-emit-ts/actions/workflows/publish.yml)
GitHub Action:

1. Update `version` in `package.json` and `package-lock.json`, commit the change,
   and wait for `main` CI to pass.
2. In GitHub, open the workflow, choose **Run workflow**, and select `main`.
3. Verify the workflow published `@action-state-group/capsule-emit` and created
   the `v<version>` GitHub release and tag on the published commit.

The npm package must have a GitHub Actions trusted publisher configured for
the `action-state-group/capsule-emit-ts` repository and
`.github/workflows/publish.yml`. No long-lived npm token is required. Re-running
the workflow is safe: it skips an existing npm version and verifies that its
Git tag points to the `gitHead` recorded by npm.

If npm contains the version but its tag is missing after this workflow has
changed, GitHub may reject recovery with the workflow's `GITHUB_TOKEN`. A
maintainer with `workflow` scope must create the tag at the npm `gitHead`, then
rerun the workflow to verify the tag and create any missing GitHub release:

```sh
version=0.1.2
git_head=$(npm view "@action-state-group/capsule-emit@$version" gitHead)
git fetch origin --tags
git tag -a "v$version" "$git_head" -m "Release v$version"
git push origin "refs/tags/v$version"
```

## License

Apache-2.0. The upstream Agent Action Capsule dependency is BSD-3-Clause.
