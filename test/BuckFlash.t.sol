// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Buck} from "../src/Buck.sol";
import {IBuckFlashBorrower} from "../src/IBuckFlashBorrower.sol";
import {BuckCompoundFixture} from "./BuckCompound.t.sol";

/// @dev Test only: hands BUCK back to whoever asks (a Carrying relay, as a pool would be).
contract FlashRelay {
    function back(Buck token, uint256 amount) external { token.transfer(msg.sender, amount); }
}

/// @dev Test only: a borrower that repays, keeps part, nests, or trades through a relay.
contract FlashBorrower is IBuckFlashBorrower {
    enum Mode { Repay, Keep, Nest, Relay }
    Buck public immutable token;
    Mode public mode;
    address public peer;
    uint256 public seenBalance;
    uint256 public seenSupply;
    uint256 public calls;

    constructor(Buck token_) { token = token_; }
    function setMode(Mode mode_, address peer_) external { mode = mode_; peer = peer_; }
    function flash(uint256 id, uint256 amount) external {
        require(abi.decode(token.flashIssue(id, amount, abi.encode(id)), (uint256)) == calls, "result");
    }

    function onBuckFlash(uint256 id, uint256 amount, bytes calldata data) external returns (bytes memory) {
        require(msg.sender == address(token), "not buck");
        require(abi.decode(data, (uint256)) == id, "data");
        calls++;
        seenBalance = token.balanceOf(address(this));
        seenSupply = token.totalIssued();
        if (mode == Mode.Keep) token.transfer(peer, amount / 2);
        else if (mode == Mode.Nest) token.flashIssue(id, amount, data);
        else if (mode == Mode.Relay) {
            token.transfer(peer, amount);
            FlashRelay(peer).back(token, amount);
        }
        return abi.encode(calls);
    }
}

/// @notice Buck.flashIssue: fresh BUCK for the length of one call, bounded by the borrower's credit
///         line, repaid exactly (owner, 2026-11-20: "very large arb transactions without permanently
///         increasing the standing BUCK supply").
contract BuckFlashTest is BuckCompoundFixture {
    FlashBorrower internal borrower;
    FlashRelay internal relay;
    uint256 internal id;

    function _service() internal {
        borrower = new FlashBorrower(buck);
        relay = new FlashRelay();
        _bind(address(borrower), true, address(this));
        _bind(address(relay), true, address(this));
        vm.prank(address(borrower)); credit.setCreditIssuer(INSURER_A, true);
        vm.prank(GOV); buck.setApprovedCarryingIssuer(address(borrower), true);
        id = _credit(address(borrower), INSURER_A, 1000e6, 0);
        // Activate the whole line and draw a little of it: room = K x 1000 - 100.
        vm.prank(address(borrower)); buck.issue(id, 1000e6, 100e6, 0, address(0));
    }

    function test_aFlashIsCreditedCalledBackAndRepaidLeavingNothingChanged() public {
        _service();
        (uint256 gross0, uint256 net0) = buck.accountBalancesScaled(address(borrower));
        uint256 supply0 = buck.totalIssued();
        (uint256 principal0, uint256 seconds0) = credit.issuanceOf(id);
        (, uint256 active0,) = credit.creditInfo(id);
        borrower.flash(id, 900e6);
        assertEq(borrower.calls(), 1);
        assertEq(borrower.seenBalance(), 1000e6, "the flash is spendable in the callback");
        assertEq(borrower.seenSupply(), supply0 + 900e6, "supply holds it for the call");
        assertEq(buck.totalIssued(), supply0, "and not after");
        (uint256 gross1, uint256 net1) = buck.accountBalancesScaled(address(borrower));
        assertEq(gross1, gross0);
        assertEq(net1, net0);
        (uint256 principal1, uint256 seconds1) = credit.issuanceOf(id);
        (, uint256 active1,) = credit.creditInfo(id);
        assertEq(principal1, principal0, "no principal");
        assertEq(seconds1, seconds0, "no claim age");
        assertEq(active1, active0, "no coverage");
    }

    function test_aFlashIsBoundedByTheLinesRoomToIssue() public {
        _service();
        vm.expectRevert(Buck.CreditExceeded.selector);
        borrower.flash(id, 900e6 + 1);
        k.setK(.5e18);                                  // room = 0.5 x 1000 - 100
        vm.expectRevert(Buck.CreditExceeded.selector);
        borrower.flash(id, 400e6 + 1);
        borrower.flash(id, 400e6);
        assertEq(borrower.calls(), 1);
    }

    function test_aShortRepaymentRevertsTheWholeTransaction() public {
        _service();
        borrower.setMode(FlashBorrower.Mode.Keep, ALICE);
        uint256 supply0 = buck.totalIssued();
        vm.expectRevert(Buck.InsufficientSpendableBalance.selector);
        borrower.flash(id, 800e6);
        assertEq(buck.totalIssued(), supply0);
        assertEq(buck.balanceOf(ALICE), 0, "the callback's transfer was undone");
    }

    function test_aFlashCannotNest() public {
        _service();
        borrower.setMode(FlashBorrower.Mode.Nest, address(0));
        vm.expectRevert(Buck.Reentrant.selector);
        borrower.flash(id, 10e6);
    }

    function test_theCallbackCanTradeAndTheCarriedFeeComesBackExactly() public {
        _service();
        vm.warp(t0 + YEAR);                             // the borrower's BUCK carry a year's fee
        (uint256 gross0, uint256 net0) = buck.accountBalancesScaled(address(borrower));
        assertGt(gross0, net0, "hollow before the flash");
        uint256 supply0 = buck.totalIssued();
        borrower.setMode(FlashBorrower.Mode.Relay, address(relay));
        borrower.flash(id, 700e6);
        (uint256 gross1, uint256 net1) = buck.accountBalancesScaled(address(borrower));
        assertEq(gross1, gross0, "nominal restored");
        assertEq(net1, net0, "net restored: the carried fee is unchanged");
        assertEq(buck.totalIssued(), supply0);
        assertEq(buck.balanceOf(address(relay)), 0);
    }

    function test_onlyAnApprovedCarryingIssuerOwningTheLineMayFlash() public {
        _service();
        vm.expectRevert(Buck.NotCreditOwner.selector);
        vm.prank(ALICE); buck.flashIssue(id, 1, "");
        vm.prank(GOV); buck.setApprovedCarryingIssuer(address(borrower), false);
        vm.expectRevert(Buck.CarryingIssuerNotApproved.selector);
        borrower.flash(id, 1);
        // An ordinary owner of a line may not flash either.
        uint256 own = _credit(ALICE, INSURER_A, 100e6, 0);
        vm.prank(ALICE); buck.issue(own, 100e6, 1e6, 0, address(0));
        vm.expectRevert(Buck.CarryingIssuerNotApproved.selector);
        vm.prank(ALICE); buck.flashIssue(own, 1, "");
        vm.prank(GOV); buck.setApprovedCarryingIssuer(address(borrower), true);
        vm.expectRevert(Buck.BadAmount.selector);
        borrower.flash(id, 0);
    }

    function test_aMaturedLineCannotFlash() public {
        _service();
        vm.warp(t0 + 50 * YEAR);
        buck.processMaturities(1);
        vm.expectRevert(Buck.FacilityMatured.selector);
        borrower.flash(id, 1);
    }
}
