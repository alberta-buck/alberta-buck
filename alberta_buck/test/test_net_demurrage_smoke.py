"""A linked BuckNet deployment against the independent demurrage reference.

This is a cash-only, zero-premium accounting smoke test. It is not a household
economy, market, insurer model, leveraged basket, or five-year validation.
"""

import shutil
from pathlib import Path

import pytest

from alberta_buck.sim.anvil import Anvil
from alberta_buck.sim.chain import Chain, load_artifact
from alberta_buck.sim.demurrage_reference import DemurrageLedger

YEAR = 365 * 24 * 60 * 60 + 6 * 60 * 60
RAW = 10**6
Q = 10**36
ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ("BuckNet", "BuckNetMaturity", "BuckNetTransfers", "JubileeFund",
            "IdentityRegistryHarness", "BuckCredit", "BuckKControllerStatic", "UniswapV3Factory",
            "BuckBasketEquity", "SimLP")


def _exec(chain, actor, target, method, args):
    data = target.encode_abi(method, args=args)
    return chain.send(actor.functions.exec(target.address, bytes.fromhex(data[2:])))


@pytest.mark.skipif(
    not shutil.which("anvil") or not all((ROOT / "out" / f"{name}.sol" / f"{name}.json").is_file()
                                         for name in REQUIRED),
    reason="requires anvil and built net/simulation artifacts; run make nix-sim-build",
)
def test_linked_net_issuance_and_reduction_match_reference():
    with Anvil(timestamp=1_900_000_000) as anvil:
        chain = Chain(anvil.w3, anvil.w3.eth.accounts[0])
        gov = chain.deployer
        registry = chain.deploy("IdentityRegistryHarness", gov)
        credit = chain.deploy("BuckCredit")
        controller = chain.deploy("BuckKControllerStatic", 10**18, gov)
        fund = chain.deploy("JubileeFund", gov)
        maturity = chain.deploy("BuckNetMaturity")
        transfers = chain.deploy("BuckNetTransfers")
        libraries = {
            "src/lib/BuckNetMaturity.sol:BuckNetMaturity": maturity.address,
            "src/lib/BuckNetTransfers.sol:BuckNetTransfers": transfers.address,
        }
        abi, bytecode = load_artifact("BuckNet", libraries=libraries)
        buck = chain.deploy("BuckNet", credit.address, controller.address, registry.address, fund.address,
                            abi=abi, bytecode=bytecode)
        factory = chain.deploy("UniswapV3Factory")
        basket = chain.deploy("BuckBasketEquity", buck.address, controller.address, factory.address,
                              gov, 3000, 600, 64, 500, 1000)
        actor = chain.deploy("SimLP")  # Public test-only contract identity, not an agent model.
        for target, carrying in ((fund.address, True), (basket.address, False), (actor.address, False)):
            chain.send(registry.functions.bindContract(target, (1, 2), ((1, 2), (1, 2)), True, carrying))
        chain.send(credit.functions.setBuck(buck.address))
        chain.send(registry.functions.setBuck(buck.address))
        chain.send(basket.functions.enableNetAccounting())
        chain.send(fund.functions.configure(buck.address, basket.address))
        _exec(chain, actor, credit, "setCreditIssuer", [gov, True])
        chain.send(credit.functions.createCredit(actor.address, 0, 100 * RAW, 100 * RAW, 0, 0, 0, 0))
        assert credit.functions.ownerOf(0).call() == actor.address
        issued = _exec(chain, actor, buck, "issue", [0, 100 * RAW, 100 * RAW])
        issued_at = anvil.w3.eth.get_block(issued["blockNumber"])["timestamp"]

        model = DemurrageLedger()
        model.issue_unspent("facility", 100, "owner", credit_limit=100)
        assert buck.functions.balanceOf(actor.address).call() == 100 * RAW
        assert buck.functions.totalIssued().call() == 100 * RAW
        runtime = anvil.w3.eth.get_code(buck.address).hex().lower()
        assert maturity.address[2:].lower() in runtime
        assert transfers.address[2:].lower() in runtime

        anvil.warp_to(issued_at + YEAR)
        model.advance(1)
        _, cash = buck.functions.fundBalancesScaled().call()
        assert cash / Q / RAW == pytest.approx(model.jubilee, abs=1e-6)
        assert buck.functions.balanceOf(actor.address).call() / RAW == pytest.approx(model.balance_of("owner"), abs=1e-6)
        quote, _, exceeds = fund.functions.previewRelief(0, RAW).call()
        assert not exceeds
        assert quote / Q / RAW == pytest.approx(model.quote_marked_retirement("facility", 1).relief, abs=1e-6)

        deadline = issued_at + YEAR + 3600
        _exec(chain, actor, buck, "authorizeReduction", [0, RAW, 0, deadline])
        retired = _exec(chain, actor, fund, "reduce", [0, RAW])
        retired_at = anvil.w3.eth.get_block(retired["blockNumber"])["timestamp"]
        model.advance((retired_at - issued_at - YEAR) / YEAR)
        model.retire_marked("facility", 1, "owner")
        assert buck.functions.totalIssued().call() == 99 * RAW
        assert credit.functions.issuanceOf(0).call()[0] == 99 * RAW
        assert buck.functions.balanceOf(actor.address).call() / RAW == pytest.approx(model.balance_of("owner"), abs=2e-6)
        assert buck.functions.fundBalancesScaled().call()[1] / Q / RAW == pytest.approx(model.jubilee, abs=2e-6)
        assert not chain.mismatches
