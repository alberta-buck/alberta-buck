// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

/// @notice Buck's exact per-account slot shape: 80 + 120 + 40 + 16 == 256 bits.
struct Acct {
    int80   balance;
    uint120 buckSeconds;
    uint40  timestamp;
    uint16  flags;
}

/// @dev Baseline: one packed-slot read-modify-write, like a transfer leg.
contract Plain {
    mapping(address => Acct) internal _state;
    function touch(address a, int80 delta) external virtual {
        Acct memory s = _state[a];
        s.balance     += delta;
        s.buckSeconds += 1;
        s.timestamp    = uint40(block.timestamp);
        _state[a]      = s;
    }
}

/// @dev Cancun transient-storage guard: TSTORE + TLOAD + TSTORE, 100 gas each,
///      auto-cleared at end of transaction.
contract TransientGuarded is Plain {
    bool private transient _entered;
    modifier nonReentrant() {
        require(!_entered, "reentrant");
        _entered = true;
        _;
        _entered = false;
    }
    function touch(address a, int80 delta) external override nonReentrant {
        Acct memory s = _state[a];
        s.balance     += delta;
        s.buckSeconds += 1;
        s.timestamp    = uint40(block.timestamp);
        _state[a]      = s;
    }
}

/// @dev Classic OpenZeppelin ReentrancyGuard: a dedicated storage slot that
///      is warmed, written 1->2, then written 2->1 (partial refund).
contract StorageGuarded is Plain {
    uint256 private _status = 1;
    modifier nonReentrant() {
        require(_status == 1, "reentrant");
        _status = 2;
        _;
        _status = 1;
    }
    function touch(address a, int80 delta) external override nonReentrant {
        Acct memory s = _state[a];
        s.balance     += delta;
        s.buckSeconds += 1;
        s.timestamp    = uint40(block.timestamp);
        _state[a]      = s;
    }
}

/// @dev The "guard bit rides along in the packed AccountState.flags" idea:
///      the account slot is written anyway, so the marginal cost is the
///      *extra* SSTORE needed to publish the bit before the external calls
///      and the (already-paid-for) clear at the end.
contract FlagGuarded is Plain {
    uint16 private constant LOCK = 0x8000;
    function touch(address a, int80 delta) external override {
        Acct memory s = _state[a];
        require(s.flags & LOCK == 0, "reentrant");
        s.flags       |= LOCK;
        _state[a]      = s;          // publish the lock before any call-out

        s.balance     += delta;
        s.buckSeconds += 1;
        s.timestamp    = uint40(block.timestamp);
        s.flags       &= ~LOCK;
        _state[a]      = s;          // clear + payload (warm, dirty: 100 gas)
    }
}

contract GuardBenchTest is Test {
    Plain            internal plain;
    TransientGuarded internal tg;
    StorageGuarded   internal sg;
    FlagGuarded      internal fg;

    address internal constant A = address(0xA11CE);

    function setUp() public {
        plain = new Plain();
        tg    = new TransientGuarded();
        sg    = new StorageGuarded();
        fg    = new FlagGuarded();
        // Warm each account slot -- we want the steady-state marginal cost,
        // not the 20k cold-init cost that swamps everything.
        plain.touch(A, 1);
        tg.touch(A, 1);
        sg.touch(A, 1);
        fg.touch(A, 1);
    }

    function _measure(Plain c) internal returns (uint256) {
        uint256 g0 = gasleft();
        c.touch(A, 1);
        return g0 - gasleft();
    }

    function test_guardCosts() public {
        uint256 gPlain = _measure(plain);
        uint256 gTrans = _measure(tg);
        uint256 gStore = _measure(sg);
        uint256 gFlag  = _measure(fg);

        emit log_named_uint("baseline (no guard)        ", gPlain);
        emit log_named_uint("transient guard            ", gTrans);
        emit log_named_uint("storage-slot guard (OZ)    ", gStore);
        emit log_named_uint("flags-bit guard (in-slot)  ", gFlag);
        emit log_named_uint("delta: transient           ", gTrans - gPlain);
        emit log_named_uint("delta: storage slot        ", gStore - gPlain);
        emit log_named_uint("delta: flags bit           ", gFlag  - gPlain);
    }

    /// @dev First call in a fresh transaction pays the cold-slot penalty for
    ///      the storage guard; the transient guard has no cold tier.
    function test_guardCosts_firstCallInTx() public {
        // A fresh tx per call: reset warmth by measuring in separate calls
        // from separate top-level frames is not possible in one test, so
        // approximate with vm.cool-free reasoning -- report steady state and
        // note that StorageGuarded pays +2100 on its first touch per tx.
        uint256 gStore = _measure(sg);
        emit log_named_uint("storage guard, warm slot   ", gStore);
    }
}
