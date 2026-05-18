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
from alberta_buck.sim.router import sqrt_price_x96, full_range_ticks

REPO = Path(__file__).resolve().parents[2]
UR_ARTIFACT = "test/stabilizer-routing-op47/artifacts/UniversalRouter.json"

E18 = 10 ** 18
FEE_USDC = 3000
FEE_BUCK = 500
TICK_SPACING = {3000: 60, 500: 10}


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
    fee_usdc: int = FEE_USDC
    fee_buck: int = FEE_BUCK


def _erc20_abi() -> list:
    abi, _ = load_artifact("MockERC20")
    return abi


def deploy(chain: Chain, anvil, scenario, rng) -> Deployment:
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
        chain.send(simlp.functions.mint(pu, lo, hi, 10 ** 16, t0, t1))
        d.pool_usdc.append(pu)

        # TOKEN/BUCK basket pool via direct mint.
        chain.send(basket.functions.addBasketToken(
            c.address, dec[i], p0, 10000 // len(tok), FEE_BUCK), sender=gov)
        pb = v3f.functions.getPool(c.address, buck.address, FEE_BUCK).call()
        chain.send(reg.functions.bindContract(
            pb, idmod.BIND_PK, idmod.BIND_E, True, True), sender=deployer)
        # Deep basket-pool seed so the BUCK pools are comparable in depth to
        # the (deep) TOKEN/USDC pools -- a small cycle fill is then low-
        # slippage on every hop.  BUCK is 6-dec; minted BUCK =
        # seed*p0/10**dec stays far under the uint80 balance cap.
        seed = 1_000_000 * 10 ** dec[i]
        chain.send(c.functions.mint(deployer, seed))
        chain.send(c.functions.approve(basket.address, seed))
        chain.send(basket.functions.depositToken(c.address, seed, 0))
        d.pool_buck.append(pb)

    return d
