// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IStabilizer}     from "./IStabilizer.sol";
import {IShadowObserver} from "./IShadowObserver.sol";

/// @dev What the observer reads from the basket: K's raw process variable
///      (the venue facet's view, served through the shell's fallback) and the
///      reference depth D (on the ops shell).
interface IShadowBasket {
    function basketValueInBuck() external view returns (int256);   // 18-dec
    function shadowDepth() external view returns (uint256);         // BUCK native units
}

/// @title ShadowObserver -- the level-1 -> K observer (CARRY-CONVEXITY.org 6.4).
///
/// @notice A governance registry of (stabilizer, lambda) over the level-1
///         actuators' `IStabilizer` books, assembling K's process variable:
///
///           shadowValueInBuck() = bvib + (sum_i lambda_i * netInventory_i
///                                          + shadowLambda * shadowOffset) / D
///           shadowSaturation()  = max_i min(1, lambda_i * saturation_i)
///
///         with D = the basket pools' BUCK reserve.  BUCK absorbed under the
///         weak side (netInventory > 0) RAISES the shadow value -- bvib as it
///         would have been without the desk -- so K keeps tightening behind
///         a position the desk is holding; BUCK issued under the strong side
///         lowers it, so K restores the headroom the fast actuators spent
///         (7.3).  With every lambda 0 the value IS basketValueInBuck(): the
///         lambda = gamma = 0 identity the controller tests assert.
///
///         `shadowOffset` / `shadowLambda` are the SIM-ONLY pseudo-stabilizer:
///         an inventory the Python UndertakingAgent / FacilityAgent book
///         off-chain before their contracts exist, so K can be fed by the
///         agent stand-ins.  Removed once the books are contract-level.
///
/// # Why its own contract
///
///         The ops shell is the natural home (it is the desk, and it is where
///         the books live) but it is 20.5 KB of a 24,576 B EIP-170 budget
///         after WP-5, and the registry plus these two views are ~2.4 KB.
///         A standalone observer that reads the basket through the seam is
///         also exactly the shape of the observer FACET of the monetary
///         Diamond (WP-11): its own state, the basket's views as inputs,
///         nothing written back.  `basket` is immutable here; it becomes
///         shared storage there.
contract ShadowObserver is IShadowObserver {

    IShadowBasket public immutable basket;
    address       public governance;

    /// @notice A registered level-1 stabilizer and its shadow gain.
    ///         lambda is 1e18-scaled (1e18 = the full inventory/depth ratio
    ///         enters the shadow value).
    struct Stabilizer {
        address addr;
        uint256 lambda;
    }

    Stabilizer[] public stabilizers;
    mapping(address => uint256) public stabilizerIndex;   // 1+index; 0 = absent

    /// @notice The sim-only pseudo-stabilizer's gain and booked inventory
    ///         (absorbed positive, issued negative, BUCK native units).
    uint256 public shadowLambda;
    int256  public shadowOffset;

    uint256 public constant MAX_LAMBDA = 1_000e18;   // sanity bound

    event GovernanceSet(address indexed governance);
    event StabilizerAdded(address indexed stabilizer, uint256 lambda);
    event StabilizerRemoved(address indexed stabilizer);
    event StabilizerLambdaSet(address indexed stabilizer, uint256 lambda);
    event ShadowLambdaSet(uint256 lambda);
    event ShadowOffsetSet(int256 netInventory);

    error NotGovernance();
    error Gov0();
    error Basket0();
    error Stabilizer0();
    error AlreadyPresent();
    error StabilizerUnknown();
    error LambdaTooLarge();

    modifier onlyGov() {
        if (msg.sender != governance) revert NotGovernance();
        _;
    }

    constructor(address _basket, address _governance) {
        if (_basket == address(0)) revert Basket0();
        if (_governance == address(0)) revert Gov0();
        basket     = IShadowBasket(_basket);
        governance = _governance;
    }

    function setGovernance(address _governance) external onlyGov {
        if (_governance == address(0)) revert Gov0();
        governance = _governance;
        emit GovernanceSet(_governance);
    }

    // --- Registry ---------------------------------------------------------- //

    function addStabilizer(address s, uint256 lambda) external onlyGov {
        if (s == address(0)) revert Stabilizer0();
        if (stabilizerIndex[s] != 0) revert AlreadyPresent();
        if (lambda > MAX_LAMBDA) revert LambdaTooLarge();
        stabilizers.push(Stabilizer({addr: s, lambda: lambda}));
        stabilizerIndex[s] = stabilizers.length;
        emit StabilizerAdded(s, lambda);
    }

    function removeStabilizer(address s) external onlyGov {
        uint256 idx1 = stabilizerIndex[s];
        if (idx1 == 0) revert StabilizerUnknown();
        uint256 last = stabilizers.length - 1;
        if (idx1 - 1 != last) {
            Stabilizer memory moved = stabilizers[last];
            stabilizers[idx1 - 1] = moved;
            stabilizerIndex[moved.addr] = idx1;
        }
        stabilizers.pop();
        delete stabilizerIndex[s];
        emit StabilizerRemoved(s);
    }

    function setStabilizerLambda(address s, uint256 lambda) external onlyGov {
        uint256 idx1 = stabilizerIndex[s];
        if (idx1 == 0) revert StabilizerUnknown();
        if (lambda > MAX_LAMBDA) revert LambdaTooLarge();
        stabilizers[idx1 - 1].lambda = lambda;
        emit StabilizerLambdaSet(s, lambda);
    }

    /// @notice Shadow gain of the sim-only pseudo-stabilizer.
    function setShadowLambda(uint256 lambda) external onlyGov {
        if (lambda > MAX_LAMBDA) revert LambdaTooLarge();
        shadowLambda = lambda;
        emit ShadowLambdaSet(lambda);
    }

    /// @notice Sim-only: book an off-chain inventory (absorbed positive,
    ///         issued negative, BUCK native units) into the shadow value.
    function setShadowOffset(int256 netInventory) external onlyGov {
        shadowOffset = netInventory;
        emit ShadowOffsetSet(netInventory);
    }

    function stabilizerCount() external view returns (uint256) {
        return stabilizers.length;
    }

    // --- Views ------------------------------------------------------------- //

    /// @notice The reference depth D, read from the basket.
    function shadowDepth() public view returns (uint256) {
        return basket.shadowDepth();
    }

    /// @notice bvib + (sum_i lambda_i * netInventory_i + shadowLambda *
    ///         shadowOffset) / D, 18-dec like basketValueInBuck().
    ///
    /// @dev    Units: lambda (1e18) * inventory (BUCK) / D (BUCK) is 1e18-
    ///         scaled and dimensionless, so it adds to the 18-dec bvib
    ///         directly.  Stabilizers at lambda 0 are not consulted at all;
    ///         with every lambda 0 (or a zero book) the return is
    ///         basketValueInBuck() itself.  A registered stabilizer that
    ///         reverts reverts this view: loud, immediate, and governance's
    ///         to remove -- silently reading 0 would move K's process
    ///         variable with no trace.
    function shadowValueInBuck() external view override returns (int256) {
        int256 bvib = basket.basketValueInBuck();
        int256 weighted = int256(shadowLambda) * shadowOffset;
        uint256 n = stabilizers.length;
        for (uint256 i = 0; i < n; i++) {
            Stabilizer storage s = stabilizers[i];
            if (s.lambda == 0) continue;
            weighted += int256(s.lambda) * IStabilizer(s.addr).netInventory();
        }
        if (weighted == 0) return bvib;
        uint256 depth = basket.shadowDepth();
        if (depth == 0) return bvib;
        return bvib + weighted / int256(depth);
    }

    /// @notice max_i min(1e18, lambda_i * saturation_i / 1e18): the
    ///         lambda-weighted maximum, so a stabilizer K is not listening
    ///         to (lambda 0) cannot schedule K's gain either.  The pseudo-
    ///         stabilizer has no bounds and contributes nothing here.
    function shadowSaturation() external view override returns (uint256 sat) {
        uint256 n = stabilizers.length;
        for (uint256 i = 0; i < n; i++) {
            Stabilizer storage s = stabilizers[i];
            if (s.lambda == 0) continue;
            uint256 w = s.lambda * IStabilizer(s.addr).saturation() / 1e18;
            if (w > 1e18) w = 1e18;
            if (w > sat) sat = w;
        }
    }
}
