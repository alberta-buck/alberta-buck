// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IStabilizer}     from "./IStabilizer.sol";
import {IShadowObserver} from "./IShadowObserver.sol";

/// @dev What the observer reads from the basket: K's raw process variable
///      (the venue facet's view, served through the shell's fallback) and the
///      reference depth D (on the ops shell), the latter only under S.
interface IShadowBasket {
    function basketValueInBuck() external view returns (int256);   // 18-dec
    function shadowDepth() external view returns (uint256);         // BUCK native units
}

/// @title ShadowObserver -- the level-1 -> K observer (CARRY-CONVEXITY.org
///        6.4 and D7; WAVE3.org WP-3a, WP-13).
///
/// @notice A governance registry over the level-1 actuators' `IStabilizer`
///         books, assembling the AGGREGATE POSITION s the controller's
///         position loop consumes, in one of two units (D7, governance-set):
///
///           S   s = (sum_i lambda_i * q_i + shadowLambda * shadowOffset) / D
///           V   s = (sum_i w_i * q_i / cap_i + shadowWeight * shadowOffset
///                    / shadowCap) / (sum_i w_i + shadowWeight)
///
///         q_i = netInventory_i (absorbed positive, issued negative), D = the
///         basket pools' BUCK reserve (S only), cap_i = the HELD cap (below),
///         and every sum runs over the INCLUDED stabilizers only.  Under V
///         each fill q_i / cap_i is clamped to [-1, 1] (a book beyond its
///         bound is full), so s_V is in [-1e18, 1e18] as its units promise.
///         `shadowValueInBuck()` = bvib + s_S regardless of the mode (D4's
///         composite, kept for telemetry; the controller integrates the raw
///         basket, decision 10).  With every gain 0, or every book 0, s is
///         0 and the shadow value is bvib exactly.
///
/// # The held cap and the sensor-fault policy (WAVE3.org decision 9)
///
///         Each registered stabilizer carries the LAST GOOD cap the observer
///         read from its `positionCap()` and the time it read it.  `refresh()`
///         (called by `observe()`, i.e. by the controller each cycle, and
///         once at registration) re-reads every cap inside try/catch:
///
///           * success, cap > 0:  heldCap := cap, stale := false; included;
///           * success, cap == 0: heldCap := 0, stale := false; the stabilizer
///                                is DISABLED and EXCLUDED from s, the V
///                                weights renormalized without it -- a book
///                                that cannot move is not a lever K should
///                                mid-range;
///           * revert:            heldCap kept, stale := true; still included
///                                if the held cap is positive -- a raid day
///                                (the spot/TWAP guard) does not read as a
///                                change of position.
///
///         Before the first successful read a stabilizer has held cap 0 and
///         is excluded and stale.  Position and saturation are computed HERE
///         from `netInventory` and the held cap (position = q / heldCap,
///         saturation = min(1, |q| / heldCap)); the stabilizer's own
///         `capacity()` / `saturation()` are no longer what K consumes.  A
///         stabilizer that reverts on `netInventory()` still reverts the
///         whole read, loudly: silently reading 0 would move K's input with
///         no trace, and it is governance's to remove.
///
///         `shadowOffset` / `shadowLambda` / `shadowWeight` / `shadowCap` are
///         the SIM-ONLY pseudo-stabilizer: an inventory the Python
///         UndertakingAgent / FacilityAgent book off-chain before their
///         contracts exist.  It has no NAV to read: under S it is included
///         whenever shadowLambda is set (as in WP-3a), under V whenever
///         shadowCap is set; it is never stale.  Removed once the books are
///         contract-level.
///
/// # Why its own contract
///
///         The ops shell is 21 KB of a 24,576 B EIP-170 budget; the registry,
///         the held caps and these views are a few KB, and a standalone
///         observer that reads the basket through the seam is exactly the
///         shape of the observer FACET of the monetary Diamond (WP-11): its
///         own state, the basket's and the stabilizers' views as inputs,
///         nothing written back.
contract ShadowObserver is IShadowObserver {

    IShadowBasket public immutable basket;
    address       public governance;

    /// @notice The aggregation in force: S (D4's price units, needs D) or V
    ///         (cost-weighted fill, no D).  Default S.
    enum Mode { S, V }
    Mode public mode;

    /// @notice A registered level-1 stabilizer, its two gains and its held
    ///         cap.  lambda (S) and weight (V) are 1e18-scaled.
    struct Stabilizer {
        address addr;
        uint256 lambda;     // S gain: 1e18 = the full inventory/depth ratio
        uint256 weight;     // V cost weight: 1e18 = unit carry class (the desk)
        uint256 heldCap;    // last good positionCap(), BUCK native; 0 = excluded
        uint64  heldAt;     // block.timestamp of the last successful read
        bool    stale;      // the last refresh reverted (or none succeeded yet)
    }

    Stabilizer[] public stabilizers;
    mapping(address => uint256) public stabilizerIndex;   // 1+index; 0 = absent

    /// @notice The sim-only pseudo-stabilizer: booked inventory (absorbed
    ///         positive, issued negative, BUCK native units), its S gain, its
    ///         V weight and its V cap.
    uint256 public shadowLambda;
    int256  public shadowOffset;
    uint256 public shadowWeight;
    uint256 public shadowCap;

    uint256 public constant MAX_LAMBDA = 1_000e18;   // sanity bound (lambda and weight)
    uint256 internal constant UNIT = 1e18;
    uint256 internal constant PSEUDO_BIT = 255;      // the pseudo-stabilizer's flag bit

    event GovernanceSet(address indexed governance);
    event ModeSet(Mode mode);
    event StabilizerAdded(address indexed stabilizer, uint256 lambda);
    event StabilizerRemoved(address indexed stabilizer);
    event StabilizerLambdaSet(address indexed stabilizer, uint256 lambda);
    event StabilizerWeightSet(address indexed stabilizer, uint256 weight);
    event StabilizerStale(address indexed stabilizer, bool stale);
    event ShadowLambdaSet(uint256 lambda);
    event ShadowWeightSet(uint256 weight);
    event ShadowCapSet(uint256 cap);
    event ShadowOffsetSet(int256 netInventory);

    error NotGovernance();
    error Gov0();
    error Basket0();
    error Stabilizer0();
    error NoCode();
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

    /// @notice Governance: the aggregation mode.  Switching modes steps s
    ///         (different units); govern it at a quiet moment, and note the
    ///         controller's position integrator carries the old units'
    ///         history until it is retuned.
    function setMode(Mode m) external onlyGov {
        mode = m;
        emit ModeSet(m);
    }

    // --- Registry ---------------------------------------------------------- //

    /// @notice Register a stabilizer at S gain `lambda`; its V weight starts
    ///         at 1e18 (the unit carry class) until `setStabilizerWeight`.
    ///         The first cap read is attempted at once, so an enabled,
    ///         readable stabilizer is included from this block.
    function addStabilizer(address s, uint256 lambda) external onlyGov {
        if (s == address(0)) revert Stabilizer0();
        if (stabilizerIndex[s] != 0) revert AlreadyPresent();
        if (lambda > MAX_LAMBDA) revert LambdaTooLarge();
        if (s.code.length == 0) revert NoCode();
        stabilizers.push(Stabilizer({addr: s, lambda: lambda, weight: UNIT,
                                     heldCap: 0, heldAt: 0, stale: true}));
        stabilizerIndex[s] = stabilizers.length;
        emit StabilizerAdded(s, lambda);
        _refreshOne(stabilizers.length - 1);
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
        if (lambda > MAX_LAMBDA) revert LambdaTooLarge();
        stabilizers[_idx(s)].lambda = lambda;
        emit StabilizerLambdaSet(s, lambda);
    }

    /// @notice Governance: the V cost weight (decision 11: the desk 1, the
    ///         undertakings 1 per side, the facility 0.5, the seeder 0.25).
    function setStabilizerWeight(address s, uint256 weight) external onlyGov {
        if (weight > MAX_LAMBDA) revert LambdaTooLarge();
        stabilizers[_idx(s)].weight = weight;
        emit StabilizerWeightSet(s, weight);
    }

    /// @notice Shadow (S) gain of the sim-only pseudo-stabilizer.
    function setShadowLambda(uint256 lambda) external onlyGov {
        if (lambda > MAX_LAMBDA) revert LambdaTooLarge();
        shadowLambda = lambda;
        emit ShadowLambdaSet(lambda);
    }

    /// @notice V weight of the sim-only pseudo-stabilizer.
    function setShadowWeight(uint256 weight) external onlyGov {
        if (weight > MAX_LAMBDA) revert LambdaTooLarge();
        shadowWeight = weight;
        emit ShadowWeightSet(weight);
    }

    /// @notice Sim-only: the pseudo-stabilizer's inventory bound (BUCK
    ///         native units) for the V fill; 0 excludes it from V.
    function setShadowCap(uint256 cap) external onlyGov {
        shadowCap = cap;
        emit ShadowCapSet(cap);
    }

    /// @notice Sim-only: book an off-chain inventory (absorbed positive,
    ///         issued negative, BUCK native units) into the aggregate.
    function setShadowOffset(int256 netInventory) external onlyGov {
        shadowOffset = netInventory;
        emit ShadowOffsetSet(netInventory);
    }

    function stabilizerCount() external view returns (uint256) {
        return stabilizers.length;
    }

    function _idx(address s) internal view returns (uint256) {
        uint256 idx1 = stabilizerIndex[s];
        if (idx1 == 0) revert StabilizerUnknown();
        return idx1 - 1;
    }

    // --- The held caps ------------------------------------------------------ //

    /// @notice Re-read every registered stabilizer's cap (decision 9): a
    ///         revert keeps the held cap and flags the stabilizer stale, a
    ///         success refreshes it (a zero disables it).  Permissionless;
    ///         the controller calls it through `observe()` every cycle.
    function refresh() public {
        uint256 n = stabilizers.length;
        for (uint256 i = 0; i < n; i++) _refreshOne(i);
    }

    function _refreshOne(uint256 i) internal {
        Stabilizer storage st = stabilizers[i];
        bool wasStale = st.stale;
        try IStabilizer(st.addr).positionCap() returns (uint256 cap) {
            st.heldCap = cap;
            st.heldAt  = uint64(block.timestamp);
            st.stale   = false;
        } catch {
            st.stale = true;
        }
        if (st.stale != wasStale) emit StabilizerStale(st.addr, st.stale);
    }

    /// @notice Refresh the held caps, then the aggregate position.
    function observe() external override returns (int256) {
        refresh();
        return aggregatePosition();
    }

    // --- Per-stabilizer views (telemetry, tests) ---------------------------- //

    /// @notice The held (last good) cap, BUCK native units; 0 = excluded.
    function heldCap(address s) external view returns (uint256) {
        return stabilizers[_idx(s)].heldCap;
    }

    /// @notice When the held cap was last read successfully (0 = never).
    function heldAt(address s) external view returns (uint256) {
        return stabilizers[_idx(s)].heldAt;
    }

    /// @notice The last refresh could not read this stabilizer's cap.
    function stale(address s) external view returns (bool) {
        return stabilizers[_idx(s)].stale;
    }

    /// @notice Excluded from the aggregate: disabled, or never read.
    function excluded(address s) external view returns (bool) {
        return stabilizers[_idx(s)].heldCap == 0;
    }

    /// @notice The stabilizer's signed fill q / heldCap, 1e18, clamped to
    ///         [-1, 1]; 0 when excluded.
    function position(address s) external view returns (int256) {
        Stabilizer storage st = stabilizers[_idx(s)];
        if (st.heldCap == 0) return 0;
        return _fill(IStabilizer(st.addr).netInventory(), st.heldCap);
    }

    /// @notice min(1, |q| / heldCap), 1e18; 0 when excluded.
    function stabilizerSaturation(address s) external view returns (uint256) {
        Stabilizer storage st = stabilizers[_idx(s)];
        if (st.heldCap == 0) return 0;
        return _absFill(IStabilizer(st.addr).netInventory(), st.heldCap);
    }

    // --- Aggregate views ---------------------------------------------------- //

    /// @notice The reference depth D, read from the basket (S only).
    function shadowDepth() public view returns (uint256) {
        return basket.shadowDepth();
    }

    /// @notice s under the mode in force (1e18; absorbed positive).
    function aggregatePosition() public view override returns (int256) {
        return mode == Mode.S ? _positionS() : _positionV();
    }

    /// @notice bvib + s_S, 18-dec like basketValueInBuck(), in either mode.
    function shadowValueInBuck() external view override returns (int256) {
        return basket.basketValueInBuck() + _positionS();
    }

    /// @dev (sum_i lambda_i * q_i + shadowLambda * shadowOffset) / D over the
    ///      included stabilizers.  Units: lambda (1e18) * inventory (BUCK) /
    ///      D (BUCK) is 1e18-scaled and dimensionless.  Stabilizers at lambda
    ///      0 are not consulted at all; with every gain 0 (or a zero book)
    ///      the return is exactly 0 -- no rounding enters the identity.
    function _positionS() internal view returns (int256) {
        int256 weighted = int256(shadowLambda) * shadowOffset;
        uint256 n = stabilizers.length;
        for (uint256 i = 0; i < n; i++) {
            Stabilizer storage st = stabilizers[i];
            if (st.lambda == 0 || st.heldCap == 0) continue;
            weighted += int256(st.lambda) * IStabilizer(st.addr).netInventory();
        }
        if (weighted == 0) return 0;
        uint256 depth = basket.shadowDepth();
        if (depth == 0) return 0;
        return weighted / int256(depth);
    }

    /// @dev sum_i w_i * fill_i / sum_i w_i over the included stabilizers
    ///      (the pseudo-stabilizer counts when its cap is set), fill_i = q_i
    ///      / heldCap_i clamped to [-1, 1].  A stabilizer at weight 0 is
    ///      included in neither sum; with nothing included s is 0.
    function _positionV() internal view returns (int256) {
        int256  num;    // sum w_i * fill_i, 1e36
        uint256 den;    // sum w_i, 1e18
        uint256 n = stabilizers.length;
        for (uint256 i = 0; i < n; i++) {
            Stabilizer storage st = stabilizers[i];
            if (st.heldCap == 0 || st.weight == 0) continue;
            den += st.weight;
            num += int256(st.weight) * _fill(IStabilizer(st.addr).netInventory(), st.heldCap);
        }
        if (shadowCap != 0 && shadowWeight != 0) {
            den += shadowWeight;
            num += int256(shadowWeight) * _fill(shadowOffset, shadowCap);
        }
        if (den == 0) return 0;
        return num / int256(den);
    }

    /// @notice max over the included stabilizers of min(1, g_i * |q_i| /
    ///         heldCap_i), g_i the mode's gain (lambda_i under S, w_i under
    ///         V) -- so a stabilizer K is not listening to cannot schedule
    ///         K's gain either.  The pseudo-stabilizer counts when its cap
    ///         is set.  Computed from the HELD cap: a guard trip never
    ///         reads as saturation (decision 9).
    function shadowSaturation() external view override returns (uint256 sat) {
        bool sMode = mode == Mode.S;
        uint256 n = stabilizers.length;
        for (uint256 i = 0; i < n; i++) {
            Stabilizer storage st = stabilizers[i];
            if (st.heldCap == 0) continue;
            uint256 g = sMode ? st.lambda : st.weight;
            if (g == 0) continue;
            uint256 w = g * _absFill(IStabilizer(st.addr).netInventory(), st.heldCap) / UNIT;
            if (w > UNIT) w = UNIT;
            if (w > sat) sat = w;
        }
        if (shadowCap != 0) {
            uint256 g = sMode ? shadowLambda : shadowWeight;
            if (g != 0) {
                uint256 w = g * _absFill(shadowOffset, shadowCap) / UNIT;
                if (w > UNIT) w = UNIT;
                if (w > sat) sat = w;
            }
        }
    }

    /// @notice Bit i: the i-th registered stabilizer is stale / excluded;
    ///         bit 255: a BOOKED pseudo-stabilizer inventory is excluded
    ///         from V (no cap) -- an inventory K is not mid-ranging.
    function flags() external view override
        returns (uint256 staleMask, uint256 excludedMask)
    {
        uint256 n = stabilizers.length;
        if (n > PSEUDO_BIT) n = PSEUDO_BIT;
        for (uint256 i = 0; i < n; i++) {
            Stabilizer storage st = stabilizers[i];
            if (st.stale) staleMask |= (1 << i);
            if (st.heldCap == 0) excludedMask |= (1 << i);
        }
        if (mode == Mode.V && shadowCap == 0 && shadowOffset != 0) excludedMask |= (1 << PSEUDO_BIT);
    }

    // --- Arithmetic --------------------------------------------------------- //

    /// @dev q / cap, 1e18, clamped to [-1e18, 1e18].
    function _fill(int256 q, uint256 cap) internal pure returns (int256 f) {
        f = q * int256(UNIT) / int256(cap);
        if (f > int256(UNIT)) f = int256(UNIT);
        else if (f < -int256(UNIT)) f = -int256(UNIT);
    }

    /// @dev min(1e18, |q| * 1e18 / cap).
    function _absFill(int256 q, uint256 cap) internal pure returns (uint256) {
        uint256 a = q < 0 ? uint256(-q) : uint256(q);
        uint256 f = a * UNIT / cap;
        return f > UNIT ? UNIT : f;
    }
}
