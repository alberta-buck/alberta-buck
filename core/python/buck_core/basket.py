"""Read an owner's actual canonical Buck basket receipt marks."""

from dataclasses import dataclass

from web3 import Web3
from web3.logs import DISCARD
from buck_core.session import TransactionReceiptPending, TransactionReverted, _txhex


MAX_UINT256 = 2**256 - 1
RECEIPT_ABI = [{"type": "function", "name": "ownerOf", "stateMutability": "view",
                "inputs": [{"name": "tokenId", "type": "uint256"}],
                "outputs": [{"name": "owner", "type": "address"}]}]


@dataclass(frozen=True)
class BasketDepositPlan:
    block_number: int
    owner: str
    token_id: int                 # zero creates a receipt; positive adds in place
    amount: int                   # full-value Ordinary BUCK
    minimum_shares: int
    prior_shares: int
    prior_basis: int


@dataclass(frozen=True)
class BasketRedemptionPlan:
    block_number: int
    owner: str
    token_id: int
    shares: int
    minimum_buck: int             # net BUCK delivered; cash-only household bound
    prior_shares: int
    prior_basis: int


@dataclass(frozen=True)
class BasketDeposit:
    token_id: int
    received: int
    shares_added: int
    shares: int
    basis: int
    receipt: object


@dataclass(frozen=True)
class BasketRedemption:
    token_id: int
    paid: int
    shares_retired: int
    shares_remaining: int
    basis_remaining: int
    in_kind: tuple
    receipt: object


class NetBasketOps:
    """Ordinary BUCK entries and selected receipt exits on the existing basket.

    PRE: caller serializes the owner, prepares allowances/identity, retains
    the quoted plan across uncertain submission and trusts its chain reader.
    POST: authenticate exact contract/owner/asset/quantity/hash and the dated
    holding transition before returning a result. No wallet/position ledger
    is changed here. A lost reply or required post-read failure retains its
    hash, permitting recovery without another deposit/redemption. Marks are
    advisory; minShares/minBuck and the basket's K/price guards govern sends.

    ``simulate(owner, contract, method, args, block_number)`` returns the raw
    return data of that call made AS the owner, with every write discarded.
    The default is a direct eth_call from the owner. A contract-held account
    needs its own read-only dispatch where the node refuses a sender with code
    (EIP-3607); it must run the unmodified target and add no authority.
    """
    def __init__(self, chain, basket, buck, execute, simulate=None):
        self.chain, self.basket, self.buck, self.execute = chain, basket, buck, execute
        self.simulate = simulate or self._call

    def _call(self, owner, contract, method, args, block_number):
        return self.chain.w3.eth.call({"from": owner, "to": contract.address,
                                       "data": contract.encode_abi(method, args=args)}, block_identifier=block_number)

    def _snapshot(self, owner, token_id):
        owner = getattr(owner, "address", owner)
        if (not isinstance(owner, str) or not Web3.is_address(owner) or int(owner, 16) == 0 or
                type(token_id) is not int or not 0 <= token_id <= MAX_UINT256):
            raise ValueError("invalid basket owner/receipt")
        block = self.chain.w3.eth.block_number
        read = lambda fn: fn.call(block_identifier=block)
        if not read(self.basket.functions.netAccounting()) or read(self.basket.functions.buck()).lower() != self.buck.address.lower():
            raise ValueError("basket is not configured for this net BUCK")
        flags = [{"type": "function", "name": n, "stateMutability": "view",
                  "inputs": [{"name": "account", "type": "address"}], "outputs": [{"type": "bool"}]}
                 for n in ("isVerified", "isCarrying")]
        reg = self.chain.w3.eth.contract(address=read(self.buck.functions.identity()), abi=flags)
        if not read(reg.functions.isVerified(owner)) or read(reg.functions.isCarrying(owner)):
            raise ValueError("basket household must be verified and Ordinary")
        shares, basis = 0, 0
        if token_id:
            receipt = self.chain.w3.eth.contract(address=read(self.basket.functions.receipt()), abi=RECEIPT_ABI)
            if read(receipt.functions.ownerOf(token_id)).lower() != owner.lower():
                raise ValueError("foreign basket receipt")
            if read(self.basket.functions.deposits(token_id))[2].lower() != self.buck.address.lower():
                raise ValueError("household position must be a BUCK receipt")
            shares, basis = read(self.basket.functions.holdings(token_id))
        if block != self.chain.w3.eth.block_number:
            raise ValueError("basket observation changed")
        return block, owner, shares, basis

    def prepare_deposit(self, owner, amount, minimum_shares, *, token_id=0):
        if any(type(v) is not int or not 0 < v <= MAX_UINT256 for v in (amount, minimum_shares)):
            raise ValueError("positive basket deposit/share bound required")
        block, address, shares, basis = self._snapshot(owner, token_id)
        return BasketDepositPlan(block, address, token_id, amount, minimum_shares, shares, basis)

    def prepare_redemption(self, owner, token_id, shares, minimum_buck):
        if any(type(v) is not int or not 0 < v <= MAX_UINT256 for v in (token_id, shares, minimum_buck)):
            raise ValueError("positive basket redemption/cash bound required")
        block, address, prior_shares, basis = self._snapshot(owner, token_id)
        if shares > prior_shares:
            raise ValueError("basket redemption exceeds owned shares")
        return BasketRedemptionPlan(block, address, token_id, shares, minimum_buck, prior_shares, basis)

    def preview_redemption(self, owner, token_id, shares):
        """Simulate the existing exit at one block; discard all EVM writes.

        This includes actual K, treasury/exit charges and current cash/in-kind
        behavior without a duplicate valuation formula or Solidity quote API.
        A revert is an unavailable exit, never permission to weaken its guard.
        POST: neither the holding nor the chain height has changed.
        """
        if any(type(v) is not int or not 0 < v <= MAX_UINT256 for v in (token_id, shares)):
            raise ValueError("positive basket receipt/shares required")
        block, address, prior, basis = self._snapshot(owner, token_id)
        if shares > prior:
            raise ValueError("basket redemption exceeds owned shares")
        raw = self.simulate(address, self.basket, "redeemShares", [token_id, shares, 0], block)
        outputs = self.basket.get_function_by_name("redeemShares").abi["outputs"]
        result = self.chain.w3.codec.decode([o["type"] for o in outputs], bytes(raw))
        if (block != self.chain.w3.eth.block_number or
                self.basket.functions.holdings(token_id).call(block_identifier=block) != [prior, basis]):
            raise ValueError("basket observation changed")
        return dict(block_number=block, position_id=result[0], paid=result[1],
                    shares_retired=result[2], shares_remaining=result[3])

    def _events(self, contract, name, receipt):
        return [e["args"] for e in getattr(contract.events, name)().process_receipt(receipt, errors=DISCARD)
                if e["address"].lower() == contract.address.lower()]

    def _evidence(self, receipt, expected_hash):
        if (not receipt or receipt.get("status") != 1 or not expected_hash or
                _txhex(receipt.get("transactionHash")).lower() != expected_hash.lower()):
            raise ValueError("basket receipt success/hash mismatch")

    def deposit_from_receipt(self, plan, receipt, *, expected_hash):
        self._evidence(receipt, expected_hash)
        events = self._events(self.basket, "Deposited", receipt)
        if len(events) != 1:
            raise ValueError("basket deposit evidence missing/ambiguous")
        e = events[0]
        tid = e["receiptId"]
        if (e["who"].lower() != plan.owner.lower() or e["token"].lower() != self.buck.address.lower() or
                e["tokenAmount"] != plan.amount or tid == 0 or plan.token_id and tid != plan.token_id):
            raise ValueError("basket deposit evidence differs from plan")
        flows = [e for e in self._events(self.buck, "NetValueTransfer", receipt)
                 if e["from"].lower() == plan.owner.lower() and e["to"].lower() == self.basket.address.lower()]
        if len(flows) != 1 or flows[0]["nominal"] != plan.amount or flows[0]["netScaled"] != plan.amount * 10**36:
            raise ValueError("basket deposit net cash evidence mismatch")
        read = lambda fn: fn.call(block_identifier=receipt["blockNumber"])
        nft = self.chain.w3.eth.contract(address=read(self.basket.functions.receipt()), abi=RECEIPT_ABI)
        shares, basis = read(self.basket.functions.holdings(tid))
        if (read(nft.functions.ownerOf(tid)).lower() != plan.owner.lower() or
                basis != plan.prior_basis + plan.amount or shares < plan.prior_shares + plan.minimum_shares):
            raise ValueError("basket deposit holding transition mismatch")
        inc = self._events(self.basket, "PositionIncreased", receipt)
        if plan.token_id:
            if (len(inc) != 1 or inc[0]["receiptId"] != tid or inc[0]["who"].lower() != plan.owner.lower() or
                    inc[0]["asset"].lower() != self.buck.address.lower() or inc[0]["nominalAmount"] != plan.amount or
                    inc[0]["received"] != plan.amount or inc[0]["sharesAdded"] != shares - plan.prior_shares):
                raise ValueError("basket top-up evidence mismatch")
        elif inc:
            raise ValueError("new basket receipt unexpectedly topped up a position")
        return BasketDeposit(tid, plan.amount, shares - plan.prior_shares, shares, basis, receipt)

    def redemption_from_receipt(self, plan, receipt, *, expected_hash):
        self._evidence(receipt, expected_hash)
        events = self._events(self.basket, "PositionReduced", receipt)
        if len(events) != 1:
            raise ValueError("basket redemption evidence missing/ambiguous")
        e = events[0]
        if (e["who"].lower() != plan.owner.lower() or e["receiptId"] != plan.token_id or
                e["requestedShares"] != plan.shares or not 0 < e["sharesRetired"] <= plan.shares or
                e["sharesRemaining"] != plan.prior_shares - e["sharesRetired"] or e["paid"] < plan.minimum_buck):
            raise ValueError("basket redemption evidence differs from plan")
        net = sum(e["netScaled"] for e in self._events(self.buck, "NetValueTransfer", receipt)
                  if e["from"].lower() == self.basket.address.lower() and e["to"].lower() == plan.owner.lower())
        if net // 10**36 != e["paid"]:
            raise ValueError("basket redemption net payout mismatch")
        shares, basis = self.basket.functions.holdings(plan.token_id).call(block_identifier=receipt["blockNumber"])
        retired = e["sharesRetired"]
        expected_basis = plan.prior_basis - (plan.prior_basis * plan.shares // plan.prior_shares) * retired // plan.shares
        if shares != e["sharesRemaining"] or basis != expected_basis:
            raise ValueError("basket redemption holding transition mismatch")
        kind = tuple((v["token"], v["amount"]) for v in self._events(self.basket, "PaidInKind", receipt)
                     if v["to"].lower() == plan.owner.lower())
        return BasketRedemption(plan.token_id, e["paid"], retired, shares, basis, kind, receipt)

    def _send(self, plan, method, args, reader):
        receipt = None
        try:
            receipt = self.execute(plan.owner, self.basket, method, args)
            if receipt and receipt.get("status") == 0 and _txhex(receipt.get("transactionHash")):
                raise TransactionReverted(method, "basket operation reverted", receipt)
            return reader(plan, receipt, expected_hash=_txhex(receipt.get("transactionHash")) if receipt else "")
        except TransactionReverted:
            raise
        except TransactionReceiptPending:
            raise
        except Exception as exc:
            raise TransactionReceiptPending(getattr(exc, "transaction_hash", None) or
                                            (_txhex(receipt.get("transactionHash")) if receipt else None)) from exc

    def deposit(self, plan):
        args = ([plan.token_id, self.buck.address, plan.amount, plan.amount, plan.minimum_shares]
                if plan.token_id else [self.buck.address, plan.amount, plan.minimum_shares])
        return self._send(plan, "increasePosition" if plan.token_id else "deposit", args, self.deposit_from_receipt)

    def redeem(self, plan):
        return self._send(plan, "redeemShares", [plan.token_id, plan.shares, plan.minimum_buck], self.redemption_from_receipt)


@dataclass(frozen=True)
class BasketPositionMark:
    token_id: int
    shares: int
    basis: int
    mark: int


@dataclass(frozen=True)
class HouseholdBasketMark:
    block_number: int
    share_price: int
    positions: tuple[BasketPositionMark, ...]
    basis: int
    mark: int

    @property
    def roi(self):
        return (self.mark - self.basis) / self.basis if self.basis else None


def mark_net_basket_positions(chain, basket, buck, owner, token_ids, *, block_number=None):
    """Read one owner's *open* receipt basis and TWAP share mark at one block.

    Basis and mark are raw BUCK, not dollars; ROI is not annualized and
    excludes exit costs. The caller must retain the current receipt IDs. A
    missing or foreign receipt, unavailable historical read or failed price
    is an error, not a zero valuation. No exit liquidity is promised.
    """
    address = getattr(owner, "address", owner)
    ids = tuple(token_ids)
    if (not isinstance(address, str) or not Web3.is_address(address) or int(address, 16) == 0 or
            any(type(i) is not int or not 0 < i <= MAX_UINT256 for i in ids) or len(ids) != len(set(ids)) or
            (block_number is not None and (type(block_number) is not int or block_number < 0))):
        raise ValueError("invalid basket mark inputs")
    block = chain.w3.eth.block_number if block_number is None else block_number
    read = lambda fn: fn.call(block_identifier=block)
    if not read(basket.functions.netAccounting()) or read(basket.functions.buck()).lower() != buck.address.lower():
        raise ValueError("basket is not configured for this net BUCK")
    receipt_address = read(basket.functions.receipt())
    receipt = chain.w3.eth.contract(address=receipt_address, abi=RECEIPT_ABI)
    price = read(basket.functions.sharePrice())
    positions = []
    for token_id in ids:
        if read(receipt.functions.ownerOf(token_id)).lower() != address.lower():
            raise ValueError("foreign basket receipt")
        shares, basis = read(basket.functions.holdings(token_id))
        # valueOf rounds once from total equity/shares. Multiplying the
        # already-rounded sharePrice can lose an additional raw unit.
        mark = read(basket.functions.valueOf(token_id))
        positions.append(BasketPositionMark(token_id, shares, basis, mark))
    return HouseholdBasketMark(block, price, tuple(positions), sum(p.basis for p in positions),
                               sum(p.mark for p in positions))
