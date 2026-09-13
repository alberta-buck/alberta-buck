// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {Buck} from "../src/Buck.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";
import {CreditSlice} from "../src/BuckTypes.sol";

interface IReenterHook {
    function onActivate() external;
}

/// @notice A BuckCredit stand-in that hands control to attacker code from
///         inside `activateFromBuck` -- i.e. at the one point in Buck's mint
///         where coverage has already been activated (credit headroom is
///         live) but the minter has not yet been debited the pool principal.
///
///         Production BuckCredit is immutable and reaches no arbitrary code,
///         so this window is not reachable today.  It is one upgrade away
///         from being reachable: BuckCredit.sol's own architectural note
///         contemplates putting it behind a UUPS proxy, and any ERC-721
///         receiver hook added to the activation path would open it too.
contract ReenteringCredit {
    address public buck;
    address public hook;
    address public holder;
    uint256 public face;
    uint256 public activated;
    uint32  public rate;
    bool    public armed;

    function configure(address _buck, address _hook, address _holder, uint256 _face, uint32 _rate)
        external
    {
        buck = _buck; hook = _hook; holder = _holder; face = _face; rate = _rate;
    }

    function arm() external { armed = true; }

    // --- surface Buck actually calls ---------------------------------------

    function totalCurrentValue(address who) external view returns (uint256) {
        return who == holder ? activated : 0;
    }

    function balanceOf(address who) external view returns (uint256) {
        return who == holder ? 1 : 0;
    }

    function ownerOf(uint256) external view returns (address) { return holder; }

    function tokenOfOwnerByIndex(address, uint256) external pure returns (uint256) { return 0; }

    function creditInfo(uint256)
        external view returns (uint256, uint256, uint32)
    { return (face, activated, rate); }

    function batchCreditInfo(uint256[] calldata ids)
        external view returns (CreditSlice[] memory slices)
    {
        slices = new CreditSlice[](ids.length);
        for (uint256 i = 0; i < ids.length; i++) {
            slices[i] = CreditSlice({
                owner: holder, faceValue: face, depreciatedFace: face,
                activatedValue: activated, premiumRate: rate
            });
        }
    }

    function activateFromBuck(uint256, address, uint256 amount) external {
        activated += amount;
        if (armed) {                       // <-- the reentrancy window
            armed = false;
            IReenterHook(hook).onActivate();
        }
    }

    function deactivateFromBuck(uint256, address, uint256 amount)
        external returns (uint256)
    { activated -= amount; return 0; }
}

/// @notice The minter.  Bound as a verified public identity so it can hold
///         BUCK and transfer; re-enters Buck from inside its own mint.
contract Attacker is IReenterHook {
    Buck    public buck;
    address public sink;
    uint256 public stealAmount;

    bool    public reentryAttempted;
    bool    public reentrySucceeded;
    string  public reentryError;
    uint256 public balanceSeenMidMint;

    function configure(Buck _buck, address _sink, uint256 _steal) external {
        buck = _buck; sink = _sink; stealAmount = _steal;
    }

    function doMint(uint256 amount, uint256[] calldata ids) external {
        buck.mint(amount, ids);
    }

    function onActivate() external {
        reentryAttempted   = true;
        // Views are never guarded -- record what an observer sees inside the
        // half-finished mint.
        balanceSeenMidMint = buck.balanceOf(address(this));
        try buck.transfer(sink, stealAmount) {
            reentrySucceeded = true;
        } catch Error(string memory reason) {
            reentryError = reason;
        }
    }
}

contract BuckReentrancyTest is Test {

    Buck                  internal buck;
    ReenteringCredit      internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry      internal reg;
    Attacker              internal attacker;

    address internal constant GOV  = address(0xA0);
    address internal constant POOL = address(0xBA51C);
    address internal constant SINK = address(0x51C0);

    function setUp() public {
        reg      = new IdentityRegistryHarness(GOV);
        credit   = new ReenteringCredit();
        kCtrl    = new BuckKControllerStatic(1e18, GOV);
        buck     = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        attacker = new Attacker();

        _bind(address(attacker), false);
        _bind(SINK, true);

        // One NFT, face 2000 BUCK, 100bp premium -> effRate 1000, denom 9000.
        credit.configure(address(buck), address(attacker), address(attacker), 2_000e6, 100);
        attacker.configure(buck, SINK, 1_000e6);
    }

    function _bind(address target, bool carrying) internal {
        if (target.code.length == 0) vm.etch(target, hex"60006000fd");
        reg.bindContract(
            target, BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}),
            true, carrying
        );
    }

    /// @notice The guard blocks a re-entrant transfer launched from inside
    ///         mint's activate-then-debit window.
    function test_guard_blocksReentrantTransferDuringMint() public {
        credit.arm();

        uint256[] memory ids = new uint256[](1);
        attacker.doMint(900e6, ids);

        assertTrue(attacker.reentryAttempted(), "hook never fired -- test is vacuous");
        assertFalse(attacker.reentrySucceeded(), "re-entrant transfer went through");
        assertEq(attacker.reentryError(), "BUCK: reentrant", "blocked, but not by the guard");
    }

    /// @notice What the guard is actually protecting: mid-mint, coverage is
    ///         activated but the pool principal has not been debited, so
    ///         balanceOf overstates the minter's real headroom.
    function test_guard_windowIsRealNotHypothetical() public {
        credit.arm();

        uint256[] memory ids = new uint256[](1);
        attacker.doMint(900e6, ids);

        uint256 midMint = attacker.balanceSeenMidMint();
        uint256 settled = buck.balanceOf(address(attacker));

        emit log_named_uint("balanceOf mid-mint (activated, not yet debited)", midMint);
        emit log_named_uint("balanceOf after mint settles                   ", settled);

        assertEq(midMint, 1_000e6, "mid-mint view sees the full activated coverage");
        assertEq(settled,   900e6, "settled balance is net of the pool principal");
        assertGt(midMint, settled,
            "no overstatement window -- guard would be belt-and-braces only");
    }

    /// @notice The guard has no exemptions, and needs none: BuckCredit does
    ///         not call back into Buck at all.  An ordinary mint, which
    ///         crosses into BuckCredit and returns, runs clean under it.
    function test_guard_hasNoExemptionsAndDoesNotDeadlock() public {
        uint256[] memory ids = new uint256[](1);
        attacker.doMint(900e6, ids);
        assertEq(buck.balanceOf(address(attacker)), 900e6, "ordinary mint must still work");

        // A second guarded call in the same transaction is fine -- the lock
        // is released on return, not held for the transaction.
        attacker.doMint(90e6, ids);
        assertEq(buck.balanceOf(address(attacker)), 990e6, "and again");
    }
}
