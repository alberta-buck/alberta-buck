"""Integer, same-block V3 estimates for household decisions.

Virtual reserves describe constant active liquidity, not a promise about
crossing ticks, future prices or fair value. BUCK sales pay nominal input
from an ordinary wallet; purchases receive the Carrying pool's proportional
net value. Actual execution still needs its on-chain output bound/guard.
"""
from dataclasses import dataclass

MAX = 2**256 - 1
PPM = 10**6
Q96 = 2**96


def _amount(value):
    if type(value) is not int or not 0 <= value <= MAX:
        raise ValueError("invalid net market amount")


def _ceil(n, d):
    return (n + d - 1) // d


@dataclass(frozen=True)
class NetMarketQuote:
    block_number: int
    reserve_usdc: int
    reserve_buck: int
    gross_scaled: int
    net_scaled: int
    fee_ppm: int
    price_num: int
    price_den: int

    def __post_init__(self):
        if (any(type(v) is not int for v in self.__dict__.values()) or self.block_number < 0 or
                min(self.reserve_usdc, self.reserve_buck, self.gross_scaled, self.price_num, self.price_den) <= 0 or
                max(self.reserve_usdc, self.reserve_buck) > MAX or
                not 0 <= self.net_scaled <= self.gross_scaled <= MAX or not 0 <= self.fee_ppm < PPM):
            raise ValueError("invalid net market quote")

    def sell_expected(self, buck):
        _amount(buck)
        effective = buck * (PPM - self.fee_ppm) // PPM
        return self.reserve_usdc * effective // (self.reserve_buck + effective)

    def buy_expected(self, usdc):
        _amount(usdc)
        effective = usdc * (PPM - self.fee_ppm) // PPM
        nominal = self.reserve_buck * effective // (self.reserve_usdc + effective)
        return nominal * self.net_scaled // self.gross_scaled

    def buy_cost(self, net_buck):
        """Estimate exact-net acquisition cost; None means no such local quote.

        Round nominal output up for carried fees, then input and trading
        fees up separately. One input-unit cushion accounts for virtual
        reserve flooring. A caller still sets its own maximum USDC and must
        authenticate the guard's actual payment/delivery after execution.
        """
        _amount(net_buck)
        if not net_buck:
            return 0
        if not self.net_scaled:
            return None
        nominal = _ceil(net_buck * self.gross_scaled, self.net_scaled)
        if nominal >= self.reserve_buck:
            return None
        effective = _ceil(self.reserve_usdc * nominal, self.reserve_buck - nominal) + 1
        cost = _ceil(effective * PPM, PPM - self.fee_ppm)
        return cost if cost <= MAX else None


POOL_ABI = [
    {"type": "function", "name": name, "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": typ}]} for name, typ in
    (("token0", "address"), ("token1", "address"), ("liquidity", "uint128"), ("fee", "uint24"))]
POOL_ABI.append({"type": "function", "name": "slot0", "stateMutability": "view", "inputs": [], "outputs": [
    {"name": name, "type": typ} for name, typ in
    (("sqrtPriceX96", "uint160"), ("tick", "int24"), ("observationIndex", "uint16"),
     ("observationCardinality", "uint16"), ("observationCardinalityNext", "uint16"),
     ("feeProtocol", "uint8"), ("unlocked", "bool"))]})


def observe_net_market(chain, buck, settlement, pool, fee_ppm, *, block_number=None):
    """Read one known venue at a pinned block; this authorizes no trade."""
    if type(fee_ppm) is not int or not 0 <= fee_ppm < PPM:
        raise ValueError("invalid net market fee")
    block = chain.w3.eth.block_number if block_number is None else block_number
    if type(block) is not int or block < 0:
        raise ValueError("invalid net market block")
    address = getattr(pool, "address", pool)
    venue = chain.w3.eth.contract(address=address, abi=POOL_ABI)
    read = lambda fn: fn.call(block_identifier=block)
    t0, t1 = read(venue.functions.token0()), read(venue.functions.token1())
    if {t0.lower(), t1.lower()} != {buck.address.lower(), settlement.address.lower()}:
        raise ValueError("net market quote requires the configured BUCK/settlement pair")
    if int(read(venue.functions.fee())) != fee_ppm:
        raise ValueError("net market quote fee mismatch")
    flags = [{"type": "function", "name": name, "stateMutability": "view", "inputs": [{"name": "account", "type": "address"}],
              "outputs": [{"name": "", "type": "bool"}]} for name in ("isVerified", "isCarrying")]
    registry = chain.w3.eth.contract(address=read(buck.functions.identity()), abi=flags)
    if not read(registry.functions.isVerified(address)) or not read(registry.functions.isCarrying(address)):
        raise ValueError("net market quote needs a verified Carrying pool")
    sqrt, liquidity = int(read(venue.functions.slot0())[0]), int(read(venue.functions.liquidity()))
    if min(sqrt, liquidity) <= 0:
        raise ValueError("net market quote needs initialized liquidity")
    r0, r1 = liquidity * Q96 // sqrt, liquidity * sqrt // Q96
    gross, net = read(buck.functions.accountBalancesScaled(address))
    rb, ru = (r0, r1) if t0.lower() == buck.address.lower() else (r1, r0)
    numerator, denominator = (sqrt * sqrt, Q96 * Q96) if t0.lower() == buck.address.lower() else (Q96 * Q96, sqrt * sqrt)
    return NetMarketQuote(block, ru, rb, gross, net, fee_ppm, numerator, denominator)
