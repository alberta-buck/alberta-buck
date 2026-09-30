"""Externally-driven Alberta-Buck simulation (anvil + web3.py).

A Python program owns the timeline and the actors; `anvil` is a hosted EVM
running the *real* BuckKControllerDirect / Buck / BuckBasket / IdentityRegistry
stack plus a real Uniswap V3 + Universal Router.

Run:  python -m alberta_buck.sim --scenario routing --days 120
"""
