// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20}    from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/// @title WorkWheel -- the chassis of a basket's work wheel.
///
/// @notice (alberta-buck-ethereum-wheel.org, "The work wheel".)  RebalanceDirectorBase carries one
///         wheel (an epoch clock, a round-robin cursor, poke(maxWork), policy
///         through two hooks); this generalizes it from "constituents x
///         epochs" to "tasks x clocks".  Everything policy-agnostic lives
///         here: the cursor, the per-block scan and its idle memo, the
///         re-arm, and the caller's pay from the reserve.  An ACTIVITY is a
///         KIND -- an abstract mixin deriving from this chassis that claims a
///         range of slots after its bases' and hands the rest to `super`
///         (see ArbKind etc.); the C3 order of a concrete wheel fixes the
///         slot layout.
///
///         The scan is amortized, not just the work: deciding a slot is due
///         can cost several cold reads (an arbitrage slot reads three pools),
///         so a call examines at most `maxScan` slots, the cursor moves past
///         every slot examined, and a block is memoized idle only when
///         consecutive calls have found every slot not due.  After that,
///         every further tick in the block is one storage read.  Anyone may
///         re-arm (a basket-touching trade may have opened new work); it
///         costs only the next caller a scan.
///
///         The reserve is the design owner's gas offset (2026-09-26): a slice
///         of the basket's yield is funded in, and each tick that does work
///         is paid `kappaBp` of the balance -- so it builds up when ticks are
///         under-called and pays out less when they are over-called.  A kind
///         that captures value also pays its caller a share of it directly.
///         An idle tick is paid nothing.
abstract contract WorkWheel {
    using SafeERC20 for IERC20;

    IERC20  public immutable payToken;      // the reserve's asset (BUCK)
    address public governance;

    uint256 public cursor;
    uint256 public idleClock;               // 1 + the clock of a block found idle; 0: none
    uint256 public scannedIdle;             // consecutive slots found not due, this block
    uint256 internal _scanClock;            // 1 + the clock scannedIdle belongs to

    uint256 public reserve;                 // payToken held for callers
    uint256 public reserveCap;              // funding beyond it is refused (stays with the funder)
    uint16  public kappaBp;                 // of the reserve, per working tick

    event Ticked(address indexed caller, uint256 work, uint256 reservePay);
    event Funded(address indexed from, uint256 taken, uint256 refused);
    event ReserveParams(uint16 kappaBp, uint256 reserveCap);

    error NotGovernance();

    modifier onlyGov() {
        if (msg.sender != governance) revert NotGovernance();
        _;
    }

    constructor(address payToken_, address governance_, uint16 kappaBp_, uint256 reserveCap_) {
        payToken   = IERC20(payToken_);
        governance = governance_;
        kappaBp    = kappaBp_;
        reserveCap = reserveCap_;
    }

    function setGovernance(address g) external onlyGov { governance = g; }

    function setReserveParams(uint16 k, uint256 cap) external onlyGov {
        kappaBp = k;
        reserveCap = cap;
        emit ReserveParams(k, cap);
    }

    /// @notice Fund the reserve (the basket's yield slice).  Takes at most up
    ///         to the cap; the rest is never pulled.
    function fund(uint256 amount) external {
        uint256 room = reserveCap > reserve ? reserveCap - reserve : 0;
        uint256 take = amount < room ? amount : room;
        if (take > 0) {
            payToken.safeTransferFrom(msg.sender, address(this), take);
            reserve += take;
        }
        emit Funded(msg.sender, take, amount - take);
    }

    /// @notice A basket-touching trade landed: new work may exist this block.
    function rearm() external { _markDirty(); }

    function slotCount() external view returns (uint256) { return _slotCount(); }

    /// @notice How many slots are due now (what a caller simulates first).
    function pending() external view returns (uint256 n) {
        uint256 s = _slotCount();
        for (uint256 i = 0; i < s; i++) {
            if (_due(i)) n++;
        }
    }

    /// @notice Advance up to `maxWork` due slots, examining at most `maxScan`
    ///         (0 = all) from the cursor.  Pays `kappaBp` of the reserve when
    ///         any work was done.
    function tick(uint256 maxWork, uint256 maxScan)
        external returns (uint256 work, uint256 reservePay)
    {
        uint256 clk = _clock() + 1;
        uint256 n = _slotCount();
        if (n == 0 || idleClock == clk) return (0, 0);
        if (_scanClock != clk) {
            _scanClock = clk;
            scannedIdle = 0;
        }
        uint256 limit = (maxScan == 0 || maxScan > n) ? n : maxScan;
        uint256 start = cursor;
        uint256 idle = scannedIdle;
        uint256 steps = 0;
        for (; steps < limit && work < maxWork; steps++) {
            uint256 slot = (start + steps) % n;
            if (!_due(slot)) {
                idle++;
                continue;
            }
            uint256 w = _run(slot);
            if (w == 0) {                  // due on the view, nothing on the run
                idle++;
                continue;
            }
            idle = 0;
            work += w;
        }
        cursor = (start + steps) % n;
        scannedIdle = idle;
        if (work > 0) {
            reservePay = reserve * kappaBp / 10000;
            if (reservePay > 0) {
                reserve -= reservePay;
                payToken.safeTransfer(msg.sender, reservePay);
            }
        } else if (idle >= n) {
            idleClock = clk;
        }
        emit Ticked(msg.sender, work, reservePay);
    }

    // --- The clock: a per-chain adapter ---------------------------------------- //

    /// @dev block.number on L1 and the OP stack.  On Arbitrum block.number is
    ///      the L1's; a deployment there overrides with ArbSys(100).arbBlockNumber().
    function _clock() internal view virtual returns (uint256) { return block.number; }

    function _markDirty() internal {
        idleClock = 0;
        scannedIdle = 0;
    }

    // --- The kind seam: the slot table is the concatenation of the kinds' ------ //

    function _slotCount() internal view virtual returns (uint256) { return 0; }
    function _due(uint256) internal view virtual returns (bool) { return false; }
    /// @dev Run one slot; return the work units done (0: nothing after all).
    function _run(uint256) internal virtual returns (uint256) { return 0; }
}
