#!/usr/bin/env python3
# SPDX-License-Identifier: CAL-1.0
"""Poseidon round constants and MDS matrices for BN254, generated from the
specification.

The constants this emits are numerically identical to circomlib's -- that is
the point, since the Alberta Buck circuits, the Solidity verifier, the Rust
kernel and the Python reference must all hash alike -- but they are DERIVED
here rather than copied, so nothing of circomlib's is redistributed.  See
NOTICE at the repository root.

Generation follows the Poseidon reference (`generate_parameters_grain.sage`
from the hadeshash distribution accompanying the paper): an 80-bit Grain
LFSR seeded with the parameter set, run as a self-shrinking generator.

    seed bits   field  2   1 = GF(p)
                sbox   4   0 = x^alpha
                n     12   254, the field size in bits
                t     12   state width
                R_F   10   8, full rounds
                R_P   10   partial rounds, per the table below
                pad   30   all ones
    discard the first 160 output bits
    feedback:   s[62] ^ s[51] ^ s[38] ^ s[23] ^ s[13] ^ s[0]
    output:     take bits in pairs; if the first is 1 emit the second,
                otherwise emit nothing (self-shrinking generator)

One subtlety decides whether the output matches: the two consumers sample
the same bit stream differently.

  * Round constants use REJECTION sampling -- draw 254 bits, discard the
    draw and redraw whenever the value is >= p.
  * The MDS x/y values REDUCE mod p instead.  In the Sage original they are
    written `F(grain_random_bits(n))`, and Sage's field constructor reduces;
    it does not reject.

The MDS is the Cauchy matrix M[i][j] = 1 / (x_i + y_j) over the 2t values
drawn immediately after the round constants, redrawn if they are not
distinct.  For every width 2..17 the first candidate is the accepted one.

Usage:

    python3 generate.py --write                 # rewrite both repo copies
    python3 generate.py --check                 # regenerate and compare
    python3 generate.py --check-against FILE    # compare with any file,
                                                #   e.g. circomlib's own
"""

import argparse
import json
import sys
from pathlib import Path

# BN254 scalar field.
P = 21888242871839275222246405745257275088548364400416034343698204186575808495617

N_BITS = 254
FIELD = 1                  # GF(p)
SBOX = 0                   # x^alpha
R_F = 8                    # full rounds

# Partial rounds by state width t = 2 .. 17 (Poseidon paper, Table 2, as
# used by circomlib).  len(C[t]) == t * (R_F + R_P[t]) is a consequence, and
# --check verifies it against the emitted file.
R_P = {
    2: 56,  3: 57,  4: 56,  5: 60,  6: 60,  7: 63,  8: 64,  9: 63,
    10: 60, 11: 66, 12: 60, 13: 65, 14: 70, 15: 60, 16: 64, 17: 68,
}

WIDTHS = range(2, 18)

# The two copies kept in the repository: the Rust kernel reads its own (a
# published crate must be self-contained), the Python reference reads the
# other.  make poseidon-constants keeps them equal.
REPO = Path(__file__).resolve().parents[4]
TARGETS = [
    REPO / "core/rust/buck-identity/constants/poseidon_constants.json",
    REPO / "alberta_buck/wallet/poseidon_constants.json",
]


class Grain:
    """The Grain LFSR of the Poseidon reference, as a self-shrinking generator."""

    def __init__(self, t, r_f, r_p, field=FIELD, sbox=SBOX, n=N_BITS):
        bits = []
        for value, width in ((field, 2), (sbox, 4), (n, 12),
                             (t, 12), (r_f, 10), (r_p, 10)):
            bits += [int(b) for b in bin(value)[2:].zfill(width)]
        bits += [1] * 30
        assert len(bits) == 80, len(bits)
        self.state = bits
        self.n = n
        for _ in range(160):
            self._step()

    def _step(self):
        s = self.state
        bit = s[62] ^ s[51] ^ s[38] ^ s[23] ^ s[13] ^ s[0]
        s.pop(0)
        s.append(bit)
        return bit

    def _bit(self):
        """Self-shrinking output: pairs of bits, emit the second when the
        first is 1."""
        while True:
            if self._step() == 1:
                return self._step()
            self._step()

    def word(self):
        """One n-bit draw, unreduced."""
        v = 0
        for _ in range(self.n):
            v = (v << 1) | self._bit()
        return v

    def rejected(self):
        """A field element by rejection sampling (the round constants)."""
        while True:
            v = self.word()
            if v < P:
                return v

    def reduced(self):
        """A field element by reduction (the MDS x/y values)."""
        return self.word() % P


def constants_for(t):
    """(round constants, MDS matrix) for state width t, as integers."""
    grain = Grain(t, R_F, R_P[t])

    c = [grain.rejected() for _ in range(t * (R_F + R_P[t]))]

    xy = [grain.reduced() for _ in range(2 * t)]
    while len(xy) != len(set(xy)):                  # distinct, or redraw
        xy = [grain.reduced() for _ in range(2 * t)]
    xs, ys = xy[:t], xy[t:]
    m = [[pow((xs[i] + ys[j]) % P, P - 2, P) for j in range(t)]
         for i in range(t)]

    return c, m


def poseidon_constants():
    """{"C": [[hex, ...], ...], "M": [[[hex, ...], ...], ...]} for t = 2..17."""
    def h(v):
        return "0x" + format(v, "064x")

    c_all, m_all = [], []
    for t in WIDTHS:
        c, m = constants_for(t)
        c_all.append([h(v) for v in c])
        m_all.append([[h(v) for v in row] for row in m])
    return {"C": c_all, "M": m_all}


def render(data):
    """Serialize in circomlib's layout: one row per line."""
    out = ['{', '  "C": [']
    rows = ['    [' + ", ".join(f'"{v}"' for v in row) + ']' for row in data["C"]]
    out.append(",\n".join(rows))
    out.append('  ],')
    out.append('  "M": [')
    blocks = []
    for mat in data["M"]:
        lines = ['      [' + ", ".join(f'"{v}"' for v in row) + ']' for row in mat]
        blocks.append('    [\n' + ",\n".join(lines) + '\n    ]')
    out.append(",\n".join(blocks))
    out.append('  ]')
    out.append('}')
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true",
                    help="rewrite the repository's copies")
    ap.add_argument("--check", action="store_true",
                    help="regenerate and compare against the repository's copies")
    ap.add_argument("--check-against", metavar="FILE",
                    help="compare the generated values against any JSON file "
                         "(circomlib's own, for instance)")
    args = ap.parse_args()

    data = poseidon_constants()
    text = render(data)

    # Structural self-check: the emitted counts must follow from (t, R_F, R_P).
    for i, t in enumerate(WIDTHS):
        assert len(data["C"][i]) == t * (R_F + R_P[t])
        assert len(data["M"][i]) == t and all(len(r) == t for r in data["M"][i])

    status = 0

    if args.write:
        for path in TARGETS:
            path.write_text(text)
            print(f"wrote {path.relative_to(REPO)}")

    if args.check:
        for path in TARGETS:
            if path.read_text(encoding="utf-8") != text:
                print(f"MISMATCH: {path.relative_to(REPO)} differs from the "
                      f"generated constants", file=sys.stderr)
                status = 1
            else:
                print(f"ok {path.relative_to(REPO)}")

    if args.check_against:
        other = json.loads(Path(args.check_against).read_text(encoding="utf-8"))
        mine = json.loads(text)
        same = (
            [[int(v, 16) for v in row] for row in mine["C"]] ==
            [[int(v, 16) for v in row] for row in other["C"]] and
            [[[int(v, 16) for v in r] for r in m] for m in mine["M"]] ==
            [[[int(v, 16) for v in r] for r in m] for m in other["M"]]
        )
        print(f"{'ok' if same else 'MISMATCH'} values equal those in "
              f"{args.check_against}")
        status = status or (0 if same else 1)

    if not (args.write or args.check or args.check_against):
        sys.stdout.write(text)

    return status


if __name__ == "__main__":
    sys.exit(main())
