"""A real V3/Anvil cash-only guard fixture; not a household economy."""
import shutil
from pathlib import Path

import pytest

from alberta_buck.sim.anvil import Anvil
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.router import full_range_ticks, sqrt_price_x96
from buck_core import NetCreditOps, NetMarketOps, deploy_net_stack

RAW = 10**6
YEAR = 365 * 24 * 60 * 60 + 6 * 60 * 60
ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ("BuckNet", "BuckNetMaturity", "BuckNetTransfers", "BuckNetQuoter", "BuckNetSwapGuard", "JubileeFund",
            "IdentityRegistryHarness", "BuckCredit", "BuckKControllerDirect", "UniswapV3Factory", "UniswapV3Pool",
            "BuckBasketEquity", "MockERC20", "SimLP")


def _exec(chain, actor, contract, method, args):
    encoded = contract.encode_abi(method, args=args)
    return chain.send(actor.functions.exec(contract.address, bytes.fromhex(encoded[2:])))


@pytest.mark.skipif(
    not shutil.which("anvil") or not all((ROOT / "out" / f"{name}.sol" / f"{name}.json").is_file()
                                         for name in REQUIRED),
    reason="requires anvil and built net/simulation artifacts; run make nix-sim-build",
)
def test_python_guard_uses_actual_net_value_from_aged_v3_pool():
    with Anvil(timestamp=1_900_000_000) as anvil:
        chain = Chain(anvil.w3, anvil.w3.eth.accounts[0])
        gov = chain.deployer
        registry = chain.deploy("IdentityRegistryHarness", gov)
        credit = chain.deploy("BuckCredit")
        controller = chain.deploy("BuckKControllerDirect", 100_000_000_000, 643_004, 0,
                                  1800, 0, 950_000_000_000_000_000, 750_000_000_000_000_000, gov)
        stack = deploy_net_stack(chain, registry, credit, controller, gov)
        buck, fund = stack.token, stack.fund
        factory = chain.deploy("UniswapV3Factory")
        basket = chain.deploy("BuckBasketEquity", buck.address, controller.address, factory.address,
                              gov, 3000, 600, 64, 500, 1000)
        usdc = chain.deploy("MockERC20", "USD Coin", "USDC", 6)
        provider, buyer = chain.deploy("SimLP"), chain.deploy("SimLP")
        g, e = (1, 2), ((1, 2), (1, 2))
        for target, carrying in ((fund.address, True), (basket.address, False),
                                 (provider.address, False), (buyer.address, False)):
            chain.send(registry.functions.bindContract(target, g, e, True, carrying))
        chain.send(basket.functions.enableNetAccounting())
        chain.send(fund.functions.configure(buck.address, basket.address))
        _exec(chain, provider, credit, "setCreditIssuer", [gov, True])
        chain.send(credit.functions.createCredit(provider.address, 0, 2_000 * RAW, 2_000 * RAW, 0, 0, 0, 0))
        tid = credit.functions.tokenOfOwnerByIndex(provider.address, 0).call()
        actors = {provider.address.lower(): provider, buyer.address.lower(): buyer}
        credit_ops = NetCreditOps(chain, stack, lambda owner, target, method, args:
                                  _exec(chain, actors[owner.lower()], target, method, args))
        credit_ops.issue(provider.address, tid, 2_000 * RAW, 1_200 * RAW)

        chain.send(factory.functions.createPool(buck.address, usdc.address, 500))
        pool_address = factory.functions.getPool(buck.address, usdc.address, 500).call()
        pool = chain.at("UniswapV3Pool", pool_address)
        chain.send(registry.functions.bindContract(pool_address, g, e, True, True))
        chain.send(pool.functions.initialize(sqrt_price_x96(buck.address, RAW, usdc.address, RAW)))
        chain.send(usdc.functions.mint(provider.address, 2_000 * RAW))
        token0, token1 = pool.functions.token0().call(), pool.functions.token1().call()
        lo, hi = full_range_ticks(10)
        chain.send(provider.functions.mint(pool_address, lo, hi, 1_000 * RAW, token0, token1))
        guard = chain.deploy("BuckNetSwapGuard", buck.address, usdc.address, factory.address)
        chain.send(usdc.functions.mint(buyer.address, 500 * RAW))
        _exec(chain, buyer, usdc, "approve", [guard.address, 2**256 - 1])
        _exec(chain, buyer, credit, "setCreditIssuer", [gov, True])
        chain.send(credit.functions.createCredit(buyer.address, 0, 200 * RAW, 200 * RAW, 0, 0, 0, 0))
        buyer_id = credit.functions.tokenOfOwnerByIndex(buyer.address, 0).call()
        credit_ops.issue(buyer.address, buyer_id, 200 * RAW, 100 * RAW)
        # Simulate the borrower having spent the issuance before buying it back.
        _exec(chain, buyer, buck, "transfer", [provider.address, 100 * RAW])
        market = NetMarketOps(chain, buck, guard, lambda owner, target, method, args:
                              _exec(chain, buyer, target, method, args))
        at = anvil.w3.eth.get_block("latest")["timestamp"]
        anvil.warp_to(at + YEAR)
        assert buck.functions.netBalanceOf(pool_address).call() < buck.functions.balanceOf(pool_address).call()
        purchase = market.buy_exact_input(buyer.address, pool_address, 100 * RAW, 70 * RAW, buyer.address)
        assert purchase.paid == 100 * RAW
        assert 70 * RAW <= purchase.received < purchase.nominal_received
        assert buck.functions.balanceOf(buyer.address).call() == purchase.received
        assert credit.functions.issuanceOf(tid).call()[0] == 1_200 * RAW
        assert credit.functions.issuanceOf(buyer_id).call()[0] == 100 * RAW
        # A market purchase alone does not retire credit. The owner authorizes
        # and settles one specific NFT, reducing issuance only at that point.
        now = anvil.w3.eth.get_block("latest")["timestamp"]
        credit_ops.authorize_reduction(buyer.address, buyer_id, 10 * RAW, 0, now + 3600)
        credit_ops.reduce_authorized(buyer.address, buyer_id, 10 * RAW)
        assert credit.functions.issuanceOf(buyer_id).call()[0] == 90 * RAW
        assert credit.functions.issuanceOf(tid).call()[0] == 1_200 * RAW
        before = usdc.functions.balanceOf(buyer.address).call()
        exact = market.buy_exact_output(buyer.address, pool_address, 20 * RAW, 50 * RAW, buyer.address)
        assert exact.received >= 20 * RAW and exact.nominal_received > exact.received
        assert usdc.functions.balanceOf(buyer.address).call() == before - exact.paid
        assert not chain.mismatches
