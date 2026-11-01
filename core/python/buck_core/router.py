"""Universal Router path and exact-input encoding shared by Python clients."""

from eth_abi import encode
from web3 import Web3


def encode_path(tokens_fees: list) -> bytes:
    """[tokenA, fee0, tokenB, ...] as a packed Uniswap V3 path."""
    out = b""
    for i, item in enumerate(tokens_fees):
        if i % 2 == 0:
            out += bytes.fromhex(Web3.to_checksum_address(item)[2:])
        else:
            out += int(item).to_bytes(3, "big")
    return out


def ur_exec_args(recipient: str, amount_in: int, path: bytes, *, minimum_out: int = 0,
                 payer_is_user: bool = False):
    """Encode a V3 exact-input swap with an explicit minimum and payer.

    Defaults preserve the older prefunded simulation route. A user-paid route
    requires prior ERC-20 approval to Permit2 and Permit2 approval to the
    router. An output minimum does not establish a fair market price.
    """
    if (type(amount_in) is not int or amount_in < 0 or type(minimum_out) is not int or minimum_out < 0
            or type(payer_is_user) is not bool or not isinstance(path, bytes)):
        raise ValueError("invalid exact-input swap arguments")
    command = bytes([0x00])
    inp = encode(["address", "uint256", "uint256", "bytes", "bool", "uint256[]"],
                 [Web3.to_checksum_address(recipient), amount_in, minimum_out, path, payer_is_user, []])
    return command, [inp]
