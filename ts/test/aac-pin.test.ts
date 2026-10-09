import { execFileSync } from "node:child_process";
import { realpathSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { AAC_COMMIT } from "./aac-pin.js";

const root = resolve(process.env.AAC_ROOT ?? "../agent-action-capsule");
const git = (...args: string[]): string =>
  execFileSync("git", ["-C", root, ...args], { encoding: "utf8" }).trim();

describe("pinned AAC corpus", () => {
  it("AAC_ROOT is agent-action-capsule at AAC_COMMIT, unmodified", () => {
    let head: string;
    try {
      // the top level, not a repository that merely contains AAC_ROOT
      if (git("rev-parse", "--show-toplevel") !== realpathSync(root))
        throw new Error("not the top level");
      head = git("rev-parse", "HEAD");
    } catch {
      throw new Error(
        `${root} is not a git checkout; check out agent-action-capsule ${AAC_COMMIT} there, or set AAC_ROOT to one`,
      );
    }
    expect(
      head,
      `AAC_ROOT is at ${head}, not the pinned ${AAC_COMMIT}: check that commit out, or bump AAC_COMMIT (test/aac-pin.ts)`,
    ).toBe(AAC_COMMIT);
    expect(
      git("status", "--porcelain", "--untracked-files=no"),
      "AAC_ROOT has modified tracked files; the corpus must be the pinned commit's",
    ).toBe("");
  });
});
