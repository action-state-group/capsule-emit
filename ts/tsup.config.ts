import { defineConfig } from "tsup";

export default defineConfig({
  entry: {
    index: "src/index.ts",
    "aac/index": "src/aac/index.ts",
    "artifact/index": "src/artifact/index.ts",
    "artifact/sqlite": "src/artifact/sqlite.ts",
    "artifact/mysql": "src/artifact/mysql.ts",
    "artifact/jsonl": "src/artifact/jsonl.ts",
  },
  external: ["@action-state-group/agent-action-capsule"],
  format: ["esm"],
  dts: false,
  sourcemap: true,
  clean: true,
  target: "node24",
});
