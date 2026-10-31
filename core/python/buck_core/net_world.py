"""Deployment and per-facility operations for the experimental BuckNet stack.

No identity, insurer or basket policy is implied by deployment.  The caller
must bind the fund as Carrying, configure a compatible basket and register each
insurer's own pool before issuing premium-bearing credit.  Quotes are advisory;
the token recomputes all authorization, K, funding and liquidity checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from buck_core.artifacts import load_artifact
from web3.logs import DISCARD


@dataclass(frozen=True)
class NetStack:
    registry: Any
    credit: Any
    controller: Any
    fund: Any
    token: Any
    quoter: Any
    maturity_library: Any
    transfers_library: Any


def deploy_net_stack(chain, registry, credit, controller, governance, *, credit_deployer=None):
    """Deploy and link a clean-genesis monetary stack and wire both authorities.

    ``credit_deployer`` must be the account that deployed BuckCredit.  The
    registry's governor separately authorizes its token.  Fund identity and
    basket configuration deliberately remain explicit steps for the caller.
    """
    gov_address = getattr(governance, "address", governance)
    fund = chain.deploy("JubileeFund", gov_address)
    maturity = chain.deploy("BuckNetMaturity")
    transfers = chain.deploy("BuckNetTransfers")
    abi, bytecode = load_artifact("BuckNet", libraries={
        "src/lib/BuckNetMaturity.sol:BuckNetMaturity": maturity.address,
        "src/lib/BuckNetTransfers.sol:BuckNetTransfers": transfers.address,
    })
    token = chain.deploy("BuckNet", credit.address, controller.address, registry.address, fund.address,
                         abi=abi, bytecode=bytecode)
    quoter = chain.deploy("BuckNetQuoter", token.address)
    chain.send(credit.functions.setBuck(token.address), sender=credit_deployer)
    chain.send(registry.functions.setBuck(token.address), sender=governance)
    return NetStack(registry, credit, controller, fund, token, quoter, maturity, transfers)


@dataclass(frozen=True)
class FacilityState:
    token_id: int
    owner: str
    face: int
    activated: int
    premium_rate: int
    issued: int
    claim_seconds: int
    premium_nominal: int
    insurer_pool: str
    spendable: int


@dataclass(frozen=True)
class IssueQuote:
    present_coverage: int
    premium: int
    reserve_required: int
    max_spendable: int
    owner_spendable: int

    @property
    def reserve_shortfall(self) -> int:
        return max(0, self.reserve_required - self.owner_spendable)


@dataclass(frozen=True)
class NetTransferValue:
    count: int
    nominal: int
    net_scaled: int


@dataclass(frozen=True)
class NetPurchase:
    paid: int
    nominal_received: int
    net_scaled: int

    @property
    def received(self) -> int:
        return self.net_scaled // 10**36


def net_transfer_value(token, receipt, sender: str, recipient: str) -> NetTransferValue:
    """Sum authentic transfer-delivery events for a direction and token.

    A zero net amount can be real; ``count`` distinguishes it from no event.
    ``net_scaled`` is in 1e36 fractional units per external raw BUCK unit.
    """
    events = token.events.NetValueTransfer().process_receipt(receipt, errors=DISCARD)
    selected = [e for e in events if e["address"].lower() == token.address.lower()
                and e["args"]["from"].lower() == sender.lower()
                and e["args"]["to"].lower() == recipient.lower()]
    return NetTransferValue(len(selected), sum(e["args"]["nominal"] for e in selected),
                            sum(e["args"]["netScaled"] for e in selected))


class NetMarketOps:
    """Atomic full-value BUCK buys through a configured single-pool guard.

    The payer must approve settlement to the guard; BUCK travels directly
    from the Carrying pool to the recipient. ``execute`` may dispatch through
    a simulation proxy. All limits and returned values use raw token units,
    except the explicitly named ``net_scaled`` field.
    """

    def __init__(self, chain, token, guard, execute: Callable | None = None):
        self.chain, self.token, self.guard = chain, token, guard
        self.execute = execute or self._send

    def _send(self, owner, contract, method, args):
        return self.chain.send(getattr(contract.functions, method)(*args), sender=owner)

    def _buy(self, owner, pool, method: str, args: list, recipient: str) -> NetPurchase:
        receipt = self.execute(owner, self.guard, method, args)
        buyer = getattr(owner, "address", owner)
        events = self.guard.events.NetBuckPurchased().process_receipt(receipt, errors=DISCARD)
        events = [e for e in events if e["address"].lower() == self.guard.address.lower()
                  and e["args"]["buyer"].lower() == buyer.lower()
                  and e["args"]["recipient"].lower() == recipient.lower()
                  and e["args"]["pool"].lower() == pool.lower()]
        if len(events) != 1:
            raise ValueError("net BUCK purchase evidence missing or ambiguous")
        event = events[0]["args"]
        transfer = net_transfer_value(self.token, receipt, pool, recipient)
        if not transfer.count or transfer.nominal != event["nominal"] or transfer.net_scaled != event["netScaled"]:
            raise ValueError("net BUCK transfer evidence mismatch")
        return NetPurchase(event["paid"], transfer.nominal, transfer.net_scaled)

    def buy_exact_input(self, owner, pool: str, amount_in: int, minimum_net: int,
                        recipient: str, price_limit: int = 0) -> NetPurchase:
        return self._buy(owner, pool, "buyExactInput", [pool, amount_in, minimum_net, recipient, price_limit], recipient)

    def buy_exact_output(self, owner, pool: str, desired_net: int, maximum_in: int,
                         recipient: str, price_limit: int = 0) -> NetPurchase:
        return self._buy(owner, pool, "buyExactOutput", [pool, desired_net, maximum_in, recipient, price_limit], recipient)


class NetCreditOps:
    """Operate on selected NFTs without inventing an account-signed debt.

    ``execute(owner, contract, method, args)`` may dispatch through an agent's
    proxy.  If omitted, ``owner`` must be a sender supported by the session.
    Reads are advisory snapshots, not transaction or market guarantees.
    Authorization and settlement are separate transactions: if settlement
    fails the prior authorization remains and the caller must retry or cancel.
    """

    def __init__(self, chain, stack: NetStack, execute: Callable | None = None):
        self.chain = chain
        self.stack = stack
        self.execute = execute or self._send

    def _send(self, owner, contract, method, args):
        return self.chain.send(getattr(contract.functions, method)(*args), sender=owner)

    def facility(self, token_id: int) -> FacilityState:
        credit, token = self.stack.credit, self.stack.token
        owner = credit.functions.ownerOf(token_id).call()
        face, active, rate = credit.functions.creditInfo(token_id).call()
        issued, seconds = credit.functions.issuanceOf(token_id).call()
        return FacilityState(token_id, owner, face, active, rate, issued, seconds,
                             token.functions.premiumPrincipal(token_id).call(),
                             token.functions.facilityPool(token_id).call(),
                             token.functions.balanceOf(owner).call())

    def quote_issue(self, token_id: int, coverage: int) -> IssueQuote:
        owner = self.stack.credit.functions.ownerOf(token_id).call()
        quote = self.stack.quoter.functions.quoteIssue(token_id, coverage).call()
        return IssueQuote(*quote, self.stack.token.functions.balanceOf(owner).call())

    def issue(self, owner, token_id: int, coverage: int, spendable: int):
        return self.execute(owner, self.stack.token, "issue", [token_id, coverage, spendable])

    def quote_reduction(self, token_id: int, principal: int):
        """Return (scaled net relief, scaled cash, exceeds the 10% cap)."""
        return self.stack.fund.functions.previewRelief(token_id, principal).call()

    def authorize_reduction(self, owner, token_id: int, principal: int,
                            minimum_relief_scaled: int, deadline: int):
        return self.execute(owner, self.stack.token, "authorizeReduction",
                            [token_id, principal, minimum_relief_scaled, deadline])

    def reduce_authorized(self, owner, token_id: int, principal: int):
        return self.execute(owner, self.stack.fund, "reduce", [token_id, principal])
