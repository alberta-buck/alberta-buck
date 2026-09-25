#!/usr/bin/env python3
"""Rewrite a stock snarkjs Groth16 verifier to walk a code-resident IC table.

snarkjs unrolls one inlined G1 multiply-accumulate per public input, each call
site carrying its two 32-byte IC coordinates as immediates -- about 166 bytes of
runtime code per input.  Past ~140 public inputs that crosses EIP-170's
24,576-byte runtime limit (mint_batch_a2 at N=32: 228 inputs, 38,805 bytes), and
the verifier cannot be deployed on any chain that enforces it.

This rewrite changes only that: IC1..ICn move into one `bytes constant` table,
copied to memory once, and the two unrolled sequences -- the field checks and the
linear combination -- become loops over it.  The verification key, the pairing
check, every public input and every proof are unchanged, so no circuit, setup or
caller changes.  The cost is ~45k gas per verify at N=32 (the table copy, and the
loop bookkeeping), on ~1.7M.

A verifier with at most --over public inputs is left byte-for-byte stock, the
repository's convention (doc/snark-regeneration.org).  A rewritten one keeps its
stock original at --reference, renamed <Contract>Stock, so
test/VerifierTable.t.sol can hold the two to identical verdicts.

Usage:  table_verifier.py --over 128 --reference test/reference/X.sol src/X.sol
"""

import argparse
import re
import sys
from pathlib import Path

CONST_IC                        = re.compile(r"\s*uint256 constant IC[1-9]\d*[xy] = \d+;$")
CALL_MULACC                     = re.compile(r"(\s*g1_mulAccC\(_pVk, IC\d+x, IC\d+y, "
                                             r"calldataload\(add\(pubSignals, \d+\)\)\))+\s*")
CALL_CHECK                      = re.compile(r"(\n\s*checkField\(calldataload\(add\(_pubSignals, \d+\)\)\))+")


def public_inputs(src: str) -> int:
    m = re.search(r"uint\[(\d+)\] calldata _pubSignals", src)
    if not m:
        raise SystemExit("not a snarkjs Groth16 verifier: no _pubSignals parameter")
    return int(m.group(1))


def tablify(src: str) -> str:
    consts = dict(re.findall(r"uint256 constant (IC\d+[xy]) = (\d+);", src))
    n = public_inputs(src)
    if f"IC{n}x" not in consts or f"IC{n + 1}x" in consts:
        raise SystemExit(f"IC constants do not match {n} public inputs")
    table = "".join(f"{int(consts[f'IC{i}x']):064x}{int(consts[f'IC{i}y']):064x}"
                    for i in range(1, n + 1))

    out = "\n".join(line for line in src.split("\n") if not CONST_IC.match(line))
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = out.replace(
        "    uint256 constant IC0y",
        f"    // IC1..IC{n}: (x, y) big-endian word pairs, walked in order by checkPairing.\n"
        f"    // Rewritten from the stock snarkjs verifier by scripts/snark/table_verifier.py\n"
        f"    // to fit EIP-170; the verification key is unchanged.\n"
        f'    bytes constant IC_TABLE = hex"{table}";\n\n    uint256 constant IC0y', 1)

    out, k = CALL_MULACC.subn(
        f"""
                for {{ let i := 0 }} lt(i, {n}) {{ i := add(i, 1) }} {{
                    let e := add(add(pIc, 32), mul(i, 64))
                    g1_mulAccC(_pVk, mload(e), mload(add(e, 32)), calldataload(add(pubSignals, mul(i, 32))))
                }}

                """, out, count=1)
    assert k == 1, "linear combination not found"
    out, k = CALL_CHECK.subn(
        f"""
            for {{ let i := 0 }} lt(i, {n}) {{ i := add(i, 1) }} {{
                checkField(calldataload(add(_pubSignals, mul(i, 32))))
            }}""", out, count=1)
    assert k == 1, "field checks not found"

    # Yul functions see only their arguments, so the table's memory copy is passed in.
    for a, b in (("function checkPairing(pA, pB, pC, pubSignals, pMem) -> isOk {",
                  "function checkPairing(pA, pB, pC, pubSignals, pMem, pIc) -> isOk {"),
                 ("let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)",
                  "let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem, ic)"),
                 ("public view returns (bool) {\n        assembly {",
                  "public view returns (bool) {\n        bytes memory ic = IC_TABLE;\n        assembly {")):
        assert out.count(a) == 1, f"template changed: {a!r}"
        out = out.replace(a, b)

    assert "IC1x" not in out
    assert out.count("g1_mulAccC(_pVk") == 1 and out.count("checkField(calldataload") == 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("verifier", type=Path, help="stock snarkjs verifier, rewritten in place")
    ap.add_argument("--over", type=int, required=True,
                    help="rewrite only when the public inputs exceed this")
    ap.add_argument("--reference", type=Path, required=True,
                    help="where the stock original is kept when rewritten")
    args = ap.parse_args()

    src = args.verifier.read_text(encoding="utf-8")
    n = public_inputs(src)
    if n <= args.over:
        if args.reference.exists():                 # a pin that shrank below the threshold
            args.reference.unlink()
        print(f"table_verifier: {args.verifier.name}: {n} public inputs, stock kept")
        return 0

    name = re.search(r"^contract (\w+) \{", src, re.M).group(1)
    args.reference.parent.mkdir(parents=True, exist_ok=True)
    args.reference.write_text(src.replace(f"contract {name} {{", f"contract {name}Stock {{", 1),
                              encoding="utf-8")
    args.verifier.write_text(tablify(src), encoding="utf-8")
    print(f"table_verifier: {args.verifier.name}: {n} public inputs, rewritten to an IC table; "
          f"stock kept at {args.reference}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
