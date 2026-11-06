// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Vm} from "forge-std/Vm.sol";
import {BN254} from "../../src/BN254.sol";
import {IdentityRegistry} from "../../src/IdentityRegistry.sol";
import {Buck} from "../../src/Buck.sol";
import {BuckCredit} from "../../src/BuckCredit.sol";
import {IJubileeBasket} from "../../src/JubileeFund.sol";
import {BuckBasketUniswapV3} from "../../src/basket/BuckBasketUniswapV3.sol";
import {WorkWheel} from "../../src/wheel/WorkWheel.sol";
import {BasketMachineWheel} from "../../src/wheel/BasketMachineWheel.sol";
import {BasketIdentityFixture} from "../basket/BasketCreditSetup.t.sol";

contract BindingDuringWork {
    BasketMachineWheel immutable wheel;
    uint256 public calls;
    constructor(BasketMachineWheel wheel_) { wheel = wheel_; }
    function configure() external { wheel.setEquity(address(this)); }
    function constituentsLength() external pure returns (uint256) { return 0; }
    function wheelDue(uint8 kind, uint256) external view returns (bool) { return kind == 2 && calls == 0; }
    function wheelStep(uint8, uint256) external returns (uint256) {
        require(msg.sender == address(wheel));
        (bool ok, bytes memory reason) = address(wheel).call(abi.encodeCall(wheel.revokeIdentityBinding, ()));
        require(!ok && bytes4(reason) == WorkWheel.WheelReentrant.selector, "binding revocation reentered");
        BN254.G1Point memory g = BN254.g1();
        IdentityRegistry.ElGamalCT memory e = IdentityRegistry.ElGamalCT(g, g);
        (ok, reason) = address(wheel).call(abi.encodeCall(wheel.authorizeIdentityBinding, (address(1), g, e)));
        require(!ok && bytes4(reason) == WorkWheel.WheelReentrant.selector, "binding authorization reentered");
        ++calls;
        return 1;
    }
}

/// @dev Production registry/credentials, real Buck cash, and actual basket
///      daily work. Private keeper introductions use a real domain-bound CP
///      proof; no mock-verifier or privileged receipt insertion is used.
contract BasketMachineIdentityTest is BasketIdentityFixture {
    uint256 constant Q = 1e36;
    uint256 constant YEAR = 365 days + 6 hours;
    BasketMachineWheel wheel;
    uint256 facility;

    function setUp() public override {
        super.setUp();
        vm.warp(1_000_000);
        wheel = new BasketMachineWheel(address(token), address(2), GOV, 1000, 20e6);
    }

    function _authorizeWheel() private {
        vm.prank(GOV);
        wheel.authorizeIdentityBinding(alice, _g1(".alice.elgamal_kp.pk"), _ct(".alice.ciphertext"));
    }

    function _bindWheel() private {
        _authorizeWheel();
        vm.prank(alice);
        reg.bindContract(address(wheel), _g1(".alice.elgamal_kp.pk"), _ct(".alice.ciphertext"), true, true);
    }

    function _ready() private {
        _bindWheel(); _bind();
        vm.prank(GOV);
        fund.authorizeIdentityBinding(reg, alice, _g1(".alice.elgamal_kp.pk"), _ct(".alice.ciphertext"));
        vm.prank(alice);
        reg.bindContract(address(fund), _g1(".alice.elgamal_kp.pk"), _ct(".alice.ciphertext"), true, true);
        BuckBasketUniswapV3 venue = new BuckBasketUniswapV3();
        vm.startPrank(GOV);
        fund.configure(token, IJubileeBasket(address(basket)));
        basket.setVenue(address(venue));
        venue = BuckBasketUniswapV3(address(basket));
        venue.setWheel(address(wheel));
        wheel.setEquity(address(basket));
        vm.stopPrank();
        vm.startPrank(alice);
        credit.setCreditIssuer(alice, true);
        facility = credit.createCredit(alice, 0, 100e6, 100e6, BuckCredit.DepreciationType.NONE, 0, 0, 0);
        token.issue(facility, 100e6, 100e6, 0, address(0));
        vm.stopPrank();
    }

    /// @dev Re-encrypt the certified Alice identity to this wheel's key,
    ///      then build the CP witness for the actual registry/chain/parties.
    function _introduce(uint256 allowance_) private {
        _introduceKeeper(alice, _u(".alice.elgamal_kp.sk"), allowance_);
    }

    function _introduceKeeper(address keeper, uint256 sk, uint256 allowance_) private {
        BN254.G1Point memory pkA = reg.pkOf(keeper);
        BN254.G1Point memory pkB = reg.pkOf(address(wheel));
        IdentityRegistry.ElGamalCT memory ea = reg.ciphertextOf(keeper);
        BN254.G1Point memory m = BN254.add(ea.C, BN254.neg(BN254.mul(ea.R, sk)));
        IdentityRegistry.ElGamalCT memory eb = IdentityRegistry.ElGamalCT(
            BN254.mul(BN254.g1(), 7), BN254.add(m, BN254.mul(pkB, 7)));
        IdentityRegistry.CPProof memory p;
        p.T1 = BN254.mul(BN254.g1(), 11);
        p.T2 = BN254.add(BN254.mul(ea.R, 11), BN254.neg(BN254.mul(pkB, 13)));
        p.T3 = BN254.mul(BN254.g1(), 13);
        BN254.G1Point[] memory points = new BN254.G1Point[](9);
        points[0] = ea.R; points[1] = ea.C; points[2] = eb.R; points[3] = eb.C;
        points[4] = pkA; points[5] = pkB; points[6] = p.T1; points[7] = p.T2; points[8] = p.T3;
        uint256[] memory scalars = new uint256[](5);
        scalars[0] = uint256(uint160(keeper)); scalars[1] = uint256(uint160(address(wheel)));
        scalars[2] = block.chainid; scalars[3] = uint256(uint160(address(reg)));
        scalars[4] = uint256(keccak256("AlbertaBuck/FiatShamir/IdentityRegistry/Approve/v2"));
        p.e = BN254.fsChallenge(points, scalars);
        p.s1 = addmod(11, mulmod(p.e, sk, BN254.R), BN254.R);
        p.s2 = addmod(13, mulmod(p.e, 7, BN254.R), BN254.R);
        assertTrue(reg.verifyApprove(keeper, address(wheel), eb, p));
        vm.prank(keeper); token.approve(address(wheel), allowance_, eb, p);
    }

    function test_onlyCurrentGovernanceCanAuthorizeAndRevoke() public {
        vm.expectRevert(WorkWheel.NotGovernance.selector);
        wheel.authorizeIdentityBinding(alice, _g1(".alice.elgamal_kp.pk"), _ct(".alice.ciphertext"));
        _authorizeWheel();
        assertTrue(reg.pendingBindingAuthorization(address(wheel)) != bytes32(0));
        vm.expectRevert(WorkWheel.NotGovernance.selector);
        wheel.revokeIdentityBinding();
        vm.prank(GOV); wheel.revokeIdentityBinding();
        assertEq(reg.pendingBindingAuthorization(address(wheel)), bytes32(0));
        vm.prank(GOV); wheel.setGovernance(ISSUER);
        vm.expectRevert(WorkWheel.NotGovernance.selector);
        vm.prank(GOV); wheel.revokeIdentityBinding();
        vm.prank(ISSUER);
        wheel.authorizeIdentityBinding(alice, _g1(".alice.elgamal_kp.pk"), _ct(".alice.ciphertext"));
        assertTrue(reg.pendingBindingAuthorization(address(wheel)) != bytes32(0));
    }

    function test_bindingPinsActualTokenRegistryAndBothServiceFlags() public {
        vm.expectRevert();
        vm.prank(alice);
        reg.bindContract(address(wheel), _g1(".alice.elgamal_kp.pk"), _ct(".alice.ciphertext"), true, true);
        _authorizeWheel();
        IdentityRegistry other = new IdentityRegistry(GOV);
        assertEq(other.pendingBindingAuthorization(address(wheel)), bytes32(0));
        bytes32 pending = reg.pendingBindingAuthorization(address(wheel));
        vm.expectRevert();
        vm.prank(alice);
        reg.bindContract(address(wheel), _g1(".alice.elgamal_kp.pk"), _ct(".alice.ciphertext"), true, false);
        assertEq(reg.pendingBindingAuthorization(address(wheel)), pending);
        vm.prank(alice);
        reg.bindContract(address(wheel), _g1(".alice.elgamal_kp.pk"), _ct(".alice.ciphertext"), true, true);
        assertTrue(reg.isVerified(address(wheel))); assertTrue(reg.isPublicIdentity(address(wheel)));
        assertTrue(reg.isCarrying(address(wheel))); assertEq(reg.binderOf(address(wheel)), alice);
        assertEq(wheel.reserve(), 0); assertEq(wheel.cursor(), 0); assertFalse(wheel.valuationReady());
        assertEq(token.totalIssued(), 0);
    }

    function test_revokedBindingCannotBeConsumedAndCanBeReauthorized() public {
        _authorizeWheel();
        vm.prank(GOV); wheel.revokeIdentityBinding();
        vm.expectRevert();
        vm.prank(alice);
        reg.bindContract(address(wheel), _g1(".alice.elgamal_kp.pk"), _ct(".alice.ciphertext"), true, true);
        _bindWheel();
        assertTrue(reg.isCarrying(address(wheel)));
        vm.expectRevert(bytes("already bound"));
        _authorizeWheel();
    }

    function test_privateFundingNeedsItsReceiptAndAnIdleTickEarnsNothing() public {
        _ready();
        vm.prank(alice); token.approve(address(wheel), 20e6);
        vm.expectRevert(Buck.SenderApprovalRequired.selector);
        vm.prank(alice); wheel.fund(20e6);
        assertEq(wheel.reserve(), 0); assertEq(token.balanceOf(address(wheel)), 0);
        assertEq(token.allowance(alice, address(wheel)), 20e6);
        _introduce(20e6);
        vm.prank(alice); wheel.fund(100e6); // Only the 20-BUCK cap is pulled.
        assertEq(wheel.reserve(), 20e6); assertEq(token.balanceOf(address(wheel)), 20e6);
        vm.prank(alice); wheel.tick(1, 0);
        uint256 reserve = wheel.reserve();
        vm.prank(alice); (uint256 work, uint256 paid) = wheel.tick(10, 0);
        assertEq(work, 0); assertEq(paid, 0); assertEq(wheel.reserve(), reserve);
    }

    function test_agedReserveAndRewardAreNominalWhileTheKeeperReceivesActualNetValue() public {
        _ready(); _introduce(20e6);
        vm.prank(alice); wheel.fund(20e6);
        vm.warp(vm.getBlockTimestamp() + YEAR);
        (uint256 gross, uint256 beforeNet) = token.accountBalancesScaled(address(wheel));
        assertEq(token.balanceOf(address(wheel)), 20e6); assertEq(wheel.reserve(), 20e6);
        assertLt(beforeNet, gross);
        uint256 keeperBefore = token.netBalanceScaled(alice);
        vm.recordLogs();
        vm.prank(alice); (uint256 work, uint256 paid) = wheel.tick(1, 0);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        assertEq(work, 1); assertEq(paid, 2e6);
        assertEq(wheel.reserve(), 18e6); assertEq(token.balanceOf(address(wheel)), 18e6);
        assertEq(basket.lastDay(), vm.getBlockTimestamp() / 1 days);
        uint256 received;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == address(token)
                && logs[i].topics[0] == keccak256("NetValueTransfer(address,address,uint256,uint256)")) {
                (uint256 nominal, uint256 net) = abi.decode(logs[i].data, (uint256, uint256));
                assertEq(nominal, paid); received += net;
            }
        }
        assertGt(received, 1_900_000 * Q); assertLt(received, paid * Q);
        assertEq(token.netBalanceScaled(alice) - keeperBefore, received);
        (, uint256 afterNet) = token.accountBalancesScaled(address(wheel));
        assertEq(beforeNet - afterNet, received);
        assertEq(token.totalIssued(), 100e6);
        (uint256 principal,) = credit.issuanceOf(facility); assertEq(principal, 100e6);
    }

    function test_unintroducedPrivatePayeeRevertsTheRewardAndAllBasketWork() public {
        _ready(); _introduce(20e6); _registerBob();
        vm.prank(alice); wheel.fund(20e6);
        vm.expectRevert(Buck.RecipientApprovalRequired.selector);
        vm.prank(bob); wheel.tick(1, 0);
        assertEq(basket.lastDay(), 0); assertEq(wheel.cursor(), 0); assertEq(wheel.reserve(), 20e6);
        assertEq(token.balanceOf(address(wheel)), 20e6);
        vm.prank(alice); (uint256 work, uint256 paid) = wheel.tick(1, 0);
        assertEq(work, 1); assertEq(paid, 2e6);
    }

    function test_privateKeeperCanIntroduceWithZeroAllowanceAndNoFundsOrFacility() public {
        _ready(); _introduce(20e6); _registerBob();
        vm.prank(alice); wheel.fund(20e6);
        _introduceKeeper(bob, _u(".bob.elgamal_kp.sk"), 0);
        assertEq(token.balanceOf(bob), 0); assertEq(credit.balanceOf(bob), 0);
        assertEq(token.allowance(bob, address(wheel)), 0);
        vm.warp(vm.getBlockTimestamp() + YEAR);
        vm.prank(bob); (uint256 work, uint256 paid) = wheel.tick(1, 0);
        assertEq(work, 1); assertEq(paid, 2e6);
        assertGt(token.netBalanceScaled(bob), 1_900_000 * Q);
        assertLt(token.netBalanceScaled(bob), paid * Q);
        assertEq(token.allowance(bob, address(wheel)), 0); assertEq(credit.balanceOf(bob), 0);
    }

    function test_zeroAllowanceIntroductionFreezesCarryingModeBeforeFunding() public {
        _bindWheel();
        assertFalse(reg.carryingFrozen(address(wheel)));
        _introduce(0);
        assertTrue(reg.carryingFrozen(address(wheel)));
        assertEq(token.allowance(alice, address(wheel)), 0);
        assertEq(wheel.reserve(), 0); assertEq(token.totalIssued(), 0);
        vm.expectRevert(bytes("carrying frozen by approval"));
        vm.prank(alice); reg.setIsCarrying(address(wheel), false);
        assertTrue(reg.isCarrying(address(wheel)));
    }

    function test_workCallbacksCannotReplaceOrClearPendingBinding() public {
        _authorizeWheel();
        bytes32 pending = reg.pendingBindingAuthorization(address(wheel));
        BindingDuringWork probe = new BindingDuringWork(wheel);
        vm.prank(GOV); wheel.setGovernance(address(probe));
        probe.configure();
        (uint256 work,) = wheel.tick(1, 0);
        assertEq(work, 1); assertEq(probe.calls(), 1);
        assertEq(reg.pendingBindingAuthorization(address(wheel)), pending);
        assertFalse(reg.isVerified(address(wheel)));
    }
}
