"""Real cash-only BuckNet basket receipts, not a leveraged economy."""
import shutil
from pathlib import Path

import pytest
from web3.logs import DISCARD

from alberta_buck.sim.anvil import Anvil
from alberta_buck.sim.chain import Chain
from buck_core import deploy_net_stack, mark_net_basket_positions


ROOT = Path(__file__).resolve().parents[2]
RAW = 10**6
YEAR = 365 * 86400 + 6 * 3600
ZERO = "0x" + "00" * 20
REQUIRED = ("BuckNet", "BuckNetMaturity", "BuckNetTransfers", "BuckNetQuoter", "JubileeFund",
            "IdentityRegistryHarness", "BuckCredit", "BuckKControllerStatic", "UniswapV3Factory",
            "BuckBasketEquity", "SimLP", "MockERC20")


@pytest.mark.skipif(not shutil.which("anvil") or not all(
    (ROOT / "out" / f"{name}.sol" / f"{name}.json").is_file() for name in REQUIRED),
    reason="requires anvil and built simulation artifacts")
def test_open_basket_basis_and_mark_follow_actual_receipts():
    with Anvil(timestamp=1_900_000_000) as anvil:
        chain = Chain(anvil.w3, anvil.w3.eth.accounts[0])
        gov = chain.deployer
        registry = chain.deploy("IdentityRegistryHarness", gov)
        credit = chain.deploy("BuckCredit")
        controller = chain.deploy("BuckKControllerStatic", 10**18, gov)
        stack = deploy_net_stack(chain, registry, credit, controller, gov)
        factory = chain.deploy("UniswapV3Factory")
        basket = chain.deploy("BuckBasketEquity", stack.token.address, controller.address, factory.address,
                              gov, 3000, 600, 64, 500, 1000)
        owner, stranger = chain.deploy("SimLP"), chain.deploy("SimLP")
        for target, carrying in ((stack.fund.address, True), (basket.address, False),
                                 (owner.address, False), (stranger.address, False)):
            chain.send(registry.functions.bindContract(target, (1, 2), ((1, 2), (1, 2)), True, carrying))
        chain.send(basket.functions.enableNetAccounting())
        chain.send(stack.fund.functions.configure(stack.token.address, basket.address))

        def execute(actor, target, name, args):
            data = target.encode_abi(name, args=args)
            return chain.send(actor.functions.exec(target.address, bytes.fromhex(data[2:])))

        execute(owner, credit, "setCreditIssuer", [gov, True])
        chain.send(credit.functions.createCredit(owner.address, 0, 100 * RAW, 100 * RAW, 0, 0, 0, 0))
        execute(owner, stack.token, "issue", [0, 100 * RAW, 100 * RAW, 0, ZERO])
        execute(owner, stack.token, "approve", [basket.address, 50 * RAW])
        ids = []
        for amount in (20 * RAW, 10 * RAW):
            receipt = execute(owner, basket, "deposit", [stack.token.address, amount, 0])
            events = basket.events.Deposited().process_receipt(receipt, errors=DISCARD)
            ids.append(events[0]["args"]["receiptId"])
        opening = mark_net_basket_positions(chain, basket, stack.token, owner.address, ids)
        assert opening.basis == sum(basket.functions.holdings(i).call()[1] for i in ids)
        assert opening.mark == sum(basket.functions.valueOf(i).call() for i in ids)
        assert opening.roi == pytest.approx((opening.mark - opening.basis) / opening.basis)
        assert mark_net_basket_positions(chain, basket, stack.token, owner.address, []).roi is None
        with pytest.raises(ValueError, match="invalid"):
            mark_net_basket_positions(chain, basket, stack.token, owner.address, [ids[0], ids[0]])
        with pytest.raises(ValueError, match="foreign"):
            mark_net_basket_positions(chain, basket, stack.token, stranger.address, ids)
        wrong_buck = chain.deploy("MockERC20", "Other", "OTHER", 6)
        with pytest.raises(ValueError, match="configured"):
            mark_net_basket_positions(chain, basket, wrong_buck, owner.address, ids)

        start = anvil.w3.eth.get_block("latest")["timestamp"]
        anvil.warp_to(start + YEAR)
        aged = mark_net_basket_positions(chain, basket, stack.token, owner.address, ids)
        assert aged.basis == opening.basis and aged.mark < opening.mark and aged.roi < 0
        execute(owner, basket, "redeemShares", [ids[0], aged.positions[0].shares // 2, 0])
        reduced = mark_net_basket_positions(chain, basket, stack.token, owner.address, ids)
        assert reduced.positions[0].shares < aged.positions[0].shares
        assert reduced.positions[0].basis < aged.positions[0].basis
        assert reduced.mark == sum(basket.functions.valueOf(i).call() for i in ids)
        historical = mark_net_basket_positions(chain, basket, stack.token, owner.address, ids,
                                               block_number=aged.block_number)
        assert historical == aged
        assert not chain.mismatches
