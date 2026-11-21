// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IdentityRegistryFixture} from "../IdentityRegistry.t.sol";
import {IdentityRegistry} from "../../src/IdentityRegistry.sol";
import {Buck} from "../../src/Buck.sol";
import {BuckCredit} from "../../src/BuckCredit.sol";
import {JubileeFund, IJubileeBasket} from "../../src/JubileeFund.sol";
import {BuckBasketEquity} from "../../src/basket/BuckBasketEquity.sol";
import {BasketUpkeepFacet} from "../../src/basket/BasketUpkeepFacet.sol";
import {IBasketUpkeep} from "../../src/basket/IBasketUpkeep.sol";
import {BuckBasketStorage} from "../../src/basket/BuckBasketStorage.sol";
import {EqController} from "./BuckBasketEquity.t.sol";

/// @dev Actual production registry and signed credential vectors, shared via
///      an abstract fixture rather than repeating its existing test suite.
abstract contract BasketIdentityFixture is IdentityRegistryFixture {
    Buck token;
    BuckCredit credit;
    JubileeFund fund;
    BuckBasketEquity basket;
    IBasketUpkeep setup;

    function setUp() public virtual override {
        super.setUp();
        _registerAlice();
        credit = new BuckCredit();
        EqController k = new EqController(); k.setK(1e18);
        fund = new JubileeFund(GOV);
        token = new Buck(address(credit), address(k), address(reg), ISSUER, address(fund));
        credit.setBuck(address(token));
        vm.prank(GOV); reg.setBuck(address(token));
        basket = new BuckBasketEquity(address(token), address(k), address(1), GOV, 3000, 600, 64, 500, 1000);
        vm.startPrank(GOV);
        basket.setEquityWheel(address(new BasketUpkeepFacet(address(0), address(0), 0, 0)));   // the machine's
        basket.enableNetAccounting();
        vm.stopPrank();
        setup = IBasketUpkeep(address(basket));
    }

    function _authorize() internal {
        vm.prank(GOV);
        setup.authorizeIdentityBinding(reg, alice, _g1(".alice.elgamal_kp.pk"), _ct(".alice.ciphertext"));
    }

    function _bind() internal {
        _authorize();
        vm.prank(alice);
        reg.bindContract(address(basket), _g1(".alice.elgamal_kp.pk"), _ct(".alice.ciphertext"), true, true);
        vm.prank(GOV); token.setApprovedCarryingIssuer(address(basket), true);
    }
}

contract BasketCreditSetupTest is BasketIdentityFixture {

    function _open() private {
        vm.prank(GOV); basket.openCredit(address(credit), 1000e6);
    }

    function test_productionBindingAndOpeningCreateOnlyAnInactiveSelfInsuredFacility() public {
        _bind();
        assertTrue(reg.isVerified(address(basket))); assertTrue(reg.isPublicIdentity(address(basket)));
        assertTrue(reg.isCarrying(address(basket))); assertEq(reg.binderOf(address(basket)), alice);
        assertTrue(token.approvedCarryingIssuer(address(basket)));
        _open();
        uint256 id = basket.creditId();
        assertEq(address(basket.credit()), address(credit)); assertEq(credit.ownerOf(id), address(basket));
        (uint256 face, uint256 active, uint32 rate) = credit.creditInfo(id);
        assertEq(face, 1000e6); assertEq(active, 0); assertEq(rate, 0);
        (uint256 principal, uint256 age) = credit.issuanceOf(id);
        assertEq(principal, 0); assertEq(age, 0); assertEq(token.totalIssued(), 0);
        assertEq(token.balanceOf(address(basket)), 0); assertEq(basket.markNow(), 0);
        assertEq(credit.insurerOf(id), address(basket));
    }

    function test_publicOpenStillRequiresTheCorrectGovernanceAndVerifiedIdentity() public {
        vm.expectRevert(BuckBasketStorage.NotGovernance.selector);
        basket.openCredit(address(credit), 1000e6);
        vm.expectRevert(IBasketUpkeep.InvalidCreditIdentity.selector);
        vm.prank(GOV); basket.openCredit(address(credit), 1000e6);
        assertEq(address(basket.credit()), address(0));
        vm.expectRevert(BuckBasketStorage.NotSelf.selector);
        setup.initializeEquityCredit(address(credit), 1000e6);
        vm.expectRevert(BuckBasketStorage.NotSelf.selector);
        vm.prank(GOV); setup.initializeEquityCredit(address(credit), 1000e6);
    }

    function test_certifiedBinderCannotBindWithoutBasketAuthorizationOrChangeItsFlags() public {
        vm.expectRevert();
        vm.prank(alice);
        reg.bindContract(address(basket), _g1(".alice.elgamal_kp.pk"), _ct(".alice.ciphertext"), true, false);
        _authorize();
        bytes32 authorization = reg.pendingBindingAuthorization(address(basket));
        vm.expectRevert();
        vm.prank(alice);
        reg.bindContract(address(basket), _g1(".alice.elgamal_kp.pk"), _ct(".alice.ciphertext"), true, false);
        assertEq(reg.pendingBindingAuthorization(address(basket)), authorization);
        vm.prank(alice);
        reg.bindContract(address(basket), _g1(".alice.elgamal_kp.pk"), _ct(".alice.ciphertext"), true, true);
        assertTrue(reg.isVerified(address(basket)));
        assertEq(reg.pendingBindingAuthorization(address(basket)), bytes32(0));
    }

    function test_governanceCanRevokeButCannotSelectAnotherRegistry() public {
        vm.expectRevert(BuckBasketStorage.NotGovernance.selector);
        setup.authorizeIdentityBinding(reg, alice, _g1(".alice.elgamal_kp.pk"), _ct(".alice.ciphertext"));
        _authorize();
        vm.prank(GOV); setup.revokeIdentityBinding(reg);
        assertEq(reg.pendingBindingAuthorization(address(basket)), bytes32(0));
        vm.expectRevert();
        vm.prank(alice);
        reg.bindContract(address(basket), _g1(".alice.elgamal_kp.pk"), _ct(".alice.ciphertext"), true, false);
        IdentityRegistry other = new IdentityRegistry(GOV);
        vm.expectRevert(IBasketUpkeep.BindingSetupClosed.selector);
        vm.prank(GOV); setup.revokeIdentityBinding(other);
        _bind(); _open();
        vm.expectRevert(IBasketUpkeep.BindingSetupClosed.selector);
        vm.prank(GOV); setup.revokeIdentityBinding(reg);
    }

    function test_foreignCreditAndChangedModelFailWithoutOpeningAFacility() public {
        _bind();
        BuckCredit other = new BuckCredit(); other.setBuck(address(token));
        vm.expectRevert(IBasketUpkeep.WrongCreditContract.selector);
        vm.prank(GOV); basket.openCredit(address(other), 1000e6);
        assertEq(address(basket.credit()), address(0)); assertEq(other.balanceOf(address(basket)), 0);
        vm.mockCall(address(token), abi.encodeWithSignature("ACCOUNTING_MODEL()"), abi.encode(bytes32(uint256(1))));
        vm.expectRevert(IBasketUpkeep.InvalidCreditModel.selector);
        vm.prank(GOV); basket.openCredit(address(credit), 1000e6);
        vm.clearMockedCalls();
        _open();
        vm.expectRevert(BuckBasketStorage.AlreadyPresent.selector);
        vm.prank(GOV); basket.openCredit(address(credit), 1000e6);
    }
}
