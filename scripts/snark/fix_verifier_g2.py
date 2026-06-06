#!/usr/bin/env python3
"""Fix snarkjs-generated Solidity verifier: apply EIP-197 G2 encoding swap.

snarkjs stores G2 as [x_re, x_im, y_re, y_im] but EIP-197 expects
[x_im, x_re, y_im, y_re].  This script swaps the embedded VK constants
(beta, gamma, delta) and fixes the B component in the assembly.

Usage: python scripts/snark/fix_verifier_g2.py <verifier.sol>
"""
import sys, re

with open(sys.argv[1]) as f:
    sol = f.read()

changes = 0

# 1. Swap VK constant pairs using temporary placeholder
for prefix in ['beta', 'gamma', 'delta']:
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

print(f"Fixed {sys.argv[1]}: {changes} changes applied (VK pairs + B swap) for EIP-197")
