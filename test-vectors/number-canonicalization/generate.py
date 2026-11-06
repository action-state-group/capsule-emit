# Regenerate: python test-vectors/number-canonicalization/generate.py test-vectors/number-canonicalization/vectors.json
import json, sys
from agent_action_capsule.canonical import json_digest
from capsule_emit.numbers import canonicalize_for_digest

cases = [
 ("fraction", '{"temperature":0.5}'),
 ("integer_valued_float", '{"v":42.0}'),
 ("integer_token_preserved", '{"n":1,"x":1.0}'),
 ("exp_1e21", '{"v":1e21}'),
 ("exp_1e20_integer_layout", '{"v":1e20}'),
 ("small_1e-5", '{"v":1e-5}'),
 ("small_1e-7_scientific", '{"v":1e-7}'),
 ("negative_zero_float", '{"v":-0.0}'),
 ("negative_zero_integer", '{"v":-0}'),
 ("negative_fraction", '{"v":-1.25}'),
 ("float_that_is_not_exact", '{"v":0.1}'),
 ("max_binary64", '{"v":1.7976931348623157e308}'),
 ("min_subnormal", '{"v":5e-324}'),
 ("exponent_spelling_integer_valued", '{"v":1E3}'),
 ("nested", '{"a":[{"b":[0.5,{"c":2.5e-3}]},3,"x",true,null]}'),
 ("safe_integer_max", '{"v":9007199254740991}'),
 ("safe_integer_min", '{"v":-9007199254740991}'),
 ("bare_float", '0.75'),
 ("large_integer_float_form", '{"v":9007199254740993.0}'),
 ("large_integer_unsafe", '{"v":9007199254740992}'),
 ("large_integer_beyond_int64", '{"v":123456789012345678901234567890}'),
 ("float_overflow", '{"v":1e999}'),
]
out = []
for name, text in cases:
    try:
        digest = json_digest(canonicalize_for_digest(json.loads(text)))
        out.append({"name": name, "input_json": text, "digest": digest})
    except Exception as e:
        out.append({"name": name, "input_json": text, "error": True})
        print(name, type(e).__name__, file=sys.stderr)
doc = {
 "format_version": "1",
 "description": "Decoded-JSON numbers through canonicalize_for_digest (Python capsule_emit.numbers) and emit.CanonicalizeForDigest (Go), then the AAC JSON-DIGEST. 'error' cases have no digest representation and must fail in both.",
 "generator": "test-vectors/number-canonicalization/generate.py (Python reference)",
 "cases": out,
}
json.dump(doc, open(sys.argv[1], "w"), indent=2); open(sys.argv[1],"a").write("\n")
