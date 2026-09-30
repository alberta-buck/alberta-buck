// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {BN254}                   from "../src/BN254.sol";
import {IdentityRegistry}        from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {Buck}                    from "../src/Buck.sol";
import {BuckCredit}              from "../src/BuckCredit.sol";
import {BuckKControllerStatic}   from "../src/BuckKControllerStatic.sol";
import {bindCarryingPool}        from "./harness/CarryingPool.sol";

/// @title BuckMintSpendable.t.sol -- `mint(amount)` delivers `amount` of
///        spendable at the K it runs at; `burn(amount)` releases as much.
///
/// @notice A unit of present-value coverage raises the credit limit by K and
///         costs a deposit of e = premiumRate x POOL_ROI_INV / BP, so it
///         yields K - e of spendable: mint(amount) activates amount / (K - e)
///         and pays the rest as the deposit.  At K = 0.75, a zero-premium
///         mint(1000) activates 1333.33; a 100 bp credit (e = 10%) activates
///         1538.46 and deposits 153.85.  The test contract is the holder and
///         its credits' insurer.
contract BuckMintSpendableTest is Test {

    Buck                  internal buck;
    BuckCredit            internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry      internal reg;

    address internal constant GOV  = address(0xA0);
    address internal constant POOL = address(0xBA51C);

    uint256 internal constant FACE = 1_000_000e6;
    uint256 internal constant K    = 0.75e18;

    function setUp() public {
        reg    = new IdentityRegistryHarness(GOV);
        credit = new BuckCredit();
        kCtrl  = new BuckKControllerStatic(K, GOV);
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        bindCarryingPool(reg, POOL);
        reg.bindContract(address(this), BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), true, false);
        vm.prank(GOV);
        reg.setBuck(address(buck));
        credit.setBuck(address(buck));
        credit.setCreditIssuer(address(this), true);
    }

    function _credit(uint32 premiumBp) internal returns (uint256[] memory ids) {
        ids = new uint256[](1);
        ids[0] = credit.createCredit(address(this), 0, FACE, 0,
                                     BuckCredit.DepreciationType.NONE, 0, 0, premiumBp);
    }

    function test_mint_zeroPremium_deliversTheAmount() public {
        uint256[] memory ids = _credit(0);
        buck.mint(1_000e6, ids);
        assertApproxEqAbs(buck.balanceOf(address(this)), 1_000e6, 1, "spendable += amount");
        (, uint256 active,) = credit.creditInfo(ids[0]);
        assertApproxEqAbs(active, 1_000e6 * 1e18 / K, 1, "coverage = amount / K");
        assertEq(buck.mintsPrincipal(ids[0]), 0, "no deposit");
    }

    function test_mint_withPremium_deliversTheAmount() public {
        uint256[] memory ids = _credit(100);                 // e = 10%
        buck.mint(1_000e6, ids);
        assertApproxEqAbs(buck.balanceOf(address(this)), 1_000e6, 1, "spendable += amount");
        (, uint256 active,) = credit.creditInfo(ids[0]);
        uint256 v = 1_000e6 * 1e18 / (K - 0.1e18);         // amount / (K - e)
        assertApproxEqAbs(active, v, 1, "coverage = amount / (K - e)");
        assertApproxEqAbs(buck.mintsPrincipal(ids[0]), v / 10, 1, "deposit = e x coverage");
        assertApproxEqAbs(uint256(-buck.signedRawBalanceOf(address(this))),
                          buck.mintsPrincipal(ids[0]), 0, "the deposit, drawn on credit");
    }

    function test_mintThenBurn_roundTrips() public {
        uint256[] memory ids = _credit(100);
        buck.mint(1_000e6, ids);
        buck.burn(buck.balanceOf(address(this)), ids);
        (, uint256 active,) = credit.creditInfo(ids[0]);
        assertLe(active, 2, "coverage released");
        assertLe(buck.mintsPrincipal(ids[0]), 1, "deposit refunded");
        assertApproxEqAbs(buck.signedRawBalanceOf(address(this)), 0, 1, "square");
    }

    function test_mintMax_activatesTheWholeFace() public {
        uint256[] memory ids = _credit(100);
        buck.mint(type(uint256).max, ids);
        (, uint256 active,) = credit.creditInfo(ids[0]);
        assertEq(active, FACE, "the whole face");
        assertApproxEqAbs(buck.balanceOf(address(this)), FACE * (K - 0.1e18) / 1e18, 1,
                          "spendable = (K - e) x face");
    }

    function test_mint_moreThanTheCreditsGive_reverts() public {
        uint256[] memory ids = _credit(0);
        vm.expectRevert(bytes("BUCK: insufficient credit allocation"));
        buck.mint(FACE, ids);                                // at most K x FACE
    }

    function test_quoteMint_matchesTheMint() public {
        uint256[] memory ids = _credit(100);
        (uint256 cov, uint256 dep) = buck.quoteMint(1_000e6, ids);
        buck.mint(1_000e6, ids);
        (, uint256 active,) = credit.creditInfo(ids[0]);
        assertEq(cov, active, "coverage quoted");
        assertEq(dep, buck.mintsPrincipal(ids[0]), "deposit quoted");
    }

    function test_mint_skipsACreditWhoseDepositExceedsK() public {
        uint256[] memory ids = _credit(800);                 // e = 80% > K
        vm.expectRevert(bytes("BUCK: insufficient credit allocation"));
        buck.mint(1e6, ids);
    }
}
