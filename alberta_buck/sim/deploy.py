"""Deploy + wire the full Direct system, V3 pools, and Universal Router.

Order mirrors test/BuckBasket.t.sol::setUp.  Only BUCK-touching contracts (BuckBasket,
each TOKEN/BUCK pool, the Universal Router) get a public bindContract
Identity; TOKEN/USDC pools and SimLP never custody BUCK so they need none.
"""

from __future__ import annotations

import json
import os
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


def wheel_arb_enabled() -> bool:
    """SIM_WHEEL_ARB=0 turns the wheel's arbitrage off (no triangles): on an
    equity basket, whose wheel must run to place deposits, it is the
    "wheel off" of the experiments."""
    return os.environ.get("SIM_WHEEL_ARB", "1") != "0"


def equity_arb_on(d) -> None:
    """Give the equity basket's wheel its arbitrage: one triangle per
    constituent (its TOKEN/BUCK pool, TOKEN/USDC, BUCK/USDC)."""
    if d.wheel is None or not wheel_arb_enabled() \
            or int(d.wheel.functions.triangleCount().call()) > 0:
        return
    for k, t in enumerate(d.tokens):
        d.chain.send(d.wheel.functions.setTriangle(
            k, (t.address, d.pool_buck[k], d.pool_usdc[k], k)), sender=d.gov)


def parse_redeem(d, rcpt, holder) -> tuple[int, int]:
    """(token_to_user, treasury_buck) from a redeem receipt, impl-aware.

    The equity baskets pay BUCK: token_to_user is then the BUCK paid.

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

    # Anything that is NOT the legacy shell pays via ERC20 transfers and
    # emits the 6-arg Redeemed.  Enumerating implementations here instead
    # silently mis-parsed the "ops" shell as legacy: it emits no
    # RedeemedFromPool, so every redemption booked 0 treasury BUCK and 0
    # TOKEN returned, and an A/B read as the desk having consumed the entire
    # treasury when nothing of the sort had happened.
    if d.basket_impl != "legacy":
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
    pool_acct: str           # Buck.insurancePool: a Carrying SimLP (+ setBasket caller)
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
    pool_fence: list = field(default_factory=list)  # BuckBasketFence bands
    pool_meta: list = field(default_factory=list)   # (pool,owner,lo,hi,group)
    fee_usdc: int = FEE_USDC
    fee_buck: int = FEE_BUCK      # TOKEN/BUCK pools
    fee_ub: int = FEE_BUCK_UB     # floating BUCK/USDC pool
    basket_impl: str = "prorata"  # "prorata" (default) | "ops" | "fence" | "equity"
                                  # | "equity-ops" | "legacy"
    venue: Any = None             # BuckBasketUniswapV3 facet (prorata only)
    director: Any = None          # rebalance director (prorata only)
    director_impl: str = "pairs"  # "pairs" (default) | "vrate"
    controller_impl: str = "direct"   # "direct" (default) | "shadow"  (WP-3a)
    observer: Any = None          # ShadowObserver (shadow controller on ops only)
    desk: Any = None              # EquityDesk ("equity-ops": the desk, its own credit holder)
    equity_director: Any = None   # EquityTurnDirector (equity baskets)
    wheel: Any = None             # BasketWheel (equity baskets: it places deposits)
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
           controller_impl="direct") -> Deployment:
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
    idmod.register_deployer(chain, reg, issuer_addr, issuer_kp, rng,
                            int(w3.eth.chain_id), sender=deployer)

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
    credit_holder = basket_impl in ("equity", "equity-ops")
    buck = chain.deploy("Buck" if credit_holder else "BuckWithBasketHooks",
                        credit.address, kctrl.address, reg.address, pool_acct)
    chain.send(reg.functions.setBuck(buck.address), sender=gov)
    # Wire BuckCredit -> Buck so activation can flow through Buck.mint ->
    # activateFromBuck (which requires msg.sender == buck).  The wiring is an
    # authorisation record only: BuckCredit never calls back into Buck.
    chain.send(credit.functions.setBuck(buck.address), sender=deployer)

    v3f = chain.deploy("UniswapV3Factory")
    # Constructor is identical for both implementations (drop-in).  The
    # spot/TWAP guard tolerance is 5% (500 bp): legacy uses it only for BUCK
    # deposits, ProRata also for the redeem value read; 5% keeps ordinary
    # inter-tick commodity moves (6h ticks vs 600s TWAP) from tripping it.
    ctor = (buck.address, kctrl.address, v3f.address, gov, FEE_BUCK, 600, 64, 500, 1000)
    venue = None
    if basket_impl in ("prorata", "ops", "fence"):
        # "ops" is BuckBasketProRata plus the monetary-operations desk on the
        # director's COMMON mode.  Identical constructor, identical venue,
        # and inert until setOpsParams enables it -- so an ops basket with no
        # policy installed is the baseline, byte for byte in behaviour.
        if basket_impl == "fence":
            # The fence tier defaults to the DEPOSITOR tier, which makes
            # _findOrCreatePool hand back the constituent's own pool.  The
            # separate tier was proposed to keep the fence out of
            # poolBuckValues, but BuckBasketFence overrides _redeem and
            # prices claims from exact V3 math on its own band, so nothing
            # reads poolBuckValues on this path.  A separate tier would
            # instead make the experiment inert: basketValueInBuck -- what K
            # reads -- comes from the constituent's pool, so a fence
            # elsewhere could not move K's signal without an arbitrageur
            # linking the two.  SIM_FENCE_TIER=500 restores the split.
            fence_tier = int(os.environ.get("SIM_FENCE_TIER", str(FEE_BUCK)))
            basket = chain.deploy("BuckBasketFence", *ctor, fence_tier)
        else:
            basket = chain.deploy(
                "BuckBasketOps" if basket_impl == "ops" else "BuckBasketProRata",
                *ctor)
        # Install the Uniswap V3 venue facet (the shell delegatecalls it) and
        # re-wrap the basket handle with the union ABI so Python can call facet
        # views (basketValueInBuck) that the shell serves via its fallback.
        venue = chain.deploy("BuckBasketUniswapV3")
        chain.send(basket.functions.setVenue(venue.address), sender=gov)
        shell_abi, _ = load_artifact(
            {"ops": "BuckBasketOps", "fence": "BuckBasketFence"}
            .get(basket_impl, "BuckBasketProRata"))
        facet_abi, _ = load_artifact("BuckBasketUniswapV3")
        union = shell_abi + [e for e in facet_abi if e not in shell_abi]
        basket = w3.eth.contract(address=basket.address, abi=union)
        deposited_topic, redeemed_topic = PRORATA_DEPOSITED_TOPIC, PRORATA_REDEEMED_TOPIC
    elif basket_impl in ("equity", "equity-ops"):
        # The equity basket (doc/BASKET-EQUITY.org 13.6): shares, one pooled
        # lien, BUCK payouts, a wallet its work wheel places.  Two facets:
        # the venue and the components; the shell emits the pro-rata
        # shells' Deposited / Redeemed, so the topics are theirs.
        # "equity-ops" adds the monetary desk BESIDE the basket (EquityDesk,
        # below): the basket itself is the same shell either way.
        name = "BuckBasketEquity"
        basket = chain.deploy(name, *ctor)
        venue = chain.deploy("BuckBasketUniswapV3")
        chain.send(basket.functions.setVenue(venue.address), sender=gov)
        eq_facet = chain.deploy("BuckBasketEquityWheel")
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
        basket = chain.deploy("BuckBasket", *ctor)
        deposited_topic, redeemed_topic = LEGACY_DEPOSITED_TOPIC, LEGACY_REDEEMED_TOPIC
    chain.send(kctrl.functions.setBasket(basket.address), sender=gov)
    if credit_holder:
        # A credit holder: bound public and NON-Carrying (it holds a lien), and
        # it issues itself its MARKED credit.  The face caps what it may ever
        # issue at K x face; 1e23 raw (1e17 BUCK) is no practical cap.
        idmod.bind_as_operator(chain, reg, basket.address, True, False, sender=deployer)
        chain.send(basket.functions.openCredit(credit.address, 10**23), sender=gov)
    desk = None
    if basket_impl == "equity-ops":
        # The monetary desk is its own credit holder beside the basket: its
        # own contract, account at Buck, MARKED credit and invoker (the
        # MonetaryKeeperAgent), trading in the basket's pools through the same
        # venue facet.  Nothing it holds or owes enters the basket's mark.
        # Its constituents are mirrored, and its policy and founding grant
        # set, once the basket has its pools (the desk block below).
        desk = chain.deploy("EquityDesk", buck.address, kctrl.address,
                            basket.address, gov)
        idmod.bind_as_operator(chain, reg, desk.address, True, False, sender=deployer)
        chain.send(desk.functions.setVenue(venue.address), sender=gov)
        chain.send(desk.functions.openCredit(credit.address, 10**23), sender=gov)
        desk_union, seen = [], set()
        for art in ("EquityDesk", "BuckBasketUniswapV3"):
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
        # The desk is the stabilizer: the ops shell itself, or the EquityDesk
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

    fence_factors: list = []

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
    ur_params = (
        Web3.to_checksum_address("0x" + "be" * 20),  # permit2 (unused)
        weth.address, Z, v3f.address,
        b"\x00" * 32, pool_init_hash,
        Z, Z, Z, Z,
    )
    ur_art = json.loads((REPO / UR_ARTIFACT).read_text())
    router = chain.deploy("UniversalRouter", ur_params,
                          abi=ur_art["abi"], bytecode=ur_art["bytecode"]["object"])
    idmod.bind_as_operator(chain, reg, router.address, True, True, sender=deployer)

    d = Deployment(w3, chain, anvil, gov, pool_acct, issuer_addr, issuer_kp,
                   reg, buck, credit, kctrl, basket, router, simlp, usdc,
                   erc20_abi, tok, dec,
                   basket_impl=basket_impl, venue=venue,
                   controller_impl=controller_impl, observer=observer, desk=desk,
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
        idmod.bind_as_operator(chain, reg, pb, True, True, sender=deployer)
        d.pool_buck.append(pb)
        if basket_impl == "fence":
            # Strike the first band.  Must follow addBasketToken: the fence
            # is priced and centred off the constituent record.
            chain.send(basket.functions.openFence(i), sender=gov)
            d.pool_fence.append(
                basket.functions.fenceOf(i).call()[0])
            fence_factors.append(i)

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
    # Buck.mint(amount) raises spendable by `amount` at the K it runs at,
    # activating amount / K of a zero-premium face.  Mint TARGET_BUCK_LP with
    # a 20% margin so full-range rounding never trips "amount exceeds
    # spendable", on a face that covers it at the live resting K0 with a
    # further 20%.  Only the LP transfer (~TARGET_BUCK_LP) actually enters
    # supply; the surplus headroom is inert, so downstream pool depth /
    # totalSupply are unchanged.
    k0 = kctrl.functions.buckK().call()
    mint_amt = dp.target_buck_lp * 12 // 10
    FACE = max(2 * dp.target_buck_lp, (mint_amt * E18 // max(1, k0)) * 12 // 10)

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

    # --- Rebalance director (standalone advisor; prorata only) ------------ #
    # Amortized rebalance-signal state machine: agents poke() it with a small
    # work budget; hints direct deposit routing and redemption draws.  Two
    # signal engines share the chassis (IRebalanceDirector-compatible):
    #   vrate -- per-constituent share-deviation regime + rate-matched sizing
    #            (window 15d, rho 3);
    #   pairs -- differential-mode: per-leg tick-EMA ladders (5..320d), pair
    #            quorum votes on the confirmed turn, matched pair trades.
    # DIRECTOR_WINDOW / DIRECTOR_DEADBAND_BP / DIRECTOR_QUORUM env overrides
    # let short smoke sims exercise the trade path (pairs quorum 4 needs the
    # 40-epoch window warm -- pass DIRECTOR_QUORUM=2|3 for a 30-day run).
    if basket_impl in ("prorata", "ops", "fence", "equity-ops"):
        # (on "equity-ops" the pairs director only feeds the desk its
        # common-mode signal; the basket rebalances itself -- see below)
        dir_deadband = int(os.environ.get("DIRECTOR_DEADBAND_BP", "150"))
        if director_impl == "pairs":
            dir_quorum = int(os.environ.get("DIRECTOR_QUORUM", "4"))
            # boundaryBp: the no-trade-region form of effort sizing.  0 is
            # not a placeholder -- it is the measured right answer for a 30bp
            # venue (the refinement costs 5bp/yr there and only pays above
            # ~50bp/leg).  Raise it if the pools ever trade expensively.
            dir_boundary = int(os.environ.get("DIRECTOR_BOUNDARY_BP", "0"))
            director = chain.deploy(
                "PairsRebalanceDirector", basket.address, gov,
                (86400, dir_quorum, 500_000_000, dir_deadband * 100_000,
                 300_000_000, 250_000_000, 50, dir_boundary))
            desc = (f"quorum={dir_quorum}/7 kappa=0.5"
                    + (f" boundary={dir_boundary}bp" if dir_boundary else ""))
        else:
            dir_window = int(os.environ.get("DIRECTOR_WINDOW", "15"))
            director = chain.deploy(
                "BasketRebalanceDirector", basket.address, gov,
                (86400, dir_window, 3_000_000_000, dir_deadband * 100_000,
                 250_000_000, 300_000_000, 250_000_000, 50))
            desc = f"window={dir_window}d rho=3"
        chain.send(director.functions.syncConstituents())
        d.director = director
        d.director_impl = director_impl
        if basket_impl == "fence" and fence_factors:
            # The factors READ the director, so the fence needs it wired.
            # Safe here where it was not for the ops basket: this shell
            # overrides depositToken and _redeem outright, so none of the
            # inherited advisory-routing paths that `director` also gates are
            # reachable.
            chain.send(basket.functions.setDirector(director.address),
                       sender=gov)
            if int(os.environ.get("SIM_FENCE_FACTORS", "0") or 0):
                # A: differential -- lean against the constituent that has
                #    diverged most from its target weight.
                # B: common -- lean the way K is about to push BUCK.
                # Off by default so the symmetric band stays the baseline
                # every comparison is measured against.
                chain.send(basket.functions.setFactorParams((
                    int(os.environ.get("SIM_FACTOR_A", "100")),
                    int(os.environ.get("SIM_FACTOR_B", "100")),
                    int(os.environ.get("SIM_FACTOR_MAXSKEW", "5000")),
                    int(os.environ.get("SIM_FACTOR_MEAS", "2")),
                    True)), sender=gov)
                if verbose:
                    print("[deploy] fence FACTORS on  "
                          f"A={os.environ.get('SIM_FACTOR_A','100')}% "
                          f"B={os.environ.get('SIM_FACTOR_B','100')}% "
                          f"maxSkew={os.environ.get('SIM_FACTOR_MAXSKEW','5000')}bp")
        if basket_impl in ("ops", "equity-ops") and director_impl == "pairs":
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
            # The desk: the ops shell itself, or the EquityDesk beside an
            # equity basket, which first mirrors the basket's pools.
            mon = desk if desk is not None else basket
            if desk is not None:
                chain.send(desk.functions.mirrorConstituents(), sender=gov)
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
        if os.environ.get("SIM_EQUITY_DIRECTOR", "1") != "0":
            eqd = chain.deploy("EquityTurnDirector", basket.address, gov)
            chain.send(basket.functions.setEquityDirector(eqd.address), sender=gov)
            d.equity_director = eqd
        wheel = chain.deploy(
            "BasketWheel", buck.address, usdc.address, gov,
            int(os.environ.get("SIM_WHEEL_KAPPA_BP", "200")),
            int(float(os.environ.get("SIM_WHEEL_RESERVE_BUCK", "100")) * E6))
        idmod.bind_as_operator(chain, reg, wheel.address, True, True, sender=deployer)
        chain.send(basket.functions.setWheel(wheel.address), sender=gov)
        chain.send(wheel.functions.setEquity(basket.address), sender=gov)
        # The arbitrage is the basket's (ruled 2026-09-29): one triangle per
        # constituent -- its own TOKEN/BUCK pool, TOKEN/USDC, BUCK/USDC --
        # the outsider's trade made first, its profit (TOKEN start) credited
        # to the basket's wallet.
        # (Its triangles are set by `equity_arb_on`, once the pools have
        # their first pairings: before the market trades, an arbitrage
        # against half-built pools only moves the marks.)
        chain.send(wheel.functions.setArb(
            d.pool_ub, basket.address,
            int(os.environ.get("SIM_WHEEL_ARB_SHARE_BP", "1000")),
            int(os.environ.get("SIM_WHEEL_ARB_CAP_BP", "200")), 1), sender=gov)
        d.wheel = wheel
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
    return d
