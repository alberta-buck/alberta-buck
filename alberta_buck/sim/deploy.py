"""Deploy + wire the full Direct system, V3 pools, and Universal Router.

Equity baskets use canonical compounded Buck, explicit NFT principal and one
Jubilee fund. The machine seeds independent full-range liquidity before its
strict TWAP venue is installed. BUCK services, including the funding SimLP,
have explicit identities. Synthetic infrastructure uses the labelled registry
harness; basket/fund/machine-wheel use their governed binding APIs as well.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from web3 import Web3
from buck_core import NetCreditOps, NetMarketOps, SettlementOps, deploy_net_basket, deploy_net_stack

from alberta_buck.sim import identity as idmod
from alberta_buck.sim.chain import Chain, Expect, load_artifact
from alberta_buck.sim.router import (
    sqrt_price_x96, full_range_ticks, MIN_SQRT_RATIO, MAX_SQRT_RATIO,
)

REPO = Path(__file__).resolve().parents[2]
UR_ARTIFACT = "alberta_buck/sim/artifacts/UniversalRouter.json"

E6 = 10 ** 6
E18 = 10 ** 18
FEE_USDC = 3000
# TOKEN/BUCK pools.  0.30% by default, but this is OUR choice and it is not
# obviously right: these pools exist to serve the BuckBasket, so the fee
# should be set by what the basket needs, not by pool revenue.  The fee cuts
# three ways -- it is a cost on the basket's own rebalancing, it is INCOME to
# the basket as the LP, and it is the gate that decides how small a
# mispricing an external arb will bother to close (i.e. how tightly the pool
# tracks the world, which is the basket's whole job as the reference mass).
# Override to compare: SIM_FEE_BUCK=500 for the 0.05% tier.
FEE_BUCK = int(os.environ.get("SIM_FEE_BUCK", "3000"))
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

# --- The basket's event topics ---------------------------------------------- #
# The basket indexes (who, receiptId), so `topics[2]` is the receiptId.
DEPOSITED_TOPIC = Web3.keccak(
    text="Deposited(address,uint256,address,uint256,uint256,uint128)")
REDEEMED_TOPIC = Web3.keccak(
    text="Redeemed(address,uint256,uint256,uint256,uint256,uint256)")


def wheel_arb_enabled(d=None) -> bool:
    """SIM_WHEEL_ARB=0 turns the machine wheel's arbitrage off (no triangles)."""
    return os.environ.get("SIM_WHEEL_ARB", "1") != "0"


def equity_arb_on(d) -> None:
    """Give the equity basket's wheel its arbitrage: one triangle per
    constituent (its TOKEN/BUCK pool, TOKEN/USDC, BUCK/USDC); then deepen the
    oracles of the routes the price book reads through them."""
    if d.wheel is None or not wheel_arb_enabled(d):
        return
    if int(d.wheel.functions.triangleCount().call()) == 0:
        _equity_triangles(d)
    if getattr(d, "signals", None) is not None and int(d.signals.functions.markets().call(), 16):
        # The price book reads these routes' TWAPs: their pools keep the basket's observations.
        # Whoever set the triangles (this, the deploy, a cast BasketWheelAgent), widen once they
        # exist: a route pool left at one observation reads "OLD" whenever it traded this block.
        d.chain.send(d.signals.functions.widen())


def _equity_triangles(d) -> None:
    if not d.reg.functions.isCarrying(d.basket.address).call() \
            or not d.buck.functions.approvedCarryingIssuer(d.basket.address).call():
        raise ValueError("funded wheel arb requires the approved Carrying basket")
    # A cycle is at most this share of its first pool's reserve: 5%, the machine wheel's limit (owner,
    # 2026-11-20: very large arbitrage; with arbFlash the credit line's room bounds it too).
    d.chain.send(d.wheel.functions.setArb(d.pool_ub, d.basket.address,
        int(os.environ.get("SIM_WHEEL_ARB_CAP_BP", "500")),
        int(os.environ.get("SIM_WHEEL_ARB_MIN_EDGE_BP", "0"))), sender=d.gov)
    for k, t in enumerate(d.tokens):
        d.chain.send(d.wheel.functions.setTriangle(
            k, (t.address, d.pool_buck[k], d.pool_usdc[k], k)), sender=d.gov)


def parse_redeem(d, rcpt, holder) -> tuple[int, int]:
    """(buck_to_user, treasury_buck) from a redeem receipt: the equity basket pays BUCK
    (depositorBuck); its treasuryBuck is the value of the cut, taken in shares. (An exit's TOKEN
    paid in kind is in the holder's portfolio, which the round trip values.)"""
    from eth_abi import decode
    token_to_user = 0
    treasury_buck = 0
    for log in rcpt["logs"]:
        if log["topics"][0] == d.redeemed_topic:
            # (burned, depositorBuck, treasuryBuck, remainingBp)
            _, db, tb, _ = decode(
                ["uint256", "uint256", "uint256", "uint256"],
                bytes(log["data"]))
            treasury_buck += tb
            token_to_user += db
    return token_to_user, treasury_buck


@dataclass
class Deployment:
    w3: Any
    chain: Chain
    anvil: Any
    gov: str
    pool_acct: str           # bootstrap insurer's Carrying SimLP; legacy setBasket caller
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
    basket_impl: str = "equity-ops"  # "equity-ops" (default: with the desk) | "equity" (the basket alone)
    buck_artifact: str = ""       # actual deployed contract, recorded in saved vectors
    buck_accounting: str = ""     # accounting model implemented by that deployment
    net_stack: Any = None         # canonical token, fund, quoter and four monetary libraries
    net_basket: Any = None        # the same basket plus its shared valuation library
    v3_factory: Any = None
    equity_upkeep: Any = None
    bootstrap_pool_meta: list = field(default_factory=list)  # independent SimLP seed positions
    net_credit_ops: Any = None    # actual selected-NFT operations through simulation proxies
    net_market_ops: Any = None    # guarded buys and bounded user-paid sales, through the same proxies
    net_settlement_ops: Any = None  # authenticated USDC payments; caller owns loan/pending bookkeeping
    permit2: Any = None
    swap_guard: Any = None
    venue: Any = None             # currently installed venue facet
    director: Any = None          # always None: the pairs director is retired (2026-11-20)
    director_impl: str = "pairs"  # the CLI's no-op --director, until the catalogue's arms are ported
    controller_impl: str = "direct"   # BuckKControllerDirect, the one left
    desk: Any = None              # BasketDesk ("equity-ops": the desk, its own credit holder)
    desk_grant: Any = None        # the desk's founding grant, TOKEN base units per constituent
    wheel: Any = None             # the basket machine's wheel
    equity_machine: bool = False  # set by deploy(): the machine is configured before opening deposits
    signals: Any = None
    pair_signals: Any = None
    pair_policy: Any = None
    monetary_policy: Any = None
    deposited_topic: bytes = DEPOSITED_TOPIC
    redeemed_topic: bytes = REDEEMED_TOPIC


def _erc20_abi() -> list:
    abi, _ = load_artifact("MockERC20")
    return abi


def deploy(chain: Chain, anvil, scenario, rng, verbose=True,
           basket_impl="equity-ops", director_impl="pairs",
           controller_impl="direct", equity_machine=None) -> Deployment:
    # The equity basket on the basket machine is the only basket (owner, 2026-11-20: the pro-rata
    # and ops shells, the earlier upkeep, the pairs director and the shadow controller are retired).
    if basket_impl not in ("equity", "equity-ops"):
        raise ValueError(f"unknown basket: {basket_impl!r} (equity, equity-ops; the others are retired)")
    if controller_impl != "direct":
        raise ValueError(f"unknown controller: {controller_impl!r} (direct; the shadow controller is retired)")
    if equity_machine is False:
        raise ValueError("the earlier upkeep is retired: every equity basket runs on the basket machine")
    if len(scenario.tokens) < 2:
        raise ValueError("the basket machine requires at least two constituents")
    w3 = chain.w3
    accts = w3.eth.accounts
    deployer, gov, issuer_addr = accts[0], accts[1], accts[3]
    erc20_abi = _erc20_abi()

    # --- identity layer ---------------------------------------------- #
    # The simulation deploys synthetic infrastructure (SimLP, routers, and
    # basket variants) that has no production binding-authorizer surface.
    # Use the explicitly test-only registry harness for those fixture binds;
    # production deployments use IdentityRegistry plus target-specific adapters.
    reg = chain.deploy("IdentityRegistryHarness", gov)
    issuer_kp = idmod.make_issuer(rng)
    chain.send(reg.functions.trustIssuer(issuer_addr, idmod.pspubkey_arg(issuer_kp)),
               sender=gov)
    # Certified-operator bind copies this identity onto BUCK-touching contracts.
    operator_args, operator_sk = idmod.register_args(
        issuer_kp, int(deployer, 16), idmod.fields_for("Deployer", 0), rng,
        int(w3.eth.chain_id), int(reg.address, 16), with_sk=True)
    chain.send(reg.functions.register(issuer_addr, *operator_args), sender=deployer)

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
    kctrl = chain.deploy("BuckKControllerDirect", KP, KI, KD, dp.dt, KMIN, KMAX, K0, gov)
    if dp.dtmax_secs:
        chain.send(kctrl.functions.setDTMax(dp.dtmax_secs), sender=gov)
    # Always: the contract's own default is a year, so a world without a leak (0) must say so.
    chain.send(kctrl.functions.setLeak(dp.leak_secs), sender=gov)
    # The insurance pool: a contract bound Carrying through the registry, as
    # Buck's pool is meant to be -- it holds premium deposits on its members'
    # behalf, so the demurrage they accrue travels with them instead of
    # eroding the reserve.  SimLP is a plain holder whose exec() lets the pool
    # act for itself (setBasket, below).
    pool = chain.deploy("SimLP")
    idmod.bind_as_operator(chain, reg, pool.address, True, True, sender=deployer)
    pool_acct = pool.address
    # Production Buck has no basket hooks: the equity basket is a credit holder (a MARKED
    # BuckCredit, a lien) and gets it as is.
    buck_artifact = "Buck"
    stack = deploy_net_stack(chain, reg, credit, kctrl, gov,
                             credit_deployer=deployer, insurance_pool=pool)
    buck = stack.token
    chain.send(stack.fund.functions.authorizeIdentityBinding(
        reg.address, deployer, operator_args[0], operator_args[1]), sender=gov)
    idmod.bind_as_operator(chain, reg, stack.fund.address, True, True, sender=deployer)
    chain.send(buck.functions.setInsurerPool(pool.address), sender=deployer)
    chain.send(pool.functions.exec(buck.address, buck.encode_abi(
        "approve(address,uint256)", args=[buck.address, 2**256 - 1])))

    v3f = chain.deploy("UniswapV3Factory")
    # The equity basket (doc/BASKET-EQUITY.org 13.6): shares, one pooled lien, BUCK payouts. Its
    # facets are the machine's (basket_machine.activate installs them once the pools have TWAP
    # history); until then the plain V3 venue serves the shell. "equity-ops" adds the monetary desk
    # BESIDE the basket (BasketDesk, below): the basket itself is the same shell either way. The
    # pools keep OBSERVATIONS of oracle history (net_world): at 12 s blocks, 2.5x the 600 s TWAP
    # window even if every block trades (the price book's route pools are widened to match).
    name = "BuckBasketEquity"
    basket_stack = deploy_net_basket(chain, stack, v3f, gov, fee_tier=FEE_BUCK)
    basket = basket_stack.basket
    venue = chain.deploy("BuckBasketUniswapV3")
    chain.send(basket.functions.setVenue(venue.address), sender=gov)
    union, seen = [], set()
    for art in (name, "BasketVenueFacet", "BasketUpkeepFacet"):
        abi, _ = load_artifact(art)
        for e in abi:        # the shell's entry wins a shared signature
            key = (e.get("type"), e.get("name"),
                   tuple(i.get("type") for i in e.get("inputs", [])))
            if key not in seen:
                seen.add(key)
                union.append(e)
    basket = w3.eth.contract(address=basket.address, abi=union)
    deposited_topic, redeemed_topic = DEPOSITED_TOPIC, REDEEMED_TOPIC
    chain.send(kctrl.functions.setBasket(basket.address), sender=gov)
    # A Carrying service: net-valued equity and separate NFT principal; it issues itself its MARKED
    # credit (opened by basket_machine.activate).
    chain.send(basket.functions.enableNetAccounting(), sender=gov)
    chain.send(stack.fund.functions.configure(buck.address, basket.address), sender=gov)
    desk = None
    if basket_impl == "equity-ops":
        # The monetary desk is its own credit holder beside the basket: its
        # own contract, account at Buck, MARKED credit and invoker (the
        # MonetaryKeeperAgent), trading in the basket's pools through the same
        # venue facet.  Nothing it holds or owes enters the basket's mark.
        # Its constituents are mirrored, and its policy and founding grant
        # set, once the basket has its pools (the desk block below).
        abi, bytecode = load_artifact("BasketDesk", libraries=basket_stack.libraries)
        desk = chain.deploy("BasketDesk", buck.address, kctrl.address,
                            basket.address, gov, abi=abi, bytecode=bytecode)
        idmod.bind_as_operator(chain, reg, desk.address, True, False, sender=deployer)
        chain.send(desk.functions.enableNetAccounting(), sender=gov)
        chain.send(desk.functions.setVenue(venue.address), sender=gov)
        chain.send(desk.functions.openCredit(credit.address, 10**23), sender=gov)
        desk_union, seen = [], set()
        for art in ("BasketDesk", "BuckBasketUniswapV3"):
            abi, _ = load_artifact(art)
            for e in abi:
                key = (e.get("type"), e.get("name"),
                       tuple(i.get("type") for i in e.get("inputs", [])))
                if key not in seen:
                    seen.add(key)
                    desk_union.append(e)
        desk = w3.eth.contract(address=desk.address, abi=desk_union)

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
    # The funding holder is public and ordinary. Canonical mint issues its
    # entire requested cash immediately; later LP transfers do not issue or
    # retire principal. The explicitly hooked legacy comparison has its
    # original headroom behavior.
    idmod.bind_as_operator(chain, reg, simlp.address, True, False, sender=deployer)
    big = 10 ** 30
    chain.send(usdc.functions.mint(simlp.address, big))
    for c in tok:
        chain.send(c.functions.mint(simlp.address, big))

    # --- WETH9 + Universal Router ------------------------------------ #
    weth = chain.deploy("WETH9")
    _, pool_bc = load_artifact("UniswapV3Pool")
    pool_init_hash = Web3.keccak(hexstr=pool_bc if pool_bc.startswith("0x") else "0x" + pool_bc)
    Z = "0x" + "0" * 40
    permit2 = None
    if stack is not None:
        permit_art = json.loads((REPO / "alberta_buck/sim/artifacts/Permit2.json").read_text())
        permit2 = chain.deploy("Permit2", abi=permit_art["abi"], bytecode=permit_art["bytecode"]["object"])
    ur_params = (
        permit2.address if permit2 is not None else Web3.to_checksum_address("0x" + "be" * 20),
        weth.address, Z, v3f.address,
        b"\x00" * 32, pool_init_hash,
        Z, Z, Z, Z,
    )
    ur_art = json.loads((REPO / UR_ARTIFACT).read_text())
    router = chain.deploy("UniversalRouter", ur_params,
                          abi=ur_art["abi"], bytecode=ur_art["bytecode"]["object"])
    idmod.bind_as_operator(chain, reg, router.address, True, True, sender=deployer)
    swap_guard = chain.deploy("BuckNetSwapGuard", buck.address, usdc.address, v3f.address) if stack else None

    d = Deployment(w3, chain, anvil, gov, pool_acct, issuer_addr, issuer_kp,
                   reg, buck, credit, kctrl, basket, router, simlp, usdc,
                   erc20_abi, tok, dec,
                   basket_impl=basket_impl, buck_artifact=buck_artifact,
                   buck_accounting="net",
                   net_stack=stack, net_basket=basket_stack, v3_factory=v3f,
                   venue=venue,
                   permit2=permit2, swap_guard=swap_guard,
                   controller_impl=controller_impl, desk=desk,
                   deposited_topic=deposited_topic, redeemed_topic=redeemed_topic)
    d.equity_machine = True
    if stack is not None:
        def execute_credit(owner, target, method, args, *, expect=Expect.OK):
            # Household credit belongs to its public simulation proxy. These
            # fixture proxies have unrestricted exec; this is not a wallet.
            proxy = chain.at("SimLP", getattr(owner, "address", owner))
            return chain.send(proxy.functions.exec(target.address, target.encode_abi(method, args=args)), expect=expect)
        def simulate_owner(owner, target, method, args, block_number=None):
            # Read-only twin of execute_credit: eth_call the same proxy exec and
            # discard its writes. A direct call from the proxy's own address is
            # refused before execution by pyrevm (EIP-3607: sender has code).
            proxy = chain.at("SimLP", getattr(owner, "address", owner))
            return proxy.functions.exec(target.address, target.encode_abi(method, args=args)).call(
                block_identifier="latest" if block_number is None else block_number)
        d.net_simulate = simulate_owner
        d.net_credit_ops = NetCreditOps(chain, stack, execute_credit, simulate_owner)
        dispatch_market = partial(execute_credit, expect=Expect.EITHER)
        d.net_market_ops = NetMarketOps(chain, buck, swap_guard, dispatch_market, router=router, settlement=usdc)
        d.net_settlement_ops = SettlementOps(chain, usdc, dispatch_market)

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
        idmod.bind_as_operator(chain, reg, pb, True, True, sender=deployer)
        d.pool_buck.append(pb)
        if verbose:
            print(f"[deploy] TOKEN/BUCK {sym}/BUCK pool {pb[:10]}...  "
                  f"fee={FEE_BUCK} ({TICK_SPACING[FEE_BUCK]}-tick)  "
                  f"targetBp={wbp[i]} (passed {pass_wbp[i]})")
            print(f"         empty pool — bootstrap DM agents will seed")

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
    # Reserve a tenth of the common depth for each independent TOKEN/BUCK
    # seed in machine mode, matching JS. A 20% cash margin covers rounding
    # and elapsed demurrage. In canonical accounting all minted cash is real
    # principal, including that margin; it remains in the funding holder.
    k0 = kctrl.functions.buckK().call()
    seed_buck = dp.target_buck // 10
    funding = dp.target_buck_lp + seed_buck * len(tok)
    mint_amt = funding * 12 // 10
    FACE = max(2 * funding, (mint_amt * E18 // max(1, k0)) * 12 // 10)

    now_ts = w3.eth.get_block("latest")["timestamp"]
    # A credit only lands where its recipient asked for it, so SimLP has to
    # name the deployer as an insurer it will accept before the credit can be
    # issued.  SimLP is a contract and cannot sign, so the opt-in goes through
    # its exec() passthrough.
    chain.send(simlp.functions.exec(
        credit.address,
        credit.encode_abi("setCreditIssuer", args=[getattr(deployer, "address", deployer), True]),
    ), sender=deployer)
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
    idmod.bind_as_operator(chain, reg, pub, True, True, sender=deployer)
    # Sized off target_buck_lp -- the knob DOCUMENTED as the BUCK/USDC seed
    # (it previously keyed off target_buck, silently coupling the floating
    # pool's depth to the TOKEN/BUCK pools').  A national-scale currency
    # pair is deep; a shallow floating pool is an artificial exit-route
    # bottleneck (in reality best-cost routing would also spread exits over
    # BUCK/TOKEN->TOKEN/USDC legs).
    if usdc.address.lower() == u0.lower():
        Lub = dp.target_buck_lp * spU // Q96
    else:
        Lub = dp.target_buck_lp * Q96 // spU
    lo_ub, hi_ub = full_range_ticks(TICK_SPACING[FEE_BUCK_UB])
    chain.send(simlp.functions.mint(pub, lo_ub, hi_ub, max(1, Lub), u0, u1))
    d.pool_ub = pub
    for i, asset in enumerate(tok):    # the machine's independent seed positions
        pc = w3.eth.contract(address=d.pool_buck[i], abi=pool_v3_abi)
        a0, a1 = pc.functions.token0().call(), pc.functions.token1().call()
        sp = sqrt_price_x96(asset.address, 10**dec[i], buck.address, scenario.prices.day0(i))
        liquidity = (seed_buck * sp // Q96 if buck.address.lower() == a0.lower()
                     else seed_buck * Q96 // sp)
        lo, hi = full_range_ticks(TICK_SPACING[FEE_BUCK])
        chain.send(simlp.functions.mint(pc.address, lo, hi, max(1, liquidity), a0, a1))
        d.bootstrap_pool_meta.append((pc.address, simlp.address, lo, hi, "bootstrap"))

    if verbose:
        buck_bal = buck.functions.balanceOf(pub).call()
        usdc_bal = usdc.functions.balanceOf(pub).call()
        print(f"[deploy] BUCK/USDC pool {pub[:10]}...  "
              f"fee={FEE_BUCK_UB} ({TICK_SPACING[FEE_BUCK_UB]}-tick)")
        print(f"         sqrtPriceX96={spU}  implied $1.00/BUCK (by construction)")
        print(f"         reserves: {buck_bal/E6:,.2f} BUCK  "
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

    # The director slot is a no-op since the pairs director's retirement (2026-11-20); the flag stays
    # until the catalogue's stress arms are ported.
    if director_impl != "pairs":
        raise ValueError(f"unknown director: {director_impl!r} (pairs)")
    # --- The basket machine's wheel ----------------------------------- #
    # The wheel is not optional -- without it nothing a deposit brings is
    # ever placed -- so it is deployed here, beside the basket, and every
    # later caller (BasketWheelAgent, or the loop's own tick) turns it.
    wheel = chain.deploy(
        "BasketMachineWheel", buck.address, usdc.address, gov,
        int(os.environ.get("SIM_WHEEL_KAPPA_BP", "200")),
        int(float(os.environ.get("SIM_WHEEL_RESERVE_BUCK", "100")) * E6))
    chain.send(wheel.functions.authorizeIdentityBinding(
        deployer, operator_args[0], operator_args[1]), sender=gov)
    idmod.bind_as_operator(chain, reg, wheel.address, True, True, sender=deployer)
    idmod.identity_approve(chain, reg, buck, deployer, operator_args, operator_sk,
                           wheel.address, rng, amount=0)
    chain.send(basket.functions.setWheel(wheel.address), sender=gov)
    chain.send(wheel.functions.setEquity(basket.address), sender=gov)
    # The arbitrage is the basket's (ruled 2026-09-29): one triangle per
    # constituent -- its own TOKEN/BUCK pool, TOKEN/USDC, BUCK/USDC. Its
    # triangles are set by `equity_arb_on`, once the pools have their first
    # pairings: before the market trades, an arbitrage against half-built
    # pools only moves the marks.
    d.wheel = wheel
    chain.send(wheel.functions.setValuationSource(stack.fund.address), sender=gov)
    chain.send(stack.fund.functions.setValuationWheel(wheel.address), sender=gov)
    chain.send(stack.fund.functions.setInvestmentKeeper(wheel.address), sender=gov)
    retire_cap = getattr(dp, "basket_retirement_cap", 0)
    chain.send(wheel.functions.setJubilee(stack.fund.address,
               (8, 1000, 100, dp.target_buck // 100, E6, retire_cap)), sender=gov)
    if verbose:
        print(f"[deploy] equity basket {basket.address[:10]}...  "
              f"wheel {wheel.address[:10]}... "
              f"({int(wheel.functions.slotCount().call())} slots)")
    from alberta_buck.sim.basket_machine import activate
    chosen = getattr(scenario, "experiment", None)
    activate(d, chosen.cfg.get("machine") if chosen is not None else None,
             turn_seconds=86_400 * max(1, getattr(scenario, "day_step", 1)) // scenario.ticks_per_day)
    return d
