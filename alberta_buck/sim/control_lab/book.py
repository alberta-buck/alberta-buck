"""The basket's recorded book, revalued at prices the reader chooses.

The contract's equity is in BUCK at its own pool marks, and the lab converts
it to USD at the BUCK/USDC spot. In a stage with no K, desk or private flow
that spot drifts with the basket's own trades, so the two units can disagree
about the sign of a change. Valuing TOKEN at the external feed and net BUCK
at the reference basket's cost removes that exchange rate from the comparison.
"""
import math


def holdings(frame):
    """TOKEN held and net BUCK, from the wallet, the full-range positions and the account.

    A full-range position of liquidity L at price p (BUCK per whole TOKEN)
    holds L/sqrt(p) TOKEN and L*sqrt(p) BUCK, in raw units 12 decimals apart.
    """
    basket = frame["basket"]
    tokens, buck = [], basket["account"] + basket["relief"]
    for idle, liquidity, price in zip(basket["idle"], basket["liquidity"], frame["token_buck"]):
        root = math.sqrt(price)
        tokens.append(idle + int(liquidity) / (root * 1e12))
        buck += int(liquidity) * root / 1e12
    return tokens, buck


def reference_value(frame, held=None):
    """USD: TOKEN at the external feed, net BUCK at what one reference basket costs.

    Pass another frame's `holdings` to value a book that was never traded.
    """
    tokens, buck = held or holdings(frame)
    return sum(q * p for q, p in zip(tokens, frame["target_usd"])) + buck * frame["reference"]["target_usd"]


def recorded(frame):
    """Frames saved before the book was recorded cannot be revalued."""
    return "idle" in frame["basket"]
