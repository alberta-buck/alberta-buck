"""Deploy + wire the full Direct system, V3 pools, and Universal Router.

Order mirrors test/BuckBasket.t.sol::setUp.  Only BUCK-touching contracts (BuckBasket,
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
FEE_BUCK = 3000         # TOKEN/BUCK pools: 0.30% (direct-mint LP profit)
FEE_BUCK_UB = 500       # BUCK/USDC pool:   0.05% (gauge-breaking, cheap)
TICK_SPACING = {3000: 60, 500: 10}

# Common BUCK reserve every TOKEN/BUCK basket pool is seeded to, so no
# single token's pool depth dominates the shared routing.
#TARGET_BUCK = 10 ** 14  # $10^14 / 10^6-dec = $100,000,000 BUCK per pool
TARGET_BUCK = 10 ** 13   # $10^13 / 10^6-dec =  $10,000,000 BUCK per pool
#TARGET_BUCK = 10 ** 12  # $10^13 / 10^6-dec =   $1,000,000 BUCK per pool - too small (spikes)

# The BUCK/USDC pool should be similarly large; BUCK-unaware arb and token accumulator agents will
# move this as they choose USDC->TOKEN vs. USDC->BUCK->TOKEN routes
#TARGET_BUCK_LP = 4 * TARGET_BUCK
TARGET_BUCK_LP = TARGET_BUCK

# --- Event topics, per basket implementation ------------------------------- #
#
# Both baskets index (who, receiptId) so `topics[2]` is the receiptId on either,
# but the signatures (hence topic[0]) differ -- legacy BuckBasket carries an
# extra `address` in Deposited and a 5-arg Redeemed + per-pool RedeemedFromPool;
# BuckBasketProRata pays out via ERC20 transfers and emits a 6-arg Redeemed.
LEGACY_DEPOSITED_TOPIC = Web3.keccak(
    text="Deposited(address,uint256,address,uint256,uint256,address,uint128)")
LEGACY_REDEEMED_TOPIC = Web3.keccak(
    text="Redeemed(address,uint256,uint256,uint256,uint256)")
REDEEMED_FROM_POOL_TOPIC = Web3.keccak(
    text="RedeemedFromPool(uint256,address,address,uint256,uint256,uint128)")

PRORATA_DEPOSITED_TOPIC = Web3.keccak(
    text="Deposited(address,uint256,address,uint256,uint256,uint128)")
PRORATA_REDEEMED_TOPIC = Web3.keccak(
    text="Redeemed(address,uint256,uint256,uint256,uint256,uint256)")

ERC20_TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)")

# Back-compat aliases (legacy is the default; older imports keep working).
DEPOSITED_TOPIC = LEGACY_DEPOSITED_TOPIC
REDEEMED_TOPIC = LEGACY_REDEEMED_TOPIC


def parse_redeem(d, rcpt, holder) -> tuple[int, int]:
    """(token_to_user, treasury_buck) from a redeem receipt, impl-aware.

    Legacy BuckBasket emits one RedeemedFromPool per pool (sum `tokToUser`) and
    Redeemed(..., retainedBuck, ...).  BuckBasketProRata has no per-pool event:
    it pays the holder via ERC20 transfers, so sum the basket-TOKEN transfers
    basket->holder, and read `treasuryBuck` from its 6-arg Redeemed.
    """
    from eth_abi import decode
    holder = Web3.to_checksum_address(holder)
    holder_bytes = bytes.fromhex(holder[2:])
    token_to_user = 0
    treasury_buck = 0

    if d.basket_impl == "prorata":
        basket_tokens = {c.address.lower() for c in d.tokens}
        for log in rcpt["logs"]:
            t0 = log["topics"][0]
            if (t0 == ERC20_TRANSFER_TOPIC
                    and log["address"].lower() in basket_tokens
                    and len(log["topics"]) >= 3
                    and bytes(log["topics"][2])[-20:] == holder_bytes):
                token_to_user += decode(["uint256"], bytes(log["data"]))[0]
            elif t0 == d.redeemed_topic:
                # (burned, depositorBuck, treasuryBuck, remainingBp)
                _, _, tb, _ = decode(
                    ["uint256", "uint256", "uint256", "uint256"],
                    bytes(log["data"]))
                treasury_buck += tb
    else:
        for log in rcpt["logs"]:
            t0 = log["topics"][0]
            if t0 == REDEEMED_FROM_POOL_TOPIC:
                tok_to_user, _, _ = decode(
                    ["uint256", "uint256", "uint128"], bytes(log["data"]))
                token_to_user += tok_to_user
            elif t0 == d.redeemed_topic:
                _, retained, _ = decode(
                    ["uint256", "uint256", "uint256"], bytes(log["data"]))
                treasury_buck += retained
    return token_to_user, treasury_buck


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
    credit: Any
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
    fee_usdc: int = FEE_USDC
    fee_buck: int = FEE_BUCK      # TOKEN/BUCK pools
    fee_ub: int = FEE_BUCK_UB     # floating BUCK/USDC pool
    basket_impl: str = "legacy"   # "legacy" (BuckBasket) | "prorata"
    venue: Any = None             # BuckBasketUniswapV3 facet (prorata only)
    deposited_topic: bytes = DEPOSITED_TOPIC
    redeemed_topic: bytes = REDEEMED_TOPIC


def _erc20_abi() -> list:
    abi, _ = load_artifact("MockERC20")
    return abi


def deploy(chain: Chain, anvil, scenario, rng, verbose=True,
           basket_impl="legacy") -> Deployment:
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
    # Rescaled direct PID (ppm process/error, dt in seconds).  Gains stored as
    # real_gain * 1e12.  K is the LTV cap => max system leverage 1/(1-K):
    # K0~0.75 rests at ~4x; the default KMAX 0.95 keeps a railed K solvent
    # (20x) instead of sitting on the 1/(1-K) spiral boundary.
    # Ki sized for a target "max variance before the rail":
    #   Ki_real = dK_rail / (e_max * tau_I)   [ per (fractional error * second) ]
    # so a sustained e_max basket deviation rails K over tau_I.
    # Deliberately SLOW: K is a structural lever, not a market maker.  It
    # glides over months while private demand (savers) does the fast
    # stabilization -- a sustained e_max deviation takes ~tau_I to reach a
    # rail and the proportional kick is tiny.  Prevents the relay/bang-bang
    # oscillation seen when K reacts as hard as the agents do.
    # All of these come from experiment.deploy_params: coded defaults, or the
    # attached experiment's [deploy] section when one is present.
    from alberta_buck.sim.experiment import deploy_params
    dp = deploy_params(getattr(scenario, "experiment", None))
    K0, KMIN, KMAX = dp.k0_wei, dp.kmin_wei, dp.kmax_wei
    KP, KI, KD = dp.kp_scaled, dp.ki_scaled, dp.kd_scaled
    kctrl = chain.deploy("BuckKControllerDirect",
                          KP, KI, KD, dp.dt, KMIN, KMAX, K0, gov)
    if dp.dtmax_secs:
        chain.send(kctrl.functions.setDTMax(dp.dtmax_secs), sender=gov)
    buck = chain.deploy("Buck", credit.address, kctrl.address, reg.address, pool_acct)
    chain.send(reg.functions.setBuck(buck.address), sender=gov)
    # Wire BuckCredit -> Buck so activation can flow through Buck.mint ->
    # activateFromBuck (which requires msg.sender == buck) and so NFT
    # mutations invalidate Buck's credit-limit cache via onCreditMutation.
    chain.send(credit.functions.setBuck(buck.address), sender=deployer)

    v3f = chain.deploy("UniswapV3Factory")
    # Constructor is identical for both implementations (drop-in).  The
    # spot/TWAP guard tolerance is 5% (500 bp): legacy uses it only for BUCK
    # deposits, ProRata also for the redeem value read; 5% keeps ordinary
    # inter-tick commodity moves (6h ticks vs 600s TWAP) from tripping it.
    ctor = (buck.address, kctrl.address, v3f.address, gov, FEE_BUCK, 600, 64, 500, 1000)
    venue = None
    if basket_impl == "prorata":
        basket = chain.deploy("BuckBasketProRata", *ctor)
        # Install the Uniswap V3 venue facet (the shell delegatecalls it) and
        # re-wrap the basket handle with the union ABI so Python can call facet
        # views (basketValueInBuck) that the shell serves via its fallback.
        venue = chain.deploy("BuckBasketUniswapV3")
        chain.send(basket.functions.setVenue(venue.address), sender=gov)
        shell_abi, _ = load_artifact("BuckBasketProRata")
        facet_abi, _ = load_artifact("BuckBasketUniswapV3")
        union = shell_abi + [e for e in facet_abi if e not in shell_abi]
        basket = w3.eth.contract(address=basket.address, abi=union)
        deposited_topic, redeemed_topic = PRORATA_DEPOSITED_TOPIC, PRORATA_REDEEMED_TOPIC
    else:
        basket = chain.deploy("BuckBasket", *ctor)
        deposited_topic, redeemed_topic = LEGACY_DEPOSITED_TOPIC, LEGACY_REDEEMED_TOPIC
    chain.send(buck.functions.setBasket(basket.address), sender=pool_acct)
    chain.send(kctrl.functions.setBasket(basket.address), sender=gov)
    chain.send(reg.functions.bindContract(
        basket.address, idmod.BIND_PK, idmod.BIND_E, True, True), sender=deployer)

    # --- tokens ------------------------------------------------------ #
    usdc = chain.deploy("MockERC20", "USD Coin", "USDC", 6)
    tok, dec, wbp = [], [], []
    for t in scenario.tokens:
        # tokens are (sym, name, decimals[, weightBp]); the optional 4th element
        # is the DESIRED FINAL basket target weight in basis points (0 => equal
        # 1/N share).  The weights across the basket sum to 10000.
        sym, name, d = t[0], t[1], t[2]
        w = t[3] if len(t) > 3 else 0
        c = chain.deploy("MockERC20", name, sym, d)
        tok.append(c); dec.append(d); wbp.append(w)

    # Translate DESIRED FINAL weights -> the SEQUENTIAL weights addBasketToken
    # expects.  addBasketToken renormalizes existing constituents on every add
    # (stick-breaking): the value passed for a token is its share of the whole
    # basket AT THE MOMENT it is added, when tokens 0..i already sum to 10000.
    # So token i's passed weight = 10000 * f_i / (f_0 + ... + f_i); this makes
    # the STORED targetWeightBp land on the desired f_i (verified below).  A raw
    # pass-through would badly distort them (the first token balloons to fill
    # 10000).  Unweighted baskets (all f == 0) pass 0 throughout (equal 1/N
    # share) -- byte-identical to the previous behaviour.
    pass_wbp, acc = [], 0
    if any(w > 0 for w in wbp):
        for w in wbp:
            acc += w
            pass_wbp.append(round(10000 * w / acc) if acc > 0 else 0)
    else:
        pass_wbp = [0] * len(wbp)

    # --- SimLP (V3 mint/swap callback helper) ------------------------ #
    simlp = chain.deploy("SimLP", sol_file="SimLP")
    # SimLP will custody BUCK to seed the floating BUCK/USDC pool, so it
    # needs a public Identity (BUCK transfers are identity-gated).
    #
    # Bind as Public + NON-Carrying.  Under Phase 1b, buck.mint(N) no
    # longer delivers N to the holder's raw balance -- it opens NFT-
    # backed credit headroom that the holder spends INTO pools (raw
    # goes negative).  A Carrying-flagged SimLP cannot go negative
    # (_carryingTransfer asserts rawSigned >= value), so the V3 mint
    # callback that transfers BUCK to the pool reverts with "BUCK:
    # Carrying amount exceeds raw".  As Non-Carrying, SimLP uses its
    # creditLimit (held + unusedCredit) as spendable; the transfer to
    # the pool drives signedRaw to -value and the pool receives freshly
    # issued BUCK.
    chain.send(reg.functions.bindContract(
        simlp.address, idmod.BIND_PK, idmod.BIND_E, True, False), sender=deployer)
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
                   reg, buck, credit, kctrl, basket, router, simlp, usdc,
                   erc20_abi, tok, dec,
                   basket_impl=basket_impl, venue=venue,
                   deposited_topic=deposited_topic, redeemed_topic=redeemed_topic)

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

        # Seed to a COMMON USDC-side depth (== dp.target_buck), NOT a fixed L.
        # A fixed L makes real reserves scale with decimals/price, leaving
        # 18-dec PAXG/AOIL pools shallow while 8-dec cbBTC is unmovably
        # deep -- so routed flow churns the thin pools faster than the
        # sparse whale snaps re-pin them.  For a full-range position
        # USDC_reserve ~= L*sqrtP/2^96 (USDC=token1) or L*2^96/sqrtP
        # (USDC=token0); invert to hit TARGET_BUCK USDC raw.
        Q96 = 1 << 96
        if usdc.address.lower() == t0.lower():     # USDC is token0
            Lusdc = dp.target_buck * sp // Q96
        else:                                       # USDC is token1
            Lusdc = dp.target_buck * Q96 // sp
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

        # TOKEN/BUCK basket pool (empty — bootstrapped by DM agents).
        # pass_wbp[i] is the sequential (stick-breaking) weight; 0 => equal share.
        chain.send(basket.functions.addBasketToken(
            c.address, dec[i], p0, pass_wbp[i], FEE_BUCK), sender=gov)
        pb = v3f.functions.getPool(c.address, buck.address, FEE_BUCK).call()
        chain.send(reg.functions.bindContract(
            pb, idmod.BIND_PK, idmod.BIND_E, True, True), sender=deployer)
        d.pool_buck.append(pb)

        if verbose:
            print(f"[deploy] TOKEN/BUCK {sym}/BUCK pool {pb[:10]}...  "
                  f"fee={FEE_BUCK} ({TICK_SPACING[FEE_BUCK]}-tick)  "
                  f"targetBp={wbp[i]} (passed {pass_wbp[i]})")
            print(f"         empty pool — bootstrap DM agents will seed")

    # Read back the basket's realized target weights (legacy exposes the
    # `constituents` array getter; targetWeightBp is the last struct field).
    # addBasketToken renormalizes existing constituents on each add, so the
    # stored weights are the SEQUENTIAL result of the passed bp, summing to
    # 10000 -- this confirms the per-token weightBp actually took effect.
    if verbose and basket_impl == "legacy":
        n = basket.functions.constituentsLength().call()
        parts, wsum = [], 0
        for i in range(n):
            w = basket.functions.constituents(i).call()[-1]  # targetWeightBp
            wsum += w
            parts.append(f"{scenario.tokens[i][0]}={w}")
        print(f"[deploy] basket target weightBp (sum={wsum}): " + "  ".join(parts))

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
    # A zero-premium mint yields *spendable* BUCK == amount * buckK
    # (creditLimit = activatedValue * buckK / 1e18), so at a resting LTV of
    # K0 < 1.0 minting TARGET_BUCK_LP no longer frees TARGET_BUCK_LP to LP.
    # Size the mint (and the backing credit face) off the live K0 so the SimLP
    # can seed ~TARGET_BUCK_LP BUCK at any resting K -- with a 20% margin so
    # full-range rounding never trips "amount exceeds spendable".  Only the LP
    # transfer (~TARGET_BUCK_LP) actually enters supply; the surplus headroom
    # is inert, so downstream pool depth / totalSupply are unchanged.
    k0 = kctrl.functions.buckK().call()
    mint_amt = (dp.target_buck_lp * E18 // max(1, k0)) * 12 // 10
    FACE = max(2 * dp.target_buck_lp, mint_amt * 12 // 10)

    now_ts = w3.eth.get_block("latest")["timestamp"]
    cc = credit.functions.createCredit(simlp.address, 0, FACE, 0, 0, 0,
                                       now_ts, 0)              # NONE, premium 0
    chain.send(cc, sender=deployer)
    # SimLP (the credit owner) mints BUCK to itself.  Minting auto-activates
    # the pledged credit (activation is collapsed into Buck.mint); the credit
    # is zero-premium, so poolPrincipal == 0 -- zero-cost insurance -- and the
    # funding-factor gate is inapplicable, so no prior BUCK reserve is needed
    # to bootstrap.
    chain.send(simlp.functions.exec(
        buck.address,
        buck.encode_abi("mint(uint256)", args=[mint_amt])))

    chain.send(v3f.functions.createPool(buck.address, usdc.address, FEE_BUCK_UB))
    pub = v3f.functions.getPool(buck.address, usdc.address, FEE_BUCK_UB).call()
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
        Lub = dp.target_buck * spU // Q96
    else:
        Lub = dp.target_buck * Q96 // spU
    lo_ub, hi_ub = full_range_ticks(TICK_SPACING[FEE_BUCK_UB])
    chain.send(simlp.functions.mint(pub, lo_ub, hi_ub, max(1, Lub), u0, u1))
    d.pool_ub = pub

    if verbose:
        buck_bal = buck.functions.balanceOf(pub).call()
        usdc_bal = usdc.functions.balanceOf(pub).call()
        print(f"[deploy] BUCK/USDC pool {pub[:10]}...  "
              f"fee={FEE_BUCK_UB} ({TICK_SPACING[FEE_BUCK_UB]}-tick)")
        print(f"         sqrtPriceX96={spU}  implied $1.00/BUCK (by construction)")
        print(f"         reserves: {buck_bal/E18:,.2f} BUCK  "
              f"{usdc_bal/E6:,.2f} USDC")

    # LP-position metadata for ROI/APR accounting: (pool, owner, lo, hi,
    # group).  TOKEN/USDC + BUCK/USDC are SimLP-funded; TOKEN/BUCK are
    # direct-mint funded (owned by BuckBasket).  All full-range.
    lou, hiu = full_range_ticks(TICK_SPACING[FEE_USDC])
    lob_tok, hib_tok = full_range_ticks(TICK_SPACING[FEE_BUCK])
    lob_ub, hib_ub = full_range_ticks(TICK_SPACING[FEE_BUCK_UB])
    d.pool_meta = (
        [(d.pool_usdc[i], simlp.address, lou, hiu, "usdc") for i in range(len(tok))]
        + [(d.pool_buck[i], basket.address, lob_tok, hib_tok, "buck") for i in range(len(tok))]
        + [(d.pool_ub, simlp.address, lob_ub, hib_ub, "ub")]
    )
    return d
