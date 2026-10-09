import { describe, expect, it } from "vitest";
import { isV4IrreversibilityClass } from "../src/index.js";

describe("isV4IrreversibilityClass", () => {
  it.each([
    "two_way",
    "one_way_recoverable",
    "one_way_consequential",
    "one_way_terminal",
  ])("accepts the draft-04 seed %s", (value) => {
    expect(isV4IrreversibilityClass(value)).toBe(true);
  });

  it("rejects an extension or misspelling", () => {
    expect(isV4IrreversibilityClass("one_way_consequental")).toBe(false);
    expect(isV4IrreversibilityClass("future_extension")).toBe(false);
  });
});
