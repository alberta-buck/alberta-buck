// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {IdentityRegistry}       from "../src/IdentityRegistry.sol";
import {Buck}                   from "../src/Buck.sol";
import {BuckCredit}             from "../src/BuckCredit.sol";
import {BuckKControllerStatic}  from "../src/BuckKControllerStatic.sol";

/// @title BuckSignedBalance.t.sol -- Phase 1a signedBalanceOf / signedRawBalanceOf
///        invariant tests.
///
/// @notice For all non-negative raw balances, signedBalanceOf MUST equal
///         int256(balanceOf) and signedRawBalanceOf MUST equal int256(raw).
///         The clamping on balanceOf / feeOwing / rawBalanceOf for negative
///         balances is exercised in Phase 1c (after the transfer-out gate
///         allows balances to actually go negative); this file establishes
///         the no-regression baseline for the unsigned views.
contract BuckSignedBalanceTest is Test {

    Buck                  internal buck;
    BuckCredit            internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry      internal reg;

    address internal constant GOV   = address(0xA0);
    address internal constant POOL  = address(0xBA51C);
    address internal constant ALICE = address(0xA11CE);

    function setUp() public {
        reg     = new IdentityRegistry(GOV);
        credit  = new BuckCredit();
        kCtrl   = new BuckKControllerStatic(1e18, GOV);
        buck    = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));
        credit.setBuck(address(buck));
    }

    /// @notice Zero balance: all views agree on 0.
    function test_zeroBalance_views_agree() public view {
        assertEq(buck.balanceOf(ALICE),         0);
        assertEq(buck.rawBalanceOf(ALICE),      0);
        assertEq(buck.signedBalanceOf(ALICE),   int256(0));
        assertEq(buck.signedRawBalanceOf(ALICE),int256(0));
        assertEq(buck.feeOwing(ALICE),          0);
        assertEq(buck.balanceOfFees(ALICE),     0);
    }

    /// @notice Positive seeded balance: signed views equal unsigned cast.
    ///         Uses vm.store to seed the raw balance without going through
    ///         the (not-yet-rewired) mint path.
    function test_positiveBalance_signed_matches_unsigned() public {
        // Slot 0: mapping(address => AccountState) _state.  Compute the
        // storage slot for _state[ALICE] and write the AccountState
        // packed as int80 balance + uint120 buckSeconds + uint40 ts +
        // uint16 flags.  We set balance only; other fields stay 0.
        uint256 slot = uint256(keccak256(abi.encode(ALICE, uint256(0))));
        uint256 raw  = 1_234_567_890;          // = 1234.56789 BUCK at 6 dec
        vm.store(address(buck), bytes32(slot), bytes32(uint256(uint80(raw))));

        // No demurrage yet (timestamp = 0, but feeOwing only fires when
        // both buckSeconds and (raw*elapsed) integrate; the new account's
        // _state.timestamp default is 0 so elapsed = block.timestamp).
        // We don't assert exact fee here -- only the *equality* between
        // signed and unsigned views at non-negative raw.
        uint256 unsignedBal = buck.balanceOf(ALICE);
        int256  signedBal   = buck.signedBalanceOf(ALICE);
        assertEq(int256(unsignedBal), signedBal,
            "signedBalanceOf must equal int256(balanceOf) for non-negative raw");

        assertEq(buck.rawBalanceOf(ALICE), raw);
        assertEq(buck.signedRawBalanceOf(ALICE), int256(raw));
    }

    /// @notice rawBalanceOf clamps a (Phase-1c-only) negative raw to 0;
    ///         signedRawBalanceOf returns the signed value.  Verified by
    ///         direct slot poke since no public path can produce negative
    ///         raw at Phase 1a.
    function test_negativeSeed_rawBalanceOf_clamps_signed_reveals() public {
        uint256 slot = uint256(keccak256(abi.encode(ALICE, uint256(0))));
        // int80 = -1234_567_890 encoded into the low 80 bits via two's
        // complement; rest of the 256-bit slot is zero.
        int80   raw   = -int80(int256(1_234_567_890));
        uint256 word  = uint256(uint80(uint256(int256(raw))));   // sign-extended within low 80 bits
        vm.store(address(buck), bytes32(slot), bytes32(word));

        assertEq(buck.rawBalanceOf(ALICE), 0,
            "rawBalanceOf must clamp negative raw to 0 for ERC-20 consumers");
        assertEq(buck.signedRawBalanceOf(ALICE), int256(raw),
            "signedRawBalanceOf must reveal the negative raw");
        assertEq(buck.balanceOf(ALICE), 0,
            "balanceOf must clamp negative raw to 0");
        assertEq(buck.signedBalanceOf(ALICE), int256(raw),
            "signedBalanceOf must equal the negative raw (no demurrage on debt)");
        assertEq(buck.feeOwing(ALICE), 0,
            "feeOwing must clamp to 0 for negative raw");
    }
}
