# SPDX-License-Identifier: Apache-2.0
"""Number-rule and canonicalization identifier for digest-bearing capsule fields.

``CANONICALIZATION_ID`` — the profile default identifier recorded by the
core emission path (``seal()``/``received()`` via
``capsule_emit.core._emit_capsule``, which delegates to
``agent_action_capsule.emit()``) at the top-level ``canonicalization_id``
field (the self-describing binding slot, inside the signed payload). It
names the digest algorithm used to compute ``capsule_id``: RFC 8785 plain
JCS (no absent-field normalization); SHA-256; lowercase hex. Registered in
the CPB Payload Canonicalization Algorithm Registry
(draft-mih-sokolov-scitt-payload-binding).

Canonicalization FOLLOWS ``format_version`` — it is not a single global
default. ``agent_action_capsule.emit()`` builds only ``format_version``
``"4"`` (draft-04) and REQUIRES ``canonicalization_id="jcs"`` (§5.1); since
the core path always delegates to that ``emit()``, this constant is pinned
to ``"jcs"``. The vintage ``format_version`` ``"2"`` profile
(``"jcs-n"``, JCS + absent-field normalization) is a *separate* value —
``capsule_emit.canonicalization.VINTAGE_CANONICALIZATION_ID`` — used only by
the format-2 vintage path (``capsule_emit/holds/capsules.py``, which builds
an ``agent_action_capsule.Capsule`` directly and MUST NOT declare
``canonicalization_id`` at all per §5.1). Do not repoint this constant to
``"jcs-n"``, and do not use it as a default for verifying format-2 records.

**draft-04 reversal (2026-08-24, [capsule-cose-sign1]):** the profile default
moved from ``jcs-n`` to ``jcs`` — plain JCS excluding only ``capsule_id``, so
``chain`` (parent_capsule_id/relation) is now committed into the preimage
too, closing the prior unauthenticated-chain gap. ``jcs-n`` (absent-field
normalization, chain excluded) is verification-only from here on, for
records minted before this reversal.

This constant is the single declaration point for the core-path profile's
algorithm. When the profile revs again, the change is one edit here;
sealing callers (seal()/received()) need no update because the value
is wired through as a parameter default, not hardcoded at each call site.

Wire rule (normative in CPB — already enforced by agent_action_capsule.canonical):
  A JSON number token in a digest-bearing field MUST match ``0|-?[1-9][0-9]*``
  with value within ±(2^53 − 1). Floating-point tokens, leading zeros, and
  ``-0`` are rejected with FloatInDigestError or UnsafeIntegerError. Non-integer
  numeric quantities travel as JSON strings.

Conversion rule (normative for producers holding binary floats):
  Convert per RFC 8785 §3.2.2.3 — ECMA-262 §7.1.12.1 Number::toString from
  binary64.  NaN and ±Infinity raise FloatInDigestError naming the field.
  −0.0 serializes as ``"0"`` per RFC 8785 Appendix B. Narrower binary widths
  (e.g. numpy float32) MUST be widened to binary64 first: ``float(x)`` is
  lossless for f32→f64.

ONE RULE, ONE PLACE — this is the single implementation for capsule-emit and
adapters that import it (capsule_sidecar, et al.). Do not build a second.
Rust side: ``ryu-js`` 1.0.3 (boa-dev), ECMAScript-compliant.
"""
from __future__ import annotations

import math

from agent_action_capsule.canonical import FloatInDigestError

#: Identifier recorded in the self-describing binding slot (``canonicalization_id``
#: at the top level of every core-path emitted capsule, inside the signed payload).
#: Core-path default: ``jcs`` (plain RFC 8785 JCS, excluding only capsule_id —
#: chain is committed; SHA-256) — REQUIRED by ``agent_action_capsule.emit()``'s
#: ``format_version`` ``"4"`` (§5.1). The format-2 vintage profile (``jcs-n``,
#: absent-field normalization, chain excluded) is a separate constant, used
#: only for verifying pre-reversal records — see
#: :data:`capsule_emit.canonicalization.VINTAGE_CANONICALIZATION_ID`.
#: When the profile revs again: change this constant only — the internal
#: primitive accepts it as a parameter default, so all call sites (via
#: seal()/received()) update automatically.
CANONICALIZATION_ID: str = "jcs"

__all__ = ["CANONICALIZATION_ID", "float_to_str"]


def float_to_str(v: float, *, field: str = "") -> str:
    """Serialize a binary64 float per RFC 8785 §3.2.2.3 (ECMA-262 §7.1.12.1).

    The returned string is the canonical decimal: an integer string for
    whole-number values (``42.0`` → ``"42"``), a decimal string for fractional
    values (``1.5`` → ``"1.5"``).  Callers MUST embed the result as a JSON
    **string** value (i.e. quoted), not a raw JSON number token, in any
    digest-bearing field — unless the result is a plain integer string AND the
    field explicitly accepts integer tokens.

    Retires both ``repr()`` (Python-specific, diverges from ES6 on ordinary
    values: ``repr(42.0)`` → ``'42.0'`` vs ES6 → ``'42'``) and
    ``f"{x:.3f}"`` (fixed width — a different convention, not a rule).

    Args:
        v:     Binary64 float.  Pass ``float(x)`` for narrower widths
               (numpy float32, etc.) — f32→f64 widening is lossless.
        field: Optional field name, used only in error messages.

    Returns:
        RFC 8785 §3.2.2.3 decimal string.

    Raises:
        FloatInDigestError: If *v* is NaN or ±Infinity.

    Examples::

        >>> float_to_str(42.0)       # integer-valued float
        '42'
        >>> float_to_str(1.5)
        '1.5'
        >>> float_to_str(-0.0)       # RFC 8785 Appendix B
        '0'
        >>> float_to_str(1e-5)       # ES6 format, not Python repr
        '0.00001'
        >>> float_to_str(1.5e20)     # within 21-digit integer range
        '150000000000000000000'
        >>> float_to_str(1.5e21)     # beyond — scientific notation
        '1.5e+21'
    """
    # RFC 8785 Appendix B: −0.0 → "0"  (also handles +0.0)
    if v == 0.0:
        return "0"

    _hint = f" (field: {field!r})" if field else ""
    if math.isnan(v):
        raise FloatInDigestError(
            f"NaN cannot appear in a digest-bearing field{_hint}; "
            "represent absent measurements as absent fields"
        )
    if math.isinf(v):
        raise FloatInDigestError(
            f"Infinity cannot appear in a digest-bearing field{_hint}"
        )

    negative = v < 0.0
    if negative:
        v = -v

    # Python 3.1+ repr() computes the shortest decimal string that round-trips
    # binary64 — the same digit set as ECMA-262 §7.1.12.1 step 5.  We
    # reformat the output to match ES6's presentation rules (steps 6–10),
    # which differ on formatting but not on which digits are chosen.
    r = repr(v)

    # Parse repr to (digits, n) where  value = int(digits) × 10^(n − k),
    # k = len(digits) = number of significant digits.
    if "e" in r:
        mant, exp_str = r.split("e")
        n_adj = int(exp_str)
        if "." in mant:
            dot = mant.index(".")
            digits = mant[:dot] + mant[dot + 1 :]
            n = n_adj + dot         # digits before decimal point in scientific mant
        else:
            digits = mant
            n = n_adj + len(digits)
    elif "." in r:
        dot = r.index(".")
        digits = r[:dot] + r[dot + 1 :]
        n = dot                     # digits before decimal point
    else:
        digits = r
        n = len(digits)

    # Strip trailing zeros (whole-number placeholders, e.g. "42.0" → digits "420").
    digits = digits.rstrip("0")
    # Strip leading zeros (sub-1 values produce them, e.g. "0.01" → raw "001" → "1"),
    # adjusting n so the value is unchanged: each leading zero removed shifts n left.
    before = len(digits)
    digits = digits.lstrip("0")
    n -= before - len(digits)

    if not digits:
        return "0"  # shouldn't reach here for non-zero v, but be safe

    k = len(digits)

    # ECMA-262 §7.1.12.1 steps 6–10
    if k <= n <= 21:
        # Integer: digits followed by (n − k) zeros.
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        # Fixed-point: insert decimal point after the n-th digit.
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        # Small fraction: "0." followed by −n leading zeros then digits.
        body = "0." + "0" * (-n) + digits
    elif k == 1:
        # Scientific, single significant digit: "de±exp"
        exp = n - 1
        body = digits[0] + ("e+" + str(exp) if exp >= 0 else "e" + str(exp))
    else:
        # Scientific, multiple significant digits: "d.reste±exp"
        exp = n - 1
        body = digits[0] + "." + digits[1:] + ("e+" + str(exp) if exp >= 0 else "e" + str(exp))

    return "-" + body if negative else body
