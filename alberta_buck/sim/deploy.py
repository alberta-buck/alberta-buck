"""Deploy + wire the full Direct system, V3 pools, and Universal Router.

Order mirrors test/stabilizer-routing-op47/RoutingSim.t.sol::setUp and
test/BuckBasket.t.sol::setUp.  Only BUCK-touching contracts (BuckBasket,
each TOKEN/BUCK pool, the Universal Router) get a public bindContract
Identity; TOKEN/USDC pools and SimLP never custody BUCK so they need none.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from web3 import Web3

from alberta_buck.sim import identity as idmod
from alberta_buck.sim.chain import Chain, load_artifact
from alberta_buck.sim.router import (
    sqrt_price_x96, full_range_ticks, MIN_SQRT_RATIO, MAX_SQRT_RATIO,
)

REPO = Path(__file__).resolve().parents[2]
UR_ARTIFACT = "alberta_buck/sim/artifacts/UniversalRouter.json"

E6 = 10 ** 6
E18 = 10 ** 18
FEE_USDC = 3000
FEE_BUCK = 500
TICK_SPACING = {3000: 60, 500: 10}
# Common BUCK reserve every TOKEN/BUCK basket pool is seeded to, so no
# single token's pool depth dominates the shared routing.
#TARGET_BUCK = 10 ** 14
TARGET_BUCK = 10 ** 12  # Bucks are 6-digit fixed, so 1,000,000 x 1e6 = 10**12

DEPOSITED_TOPIC = Web3.keccak(
    text="Deposited(address,uint256,address,uint256,uint256,uint128)")
REDEEMED_TOPIC = Web3.keccak(
    text="Redeemed(address,uint256,address,uint256,uint256,uint256,uint256,uint128)")


@dataclass
class Deployment:
    w3: Any
    chain: Chain
    anvil: Any
    gov: str
    pool_acct: str           # Buck.insurancePool + setBasket caller
    issuer_addr: str
    issuer_kp: Any
    reg: Any
    buck: Any
    kctrl: Any
    basket: Any
    router: Any
    simlp: Any
    usdc: Any
    erc20_abi: list
    tokens: list = field(default_factory=list)      # web3 contracts
    dec: list = field(default_factory=list)         # decimals
    pool_usdc: list = field(default_factory=list)   # TOKEN/USDC addrs
    pool_buck: list = field(default_factory=list)   # TOKEN/BUCK addrs
    pool_ub: str = ""                               # floating BUCK/USDC pool
    pool_meta: list = field(default_factory=list)   # (pool,owner,lo,hi,group)
    pool_receipts: dict = field(default_factory=dict)  # token_index -> receiptId
    fee_usdc: int = FEE_USDC
    fee_buck: int = FEE_BUCK


def _erc20_abi() -> list:
    abi, _ = load_artifact("MockERC20")
    return abi


def deploy(chain: Chain, anvil, scenario, rng, verbose=True) -> Deployment:
    w3 = chain.w3
    accts = w3.eth.accounts
    deployer, gov, pool_acct, issuer_addr = accts[0], accts[1], accts[2], accts[3]
    erc20_abi = _erc20_abi()

    # --- identity layer ---------------------------------------------- #
    reg = chain.deploy("IdentityRegistry", gov)
    issuer_kp = idmod.make_issuer(rng)
    chain.send(reg.functions.trustIssuer(issuer_addr, idmod.pspubkey_arg(issuer_kp)),
               sender=gov)

    # --- Direct BUCK stack ------------------------------------------- #
    credit = chain.deploy("BuckCredit")
    kctrl = chain.deploy("BuckKControllerDirect",
                          int(0.1 * E18), int(0.01 * E18), 0, 60,
                          int(0.50 * E18), int(1.50 * E18), E18, gov)
    buck = chain.deploy("Buck", credit.address, kctrl.address, reg.address, pool_acct)
    chain.send(reg.functions.setBuck(buck.address), sender=gov)

    v3f = chain.deploy("UniswapV3Factory")
    basket = chain.deploy("BuckBasket", buck.address, kctrl.address, v3f.address,
                           gov, FEE_BUCK, 600, 64, 50, 1000)
    chain.send(buck.functions.setBasket(basket.address), sender=pool_acct)
    chain.send(kctrl.functions.setBasket(basket.address), sender=gov)
    chain.send(reg.functions.bindContract(
        basket.address, idmod.BIND_PK, idmod.BIND_E, True, True), sender=deployer)

    # --- tokens ------------------------------------------------------ #
    usdc = chain.deploy("MockERC20", "USD Coin", "USDC", 6)
    tok, dec = [], []
    for (sym, name, d) in scenario.tokens:
        c = chain.deploy("MockERC20", name, sym, d)
        tok.append(c); dec.append(d)

    # --- SimLP (V3 mint/swap callback helper) ------------------------ #
    simlp = chain.deploy("SimLP", sol_file="SimLP")
    # SimLP will custody BUCK to seed the floating BUCK/USDC pool, so it
    # needs a public Identity (BUCK transfers are identity-gated).
    chain.send(reg.functions.bindContract(
        simlp.address, idmod.BIND_PK, idmod.BIND_E, True, True), sender=deployer)
    big = 10 ** 30
    chain.send(usdc.functions.mint(simlp.address, big))
    for c in tok:
        chain.send(c.functions.mint(simlp.address, big))

    # --- WETH9 + Universal Router ------------------------------------ #
    weth = chain.deploy("WETH9")
    _, pool_bc = load_artifact("UniswapV3Pool")
    pool_init_hash = Web3.keccak(hexstr=pool_bc if pool_bc.startswith("0x") else "0x" + pool_bc)
    Z = "0x" + "0" * 40
    ur_params = (
        Web3.to_checksum_address("0x" + "be" * 20),  # permit2 (unused)
        weth.address, Z, v3f.address,
        b"\x00" * 32, pool_init_hash,
        Z, Z, Z, Z,
    )
    ur_art = json.loads((REPO / UR_ARTIFACT).read_text())
    router = chain.deploy("UniversalRouter", ur_params,
                          abi=ur_art["abi"], bytecode=ur_art["bytecode"]["object"])
    chain.send(reg.functions.bindContract(
        router.address, idmod.BIND_PK, idmod.BIND_E, True, True), sender=deployer)

    d = Deployment(w3, chain, anvil, gov, pool_acct, issuer_addr, issuer_kp,
                   reg, buck, kctrl, basket, router, simlp, usdc, erc20_abi,
                   tok, dec)

    # --- pools: TOKEN/USDC (truth) + TOKEN/BUCK (basket) ------------- #
    pool_v3_abi, _ = load_artifact("UniswapV3Pool")
    for i, c in enumerate(tok):
        sym = scenario.tokens[i][0]
        p0 = scenario.prices.day0(i)
        # TOKEN/USDC pool, initialized at day-0 price, deep SimLP liquidity.
        chain.send(v3f.functions.createPool(c.address, usdc.address, FEE_USDC))
        pu = v3f.functions.getPool(c.address, usdc.address, FEE_USDC).call()
        pool = w3.eth.contract(address=pu, abi=pool_v3_abi)
        sp = sqrt_price_x96(c.address, 10 ** dec[i], usdc.address, p0)
        chain.send(pool.functions.initialize(sp))
        t0 = pool.functions.token0().call()
        t1 = pool.functions.token1().call()
        lo, hi = full_range_ticks(TICK_SPACING[FEE_USDC])
        # Seed to a COMMON USDC-side depth (== TARGET_BUCK), NOT a fixed L.
        # A fixed L makes real reserves scale with decimals/price, leaving
        # 18-dec PAXG/AOIL pools shallow while 8-dec cbBTC is unmovably
        # deep -- so routed flow churns the thin pools faster than the
        # sparse whale snaps re-pin them.  For a full-range position
        # USDC_reserve ~= L*sqrtP/2^96 (USDC=token1) or L*2^96/sqrtP
        # (USDC=token0); invert to hit TARGET_BUCK USDC raw.
        Q96 = 1 << 96
        if usdc.address.lower() == t0.lower():     # USDC is token0
            Lusdc = TARGET_BUCK * sp // Q96
        else:                                       # USDC is token1
            Lusdc = TARGET_BUCK * Q96 // sp
        rcpt = chain.send(simlp.functions.mint(pu, lo, hi, max(1, Lusdc), t0, t1))
        d.pool_usdc.append(pu)

        if verbose:
            tok_bal = c.functions.balanceOf(pu).call()
            usdc_bal = usdc.functions.balanceOf(pu).call()
            implied = usdc_bal * (10 ** dec[i]) // tok_bal if tok_bal else 0
            print(f"[deploy] TOKEN/USDC {sym}/USDC pool {pu[:10]}...  "
                  f"fee={FEE_USDC} ({TICK_SPACING[FEE_USDC]}-tick)")
            print(f"         sqrtPriceX96={sp}  implied ${implied/E6:,.2f}/{sym}")
            print(f"         reserves: {tok_bal/(10**dec[i]):,.6g} {sym}  "
                  f"{usdc_bal/E6:,.2f} USDC")

        # TOKEN/BUCK basket pool via direct mint.
        chain.send(basket.functions.addBasketToken(
            c.address, dec[i], p0, 10000 // len(tok), FEE_BUCK), sender=gov)
        pb = v3f.functions.getPool(c.address, buck.address, FEE_BUCK).call()
        chain.send(reg.functions.bindContract(
            pb, idmod.BIND_PK, idmod.BIND_E, True, True), sender=deployer)
        # Seed every basket pool to a COMMON BUCK depth.  depositToken mints
        # BUCK = seed*p0/10**dec, so seeding a fixed token count would make
        # the BUCK reserve scale with p0 (~1000x spread across PAXG/cbBTC/
        # AOIL) and the deepest pool (cbBTC) would monopolize all routing.
        # Solve seed so minted BUCK ~= TARGET_BUCK for every token.
        seed = max(10 ** dec[i], TARGET_BUCK * (10 ** dec[i]) // p0)
        chain.send(c.functions.mint(deployer, seed))
        chain.send(c.functions.approve(basket.address, seed))
        dep_rcpt = chain.send(basket.functions.depositToken(c.address, seed, 0))
        d.pool_buck.append(pb)

        # Extract receiptId from the Deposited event.
        rid = None
        for log in dep_rcpt["logs"]:
            if log["topics"][0] == DEPOSITED_TOPIC:
                rid = int.from_bytes(log["topics"][2], "big")
                d.pool_receipts[i] = rid
                break

        if verbose:
            tok_bal = c.functions.balanceOf(pb).call()
            buck_bal = buck.functions.balanceOf(pb).call()
            implied = buck_bal * (10 ** dec[i]) // tok_bal if tok_bal else 0
            print(f"[deploy] TOKEN/BUCK {sym}/BUCK pool {pb[:10]}...  "
                  f"fee={FEE_BUCK} ({TICK_SPACING[FEE_BUCK]}-tick)")
            print(f"         seed={seed/(10**dec[i]):,.6g} {sym}  "
                  f"initialPrice={p0/E6:,.2f} BUCK/{sym}")
            print(f"         reserves: {tok_bal/(10**dec[i]):,.6g} {sym}  "
                  f"{buck_bal/E18:,.2f} BUCK")
            print(f"         receiptId={rid}")

    # --- floating BUCK/USDC pool (gauge-breaking, not a peg) ---------- #
    #
    # A large, low-fee BUCK/USDC pool established at the initial
    # *instantaneous* BUCK basket valuation in USD (every TOKEN/USDC and
    # TOKEN/BUCK pool is seeded at the same p0, so 1 BUCK == 1 USDC at t0)
    # and then left to FLOAT on supply/demand -- never controlled; it
    # exists only to be *used by routing*.
    #
    # Realistic, all-PUBLIC funding (no identity fakes anywhere): the
    # public, identity-bound SimLP contract is itself the BUCK-backed LP.
    # It pledges an insured asset (BuckCredit, zero premium -> zero
    # insurance poolPrincipal -> the funding-factor gate is inapplicable),
    # mints BUCK against it, and LPs that BUCK + USDC.  Every resulting
    # BUCK transfer is SimLP(public) -> pool(public).  No private EOA, no
    # storage fakery, and no existing pool is drained.
    Q96 = 1 << 96
    TARGET_BUCK_LP = 4 * TARGET_BUCK
    FACE = 2 * TARGET_BUCK_LP

    now_ts = w3.eth.get_block("latest")["timestamp"]
    cc = credit.functions.createCredit(simlp.address, 0, FACE, 0, 0, 0,
                                       now_ts, 0)              # NONE, premium 0
    tid = cc.call({"from": deployer})
    chain.send(cc, sender=deployer)
    # SimLP (the credit owner) activates it and mints BUCK to itself.
    chain.send(simlp.functions.exec(
        credit.address, credit.encode_abi("activate", args=[tid, FACE])))
    chain.send(simlp.functions.exec(
        buck.address,
        buck.encode_abi("mint(uint256)", args=[TARGET_BUCK_LP])))

    chain.send(v3f.functions.createPool(buck.address, usdc.address, FEE_BUCK))
    pub = v3f.functions.getPool(buck.address, usdc.address, FEE_BUCK).call()
    poolU = w3.eth.contract(address=pub, abi=pool_v3_abi)
    spU = sqrt_price_x96(buck.address, 1_000_000, usdc.address, 1_000_000)
    chain.send(poolU.functions.initialize(spU))
    u0 = poolU.functions.token0().call()
    u1 = poolU.functions.token1().call()
    # Bind the pool BEFORE LPing it (public): the mint callback transfers
    # BUCK into it, and BUCK transfers are identity-gated on the recipient.
    chain.send(reg.functions.bindContract(
        pub, idmod.BIND_PK, idmod.BIND_E, True, True), sender=deployer)
    if usdc.address.lower() == u0.lower():
        Lub = TARGET_BUCK * spU // Q96
    else:
        Lub = TARGET_BUCK * Q96 // spU
    lo, hi = full_range_ticks(TICK_SPACING[FEE_BUCK])
    chain.send(simlp.functions.mint(pub, lo, hi, max(1, Lub), u0, u1))
    d.pool_ub = pub

    if verbose:
        buck_bal = buck.functions.balanceOf(pub).call()
        usdc_bal = usdc.functions.balanceOf(pub).call()
        print(f"[deploy] BUCK/USDC pool {pub[:10]}...  "
              f"fee={FEE_BUCK} ({TICK_SPACING[FEE_BUCK]}-tick)")
        print(f"         sqrtPriceX96={spU}  implied $1.00/BUCK (by construction)")
        print(f"         reserves: {buck_bal/E18:,.2f} BUCK  "
              f"{usdc_bal/E6:,.2f} USDC")

    # LP-position metadata for ROI/APR accounting: (pool, owner, lo, hi,
    # group).  TOKEN/USDC + BUCK/USDC are SimLP-funded; TOKEN/BUCK are
    # direct-mint funded (owned by BuckBasket).  All full-range.
    lou, hiu = full_range_ticks(TICK_SPACING[FEE_USDC])
    lob, hib = full_range_ticks(TICK_SPACING[FEE_BUCK])
    d.pool_meta = (
        [(d.pool_usdc[i], simlp.address, lou, hiu, "usdc") for i in range(len(tok))]
        + [(d.pool_buck[i], basket.address, lob, hib, "buck") for i in range(len(tok))]
        + [(d.pool_ub, simlp.address, lob, hib, "ub")]
    )
    return d
