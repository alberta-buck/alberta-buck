#!/usr/bin/env python3
"""Adapt a STOCK snarkjs Solidity verifier to this repo's calldata convention.

We use snarkjs as published.  Nothing here is a bug fix and nothing upstream
needs changing -- snarkjs is EIP-197 correct end to end, and its own
`zkey export soliditycalldata` output verifies against its own exported
verifier on-chain.  What this script does is bend that verifier to accept the
calldata WE produce, which is packed differently.

THE TWO CONVENTIONS

The bn256Pairing precompile requires G2 points as [x_im, x_re, y_im, y_re].
snarkjs satisfies that by splitting the work: the verifier embeds its VK
constants im-first, and expects the CALLER to hand it `_pB` already in
EIP-197 order.  `exportSolidityCallData` performs that swap.  So:

    stock verifier   <->  caller swaps pi_b       (snarkjs's ABI)
    patched verifier <->  caller packs pi_b as-is (this script)

Both are self-consistent and both verify on-chain.  They are not
interchangeable: pairing a stock verifier with unswapped calldata, or a
patched verifier with swapped calldata, yields an off-curve point and every
proof fails.

WHY WE PATCH RATHER THAN SWAP AT THE CALLER

Our Python and shell fixture generators pack straight out of proof.json --
`pi_b[0][0], pi_b[0][1], pi_b[1][0], pi_b[1][1]` -- because remembering to
swap at every one of those call sites is the kind of thing that gets missed
once and then debugged for a day.  Moving the swap into the verifier makes it
a single, testable place.

Note this repo runs BOTH conventions on purpose:

    scripts/snark/prove_*.js   swap caller-side  -> use STOCK verifiers
    scripts/snark/*.sh, *.py   pack natural      -> use PATCHED verifiers

Do not "unify" them without regenerating every dependent proof vector; see
doc/snark-regeneration.org.

USAGE

    python3 fix_verifier_g2.py --b-only <verifier.sol>

--b-only swaps ONLY the proof-B component in the assembly, which is what
snarkjs 0.7.5+ needs: it already emits the VK constants in EIP-197 order, so
touching those swaps correct values a second time and corrupts the verifier.
Always pass --b-only.  The flagless mode exists for verifiers exported by
much older snarkjs that emitted VK constants real-first; it is almost
certainly not what you want.

Apply EXACTLY ONCE per verifier.  The swap is an involution, so a second
application silently undoes the first.
"""
import sys, re

b_only = "--b-only" in sys.argv
args = [a for a in sys.argv[1:] if a != "--b-only"]
sys.argv = [sys.argv[0]] + args

with open(sys.argv[1]) as f:
    sol = f.read()

changes = 0

# 1. Swap VK constant pairs using temporary placeholder
for prefix in ([] if b_only else ['beta', 'gamma', 'delta']):
    for xy in ['x', 'y']:
        p1 = rf'uint256 constant {prefix}{xy}1\s*=\s*(\d+);'
        p2 = rf'uint256 constant {prefix}{xy}2\s*=\s*(\d+);'
        m1 = re.search(p1, sol)
        m2 = re.search(p2, sol)
        if m1 and m2:
            v1, v2 = m1.group(1), m2.group(1)
            # Three-step swap: 1→TMP, 2→1, TMP→2
            sol = sol.replace(m1.group(0), f'uint256 constant {prefix}{xy}1_TMP = {v1};', 1)
            sol = sol.replace(m2.group(0), f'uint256 constant {prefix}{xy}2 = {v1};', 1)
            sol = sol.replace(f'uint256 constant {prefix}{xy}1_TMP = {v1};', f'uint256 constant {prefix}{xy}1 = {v2};', 1)
            changes += 1

# 2. Fix B component in assembly: swap x_re↔x_im, y_re↔y_im
# The snarkjs assembly stores B as [b[0][0], b[0][1], b[1][0], b[1][1]]
# We need EIP-197: [b[0][1], b[0][0], b[1][1], b[1][0]]
old = (
    'mstore(add(_pPairing, 64), calldataload(pB))\n'
    '                mstore(add(_pPairing, 96), calldataload(add(pB, 32)))\n'
    '                mstore(add(_pPairing, 128), calldataload(add(pB, 64)))\n'
    '                mstore(add(_pPairing, 160), calldataload(add(pB, 96)))'
)
new = (
    'mstore(add(_pPairing, 64), calldataload(add(pB, 32)))\n'
    '                mstore(add(_pPairing, 96), calldataload(pB))\n'
    '                mstore(add(_pPairing, 128), calldataload(add(pB, 96)))\n'
    '                mstore(add(_pPairing, 160), calldataload(add(pB, 64)))'
)
if old in sol:
    sol = sol.replace(old, new)
    changes += 1
else:
    # Try with different leading whitespace
    for indent in ['                ', '            ', '        ', '    ']:
        o = old.replace('                ', indent)
        n = new.replace('                ', indent)
        if o in sol:
            sol = sol.replace(o, n)
            changes += 1
            break

with open(sys.argv[1], 'w') as f:
    f.write(sol)

mode = "B swap only" if b_only else "VK pairs + B swap"
print(f"Fixed {sys.argv[1]}: {changes} changes applied ({mode}) for EIP-197")
