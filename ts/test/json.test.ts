import { describe, expect, it } from "vitest";
import { decodeStrictJson, jcs } from "../src/aac/index.js";

describe("strict JSON and JCS", () => {
  it.each(['{"n":1e2}', '{"n":100.0}', '{"n":01}', '{"n":1,"n":2}'])(
    "rejects nonconforming raw digest input %s",
    (input) => {
      expect(() => jcs(decodeStrictJson(input))).toThrow();
    },
  );

  it("keeps JCS negative-zero normalization separate from optional input verification", () => {
    expect(jcs(decodeStrictJson('{"n":-0}'))).toEqual(
      jcs(decodeStrictJson('{"n":0}')),
    );
  });
  it("preserves literal backslash-slash and backslash-u text", () => {
    expect(new TextDecoder().decode(jcs({ x: "a\\/b", y: "\\u00AB" }))).toBe(
      '{"x":"a\\\\/b","y":"\\\\u00AB"}',
    );
  });

  it.each(["\u000b", "\u000c", "\u00a0", "\u2028", "\ufeff"])(
    "rejects non-JSON whitespace U+%s",
    (whitespace) => {
      expect(() => decodeStrictJson(`{"a":${whitespace}1}`)).toThrow();
    },
  );

  it.each([" ", "\t", "\n", "\r"])(
    "accepts RFC 8259 whitespace %j",
    (whitespace) => {
      expect(decodeStrictJson(`{"a":${whitespace}1}`)).toEqual({
        a: expect.objectContaining({ raw: "1" }),
      });
    },
  );
});
