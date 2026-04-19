"""Command-line entry point for the wallet reference impl.

Subcommands:

  emit-vectors  Write canonical JSON test vectors (consumed by Solidity tests).
  identity      Print the canonical identity_data and m for a JSON identity file.

Run via ``python -m alberta_buck.wallet.cli ...``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.vectors import emit_vectors


def _cmd_emit_vectors(args: argparse.Namespace) -> int:
    data = emit_vectors(args.out, seed=int(args.seed, 0))
    sys.stderr.write(
        f"wrote {args.out} (seed=0x{int(args.seed, 0):x}, "
        f"{len(json.dumps(data))} bytes JSON)\n"
    )
    return 0


def _cmd_identity(args: argparse.Namespace) -> int:
    with open(args.path) as f:
        fields = json.load(f)
    canonical = canonical_identity_data(fields)
    m = identity_scalar(canonical)
    print(canonical)
    print(f"m = 0x{m:064x}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="alberta-buck-wallet")
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("emit-vectors", help="emit canonical JSON test vectors")
    pe.add_argument("--out",  default="test/vectors/identity.json",
                    help="output path (default: test/vectors/identity.json)")
    pe.add_argument("--seed", default="0xa1bcb0ca",
                    help="hex/decimal seed for the deterministic RNG")
    pe.set_defaults(func=_cmd_emit_vectors)

    pi = sub.add_parser("identity", help="canonicalize and hash an identity JSON file")
    pi.add_argument("path", help="path to JSON file with identity fields")
    pi.set_defaults(func=_cmd_identity)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
