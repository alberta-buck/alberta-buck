"""Read a household's actual BuckNet equity-basket receipt marks."""

from dataclasses import dataclass

from web3 import Web3


WAD = 10**18
MAX_UINT256 = 2**256 - 1
RECEIPT_ABI = [{"type": "function", "name": "ownerOf", "stateMutability": "view",
                "inputs": [{"name": "tokenId", "type": "uint256"}],
                "outputs": [{"name": "owner", "type": "address"}]}]


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
        product = shares * price
        if product > MAX_UINT256:
            raise ValueError("basket mark overflows contract arithmetic")
        positions.append(BasketPositionMark(token_id, shares, basis, product // WAD))
    return HouseholdBasketMark(block, price, tuple(positions), sum(p.basis for p in positions),
                               sum(p.mark for p in positions))
