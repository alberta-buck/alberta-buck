// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketEquityTest, IBasketUpkeep, IPool} from "./BuckBasketEquity.t.sol";
import {BuckBasketEquityStorage} from "../../src/basket/BuckBasketEquityStorage.sol";
import {BasketUpkeepFacet} from "../../src/basket/BasketUpkeepFacet.sol";
import {IBuckBasketVenue} from "../../src/basket/IBuckBasketVenue.sol";
import {BasketVenueFacet} from "../../src/basket/BasketVenueFacet.sol";
import {BasketUpkeepMaintenanceControl} from "../harness/BasketUpkeepMaintenanceControl.sol";

contract ControlledPairIntent {
    address public basket;
    uint256 public constant count = 3;
    uint256 public rate = 1e18;
    uint256 public impacts;
    uint64 public lastExecutionAt;
    uint256 public onlyRecipient = type(uint256).max;
    constructor(address basket_) { basket = basket_; }
    function signals() external view returns (address) { return address(this); }
    function isDue() external pure returns (bool) { return true; }
    function observe() external {}
    function setRate(uint256 r) external { rate = r; }
    function setRecipient(uint256 j) external { onlyRecipient = j; }
    function desire(uint256, uint256 j) external view returns (uint256) {
        return onlyRecipient == type(uint256).max || onlyRecipient == j ? rate : 0;
    }
    // The share of a gap to move: any rule will do for the facet's tests.
    function share(uint256, uint256 r) external view returns (uint256) {
        uint256 elapsed = block.timestamp - lastExecutionAt;
        if (elapsed > 1 days) elapsed = 1 days;
        uint256 u = r * elapsed / 1 days;
        return u * 1e18 / (1e18 + u);
    }
    uint256[] public asked;
    function recordRates(uint256[] calldata rates) external { require(msg.sender == basket); asked = rates; }
    function recordImpact(uint256, int24, int24) external { require(msg.sender == basket); ++impacts; }
    function recordExecution(uint256 donor) external {
        require(msg.sender == basket); lastExecutionAt = uint64(block.timestamp); acted = donor;
    }
    // Urgency, as the facet sees it: a mask the test sets, and the gaps it was last told.
    uint256 public patience;
    uint256 public urgentMask;
    uint256 public acted = type(uint256).max; // the donor of the last transfer
    int256[] public reported;
    function setUrgent(uint256 patience_, uint256 mask) external { patience = patience_; urgentMask = mask; }
    function urgent(int256[] calldata) external view returns (uint256) { return urgentMask; }
    function recordGaps(int256[] calldata gaps) external { require(msg.sender == basket); reported = gaps; }
}

/// A parity reference the test sets: the basket's cost in BUCK, and whether it is fresh.
contract ParityStub {
    int256 public rawValue = 1e18;
    bool public fresh = true;
    function set(int256 value, bool fresh_) external { rawValue = value; fresh = fresh_; }
}

contract BasketUpkeepFacetTest is BuckBasketEquityTest {
    ControlledPairIntent intent;
    ParityStub parity;
    uint256[3] ids;

    // One constituent 6.7 points over its third and two 3.3 points under, all inside their leashes.
    function _install() private { _installValues(40_000e18, 30_000e18, 30_000e18, 0); }

    function _installValues(uint256 a, uint256 c1, uint256 c2, uint256 parityGain) private {
        _installGains(a, c1, c2, parityGain, 0);
    }

    function _installGains(uint256 a, uint256 c1, uint256 c2, uint256 parityGain, uint256 parityGainDear) private {
        ids[0] = _deposit(alice, address(tok[0]), a);
        ids[1] = _deposit(alice, address(tok[1]), c1 / 2);
        ids[2] = _deposit(alice, address(tok[2]), c2 / 3);
        intent = new ControlledPairIntent(address(b));
        parity = new ParityStub();
        vm.startPrank(GOV);
        b.setVenue(address(new BasketVenueFacet()));
        b.setEquityWheel(address(new BasketUpkeepFacet(address(intent), address(parity), parityGain, parityGainDear)));
        b.setResponse(BuckBasketEquityStorage.ResponseParams(3000, 1800, 0, 0));
        b.setNetMark(true);
        vm.stopPrank();
    }

    function _response() private returns (uint256) {
        vm.warp(vm.getBlockTimestamp() + 1800); vm.roll(vm.getBlockNumber() + 1);
        return IBasketUpkeep(address(b)).wheelStep(2, 0);
    }

    // The first response of a policy that asks for nothing: every deposit paired, nothing traded.
    function _placed3() private returns (uint128[3] memory liquidity) {
        intent.setRate(0);
        assertEq(_response(), 1);
        intent.setRate(1e18);
        liquidity = [b.liquidityOf(0), b.liquidityOf(1), b.liquidityOf(2)];
    }

    function _spots() private view returns (uint160[3] memory spot) {
        for (uint256 i; i < 3; ++i) (spot[i],,,,,,) = IPool(pools[i]).slot0();
    }

    function test_upkeep_needsTheNetMark() public {
        _install();
        vm.prank(GOV); b.setNetMark(false);
        vm.expectRevert(bytes("net mark"));
        IBasketUpkeep(address(b)).wheelStep(2, 0);
    }

    function test_upkeep_onlyGovernanceSetsTheMark() public {
        vm.expectRevert();
        b.setNetMark(true);
        assertFalse(b.netMark());
    }

    function test_upkeep_everyDepositIsPairedAtOnceAndTheLineIsWhole() public {
        _installValues(30_000e18, 30_000e18, 30_000e18, 0);
        uint160[3] memory spot = _spots();
        _placed3();
        for (uint256 i; i < 3; ++i) {
            assertGt(b.liquidityOf(i), 0, "placed");
            assertLt(b.idleToken(i) * price[i] / 1e18, b.equity() / 1000, "nothing left waiting");
        }
        uint160[3] memory afterSpot = _spots();
        for (uint256 i; i < 3; ++i) assertEq(afterSpot[i], spot[i], "pairing swaps nothing");
        // A BUCK was drawn for every BUCK of TOKEN, far beyond K x equity ...
        assertGt(_lien(), b.equity() * 9 / 10, "the whole book is paired");
        // ... and all of K x equity is still there to spend.
        assertApproxEqRel(b.liquidity(), ctrl.k() * b.equity() / 1e18, 0.03e18, "the line is whole");
        assertEq(intent.impacts(), 0); assertEq(intent.lastExecutionAt(), 0, "placing spends no allowance");
        assertEq(_response(), 0, "a paired book at its targets has nothing to do");
        _books();
    }

    function test_upkeep_eachConstituentIsWorkedOnItsOwn() public {
        _install();
        uint128[3] memory placed = _placed3();
        uint256 lien = _lien();
        assertEq(_response(), 1);
        assertLt(b.liquidityOf(0), placed[0], "the one over its target is sold");
        assertGt(b.liquidityOf(1), placed[1], "those under are bought");
        assertGt(b.liquidityOf(2), placed[2]);
        assertEq(intent.impacts(), 3, "each pool's own impact is recorded");
        assertGt(intent.asked(0), 0, "and the rate each was worked at");
        assertEq(intent.lastExecutionAt(), vm.getBlockTimestamp());
        // Gaps are reported as signed shares of the book.
        assertApproxEqAbs(intent.reported(0), 0.0667e18, 0.001e18);
        assertApproxEqAbs(intent.reported(1), -0.0333e18, 0.001e18);
        assertTrue(_lien() != lien, "the two legs need not match: the line carries the difference");
        _books();
    }

    function test_upkeep_aConstituentWithNoEvidenceIsLeft() public {
        _install();
        uint128[3] memory placed = _placed3();
        // The policy sees every pair closing toward TOKEN 1 only.
        intent.setRecipient(1);
        uint160[3] memory spot = _spots();
        assertEq(_response(), 1);
        assertLt(b.liquidityOf(0), placed[0], "sold: closing against a part of the rest");
        assertGt(b.liquidityOf(1), placed[1], "bought: the rest is closing toward it");
        assertEq(b.liquidityOf(2), placed[2], "under its target, and left: nothing closes toward it");
        (uint160 third,,,,,,) = IPool(pools[2]).slot0();
        assertEq(third, spot[2]);
        _books();
    }

    function test_upkeep_aPolicyThatAsksNothingTradesNothing() public {
        _install();
        _placed3();
        intent.setRate(0);
        uint160[3] memory spot = _spots();
        _response();
        uint160[3] memory afterSpot = _spots();
        for (uint256 i; i < 3; ++i) assertEq(afterSpot[i], spot[i]);
        assertEq(intent.impacts(), 0); assertEq(intent.lastExecutionAt(), 0);
    }

    function test_upkeep_bandThenUrgencyDecideWhichGapsMayBeWorked() public {
        // 1.2 points over and 0.8 and 0.3 under: all inside the 2-point band.
        _installValues(34_500e18, 32_500e18, 33_000e18, 0);
        uint128[3] memory placed = _placed3();
        _response();
        assertEq(intent.impacts(), 0, "inside the band nothing is worked");
        // A patient policy replaces the band: only what it calls urgent is worked.
        intent.setUrgent(0.02e18, 1);
        assertEq(_response(), 1);
        assertLt(b.liquidityOf(0), placed[0], "urgent, and inside the band");
        assertEq(b.liquidityOf(1), placed[1]); assertEq(b.liquidityOf(2), placed[2]);
        assertApproxEqAbs(intent.reported(0), 0.0117e18, 0.001e18);
        assertEq(intent.acted(), 0, "its allowance is spent");
        _books();
    }

    function test_upkeep_aPieceBelowTheGrainIsLeft() public {
        _install();
        _placed3();
        // Half a percent of a 6,667 gap is 33 BUCKs, under twice the grain.
        intent.setRate(0.005e18);
        _response();
        assertEq(intent.impacts(), 0); assertEq(intent.lastExecutionAt(), 0);
        intent.setRate(0.1e18);
        assertEq(_response(), 1);
        assertGt(intent.impacts(), 0, "a larger release clears it");
        _books();
    }

    function test_upkeep_aConstituentBeyondItsLeashIsBroughtBackWithNoSignal() public {
        // Half the book in the first TOKEN, beyond its leash; the others short of their weight but inside theirs.
        _installValues(50_000e18, 26_000e18, 24_000e18, 0);
        intent.setRate(0);
        uint160[3] memory spot = _spots();
        vm.expectEmit(true, false, false, false, address(b));
        emit BasketUpkeepFacet.Responded(4, 0, 0, 0, 0);
        vm.expectEmit(true, false, false, false, address(b));
        emit BasketUpkeepFacet.Responded(1, 0, 0, 0, 0);
        assertEq(_response(), 1);
        uint160[3] memory afterSpot = _spots();
        assertTrue(afterSpot[0] != spot[0], "sold toward its weight, policy or not");
        assertEq(afterSpot[1], spot[1], "inside their leashes, and nothing asked");
        assertEq(afterSpot[2], spot[2]);
        _books();
    }

    function test_upkeep_aLargeExitIsPaidInBucksAndSoldDownToTheCeiling() public {
        _installValues(30_000e18, 30_000e18, 30_000e18, 0);
        uint128[3] memory placed = _placed3();
        intent.setRate(0);
        uint256 lien = _lien();
        uint256 held = buck.balanceOf(alice);
        // A third of the book leaves in one go.
        vm.prank(alice);
        uint256 paid = b.redeem(ids[0], 10000);
        assertEq(buck.balanceOf(alice) - held, paid, "paid in BUCK");
        for (uint256 i; i < 3; ++i) assertEq(b.liquidityOf(i), placed[i], "no position was sold to pay it");
        assertEq(_lien(), lien + paid, "it was issued");
        // That is more than half of K x what stays: every constituent gives its share, and no policy is asked.
        assertEq(_response(), 1);
        for (uint256 i; i < 3; ++i) assertLt(b.liquidityOf(i), placed[i], "each gives its share");
        assertLt(_lien(), lien + paid);
        _books();
    }

    function test_upkeep_maintenanceControlKeepsTheBookAndTakesNoView() public {
        _installValues(40_000e18, 30_000e18, 30_000e18, 0);
        address control = address(new BasketUpkeepMaintenanceControl(address(intent), address(parity), 0, 0));
        vm.prank(GOV);
        b.setEquityWheel(control);
        uint160[3] memory spot = _spots();
        assertEq(_response(), 1); // The policy asks for everything, and is not heard.
        uint160[3] memory afterSpot = _spots();
        for (uint256 i; i < 3; ++i) { assertEq(afterSpot[i], spot[i]); assertGt(b.liquidityOf(i), 0, "placed all the same"); }
        assertEq(intent.impacts(), 0); assertEq(intent.lastExecutionAt(), 0);
        // The ceiling is kept: a large exit is still sold down.
        uint128[3] memory placed = [b.liquidityOf(0), b.liquidityOf(1), b.liquidityOf(2)];
        vm.prank(alice); b.redeem(ids[0], 10000);
        assertEq(_response(), 1);
        for (uint256 i; i < 3; ++i) assertLt(b.liquidityOf(i), placed[i]);
        _books();
    }

    function test_upkeep_theBookHoldsBucksWhenCheapAndLetsNoneOutWhenDear() public {
        _installValues(30_000e18, 30_000e18, 30_000e18, 5e18);      // as deployed: no gain on the dear side
        uint128[3] memory placed = _placed3();
        // The basket costs 0.98 BUCKs: the BUCK is dear. The book lets none out for that.
        parity.set(0.98e18, true);
        _response();
        for (uint256 i; i < 3; ++i) assertLe(b.liquidityOf(i), placed[i], "no TOKEN bought with issued BUCKs");
        // The basket costs 1.02 BUCKs: the BUCK is cheap, so hold some. TOKEN is sold for it.
        for (uint256 i; i < 3; ++i) _repin(i);
        placed = [b.liquidityOf(0), b.liquidityOf(1), b.liquidityOf(2)];
        parity.set(1.02e18, true);
        assertEq(_response(), 1);
        for (uint256 i; i < 3; ++i) assertLt(b.liquidityOf(i), placed[i], "TOKEN sold for BUCKs");
        _books();
    }

    /// @dev Responses, the outside market re-pinning every pool after each.
    function _responses(uint256 n) private {
        for (uint256 r; r < n; ++r) {
            _response();
            for (uint256 i; i < 3; ++i) _repin(i);
        }
    }

    function test_upkeep_aBuckDepositIsBoughtIntoTheBookWithNoWordFromThePolicy() public {
        _installValues(30_000e18, 30_000e18, 30_000e18, 0);
        uint128[3] memory placed = _placed3();
        intent.setRate(0);                       // as the pair policy says while no pair has separated
        uint256 lien = _lien();
        // A deposit inside the band is left where it is: it repays the lien.
        _deposit(bob, address(buck), 1_000 * B);
        assertApproxEqAbs(_lien(), lien - 1_000 * B, B, "a BUCK deposit repays the lien");
        _responses(2);
        for (uint256 i; i < 3; ++i) assertEq(b.liquidityOf(i), placed[i], "inside the band");
        // One beyond it is bought into every constituent, and lent again.
        _deposit(bob, address(buck), 15_000 * B);
        _responses(8);
        for (uint256 i; i < 3; ++i) assertGt(b.liquidityOf(i), placed[i], "the book grows by the deposit");
        assertGt(_lien(), lien, "the deposit is lent again");
        assertGt(intent.lastExecutionAt(), 0, "and the policy hears of each trade");
        _books();
    }

    function test_upkeep_aDepositFillsTheConstituentsUnderTheirWeights() public {
        _install();                              // 40,000 / 30,000 / 30,000 BUCKs of TOKEN
        uint128[3] memory placed = _placed3();
        intent.setRate(0);
        uint160[3] memory spot = _spots();
        // 20,000 BUCKs more: a third of the book is 40,000, so the first is at its target and the
        // others 10,000 short each. The deposit goes to them, equally, and none to the first.
        _deposit(bob, address(buck), 20_000 * B);
        _responses(8);
        assertEq(b.liquidityOf(0), placed[0], "at its target: not bought");
        (uint160 first,,,,,,) = IPool(pools[0]).slot0();
        assertEq(first, spot[0]);
        uint256 grew1 = (b.liquidityOf(1) - placed[1]) * 1e18 / placed[1];
        uint256 grew2 = (b.liquidityOf(2) - placed[2]) * 1e18 / placed[2];
        assertGt(grew1, 0.2e18, "under its weight: filled");
        assertApproxEqRel(grew1, grew2, 0.05e18, "each in proportion to its gap");
        _books();
    }

    function test_upkeep_theLeanMovesTheBookWithNoWordFromThePolicy() public {
        _installGains(30_000e18, 30_000e18, 30_000e18, 5e18, 5e18);
        uint128[3] memory placed = _placed3();
        intent.setRate(0);
        parity.set(1.02e18, true);              // the BUCK is cheap: hold some
        _responses(1);
        for (uint256 i; i < 3; ++i) assertLt(b.liquidityOf(i), placed[i], "TOKEN sold for BUCKs");
        placed = [b.liquidityOf(0), b.liquidityOf(1), b.liquidityOf(2)];
        parity.set(0.98e18, true);              // dear: let some out
        _responses(1);
        for (uint256 i; i < 3; ++i) assertGt(b.liquidityOf(i), placed[i], "TOKEN bought with issued BUCKs");
        _books();
    }

    function test_upkeep_aDearGainLeansBothWays() public {
        _installGains(30_000e18, 30_000e18, 30_000e18, 5e18, 5e18);
        uint128[3] memory placed = _placed3();
        // The basket costs 1.02 BUCKs: the BUCK is cheap, so hold some. TOKEN is sold for it.
        parity.set(1.02e18, true);
        assertEq(_response(), 1);
        for (uint256 i; i < 3; ++i) assertLt(b.liquidityOf(i), placed[i], "TOKEN sold for BUCKs");
        // It costs 0.98: the BUCK is dear, so issue some. TOKEN is bought with it.
        parity.set(0.98e18, true);
        for (uint256 i; i < 3; ++i) _repin(i);
        placed = [b.liquidityOf(0), b.liquidityOf(1), b.liquidityOf(2)];
        assertEq(_response(), 1);
        for (uint256 i; i < 3; ++i) assertGt(b.liquidityOf(i), placed[i], "TOKEN bought with issued BUCKs");
        // A reference that is not fresh leans nothing: the book stays the size it is.
        parity.set(0.5e18, false);
        for (uint256 i; i < 3; ++i) _repin(i);
        uint160[3] memory spot = _spots();
        _response();
        uint160[3] memory afterSpot = _spots();
        for (uint256 i; i < 3; ++i) assertEq(afterSpot[i], spot[i]);
        _books();
    }
}
