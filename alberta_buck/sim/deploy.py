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

# --- Event topics, per basket implementation ------------------------------- #
#
# Every basket indexes (who, receiptId), so `topics[2]` is the receiptId.
# BuckBasketProRata pays out via ERC20 transfers and emits a 6-arg Redeemed;
# the equity shell emits the same two events.
PRORATA_DEPOSITED_TOPIC = Web3.keccak(
    text="Deposited(address,uint256,address,uint256,uint256,uint128)")
PRORATA_REDEEMED_TOPIC = Web3.keccak(
    text="Redeemed(address,uint256,uint256,uint256,uint256,uint256)")

ERC20_TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)")

# The names older imports use.
DEPOSITED_TOPIC = PRORATA_DEPOSITED_TOPIC
REDEEMED_TOPIC = PRORATA_REDEEMED_TOPIC


def wheel_arb_enabled(d=None) -> bool:
    """SIM_WHEEL_ARB=0 turns the wheel's arbitrage off (no triangles): on an
    equity basket, whose wheel must run to place deposits, it is the
    "wheel off" of the experiments."""
    if getattr(d, "buck_accounting", "") == "net":
        if not getattr(d, "equity_machine", False):
            if os.environ.get("SIM_WHEEL_ARB") not in (None, "0"):
                raise NotImplementedError("canonical funded arbitrage uses the machine wheel; legacy comparisons use SIM_WHEEL_ARB=0")
            return False
    return os.environ.get("SIM_WHEEL_ARB", "1") != "0"


def equity_arb_on(d) -> None:
    """Give the equity basket's wheel its arbitrage: one triangle per
    constituent (its TOKEN/BUCK pool, TOKEN/USDC, BUCK/USDC)."""
    if d.wheel is None or not wheel_arb_enabled(d) \
            or int(d.wheel.functions.triangleCount().call()) > 0:
        return
    if getattr(d, "buck_accounting", "") == "net":
        if not d.reg.functions.isCarrying(d.basket.address).call() \
                or not d.buck.functions.approvedCarryingIssuer(d.basket.address).call():
            raise ValueError("funded wheel arb requires the approved Carrying basket")
        d.chain.send(d.wheel.functions.setArb(d.pool_ub, d.basket.address, 0,
            int(os.environ.get("SIM_WHEEL_ARB_CAP_BP", "100")),
            int(os.environ.get("SIM_WHEEL_ARB_MIN_EDGE_BP", "0"))), sender=d.gov)
    for k, t in enumerate(d.tokens):
        d.chain.send(d.wheel.functions.setTriangle(
            k, (t.address, d.pool_buck[k], d.pool_usdc[k], k)), sender=d.gov)


def parse_redeem(d, rcpt, holder) -> tuple[int, int]:
    """(token_to_user, treasury_buck) from a redeem receipt, impl-aware.

    The equity baskets pay BUCK: token_to_user is then the BUCK paid.

    BuckBasketProRata pays the holder via ERC20 transfers, so sum the
    basket-TOKEN transfers basket->holder, and read `treasuryBuck` from its
    6-arg Redeemed.
    """
    from eth_abi import decode
    holder = Web3.to_checksum_address(holder)
    holder_bytes = bytes.fromhex(holder[2:])
    token_to_user = 0
    treasury_buck = 0

    basket_tokens = {c.address.lower() for c in d.tokens}
    # The equity baskets pay BUCK; a pro-rata exit's TOKEN (paid in kind)
    # is in the holder's portfolio, which the round trip values -- the
    # counter stays in BUCK.
    equity = str(d.basket_impl).startswith("equity")
    for log in rcpt["logs"]:
        t0 = log["topics"][0]
        if (not equity and t0 == ERC20_TRANSFER_TOPIC
                and log["address"].lower() in basket_tokens
                and len(log["topics"]) >= 3
                and bytes(log["topics"][2])[-20:] == holder_bytes):
            token_to_user += decode(["uint256"], bytes(log["data"]))[0]
        elif t0 == d.redeemed_topic:
            # (burned, depositorBuck, treasuryBuck, remainingBp)
            _, db, tb, _ = decode(
                ["uint256", "uint256", "uint256", "uint256"],
                bytes(log["data"]))
            treasury_buck += tb
            if equity:
                # The equity basket pays BUCK (depositorBuck); its
                # treasuryBuck is the value of the cut, taken in shares.
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
    basket_impl: str = "prorata"  # "prorata" (default) | "ops" | "equity" | "equity-ops"
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
    director: Any = None          # rebalance director (prorata only)
    director_impl: str = "pairs"  # "pairs": the one signal engine left
    controller_impl: str = "direct"   # "direct" (default) | "shadow"  (WP-3a)
    observer: Any = None          # ShadowObserver (shadow controller on ops only)
    desk: Any = None              # BasketDesk ("equity-ops": the desk, its own credit holder)
    equity_director: Any = None   # EquityTurnDirector (equity baskets)
    desk_grant: Any = None        # the desk's founding grant, TOKEN base units per constituent
    wheel: Any = None             # existing machine or component wheel
    equity_machine: bool = False  # machine configured before canonical opening deposits
    signals: Any = None
    pair_signals: Any = None
    pair_policy: Any = None
    monetary_policy: Any = None
    deposited_topic: bytes = DEPOSITED_TOPIC
    redeemed_topic: bytes = REDEEMED_TOPIC
    # WP-14: the per-class sim-only stabilizers (SimStabilizer per agent
    # class, decision 17), the classes registered with the observer, and
    # each class's (lambda, weight) 1e18 for lazy registration.
    sim_stabs: dict = field(default_factory=dict)
    sim_stab_reg: set = field(default_factory=set)
    sim_stab_gains: dict = field(default_factory=dict)


def _erc20_abi() -> list:
    abi, _ = load_artifact("MockERC20")
    return abi


def deploy(chain: Chain, anvil, scenario, rng, verbose=True,
           basket_impl="prorata", director_impl="pairs",
           controller_impl="direct", equity_machine=None) -> Deployment:
    w3 = chain.w3
    accts = w3.eth.accounts
    deployer, gov, issuer_addr = accts[0], accts[1], accts[3]
    credit_holder = basket_impl in ("equity", "equity-ops")
    machine = (os.environ.get("SIM_EQUITY_MACHINE", "0") == "1"
               if equity_machine is None else bool(equity_machine))
    if machine and (not credit_holder or controller_impl != "direct"):
        raise ValueError("the basket machine requires an equity basket and the direct K controller")
    if machine and len(scenario.tokens) < 2:
        raise ValueError("the basket machine requires at least two constituents")
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
    # WP-3a: "shadow" is BuckKControllerDirect reading the ops shell's
    # observer (bvib + lambda * netInventory / D) with gain scheduling on
    # the desk's saturation; same constructor, and with no observer wired
    # (or lambda = gamma = 0) it computes exactly what Direct does.
    kctrl = chain.deploy(
        "BuckKControllerShadow" if controller_impl == "shadow"
        else "BuckKControllerDirect",
        KP, KI, KD, dp.dt, KMIN, KMAX, K0, gov)
    if dp.dtmax_secs:
        chain.send(kctrl.functions.setDTMax(dp.dtmax_secs), sender=gov)
    # The insurance pool: a contract bound Carrying through the registry, as
    # Buck's pool is meant to be -- it holds premium deposits on its members'
    # behalf, so the demurrage they accrue travels with them instead of
    # eroding the reserve.  SimLP is a plain holder whose exec() lets the pool
    # act for itself (setBasket, below).
    pool = chain.deploy("SimLP")
    idmod.bind_as_operator(chain, reg, pool.address, True, True, sender=deployer)
    pool_acct = pool.address
    # Production Buck has no basket hooks: the equity baskets are credit
    # holders (a MARKED BuckCredit, a lien) and get it as is.  The pro-rata
    # baskets mint and burn through the hooks, so for them the sim deploys the
    # hooked subclass (src/legacy/BuckWithBasketHooks.sol).
    buck_artifact = "Buck" if credit_holder else "BuckWithBasketHooks"
    stack, basket_stack = None, None
    if credit_holder:
        stack = deploy_net_stack(chain, reg, credit, kctrl, gov,
                                 credit_deployer=deployer, insurance_pool=pool)
        buck = stack.token
        chain.send(stack.fund.functions.authorizeIdentityBinding(
            reg.address, deployer, operator_args[0], operator_args[1]), sender=gov)
        idmod.bind_as_operator(chain, reg, stack.fund.address, True, True, sender=deployer)
        chain.send(buck.functions.setInsurerPool(pool.address), sender=deployer)
        chain.send(pool.functions.exec(buck.address, buck.encode_abi(
            "approve(address,uint256)", args=[buck.address, 2**256 - 1])))
    else:
        buck = chain.deploy(buck_artifact, credit.address, kctrl.address, reg.address, pool_acct)
        chain.send(reg.functions.setBuck(buck.address), sender=gov)
        chain.send(credit.functions.setBuck(buck.address), sender=deployer)

    v3f = chain.deploy("UniswapV3Factory")
    # Constructor is identical for every shell (drop-in).  The spot/TWAP
    # guard tolerance is 5% (500 bp), for BUCK deposits and the redeem value
    # read; 5% keeps ordinary inter-tick commodity moves (6h ticks vs 600s
    # TWAP) from tripping it.
    ctor = (buck.address, kctrl.address, v3f.address, gov, FEE_BUCK, 600, 64, 500, 1000)
    venue = None
    if basket_impl in ("prorata", "ops"):
        # "ops" is BuckBasketProRata plus the monetary-operations desk on the
        # director's COMMON mode.  Identical constructor, identical venue,
        # and inert until setOpsParams enables it -- so an ops basket with no
        # policy installed is the baseline, byte for byte in behaviour.
        basket = chain.deploy(
            "BuckBasketOps" if basket_impl == "ops" else "BuckBasketProRata",
            *ctor)
        # Install the Uniswap V3 venue facet (the shell delegatecalls it) and
        # re-wrap the basket handle with the union ABI so Python can call facet
        # views (basketValueInBuck) that the shell serves via its fallback.
        venue = chain.deploy("BuckBasketUniswapV3")
        chain.send(basket.functions.setVenue(venue.address), sender=gov)
        shell_abi, _ = load_artifact(
            "BuckBasketOps" if basket_impl == "ops" else "BuckBasketProRata")
        facet_abi, _ = load_artifact("BuckBasketUniswapV3")
        union = shell_abi + [e for e in facet_abi if e not in shell_abi]
        basket = w3.eth.contract(address=basket.address, abi=union)
        deposited_topic, redeemed_topic = PRORATA_DEPOSITED_TOPIC, PRORATA_REDEEMED_TOPIC
    elif basket_impl in ("equity", "equity-ops"):
        # The equity basket (doc/BASKET-EQUITY.org 13.6): shares, one pooled
        # lien, BUCK payouts, a wallet its work wheel places.  Two facets:
        # the venue and the components; the shell emits the pro-rata
        # shells' Deposited / Redeemed, so the topics are theirs.
        # "equity-ops" adds the monetary desk BESIDE the basket (BasketDesk,
        # below): the basket itself is the same shell either way.
        name = "BuckBasketEquity"
        basket_stack = deploy_net_basket(chain, stack, v3f, gov, fee_tier=FEE_BUCK)
        basket = basket_stack.basket
        venue = chain.deploy("BuckBasketUniswapV3")
        chain.send(basket.functions.setVenue(venue.address), sender=gov)
        eq_facet = None
        if not machine:
            abi, bytecode = load_artifact("BuckBasketEquityWheel", libraries=basket_stack.libraries)
            eq_facet = chain.deploy("BuckBasketEquityWheel", abi=abi, bytecode=bytecode)
            chain.send(basket.functions.setEquityWheel(eq_facet.address), sender=gov)
        union, seen = [], set()
        for art in (name, "BuckBasketUniswapV3", "BuckBasketEquityWheel"):
            abi, _ = load_artifact(art)
            for e in abi:        # the shell's entry wins a shared signature
                key = (e.get("type"), e.get("name"),
                       tuple(i.get("type") for i in e.get("inputs", [])))
                if key not in seen:
                    seen.add(key)
                    union.append(e)
        basket = w3.eth.contract(address=basket.address, abi=union)
        deposited_topic, redeemed_topic = PRORATA_DEPOSITED_TOPIC, PRORATA_REDEEMED_TOPIC
    else:
        raise ValueError(f"unknown basket: {basket_impl!r} (prorata, ops, equity, equity-ops)")
    chain.send(kctrl.functions.setBasket(basket.address), sender=gov)
    if credit_holder:
        # A Carrying service: net-valued equity and separate NFT principal;
        # it issues itself its MARKED credit.  The face caps what it may ever
        # issue at K x face; 1e23 raw (1e17 BUCK) is no practical cap.
        chain.send(basket.functions.enableNetAccounting(), sender=gov)
        chain.send(stack.fund.functions.configure(buck.address, basket.address), sender=gov)
        if not machine:
            chain.send(basket.functions.authorizeIdentityBinding(
                reg.address, deployer, operator_args[0], operator_args[1]), sender=gov)
            idmod.bind_as_operator(chain, reg, basket.address, True, True, sender=deployer)
            chain.send(buck.functions.setApprovedCarryingIssuer(basket.address, True), sender=gov)
            chain.send(basket.functions.openCredit(credit.address, 10**23), sender=gov)
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
    if not credit_holder:
        chain.send(pool.functions.exec(
            buck.address, buck.encode_abi("setBasket", args=[basket.address])),
            sender=deployer)
        idmod.bind_as_operator(chain, reg, basket.address, True, True, sender=deployer)

    # --- WP-3a: the stabilizer seam and the observer ------------------ #
    # The observer (ShadowObserver) reads the basket's bvib and the ops
    # shell's shadowDepth(), so it is deployed only under --basket ops; on
    # any other basket the shadow controller reads basketValueInBuck()
    # directly and IS Direct.  The desk is the first registered stabilizer
    # and the sim's agent-booked offset the second, both at
    # SIM_SHADOW_LAMBDA (1.0 = the full inventory/depth ratio);
    # SIM_SHADOW_GAMMA schedules Ki by the desk's saturation.  Defaults 0:
    # the observer wired but inert, byte-for-byte Direct's trajectory.
    observer = None
    if controller_impl == "shadow" and basket_impl in ("ops", "equity-ops"):
        shadow_lambda = int(float(os.environ.get("SIM_SHADOW_LAMBDA", "0")) * E18)
        shadow_gamma = int(float(os.environ.get("SIM_SHADOW_GAMMA", "0")) * E18)
        # The desk is the stabilizer: the ops shell itself, or the BasketDesk
        # beside an equity basket (its venue answers bvib from the same pools).
        stab = desk if desk is not None else basket
        observer = chain.deploy("ShadowObserver", stab.address, gov)
        chain.send(observer.functions.addStabilizer(stab.address, shadow_lambda),
                   sender=gov)
        chain.send(observer.functions.setShadowLambda(shadow_lambda), sender=gov)
        chain.send(kctrl.functions.setObserver(observer.address), sender=gov)
        if shadow_gamma:
            chain.send(kctrl.functions.setGamma(shadow_gamma), sender=gov)
        if verbose:
            print(f"[deploy] shadow controller: observer={observer.address[:10]}... "
                  f"desk+offset lambda={shadow_lambda / E18:g} "
                  f"gamma={shadow_gamma / E18:g}")
    elif controller_impl == "shadow" and verbose:
        print(f"[deploy] shadow controller on a {basket_impl} basket: no "
              "observer, reads basketValueInBuck (== direct)")

    # --- WP-13: the corrected controller (D7) -------------------------- #
    # The position loop's gains, the observer's aggregation mode and the
    # per-stabilizer gains / weights, all from the environment (the form
    # the star driver's `env` axes use).  Defaults leave every banked cell
    # unchanged: mode S, Kq = Kqi = Kqd = 0 (the position loop inert, so
    # K is byte-for-byte Direct's), the desk and the pseudo-stabilizer at
    # weight 1 (decision 11: the offset stands in for the undertakings
    # today), lambdas as SIM_SHADOW_LAMBDA set them above.
    #
    #   SIM_SHADOW_MODE                s | v  (--shadow-mode sets it)
    #   SIM_SHADOW_KQ / _KQI / _KQD    REAL gains, stored real * 1e12 like
    #                                  kp / ki (experiment.derive_gains)
    #   SIM_SHADOW_LAMBDA_DESK / _OFFSET   S gains per stabilizer
    #                                  (default: SIM_SHADOW_LAMBDA, both)
    #   SIM_SHADOW_W_DESK / _OFFSET    V cost weights (default 1 and 1)
    #   SIM_SHADOW_OFFSET_CAP_USD      the pseudo-stabilizer's V cap in USD
    #                                  (BUCK 6-dec): the undertakings size
    #                                  their weak-side book reserve_frac (0.5)
    #                                  x NAV, and NAV ~ 2 x target_buck_m at
    #                                  the seed, so the default is
    #                                  target_buck_m ($10M); 0 excludes it
    #                                  from V (S needs no cap)
    # The stand-ins' inventory is booked into the pseudo-stabilizer by the
    # loop before each daily compute() (shadow_book.py).
    if observer is not None:
        shadow_mode = os.environ.get("SIM_SHADOW_MODE", "s").strip().lower()
        if shadow_mode not in ("s", "v"):
            raise ValueError(f"SIM_SHADOW_MODE must be s or v, not {shadow_mode!r}")
        kq  = int(round(float(os.environ.get("SIM_SHADOW_KQ",  "0")) * 1e12))
        kqi = int(round(float(os.environ.get("SIM_SHADOW_KQI", "0")) * 1e12))
        kqd = int(round(float(os.environ.get("SIM_SHADOW_KQD", "0")) * 1e12))
        lam_desk = os.environ.get("SIM_SHADOW_LAMBDA_DESK")
        lam_off  = os.environ.get("SIM_SHADOW_LAMBDA_OFFSET")
        w_desk = int(float(os.environ.get("SIM_SHADOW_W_DESK", "1")) * E18)
        w_off  = int(float(os.environ.get("SIM_SHADOW_W_OFFSET", "1")) * E18)
        cap_env = os.environ.get("SIM_SHADOW_OFFSET_CAP_USD")
        cap_off = (int(float(cap_env) * 10 ** 6) if cap_env is not None
                   else int(dp.target_buck))
        if shadow_mode == "v":
            chain.send(observer.functions.setMode(1), sender=gov)
        if kq or kqi or kqd:
            chain.send(kctrl.functions.setPositionGains(kq, kqi, kqd), sender=gov)
        if lam_desk is not None:
            chain.send(observer.functions.setStabilizerLambda(
                basket.address, int(float(lam_desk) * E18)), sender=gov)
        if lam_off is not None:
            chain.send(observer.functions.setShadowLambda(
                int(float(lam_off) * E18)), sender=gov)
        if w_desk != E18:
            chain.send(observer.functions.setStabilizerWeight(
                basket.address, w_desk), sender=gov)
        if w_off:
            chain.send(observer.functions.setShadowWeight(w_off), sender=gov)
        if cap_off:
            chain.send(observer.functions.setShadowCap(cap_off), sender=gov)
        if verbose:
            print(f"[deploy] WP-13 position loop: mode={shadow_mode.upper()} "
                  f"Kq={kq / 1e12:g} Kqi={kqi / 1e12:g} Kqd={kqd / 1e12:g}  "
                  f"lambda desk={float(lam_desk) if lam_desk is not None else shadow_lambda / E18:g} "
                  f"offset={float(lam_off) if lam_off is not None else shadow_lambda / E18:g}  "
                  f"w desk={w_desk / E18:g} offset={w_off / E18:g}  "
                  f"offset cap=${cap_off / 10 ** 6:,.0f}")

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
                   buck_accounting="net" if credit_holder else "legacy-signed",
                   net_stack=stack, net_basket=basket_stack, v3_factory=v3f,
                   equity_upkeep=eq_facet if credit_holder else None, venue=venue,
                   permit2=permit2, swap_guard=swap_guard,
                   controller_impl=controller_impl, observer=observer, desk=desk,
                   deposited_topic=deposited_topic, redeemed_topic=redeemed_topic)
    d.equity_machine = machine
    if stack is not None:
        def execute_credit(owner, target, method, args, *, expect=Expect.OK):
            # Household credit belongs to its public simulation proxy. These
            # fixture proxies have unrestricted exec; this is not a wallet.
            proxy = chain.at("SimLP", getattr(owner, "address", owner))
            return chain.send(proxy.functions.exec(target.address, target.encode_abi(method, args=args)), expect=expect)
        d.net_credit_ops = NetCreditOps(chain, stack, execute_credit)
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
    seed_buck = dp.target_buck // 10 if machine else 0
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
    if machine:
        for i, asset in enumerate(tok):
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

    # --- Rebalance director (standalone advisor; prorata only) ------------ #
    # Amortized rebalance-signal state machine: agents poke() it with a small
    # work budget; hints direct deposit routing and redemption draws.  Its
    # signal engine is differential-mode: per-leg tick-EMA ladders (5..320d),
    # pair quorum votes on the confirmed turn, matched pair trades.
    # DIRECTOR_DEADBAND_BP / DIRECTOR_QUORUM env overrides let short smoke
    # sims exercise the trade path (quorum 4 needs the 40-epoch window warm
    # -- pass DIRECTOR_QUORUM=2|3 for a 30-day run).
    if director_impl != "pairs":
        raise ValueError(f"unknown director: {director_impl!r} (pairs)")
    if basket_impl in ("prorata", "ops", "equity-ops") and not machine:
        # (on "equity-ops" the pairs director only feeds the desk its
        # common-mode signal; the basket rebalances itself -- see below)
        dir_deadband = int(os.environ.get("DIRECTOR_DEADBAND_BP", "150"))
        dir_quorum = int(os.environ.get("DIRECTOR_QUORUM", "4"))
        # boundaryBp: the no-trade-region form of effort sizing.  0 is not a
        # placeholder -- it is the measured right answer for a 30bp venue (the
        # refinement costs 5bp/yr there and only pays above ~50bp/leg).  Raise
        # it if the pools ever trade expensively.
        dir_boundary = int(os.environ.get("DIRECTOR_BOUNDARY_BP", "0"))
        director = chain.deploy(
            "PairsRebalanceDirector", basket.address, gov,
            (86400, dir_quorum, 500_000_000, dir_deadband * 100_000,
             300_000_000, 250_000_000, 50, dir_boundary))
        desc = (f"quorum={dir_quorum}/7 kappa=0.5"
                + (f" boundary={dir_boundary}bp" if dir_boundary else ""))
        chain.send(director.functions.syncConstituents())
        d.director = director
        d.director_impl = director_impl
        if basket_impl in ("ops", "equity-ops"):
            # Thresholds are in tick*1e9 and a tick is ~1bp, so 100e9 reads as
            # 100bp.  measIdx 2 is the 20-epoch rung: the article's sweep puts
            # the knee there, and past the 80-epoch rung the lagged reading
            # still says "dear" after the market has gone cheap, which answers
            # an inflation excursion by issuing more.  That is a correctness
            # bound, and setMonParams refuses anything slower than rung 3.
            mon_cap  = int(os.environ.get("SIM_OPS_CAP_BP", "40"))
            mon_meas = int(os.environ.get("SIM_OPS_MEAS", "2"))
            mon_pers = int(os.environ.get("SIM_OPS_PERSIST", "30"))
            chain.send(director.functions.setMonParams(
                (100_000_000_000,      # deadband 100bp
                 200_000_000_000,      # leash    200bp
                 250_000_000,          # kappa    0.25
                 mon_cap, mon_pers, mon_meas)), sender=gov)
            # The bounds.  They exist to stop the desk substituting a fast fix
            # for the slow, structural withdrawal of BUCK that K performs
            # through creditLimit -- so they are the knob that decides where
            # the two mandates overlap, and they are meant to be swept.
            leg_bp  = int(os.environ.get("SIM_OPS_LEG_BP", "40"))
            pos_bp  = int(os.environ.get("SIM_OPS_POSITION_BP", "1000"))
            out_bp  = int(os.environ.get("SIM_OPS_OUTRIGHT_BP", "1000"))
            # The desk: the ops shell itself, or the BasketDesk beside an
            # equity basket, which first mirrors the basket's pools.
            mon = desk if desk is not None else basket
            if desk is not None:
                chain.send(desk.functions.mirrorConstituents(), sender=gov)
                # Every credit-holder desk uses a finite gross programme.
                # Reversing direction does not replenish this allowance.
                capital_per_leg = int(os.environ.get("SIM_OPS_CAPITAL_USD", "400000"))
                budget = int(os.environ.get("SIM_OPS_TURNOVER_BUCK", str(capital_per_leg * len(tok))))
                floor = int(os.environ.get("SIM_OPS_CAPITAL_FLOOR_BUCK", "0"))
                chain.send(desk.functions.setRiskBudget(budget * E6, floor * E6, 500), sender=gov)
            chain.send(mon.functions.setMonetaryDirector(
                director.address), sender=gov)
            chain.send(mon.functions.setOpsParams(
                (leg_bp, pos_bp, out_bp, True)), sender=gov)
            # Founding reserves.  Without them the desk is inert in exactly
            # the regime it exists for: Q1/Q2 are TOKEN-funded and it may not
            # spend depositor TOKEN, so with BUCK persistently cheap it never
            # accumulates anything to defend with.
            # ref() is USDC-MICRO per whole token, so the budget has to be
            # micro too.  Plain dollars here under-capitalized the desk by
            # exactly 1e6 -- it received 0.0001 PAXG instead of 100, drained
            # the whole book on its first operation buying 1.3 BUCK, and then
            # reported "idle" for the rest of the run.
            cap_usd = int(os.environ.get("SIM_OPS_CAPITAL_USD", "400000"))
            for i, tc in enumerate(tok):
                ref0 = scenario.prices.ref(i, 0)
                amt = cap_usd * 10 ** 6 * (10 ** dec[i]) // ref0 if ref0 else 0
                if amt <= 0:
                    continue
                chain.send(tc.functions.mint(gov, amt))
                chain.send(tc.functions.approve(mon.address, amt), sender=gov)
                chain.send(mon.functions.capitalizeMonetary(i, amt), sender=gov)
                d.desk_grant = d.desk_grant or [0] * len(tok)
                d.desk_grant[i] = amt
            if verbose:
                print(f"[deploy] monetary desk capitalized "
                      f"${cap_usd:,}/token across {len(tok)} tokens")
            if verbose:
                print(f"[deploy] monetary desk ON  cap={mon_cap}bp/epoch "
                      f"meas=rung{mon_meas} persist={mon_pers}ep  "
                      f"leg={leg_bp}bp pos={pos_bp}bp outright={out_bp}bp")
        if verbose:
            print(f"[deploy] {director_impl} rebalance director "
                  f"{director.address[:10]}...  {desc}"
                  f" deadband={dir_deadband}bp cap=50bp/epoch")
    # --- The equity basket's director and work wheel ----------------- #
    # The basket rebalances itself: its wheel's Trim and Fund, gated and
    # leaned by the EquityTurnDirector (doc/BASKET-EQUITY.org 12.3, 13.6).
    # The wheel is not optional -- without it nothing a deposit brings is
    # ever placed -- so it is deployed here, beside the basket, and every
    # later caller (BasketWheelAgent, or the loop's own tick) turns it.
    if basket_impl in ("equity", "equity-ops"):
        # The director is optional (SIM_EQUITY_DIRECTOR=0: none -- the
        # components then keep the plain band).
        if not machine and os.environ.get("SIM_EQUITY_DIRECTOR", "1") != "0":
            eqd = chain.deploy("EquityTurnDirector", basket.address, gov)
            chain.send(basket.functions.setEquityDirector(eqd.address), sender=gov)
            d.equity_director = eqd
        wheel = chain.deploy(
            "BasketMachineWheel" if machine else "BasketWheel", buck.address, usdc.address, gov,
            int(os.environ.get("SIM_WHEEL_KAPPA_BP", "200")),
            int(float(os.environ.get("SIM_WHEEL_RESERVE_BUCK", "100")) * E6))
        if machine:
            chain.send(wheel.functions.authorizeIdentityBinding(
                deployer, operator_args[0], operator_args[1]), sender=gov)
        idmod.bind_as_operator(chain, reg, wheel.address, True, True, sender=deployer)
        idmod.identity_approve(chain, reg, buck, deployer, operator_args, operator_sk,
                               wheel.address, rng, amount=0)
        chain.send(basket.functions.setWheel(wheel.address), sender=gov)
        chain.send(wheel.functions.setEquity(basket.address), sender=gov)
        # The arbitrage is the basket's (ruled 2026-09-29): one triangle per
        # constituent -- its own TOKEN/BUCK pool, TOKEN/USDC, BUCK/USDC --
        # the outsider's trade made first, its profit (TOKEN start) credited
        # to the basket's wallet.
        # (Its triangles are set by `equity_arb_on`, once the pools have
        # their first pairings: before the market trades, an arbitrage
        # against half-built pools only moves the marks.)
        if not machine:
            chain.send(wheel.functions.setArb(
                d.pool_ub, basket.address,
                int(os.environ.get("SIM_WHEEL_ARB_SHARE_BP", "1000")),
                int(os.environ.get("SIM_WHEEL_ARB_CAP_BP", "200")), 1), sender=gov)
        d.wheel = wheel
        chain.send(wheel.functions.setValuationSource(stack.fund.address), sender=gov)
        chain.send(stack.fund.functions.setValuationWheel(wheel.address), sender=gov)
        chain.send(stack.fund.functions.setInvestmentKeeper(wheel.address), sender=gov)
        chain.send(wheel.functions.setJubilee(stack.fund.address,
                   (8, 1000, 100, dp.target_buck // 100, E6, 0)), sender=gov)
        if verbose:
            eqd_s = d.equity_director.address[:10] + "..." if d.equity_director else "none"
            print(f"[deploy] equity basket {basket.address[:10]}...  "
                  f"director {eqd_s}  wheel {wheel.address[:10]}... "
                  f"({int(wheel.functions.slotCount().call())} slots)")

    # --- WP-14: one sim-only stabilizer per agent class (decision 17) --- #
    # Replaces the ONE lumped pseudo-stabilizer of WP-3a / WP-13 (three
    # books summed under one lambda, one weight and one cap that was
    # target_buck_m rather than any class's real bound -- WP-16 read a full
    # undertakings book as a fill of 0.76 at depth 10 and 0.20 at depth 40)
    # with a SimStabilizer per class, registered at decision 11's lambdas
    # and cost weights, each booked with its REAL cap by shadow_book.py:
    #
    #   uts  the undertakings' strong side (issued)   cap reserve_frac x NAV
    #   utw  the undertakings' weak side (absorbed)   cap the ladder's R0
    #   fac  the facility population (drawn lines)   cap sum max_frac_i x L_i
    #   sd   the seeder's converted range            cap its funded amount
    #
    #   SIM_SHADOW_PERCLASS             1 (default) | 0 -- the lumped path
    #   SIM_SHADOW_W_UTS / _UTW / _FAC / _SD        V weights: 1, 1, 0.5, 0.25
    #   SIM_SHADOW_LAMBDA_UTS / _UTW / _FAC / _SD   S lambdas: default the
    #                                   pseudo-stabilizer's (SIM_SHADOW_LAMBDA_
    #                                   OFFSET, else SIM_SHADOW_LAMBDA)
    #
    # Only the classes PRESENT in the cell are registered (an absent class
    # would sit excluded and set a flag bit); the contracts for the other
    # classes are deployed anyway and registered by shadow_book.py on their
    # first cap (a class added by an intervention).  The contracts are
    # created from `gov` -- never from the deployer -- so the deployer's
    # nonce and every later address (tokens, pools, the agents' proxies)
    # stay exactly what they are on the lumped path: under S the per-class
    # sum is the same integer as the lumped offset, and K is identical.
    # The pseudo-stabilizer's V cap is cleared so an idle offset does not
    # dilute V (its lambda times a zero offset is 0 under S).
    # Default 1: decision 11's per-class caps and weights only mean what
    # they say per class; cells without an observer are untouched either
    # way, and the lumped path stays reachable for comparisons.
    perclass = os.environ.get("SIM_SHADOW_PERCLASS", "1").strip() not in ("0", "", "false", "no")
    if observer is not None and perclass:
        from buck_core.session import DEPLOY_GAS
        counts = getattr(scenario, "agents", {}) or {}
        present = {"uts": counts.get("UndertakingAgent", 0) > 0,
                   "utw": counts.get("UndertakingAgent", 0) > 0,
                   "fac": counts.get("FacilityAgent", 0) > 0,
                   "sd": counts.get("SeederAgent", 0) > 0}
        w_default = {"uts": "1", "utw": "1", "fac": "0.5", "sd": "0.25"}
        lam_default = os.environ.get("SIM_SHADOW_LAMBDA_OFFSET",
                                     os.environ.get("SIM_SHADOW_LAMBDA", "0"))
        sim_abi, sim_bc = load_artifact("SimStabilizer", "SimStabilizer")
        for cls in ("uts", "utw", "fac", "sd"):
            lam = int(float(os.environ.get(f"SIM_SHADOW_LAMBDA_{cls.upper()}",
                                           lam_default)) * E18)
            w = int(float(os.environ.get(f"SIM_SHADOW_W_{cls.upper()}",
                                         w_default[cls])) * E18)
            ctor = w3.eth.contract(abi=sim_abi, bytecode=sim_bc).constructor(
                gov, cls.encode().ljust(32, b"\0"))
            h = ctor.transact({"from": gov, "gas": DEPLOY_GAS, "gasPrice": 0})
            rcpt = w3.eth.wait_for_transaction_receipt(h)
            chain.clear_balance_cache()
            if rcpt["status"] != 1:
                raise RuntimeError(f"deploy SimStabilizer {cls} reverted")
            st = w3.eth.contract(address=rcpt["contractAddress"], abi=sim_abi)
            d.sim_stabs[cls] = st
            d.sim_stab_gains[cls] = (lam, w)
            if present[cls]:
                chain.send(observer.functions.addStabilizer(st.address, lam),
                           sender=gov)
                if w != E18:
                    chain.send(observer.functions.setStabilizerWeight(
                        st.address, w), sender=gov)
                d.sim_stab_reg.add(cls)
        if int(observer.functions.shadowCap().call()):
            chain.send(observer.functions.setShadowCap(0), sender=gov)
        if verbose:
            regs = " ".join(c for c in ("uts", "utw", "fac", "sd") if c in d.sim_stab_reg) or "none"
            print("[deploy] WP-14 per-class stabilizers: registered " + regs + "  "
                  + " ".join(f"{c}:lambda={d.sim_stab_gains[c][0] / E18:g}/w={d.sim_stab_gains[c][1] / E18:g}"
                             for c in ("uts", "utw", "fac", "sd"))
                  + "  (pseudo-stabilizer cap cleared)")
    if machine:
        from alberta_buck.sim.basket_machine import activate
        chosen = getattr(scenario, "experiment", None)
        activate(d, chosen.cfg.get("machine") if chosen is not None else None,
                 turn_seconds=86_400 * max(1, getattr(scenario, "day_step", 1)) // scenario.ticks_per_day)
    return d
