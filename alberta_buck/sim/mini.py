"""The journal-parity mini-scenario, Python side.

EXACTLY the op sequence ``core/js/src/scenarios/mini.js`` emits -- same
tags, same order, same values -- so the two platforms' journals compare
field-by-field (the Phase 4 Stage 4 acceptance in
alberta-buck-platform.org).  ``core/vectors/mini-scenario.json`` is the
single source of the numbers; this module is a faithful transcription of
the JS ``buildOnePool`` + ``PinWhale`` + ``RoundTripTrader`` ops, NOT a
port of their abstractions -- keeping the op stream auditable line by
line.

Run standalone against a fresh anvil:

    python -m alberta_buck.sim.mini --rpc http://127.0.0.1:8545 \\
        --journal mini-py.jsonl
"""

from __future__ import annotations

import json
from pathlib import Path

from alberta_buck.sim.chain import Chain, load_artifact, repo_root
from alberta_buck.sim.router import (
    MAX_SQRT_RATIO, MIN_SQRT_RATIO, full_range_ticks, sqrt_price_x96,
)
from buck_core.session import Expect

FEE = 3000                 # 60-tick spacing, as onepool.js
TICK_SPACING = 60
Q96 = 1 << 96
HUGE = (1 << 127) - 1      # int256 swap-to-the-limit amount


def load_scenario(path: str | Path | None = None) -> dict:
    p = Path(path) if path else repo_root() / "core" / "vectors" / "mini-scenario.json"
    return json.loads(Path(p).read_text())


def run_mini(chain: Chain, sc: dict) -> dict:
    """Drive the mini-scenario; returns the world's contract handles."""
    price = int(sc["price"])
    usdc_depth = int(sc["usdcDepth"])
    token_dec = int(sc["tokenDec"])
    me = chain.deployer

    # ---- buildOnePool, op for op (tags match onepool.js, which labels
    # deploys by INSTANCE -- "USDC"/"TOK" -- not by contract) -----------------
    usdc = chain.deploy("MockERC20", "USD Coin", "USDC", 6, tag="deploy:USDC")
    token = chain.deploy("MockERC20", "Test Token", "TOK", 18, tag="deploy:TOK")
    factory = chain.deploy("UniswapV3Factory")
    simlp = chain.deploy("SimLP")

    chain.send(factory.functions.createPool(token.address, usdc.address, FEE),
               tag="onepool:createPool")
    pool_addr = factory.functions.getPool(token.address, usdc.address, FEE).call()
    pool_abi, _ = load_artifact("UniswapV3Pool")
    pool = chain.w3.eth.contract(address=pool_addr, abi=pool_abi)

    sqrt = sqrt_price_x96(token.address, 10 ** token_dec, usdc.address, price)
    chain.send(pool.functions.initialize(sqrt), tag="onepool:init")

    token_stock = (4 * usdc_depth * 10 ** token_dec) // price
    chain.send(usdc.functions.mint(simlp.address, 4 * usdc_depth),
               tag="onepool:stock-usdc")
    chain.send(token.functions.mint(simlp.address, token_stock),
               tag="onepool:stock-token")

    usdc_is0 = usdc.address.lower() < token.address.lower()
    liq = (usdc_depth * sqrt) // Q96 if usdc_is0 else (usdc_depth * Q96) // sqrt
    lo, hi = full_range_ticks(TICK_SPACING)
    t0, t1 = ((usdc.address, token.address) if usdc_is0
              else (token.address, usdc.address))
    chain.send(simlp.functions.mint(pool.address, lo, hi, liq, t0, t1),
               tag="onepool:mint-liquidity")

    # ---- the trader's base-asset stock (tag matches mini.js) ---------------
    chain.send(token.functions.mint(me, int(sc["traderStock"])),
               tag="mini:stock-trader")

    # ---- per-day agents, transcribed from whale.js / trader.js -------------
    token_is0 = token.address.lower() < usdc.address.lower()
    pt0, pt1 = pool.functions.token0().call(), pool.functions.token1().call()
    amount = int(sc["tradeTokens"])

    def swap_args(zero_for_one: bool, amount_in: int, limit: int | None = None):
        if limit is None:
            limit = (MIN_SQRT_RATIO + 1) if zero_for_one else (MAX_SQRT_RATIO - 1)
        st0, st1 = ((token.address, usdc.address) if token_is0
                    else (usdc.address, token.address))
        return (pool.address, me, zero_for_one, amount_in, limit, st0, st1)

    for day in range(int(sc["days"])):
        # PinWhale.act: skip when already on target, else swap TO the limit.
        cur = pool.functions.slot0().call()[0]
        targ = sqrt_price_x96(token.address, 10 ** token_dec,
                              usdc.address, int(sc["targets"][day]))
        if cur != targ:
            chain.send(simlp.functions.swap(
                pool.address, simlp.address, targ < cur, HUGE, targ, pt0, pt1),
                tag=f"whale:snap:d{day}")

        # RoundTripTrader.act
        if day == 0:
            bad = swap_args(True, amount, limit=MAX_SQRT_RATIO - 1)
            chain.send(simlp.functions.swap(*bad),
                       expect=Expect.REVERT, tag=f"trader:spl-demo:d{day}")

        quote_before = usdc.functions.balanceOf(me).call()
        chain.send(token.functions.transfer(simlp.address, amount),
                   tag=f"trader:fund:d{day}")
        chain.send(simlp.functions.swap(*swap_args(token_is0, amount)),
                   tag=f"trader:sell:d{day}")
        proceeds = usdc.functions.balanceOf(me).call() - quote_before

        chain.send(usdc.functions.transfer(simlp.address, proceeds),
                   tag=f"trader:refund:d{day}")
        chain.send(simlp.functions.swap(*swap_args(not token_is0, proceeds)),
                   tag=f"trader:buyback:d{day}")

    return {"usdc": usdc, "token": token, "pool": pool, "simlp": simlp}


def main(argv=None) -> int:
    import argparse

    from web3 import Web3

    ap = argparse.ArgumentParser(prog="alberta-buck-mini")
    ap.add_argument("--rpc", required=True, help="fresh anvil RPC URL")
    ap.add_argument("--journal", required=True, help="journal JSONL out path")
    ap.add_argument("--scenario", default=None)
    args = ap.parse_args(argv)

    w3 = Web3(Web3.HTTPProvider(args.rpc))
    chain = Chain(w3, w3.eth.accounts[0], journal=args.journal)
    run_mini(chain, load_scenario(args.scenario))
    print(json.dumps({"backend": "anvil", "mismatches": len(chain.mismatches)}))
    return 0 if not chain.mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())
