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
from buck_core.session import TransactionReceiptPending, TransactionReverted, _txhex
from web3 import Web3
from web3.logs import DISCARD

Q = 10**36
WAD = 10**18
YEAR = 365 * 24 * 60 * 60 + 6 * 60 * 60
MAX_BALANCE = 2**79 - 1  # BuckTypes.MAX_BALANCE in raw BUCK units
MAX_UINT256 = 2**256 - 1
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


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
class IssueCandidate:
    """Same-block assessment of one explicitly selected issuance candidate.

    Face coverage and all BUCK amounts are raw integer units. ``reasons`` is
    empty only when the observed checks pass; this is not an execution promise.
    """
    block_number: int
    token_id: int
    coverage: int
    requested_spendable: int
    max_premium: int
    prior_principal: int
    before_coverage: int
    after_coverage: int
    current_k: int
    shortfall_before: int
    shortfall_after_premium: int
    premium: int
    reserve_required: int
    reserve_buffer: int
    owner_spendable: int
    reserve_shortfall: int
    max_spendable: int
    premium_pool: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class NetIssuance:
    token_id: int
    coverage: int
    spendable: int
    principal: int
    premium: int
    pool: str
    receipt: Any


@dataclass(frozen=True)
class HouseholdCreditState:
    """An owner's money and independent facilities, in raw token units."""
    spendable: int
    issued: int
    unactivated: int
    premium_nominal: int
    facilities: tuple[FacilityState, ...]


@dataclass(frozen=True)
class ActivatedCapacityQuote:
    """Same-block zero-coverage issuance estimates, in raw BUCK units."""
    block_number: int
    facilities: tuple[tuple[int, int], ...]  # (NFT, independently quoted max spendable)
    unactivated_present: int               # marked value not yet activated on eligible NFTs
    shared_limit: int                       # account and total-issuance numerical room
    potential_capacity: int                 # bounded sum before the maturity gate
    capacity: int                           # zero while maturities need processing
    maturities_pending: bool


def bound_activated_capacity(capacities, held_scaled: int, unspent_scaled: int, total_issued: int):
    """Apply BuckNetQuoter's shared numerical bounds to independent zero-premium quotes."""
    values = tuple(capacities)
    if (any(type(v) is not int or v < 0 for v in (*values, held_scaled, unspent_scaled, total_issued))
            or held_scaled + unspent_scaled > MAX_BALANCE * Q or total_issued > MAX_UINT256 // Q):
        raise ValueError("invalid activated capacity inputs")
    shared = min((MAX_BALANCE * Q - held_scaled - unspent_scaled) // Q, MAX_UINT256 // Q - total_issued)
    return min(sum(values), shared), shared


def issue_coverage_candidates(face: int, activated: int, mark: int, principal: int, k: int,
                              premium_rate_bp: int, requested: int) -> tuple[int, ...]:
    """Return a bounded menu of face increments near the needed coverage.

    Inputs and results are exact integer token units; K is scaled by 1e18.
    The estimate includes an existing facility shortfall and premium, but
    its candidates must be quoted individually. It does not assume those
    integer quotes are monotone or promise a globally minimal activation.
    """
    values = (face, activated, mark, principal, k, premium_rate_bp, requested)
    if (any(type(v) is not int or v < 0 or v > MAX_UINT256 for v in values) or
            activated > face or mark > face or premium_rate_bp * 10 >= 10_000 or requested == 0):
        raise ValueError("invalid issuance coverage inputs")
    remaining = face - activated
    if not remaining or not mark:
        return (0,)
    choices = {0, 1, remaining}
    choices.update(remaining * n // 8 for n in range(1, 8))
    before = mark * activated // face
    before_limit = before * k // WAD
    needed = max(0, principal + requested - before_limit)
    spread = k - premium_rate_bp * 10 * WAD // 10_000
    if needed and spread > 0:
        ceil_div = lambda a, b: (a + b - 1) // b
        present = ceil_div(needed * WAD, spread)
        estimate = min(remaining, ceil_div(present * face, mark))
        step = ceil_div(face, mark)
        choices.update(max(0, min(remaining, estimate + offset * step)) for offset in (-2, -1, 0, 1, 2))
    return tuple(sorted(choices))


def _validate_issue_candidate(token_id, coverage, requested, max_premium, reserve_buffer, block_number):
    values = (token_id, coverage, requested, max_premium, reserve_buffer)
    if (any(type(v) is not int or v < 0 or v > MAX_UINT256 for v in values) or requested == 0 or
            (block_number is not None and (type(block_number) is not int or block_number < 0))):
        raise ValueError("invalid issuance candidate")


@dataclass(frozen=True)
class ReductionPlan:
    token_id: int
    principal: int
    relief_scaled: int
    minimum_relief_scaled: int
    cash_scaled: int
    reason: str


@dataclass(frozen=True)
class NetReduction:
    principal: int
    relief_scaled: int
    premium_nominal: int
    receipt: Any

    @property
    def owner_cost_scaled(self) -> int:
        return self.principal * 10**36 - self.relief_scaled


@dataclass(frozen=True)
class HouseholdRetirement:
    requested: int
    retired: int
    relief_scaled: int
    owner_cost_scaled: int
    premium_nominal: int
    status: str
    blocked_token_id: int | None
    pending_principal: int | None
    pending_minimum_relief_scaled: int | None
    pending_deadline: int | None
    pending_transaction_hash: str | None
    pending_reverted: bool
    pending_phase: str | None
    error: str | None
    settlements: tuple[NetReduction, ...]


@dataclass(frozen=True)
class ReliefRateQuote:
    block_number: int
    selected_seconds: int
    total_seconds: int
    relief_scaled: int
    cash_scaled: int
    exceeds_liquidity_limit: bool
    annual_rate_wad: int | None
    capped: bool


@dataclass(frozen=True)
class PremiumRefundQuote:
    block_number: int
    premium: int
    pool: str | None
    gross_scaled: int
    net_scaled: int
    refund_fraction_wad: int | None
    reason: str


class ReductionPending(RuntimeError):
    """An authorization or settlement outcome requires reconciliation."""

    def __init__(self, plan: ReductionPlan, transaction_hash: str | None = None, reverted: bool = False,
                 phase: str = "settlement"):
        self.plan = plan
        self.transaction_hash = transaction_hash
        self.reverted = reverted
        self.phase = phase
        super().__init__(f"{phase} of facility {plan.token_id} needs transaction reconciliation")


class IssuancePending(RuntimeError):
    """A submitted issue needs its exact receipt before it can be counted."""

    def __init__(self, candidate: IssueCandidate, transaction_hash: str | None = None, reverted: bool = False):
        self.candidate = candidate
        self.transaction_hash = transaction_hash
        self.reverted = reverted
        super().__init__(f"issuance of facility {candidate.token_id} needs transaction reconciliation")


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
        if receipt and receipt.get("status") == 0:
            raise TransactionReverted(method, "net purchase refused", receipt)
        try:
            return self.purchase_from_receipt(owner, pool, recipient, receipt)
        except Exception as exc:
            tx_hash = _txhex(receipt.get("transactionHash")) if receipt else ""
            if tx_hash:
                raise TransactionReceiptPending(tx_hash) from exc
            raise

    def purchase_from_receipt(self, owner, pool: str, recipient: str, receipt,
                              expected_hash: str | None = None) -> NetPurchase:
        """Read one authenticated guard purchase; optionally bind it to a saved transaction."""
        if not receipt or receipt.get("status") != 1:
            raise ValueError("net purchase receipt is not successful")
        if expected_hash is not None and _txhex(receipt.get("transactionHash")).lower() != expected_hash.lower():
            raise ValueError("net purchase transaction hash mismatch")
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
        return self._facility(token_id)

    def _facility(self, token_id: int, spendable: int | None = None) -> FacilityState:
        credit, token = self.stack.credit, self.stack.token
        owner = credit.functions.ownerOf(token_id).call()
        face, active, rate = credit.functions.creditInfo(token_id).call()
        issued, seconds = credit.functions.issuanceOf(token_id).call()
        return FacilityState(token_id, owner, face, active, rate, issued, seconds,
                             token.functions.premiumPrincipal(token_id).call(),
                             token.functions.facilityPool(token_id).call(),
                             token.functions.balanceOf(owner).call() if spendable is None else spendable)

    def household(self, owner, token_ids) -> HouseholdCreditState:
        """Read one wallet once and add only its explicitly selected facilities.

        The wallet is money, not a signed liability; purchasing or receiving
        BUCK does not change any facility's issued principal. Duplicate or
        foreign NFTs are rejected instead of silently corrupting the totals.
        """
        address = getattr(owner, "address", owner)
        ids = tuple(token_ids)
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate household facility")
        if self.stack.registry.functions.isCarrying(address).call():
            raise ValueError("household wallet must be non-Carrying")
        spendable = self.stack.token.functions.balanceOf(address).call()
        facilities = tuple(self._facility(token_id, spendable) for token_id in ids)
        if any(f.owner.lower() != address.lower() for f in facilities):
            raise ValueError("foreign household facility")
        return HouseholdCreditState(
            spendable, sum(f.issued for f in facilities),
            sum(f.face - f.activated for f in facilities), sum(f.premium_nominal for f in facilities), facilities)

    def quote_issue(self, token_id: int, coverage: int) -> IssueQuote:
        owner = self.stack.credit.functions.ownerOf(token_id).call()
        quote = self.stack.quoter.functions.quoteIssue(token_id, coverage).call()
        return IssueQuote(*quote, self.stack.token.functions.balanceOf(owner).call())

    def assess_issue_candidate(self, owner, token_id: int, coverage: int, requested_spendable: int,
                               max_premium: int, *, reserve_buffer: int = 0,
                               block_number: int | None = None) -> IssueCandidate:
        """Assess exactly one chosen NFT and face-coverage amount at one block.

        This does not find the cheapest coverage, estimate a market trade, or
        submit a transaction. The optional extra reserve is required only for a
        nonzero premium. Pending reductions are blocked by the household
        workflow even though the token does not itself prohibit a new issue.
        K, marks, balances, permissions and the fund's mark can change before
        execution, which still has to re-quote and use the premium ceiling.
        A caller may pin several assessments to the same supplied block number;
        availability of historical state and chain finality are separate.
        """
        _validate_issue_candidate(token_id, coverage, requested_spendable, max_premium, reserve_buffer, block_number)
        block = self.chain.w3.eth.block_number if block_number is None else block_number
        return self._assess_issue_at(owner, token_id, coverage, requested_spendable, max_premium,
                                     reserve_buffer, block, self._issue_block_reader(block))

    @staticmethod
    def _issue_block_reader(block):
        """Memoize only identical contract reads within one ephemeral block snapshot."""
        cache = {}
        def read(contract, method, *args):
            key = (contract.address.lower(), method, args)
            if key not in cache:
                cache[key] = getattr(contract.functions, method)(*args).call(block_identifier=block)
            return cache[key]
        return read

    def _assess_issue_at(self, owner, token_id, coverage, requested_spendable, max_premium, reserve_buffer, block, read):
        address = getattr(owner, "address", owner)
        registry, credit, token, quoter, fund = (self.stack.registry, self.stack.credit, self.stack.token,
                                                 self.stack.quoter, self.stack.fund)
        if not read(registry, "isVerified", address) or read(registry, "isCarrying", address):
            raise ValueError("household owner must be verified and non-Carrying")
        if read(credit, "ownerOf", token_id).lower() != address.lower():
            raise ValueError("foreign household facility")
        face, activated, _ = read(credit, "creditInfo", token_id)
        if coverage > face - activated:
            raise ValueError("issuance candidate exceeds unactivated coverage")
        before, _, _, _ = read(quoter, "quoteIssue", token_id, 0)
        after, premium, reserve, maximum = read(quoter, "quoteIssue", token_id, coverage)
        if after < before:
            raise ValueError("issuance candidate reduced current coverage")
        principal, age = read(credit, "issuanceOf", token_id)
        k = read(self.stack.controller, "currentBuckK")
        spendable = read(token, "balanceOf", address)
        pending, _, _ = read(token, "reductionAuthorization", token_id)
        reasons = []
        configured_token = read(fund, "token")
        if (configured_token.lower() != token.address.lower() or
                not read(registry, "isVerified", fund.address) or
                not read(registry, "isCarrying", fund.address)):
            reasons.append("fund-unavailable")
        if read(token, "maturitiesPending"):
            reasons.append("maturities-pending")
        if pending:
            reasons.append("authorization-pending")
        if principal and age >= principal * 50 * YEAR:
            reasons.append("facility-matured")
        pool = ZERO_ADDRESS
        if premium:
            pool = read(token, "facilityPool", token_id)
            if pool.lower() == ZERO_ADDRESS:
                insurer = read(credit, "insurerOf", token_id)
                pool = read(token, "insurerPool", insurer)
            if (pool.lower() == ZERO_ADDRESS or not read(registry, "isVerified", pool) or
                    not read(registry, "isCarrying", pool)):
                reasons.append("insurer-pool-unavailable")
        if premium > max_premium:
            reasons.append("premium-limit")
        shortfall = max(0, reserve + (reserve_buffer if premium else 0) - spendable)
        if shortfall:
            reasons.append("reserve-shortfall")
        if requested_spendable > maximum:
            reasons.append("insufficient-capacity")
        return IssueCandidate(block, token_id, coverage, requested_spendable, max_premium, principal, before, after,
                              k, max(0, principal - before * k // WAD),
                              max(0, principal + premium - after * k // WAD), premium, reserve, reserve_buffer,
                              spendable, shortfall, maximum, pool, tuple(reasons))

    def issue_coverage_menu(self, owner, token_id: int, requested_spendable: int, max_premium: int,
                            *, reserve_buffer: int = 0) -> tuple[IssueCandidate, ...]:
        """Quote a small, explicitly bounded set of coverage increments at one block.

        Use no extra coverage when currently activated room suffices. Otherwise
        report the exact assessment for each candidate, including failures;
        the caller decides which are affordable and worth exercising. This
        finite menu is not an exhaustive search or an execution promise.
        """
        _validate_issue_candidate(token_id, 0, requested_spendable, max_premium, reserve_buffer, None)
        block = self.chain.w3.eth.block_number
        read = self._issue_block_reader(block)
        zero = self._assess_issue_at(owner, token_id, 0, requested_spendable, max_premium, reserve_buffer, block, read)
        common_blockers = {"fund-unavailable", "maturities-pending", "authorization-pending", "facility-matured"}
        if not zero.reasons or common_blockers.intersection(zero.reasons):
            return (zero,)
        credit = self.stack.credit
        face, activated, rate = read(credit, "creditInfo", token_id)
        mark = read(credit, "depreciatedFaceValue", token_id)
        principal, _ = read(credit, "issuanceOf", token_id)
        k = read(self.stack.controller, "currentBuckK")
        coverages = issue_coverage_candidates(face, activated, mark, principal, k, rate, requested_spendable)
        return (zero,) + tuple(self._assess_issue_at(owner, token_id, coverage, requested_spendable, max_premium,
                                                      reserve_buffer, block, read)
                               for coverage in coverages if coverage)

    def quote_household_activated_capacity(self, owner, token_ids) -> ActivatedCapacityQuote:
        """Observe activated room and unactivated marked coverage at one block.

        Require a verified non-Carrying owner and distinct owned NFTs. Apply
        the account and supply bounds once across their independent quotes,
        and exclude fully matured NFTs from newly activatable coverage. This
        is a numerical observation, not a guarantee: K is recomputed during
        issue and identity, fund, authorization and liquidity can change.
        A pending maturity blocks issuance until the keeper processes it.
        """
        address = getattr(owner, "address", owner)
        ids = tuple(token_ids)
        if any(type(v) is not int or v < 0 for v in ids) or len(ids) != len(set(ids)):
            raise ValueError("invalid or duplicate household facility")
        block = self.chain.w3.eth.block_number
        read = lambda fn: fn.call(block_identifier=block)
        registry, credit, token, quoter = self.stack.registry, self.stack.credit, self.stack.token, self.stack.quoter
        if not read(registry.functions.isVerified(address)) or read(registry.functions.isCarrying(address)):
            raise ValueError("household owner must be verified and non-Carrying")
        if any(read(credit.functions.ownerOf(token_id)).lower() != address.lower() for token_id in ids):
            raise ValueError("foreign household facility")
        facilities = []
        unactivated_present = 0
        for token_id in ids:
            present, premium, reserve, maximum = read(quoter.functions.quoteIssue(token_id, 0))
            if premium or reserve:
                raise ValueError("zero-coverage quote unexpectedly requires a premium or reserve")
            facilities.append((token_id, maximum))
            principal, age = read(credit.functions.issuanceOf(token_id))
            if not principal or age < principal * 50 * YEAR:
                mark = read(credit.functions.depreciatedFaceValue(token_id))
                if present > mark:
                    raise ValueError("activated coverage exceeds current face mark")
                unactivated_present += mark - present
        held, unspent = read(token.functions.heldAndUnspentScaled(address))
        total = read(token.functions.totalIssued())
        potential, shared = bound_activated_capacity((capacity for _, capacity in facilities), held, unspent, total)
        pending = read(token.functions.maturitiesPending())
        return ActivatedCapacityQuote(block, tuple(facilities), unactivated_present, shared, potential,
                                      0 if pending else potential, pending)

    def issue(self, owner, token_id: int, coverage: int, spendable: int, max_premium: int, expected_pool: str):
        """Issue with an explicit premium ceiling and expected insurer pool."""
        if type(max_premium) is not int or not 0 <= max_premium <= MAX_UINT256:
            raise ValueError("invalid maximum premium")
        if not isinstance(expected_pool, str) or not Web3.is_address(expected_pool):
            raise ValueError("invalid expected insurer pool")
        return self.execute(owner, self.stack.token, "issue",
                            [token_id, coverage, spendable, max_premium, Web3.to_checksum_address(expected_pool)])

    def issuance_from_receipt(self, owner, candidate: IssueCandidate, receipt, *, expected_hash: str) -> NetIssuance:
        """Authenticate one exact issued facility; the caller must deduplicate it.

        A hash recovered from a receipt is evidence for this single dispatcher,
        not chain finality. Reject extra issuance events rather than quietly
        ignoring another facility changed by a custom dispatcher.
        """
        if not isinstance(candidate, IssueCandidate) or not receipt or receipt.get("status") != 1:
            raise ValueError("issuance receipt is not successful")
        if (not isinstance(expected_hash, str) or not expected_hash or
                _txhex(receipt.get("transactionHash")).lower() != expected_hash.lower()):
            raise ValueError("issuance transaction hash mismatch")
        address = getattr(owner, "address", owner)
        events = self.stack.token.events.FacilityIssued().process_receipt(receipt, errors=DISCARD)
        events = [e for e in events if e["address"].lower() == self.stack.token.address.lower()]
        if len(events) != 1:
            raise ValueError("issuance receipt missing or ambiguous")
        event = events[0]["args"]
        if (event["owner"].lower() != address.lower() or event["tokenId"] != candidate.token_id or
                event["coverage"] != candidate.coverage or event["premium"] > candidate.max_premium or
                event["principal"] != candidate.requested_spendable + event["premium"] or
                event["pool"].lower() != (candidate.premium_pool if event["premium"] else ZERO_ADDRESS).lower()):
            raise ValueError("issuance receipt does not match candidate")
        return NetIssuance(candidate.token_id, event["coverage"], candidate.requested_spendable,
                           event["principal"], event["premium"], event["pool"], receipt)

    def issue_candidate(self, owner, candidate: IssueCandidate) -> NetIssuance:
        """Submit one observed candidate and authenticate its resulting event.

        Reassess the economics before this call. State may change again at
        execution, where the token enforces the ceiling and pool. The caller
        must serialize an owner's actions and retain any IssuancePending,
        including an unknown hash, before deciding whether retry is safe.
        """
        if not isinstance(candidate, IssueCandidate) or candidate.reasons:
            raise ValueError("issuance candidate is not available")
        address = getattr(owner, "address", owner)
        if self.stack.credit.functions.ownerOf(candidate.token_id).call().lower() != address.lower():
            raise ValueError("foreign household facility")
        if self.stack.token.functions.reductionAuthorization(candidate.token_id).call()[0]:
            raise ValueError("reduction authorization pending")
        receipt = None
        try:
            receipt = self.issue(owner, candidate.token_id, candidate.coverage, candidate.requested_spendable,
                                 candidate.max_premium, candidate.premium_pool)
            tx_hash = _txhex(receipt.get("transactionHash")) if receipt else ""
            if not receipt or receipt.get("status") != 1 or not tx_hash:
                raise IssuancePending(candidate, tx_hash or None,
                                      bool(tx_hash and receipt and receipt.get("status") == 0))
            return self.issuance_from_receipt(owner, candidate, receipt, expected_hash=tx_hash)
        except IssuancePending:
            raise
        except Exception as exc:
            tx_hash = getattr(exc, "transaction_hash", None)
            if not tx_hash:
                try:
                    tx_hash = _txhex(receipt.get("transactionHash")) or None
                except Exception:
                    pass
            raise IssuancePending(candidate, tx_hash,
                                  isinstance(exc, TransactionReverted) and exc.receipt.get("status") == 0
                                  and bool(exc.transaction_hash)) from exc

    def quote_reduction(self, token_id: int, principal: int):
        """Return (scaled net relief, scaled cash, exceeds the 10% cap)."""
        return self.stack.fund.functions.previewRelief(token_id, principal).call()

    def quote_relief_rate(self, token_id: int, principal: int) -> ReliefRateQuote:
        """Infer today's marked annual relief rate from one same-block NFT quote.

        A fresh claim has no rate. A principal-capped quote provides only a
        lower bound; an exceeded liquidity cap remains visible. This is a
        current valuation, not a forecast of future fund returns or liquidity.
        """
        if type(token_id) is not int or token_id < 0 or type(principal) is not int or principal <= 0:
            raise ValueError("invalid relief rate sample")
        block = self.chain.w3.eth.block_number
        selected, total = self.stack.token.functions.selectedClaimSeconds(token_id, principal).call(
            block_identifier=block)
        relief, cash, exceeds = self.stack.fund.functions.previewRelief(token_id, principal).call(
            block_identifier=block)
        rate = relief * YEAR * WAD // (selected * Q) if selected else None
        return ReliefRateQuote(block, selected, total, relief, cash, exceeds, rate,
                               bool(selected and relief == principal * Q))

    def quote_premium_refund(self, token_id: int, coverage: int, *, block_number: int | None = None) -> PremiumRefundQuote:
        """Estimate the immediate mixed-pool refund fraction for new coverage.

        Only the pool's current BUCK account and the proposed full-value
        premium are included. Other insurer investments, future activity and
        liquidity preparation are unknown; the result is not a refund promise.
        Supply a block number to compare it with same-block issue candidates.
        """
        if (type(token_id) is not int or token_id < 0 or type(coverage) is not int or coverage < 0 or
                (block_number is not None and (type(block_number) is not int or block_number < 0))):
            raise ValueError("invalid premium sample")
        block = self.chain.w3.eth.block_number if block_number is None else block_number
        token, credit = self.stack.token, self.stack.credit
        _, premium, _, _ = self.stack.quoter.functions.quoteIssue(token_id, coverage).call(block_identifier=block)
        if not premium:
            return PremiumRefundQuote(block, 0, None, 0, 0, None, "no-premium")
        pool = token.functions.facilityPool(token_id).call(block_identifier=block)
        if pool.lower() == ZERO_ADDRESS:
            insurer = credit.functions.insurerOf(token_id).call(block_identifier=block)
            pool = token.functions.insurerPool(insurer).call(block_identifier=block)
        if pool.lower() == ZERO_ADDRESS:
            return PremiumRefundQuote(block, premium, None, 0, 0, None, "no-insurer-pool")
        verified = self.stack.registry.functions.isVerified(pool).call(block_identifier=block)
        carrying = self.stack.registry.functions.isCarrying(pool).call(block_identifier=block)
        if not verified or not carrying:
            return PremiumRefundQuote(block, premium, pool, 0, 0, None, "invalid-insurer-pool")
        gross, net = token.functions.accountBalancesScaled(pool).call(block_identifier=block)
        fraction = (net + premium * Q) * WAD // (gross + premium * Q)
        return PremiumRefundQuote(block, premium, pool, gross, net, fraction, "cash-only")

    def authorize_reduction(self, owner, token_id: int, principal: int,
                            minimum_relief_scaled: int, deadline: int):
        return self.execute(owner, self.stack.token, "authorizeReduction",
                            [token_id, principal, minimum_relief_scaled, deadline])

    def reduce_authorized(self, owner, token_id: int, principal: int):
        return self.execute(owner, self.stack.fund, "reduce", [token_id, principal])

    def cancel_reduction(self, owner, token_id: int):
        return self.authorize_reduction(owner, token_id, 0, 0, 0)

    def plan_reduction(self, owner, token_id: int, requested: int, *, buffer: int = 1,
                       relief_slippage_bp: int = 100) -> ReductionPlan:
        """Size one selected NFT reduction against real cash and relief.

        The conservative owner budget excludes the prospective premium refund
        and uses the minimum accepted relief. Fund liquidity and owner funds
        may change before settlement; the contract remains authoritative.
        An existing authorization is never overwritten. All scaled values are
        in 1e36 fractional units per raw BUCK unit.
        """
        if (not all(type(v) is int for v in (token_id, requested, buffer, relief_slippage_bp))
                or token_id < 0 or requested < 0 or buffer < 0 or not 0 <= relief_slippage_bp <= 10_000):
            raise ValueError("invalid reduction budget or relief tolerance")
        address = getattr(owner, "address", owner)
        if self.stack.registry.functions.isCarrying(address).call():
            raise ValueError("household wallet must be non-Carrying")
        facility = self.facility(token_id)
        if facility.owner.lower() != address.lower():
            raise ValueError("foreign household facility")
        pending, _, _ = self.stack.token.functions.reductionAuthorization(token_id).call()
        if pending:
            return ReductionPlan(token_id, 0, 0, 0, 0, "authorization-pending")
        amount = min(requested, facility.issued)
        budget_scaled = max(0, facility.spendable - buffer) * 10**36
        if not amount:
            return ReductionPlan(token_id, 0, 0, 0, 0, "no-principal")
        reason = "quote-unstable"
        for _ in range(16):
            relief, cash, exceeds = self.quote_reduction(token_id, amount)
            floor = relief * (10_000 - relief_slippage_bp) // 10_000
            need = amount * 10**36 - floor
            candidate = amount
            if exceeds:
                candidate = min(candidate, amount * (cash // 10) // relief)
                reason = "fund-liquidity"
            if need > budget_scaled:
                candidate = min(candidate, amount * budget_scaled // need)
                reason = "owner-funds"
            if candidate == amount:
                return ReductionPlan(token_id, amount, relief, floor, cash, "ready")
            if not candidate:
                return ReductionPlan(token_id, 0, relief, floor, cash, reason)
            amount = candidate
        return ReductionPlan(token_id, 0, 0, 0, 0, reason)

    def settle_plan(self, owner, plan: ReductionPlan, deadline: int):
        """Authorize and settle a ready plan; surface a possibly pending authorization.

        Reads and transactions are separate: an intervening callback, balance,
        mark or liquidity change can make settlement fail. An uncertain
        authorization and an uncertain settlement are distinct phases; neither
        is a successful repayment. Preserve the submitted hash, if available,
        and reconcile the corresponding receipt before retrying or canceling.
        """
        if plan.reason != "ready" or plan.principal <= 0:
            raise ValueError("reduction plan is not ready")
        if self.stack.token.functions.reductionAuthorization(plan.token_id).call()[0]:
            raise ValueError("reduction authorization already pending")
        authorized = None
        try:
            authorized = self.authorize_reduction(owner, plan.token_id, plan.principal,
                                                  plan.minimum_relief_scaled, deadline)
            if not authorized or authorized.get("status") != 1:
                tx_hash = _txhex(authorized.get("transactionHash")) if authorized else ""
                raise ReductionPending(plan, tx_hash or None,
                                       bool(tx_hash and authorized.get("status") == 0), "authorization")
            self.authorization_from_receipt(owner, plan, deadline, authorized)
        except ReductionPending:
            raise
        except Exception as exc:
            tx_hash = getattr(exc, "transaction_hash", None)
            if not tx_hash:
                try:
                    tx_hash = _txhex(authorized.get("transactionHash")) or None
                except Exception:
                    pass
            raise ReductionPending(plan, tx_hash,
                                   isinstance(exc, TransactionReverted) and exc.receipt.get("status") == 0
                                   and bool(exc.transaction_hash), "authorization") from exc
        receipt = None
        try:
            receipt = self.reduce_authorized(owner, plan.token_id, plan.principal)
            if not receipt or receipt.get("status") != 1:
                tx_hash = _txhex(receipt.get("transactionHash")) if receipt else ""
                raise ReductionPending(plan, tx_hash or None, bool(tx_hash and receipt.get("status") == 0))
            return self.reduction_from_receipt(owner, plan.token_id, plan.principal, receipt)
        except ReductionPending:
            raise
        except Exception as exc:
            tx_hash = getattr(exc, "transaction_hash", None)
            if not tx_hash:
                try:
                    tx_hash = _txhex(receipt.get("transactionHash")) or None
                except Exception:
                    pass
            raise ReductionPending(plan, tx_hash,
                                   isinstance(exc, TransactionReverted) and exc.receipt.get("status") == 0
                                   and bool(exc.transaction_hash)) from exc

    def authorization_from_receipt(self, owner, plan: ReductionPlan, deadline: int, receipt,
                                   expected_hash: str | None = None):
        """Authenticate the exact authorization; it is not a repayment."""
        if not receipt or receipt.get("status") != 1:
            raise ValueError("authorization receipt is not successful")
        if expected_hash is not None and _txhex(receipt.get("transactionHash")).lower() != expected_hash.lower():
            raise ValueError("authorization transaction hash mismatch")
        address = getattr(owner, "address", owner)
        events = self.stack.token.events.ReductionAuthorized().process_receipt(receipt, errors=DISCARD)
        events = [e for e in events if e["address"].lower() == self.stack.token.address.lower()
                  and e["args"]["owner"].lower() == address.lower() and e["args"]["tokenId"] == plan.token_id]
        if len(events) != 1 or (events[0]["args"]["principal"], events[0]["args"]["minimumReliefScaled"],
                                events[0]["args"]["deadline"]) != (plan.principal, plan.minimum_relief_scaled, deadline):
            raise ValueError("authorization receipt missing or ambiguous")
        return events[0]

    def reduction_from_receipt(self, owner, token_id: int, principal: int, receipt,
                               expected_hash: str | None = None) -> NetReduction:
        """Read one authenticated facility event, optionally bound to a saved transaction."""
        if not receipt or receipt.get("status") != 1:
            raise ValueError("settlement receipt is not successful")
        if expected_hash is not None and _txhex(receipt.get("transactionHash")).lower() != expected_hash.lower():
            raise ValueError("settlement transaction hash mismatch")
        address = getattr(owner, "address", owner)
        events = self.stack.token.events.FacilityReduced().process_receipt(receipt, errors=DISCARD)
        events = [e for e in events if e["address"].lower() == self.stack.token.address.lower()
                  and e["args"]["owner"].lower() == address.lower() and e["args"]["tokenId"] == token_id]
        if len(events) != 1 or events[0]["args"]["principal"] != principal:
            raise ValueError("settlement receipt missing or ambiguous")
        event = events[0]["args"]
        return NetReduction(event["principal"], event["reliefScaled"], event["premiumRefund"], receipt)

    def retire_household(self, owner, token_ids, requested: int, deadline: int, *, buffer: int = 1,
                         relief_slippage_bp: int = 100) -> HouseholdRetirement:
        """Retire an ordered household's actual facilities, returning settled events only.

        Each facility is planned against fresh state. A refusal stops the batch;
        an uncertain transaction returns ``authorization-pending``, its
        phase and its NFT. The caller must reconcile before retrying or
        canceling. Earlier successful transactions remain in ``settlements``.
        """
        if (not all(type(v) is int for v in (requested, deadline, buffer, relief_slippage_bp))
                or requested < 0 or not 0 <= deadline < 2**64 or buffer < 0
                or not 0 <= relief_slippage_bp <= 10_000):
            raise ValueError("invalid reduction budget or relief tolerance")
        ids = tuple(token_ids)
        self.household(owner, ids)  # reject duplicates and foreign NFTs before any transaction
        settled = []
        retired = 0
        status, blocked, pending_principal, pending_minimum, pending_deadline = "complete", None, None, None, None
        pending_hash, pending_reverted, pending_phase, error = None, False, None, None
        for token_id in ids:
            if retired >= requested:
                break
            try:
                plan = self.plan_reduction(owner, token_id, requested - retired,
                                           buffer=buffer, relief_slippage_bp=relief_slippage_bp)
            except Exception as exc:
                status, blocked, error = "plan-unavailable", token_id, str(exc)[:200]
                break
            if plan.reason == "no-principal":
                continue
            if plan.reason != "ready":
                status, blocked = plan.reason, token_id
                break
            try:
                result = self.settle_plan(owner, plan, deadline)
            except ReductionPending as exc:
                status, blocked, pending_principal = "authorization-pending", token_id, plan.principal
                pending_minimum, pending_deadline = plan.minimum_relief_scaled, deadline
                pending_hash, pending_reverted = exc.transaction_hash, exc.reverted
                pending_phase = exc.phase
                break
            except Exception as exc:
                # settle_plan wraps exceptions after transaction submission in
                # ReductionPending; this is a failure of its preflight read.
                status, blocked, error = "preflight-unavailable", token_id, str(exc)[:200]
                break
            settled.append(result)
            retired += result.principal
        else:
            if retired < requested:
                status = "partial"
        return HouseholdRetirement(requested, retired, sum(x.relief_scaled for x in settled),
                                   sum(x.owner_cost_scaled for x in settled),
                                   sum(x.premium_nominal for x in settled), status, blocked, pending_principal,
                                   pending_minimum, pending_deadline, pending_hash, pending_reverted, pending_phase, error,
                                   tuple(settled))
