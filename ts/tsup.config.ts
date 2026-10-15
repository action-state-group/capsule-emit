import { defineConfig } from "tsup";

export default defineConfig({
  entry: {
    index: "src/index.ts",
    "aac/index": "src/aac/index.ts",
    "artifact/index": "src/artifact/index.ts",
    "artifact/sqlite": "src/artifact/sqlite.ts",
    "artifact/mysql": "src/artifact/mysql.ts",
    "artifact/jsonl": "src/artifact/jsonl.ts",
    "artifact/memory": "src/artifact/memory.ts",
    "artifact/conformance": "src/artifact/conformance.ts",
  },
  external: ["@action-state-group/agent-action-capsule"],
  format: ["esm"],
  dts: false,
  sourcemap: true,
  clean: true,
  target: "node24",
});
