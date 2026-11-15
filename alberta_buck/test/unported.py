"""Markers for current tests whose code under test is not yet ported to the canonical Buck."""
import pytest
from buck_core.session import TransactionReverted
from web3.exceptions import ABIFunctionNotFound

# The control lab's EVM world (alberta_buck/sim/control_lab) deploys basket_impl="equity-ops", which
# is the canonical net Buck since 954ecb10, but the lab still drives the pre-canonical one:
#   1. its consistency-arbitrage actor is a BasketMachineWheel, whose setStartMode reverts
#      InvalidArbConfiguration since 9e0364d3 (the isolated flash arbitrage lives on in BasketWheel);
#   2. its books read the legacy-signed Buck: signedBalanceOf, reliefOf, mint(uint256) issuance.
# Strict: once the lab is ported these pass, and the marker must come off.
CONTROL_LAB_WORLD               = pytest.mark.xfail(
    strict=True, raises=(TransactionReverted, ABIFunctionNotFound),
    reason="control lab world not yet ported to the canonical Buck (alberta_buck/test/unported.py)")
