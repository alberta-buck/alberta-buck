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
from alberta_buck.wallet.envelope import parse_envelope, deserialize_core
from alberta_buck.wallet.verify_receipt import verify_receipt
from alberta_buck.wallet.render import render_receipt, TextDriver, Detail


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


def _cmd_verify(args: argparse.Namespace) -> int:
    import sys as _sys
    if args.file == "-":
        text = _sys.stdin.read()
    else:
        with open(args.file) as f:
            text = f.read()
    try:
        b = parse_envelope(text)
    except ValueError as e:
        print(f"parse: {e}", file=_sys.stderr)
        return 1
    core = deserialize_core(b)
    res = verify_receipt(core)
    if res.ok:
        print(f"status: {'VALID' if not (res.reason or '').startswith('UNVERIFIED') else 'UNVERIFIED ISSUER'}")
        if res.identity_M is not None:
            x = int(res.identity_M[0])
            y = int(res.identity_M[1])
            print(f"payer M: (0x{x:064x}, 0x{y:064x})")
        if res.value is not None:
            print(f"value:   {res.value}")
        if res.reason:
            print(f"banner:  {res.reason}")
        return 0
    else:
        print(f"status: INVALID")
        print(f"reason: {res.reason}")
        return 1


def _cmd_receipt(args: argparse.Namespace) -> int:
    import sys as _sys
    if args.file == "-":
        text = _sys.stdin.read()
    else:
        with open(args.file) as f:
            text = f.read()
    try:
        b = parse_envelope(text)
    except ValueError as e:
        print(f"parse: {e}", file=_sys.stderr)
        return 1
    core = deserialize_core(b)

    detail_map = {"minimal": Detail.MINIMAL, "name": Detail.NAME,
                  "normal": Detail.NORMAL, "full": Detail.FULL}
    txn_map = {"minimal": Detail.MINIMAL, "normal": Detail.NORMAL,
               "full": Detail.FULL}
    verify_map = {"minimal": Detail.MINIMAL, "normal": Detail.NORMAL,
                  "full": Detail.FULL}

    doc = render_receipt(
        core,
        payer_detail=detail_map[args.detail],
        payee_detail=detail_map[args.detail],
        txn_detail=txn_map[args.txn_detail],
        verify_detail=verify_map[args.verify_detail],
    )
    output = TextDriver(width=args.width).render(doc)
    _sys.stdout.write(output)
    return 0


def _cmd_render_golden(args: argparse.Namespace) -> int:
    from alberta_buck.wallet.render import render_receipt, TextDriver
    from alberta_buck.wallet.envelope import deserialize_core, parse_envelope
    from alberta_buck.wallet.vectors import build_vectors

    v = build_vectors()
    # Golden receipts are consumed only by the Python tests, so they live in
    # the Python tree (test/vectors/ holds the artifacts the forge tests read).
    for kind in sorted(v["abrcpt"]):
        env = v["abrcpt"][kind]["envelope"]
        core = deserialize_core(parse_envelope(env))
        text = TextDriver(48).render(render_receipt(core))
        dest = f"alberta_buck/test/vectors/receipt-{kind}.golden.txt"
        with open(dest, "w") as f:
            f.write(text)
        print(f"  wrote {dest}  ({len(text)} B)")
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

    pv = sub.add_parser("verify", help="verify an AB-RCPT/2 receipt envelope")
    pv.add_argument("file", nargs="?", default="-",
                    help="envelope file (default: stdin)")
    pv.set_defaults(func=_cmd_verify)

    prg = sub.add_parser("render-golden",
                           help="(re)generate alberta_buck/test/vectors/receipt-*.golden.txt")
    prg.set_defaults(func=_cmd_render_golden)

    pr = sub.add_parser("receipt", help="render an AB-RCPT/2 envelope as a receipt")
    pr.add_argument("file", nargs="?", default="-",
                    help="envelope file (default: stdin)")
    pr.add_argument("--width", type=int, default=48,
                    help="output width in characters (default: 48)")
    pr.add_argument("--detail", choices=["minimal", "name", "normal", "full"],
                    default="name", help="identity detail level (default: name)")
    pr.add_argument("--txn-detail", choices=["minimal", "normal", "full"],
                    default="normal", help="transaction detail (default: normal)")
    pr.add_argument("--verify-detail", choices=["minimal", "normal", "full"],
                    default="normal", help="verification detail (default: normal)")
    pr.set_defaults(func=_cmd_receipt)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
