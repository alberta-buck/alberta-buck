// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IBuckKController}   from "../IBuckKController.sol";
import {IBuckBasketVenue}   from "./IBuckBasketVenue.sol";
import {IBuckMintBurn}      from "./BuckBasketStorage.sol";
import {IBuckHolder, IMarkedCredit, IBuckKSource} from "./BuckBasketEquityStorage.sol";
import {MonetaryDesk}       from "./MonetaryDesk.sol";
import {IMonetaryDirector}  from "./MonetaryDesk.sol";

/// @notice What the desk reads from the equity basket it runs beside.
interface IEquityBasketView {
    function equity() external view returns (uint256);
    function twapWindow() external view returns (uint32);
    function constituentsLength() external view returns (uint256);
    function constituents(uint256 i) external view returns (
        address token, uint8 decimals, uint256 basketAmount, uint256 initialPriceInBuck,
        uint24 feeTier, address pool, int24 tickLower, int24 tickUpper, bool buckIsToken0,
        uint256 targetWeightBp, uint128 treasuryLiquidity);
}

interface IMonetaryExecutionGuard {
    function executionAllowed(int32 effort) external view returns (bool);
}

/// @title EquityDesk -- the monetary desk as its own BUCK credit holder,
///        beside the equity basket.
///
/// @notice The desk reuses MonetaryDesk's configuration and stabilizer views,
///         and replaces its privileged mint/burn operations with actual
///         credit-holder accounting. It runs beside a
///         BuckBasketEquity and shares nothing with it but the pools it
///         trades in:
///
///           * its own address, so its own account at Buck: its own signed
///             balance, lien, relief, and limit;
///           * its own self-issued MARKED BuckCredit, marked at its own net
///             value -- its TOKEN at the pools' low marks, plus its account and
///             the relief accrued on it -- so Buck holds its issuance within
///             K x the desk's value and the basket's within K x the
///             depositors' equity, each account separately;
///           * its own capital: the founding grant (`capitalizeMonetary`) is
///             the desk's TOKEN, collateral for the desk alone;
///           * its own invoker: `monetaryOperation`, called by its own work
///             wheel (the ops kind, pointed at this contract) or a keeper.
///
///         From the basket it reads only two things: the basket's equity,
///         which sizes the desk's bounds (as the shell's NAV sized them when
///         the desk was a mixin), and its constituents, mirrored here so the
///         desk trades in the same pools through the same venue facet (this
///         contract's fallback delegatecalls to it, as a basket shell does).
///
///         Invariants:
///           D1  the desk spends only after a mark at its own net value, so
///               its lien stays within K x that value (Buck enforces it)
///           D2  nothing the desk holds or owes enters the basket's books,
///               mark or limit: they are two accounts
///           D3  relief on the desk's lien is the desk's, and melts its
///               outstanding issuance
contract EquityDesk is MonetaryDesk {

    IEquityBasketView public basket;
    IMarkedCredit     public credit;
    uint256           public creditId;
    bool              public creditLive;

    /// @notice Finite gross intervention programme, in native BUCK units.
    ///         Neither repayments nor opposite-side trades replenish it.
    ///         Off until governance allocates a programme and a capital floor.
    uint256 public turnoverRemaining;
    uint256 public capitalFloor;
    address public executionGuard;

    uint8 internal constant DEP_MARKED = 3;              // BuckCredit.DepreciationType.MARKED

    event CreditOpened(address indexed credit, uint256 indexed tokenId, uint256 face);
    event DeskMarked(uint256 value);
    event RiskBudgetSet(uint256 turnover, uint256 capitalFloor, uint256 executionDeviationBp);
    event ExecutionGuardSet(address indexed guard);

    constructor(address _buck, address _controller, address _basket, address _governance) {
        buck       = IBuckMintBurn(_buck);
        controller = IBuckKController(_controller);
        basket     = IEquityBasketView(_basket);
        governance = _governance;
    }

    // --- Wiring (governance) ------------------------------------------------ //

    function setVenue(address v) external onlyGov {
        venue = IBuckBasketVenue(v);
        emit VenueSet(v);
    }

    function setRiskBudget(uint256 turnover, uint256 floor, uint256 executionDeviationBp) external onlyGov {
        require(executionDeviationBp > 0 && executionDeviationBp <= 1000, "execution band");
        turnoverRemaining = turnover;
        capitalFloor = floor;
        defaultMaxDeviationBp = executionDeviationBp;
        emit RiskBudgetSet(turnover, floor, executionDeviationBp);
    }

    function setExecutionGuard(address guard) external onlyGov {
        executionGuard = guard;
        emit ExecutionGuardSet(guard);
    }

    /// @notice Mirror the basket's constituents (the same tokens and pools)
    ///         and its TWAP window; appends any the basket added since.
    function mirrorConstituents() external onlyGov {
        twapWindow = basket.twapWindow();
        uint256 n = basket.constituentsLength();
        for (uint256 i = constituents.length; i < n; i++) {
            (address token, uint8 dec, uint256 amt, uint256 p0, uint24 fee, address pool,
             int24 lo, int24 hi, bool b0, uint256 w,) = basket.constituents(i);
            constituents.push(Constituent(token, dec, amt, p0, fee, pool, lo, hi, b0, w, 0));
            indexOf[token] = i + 1;
        }
    }

    /// @notice Open the desk's own MARKED credit, the desk its own insurer
    ///         (zero premium); activated by its first mark above zero.
    function openCredit(address credit_, uint256 face) external onlyGov {
        if (address(credit) != address(0)) revert AlreadyPresent();
        IMarkedCredit c = IMarkedCredit(credit_);
        c.setCreditIssuer(address(this), true);
        creditId = c.createCredit(address(this), 0, face, 0, DEP_MARKED, 0, 0, 0);
        credit = c;
        emit CreditOpened(credit_, creditId, face);
    }

    // --- The desk's own value, and its mark ------------------------------------ //

    function _bh() internal view returns (IBuckHolder) {
        return IBuckHolder(address(buck));
    }

    function _venueMarks(uint256 i) internal view returns (IBuckBasketVenue.Marks memory) {
        return IBuckBasketVenue(address(this)).marks(i, 0);
    }

    /// @notice The desk's net value in BUCK at the pools' low marks: its TOKEN,
    ///         plus its account at Buck (negative: its lien) and the relief
    ///         accrued on it.  What its credit is marked at (D1).
    function netValue() public view returns (uint256) {
        int256 v = int256(_bh().signedBalanceOf(address(this)))
                 + int256(_bh().reliefOf(address(this)));
        for (uint256 i = 0; i < constituents.length; i++) {
            uint256 held = monetaryTokenHeld[i];
            if (held == 0) continue;
            v += int256(held * _venueMarks(i).pLow / (10 ** constituents[i].decimals));
        }
        return v > 0 ? uint256(v) : 0;
    }

    function markNow() external view returns (uint256) {
        return address(credit) == address(0) ? 0 : credit.markOf(creditId);
    }

    /// @dev Mark the credit at the desk's net value, activating it the first
    ///      time the value is above zero.
    function _mark() internal {
        IMarkedCredit c = credit;
        if (address(c) == address(0)) return;
        uint256 value = netValue();
        if (c.markOf(creditId) != value) {
            c.mark(creditId, value);
            emit DeskMarked(value);
        }
        if (!creditLive && value > 0) {
            // One mint for all the credit can give activates its whole face:
            // zero premium, so no deposit.
            uint256[] memory ids = new uint256[](1);
            ids[0] = creditId;
            _bh().mint(type(uint256).max, ids);
            creditLive = true;
        }
    }

    /// @dev Relief paid into the desk's account melts its outstanding issuance
    ///      (D3): collected before each operation.
    function _collectRelief() internal {
        int256 s0 = _bh().signedBalanceOf(address(this));
        if (s0 >= 0 || _bh().reliefOf(address(this)) == 0) return;
        _bh().settleRelief();
    }

    /// @notice Publish the actual account: receipts first repay a lien, then
    ///         become held BUCK. An ordinary credit holder cannot burn a
    ///         positive balance by changing an auxiliary counter.
    function _syncAccount() internal {
        int256 s = _bh().signedBalanceOf(address(this));
        monetaryBuckHeld = s > 0 ? uint256(s) : 0;
        monetaryOutstanding = s < 0 ? -s : int256(0);
    }

    function refresh() external {
        _collectRelief();
        _syncAccount();
        _mark();
    }

    function netInventory() public view override returns (int256) {
        return _bh().signedBalanceOf(address(this));
    }

    function capacity() public view override returns (uint256) {
        if (turnoverRemaining == 0) return 0;
        try this.netValue() returns (uint256 value) {
            if (value <= capitalFloor) return 0;
        } catch { return 0; }
        return super.capacity();
    }

    /// @notice The credit-holder implementation replaces the legacy mint/burn
    ///         quadrants. Buy receipts retire only debt that actually exists;
    ///         positive receipts are held even when the policy is persistent.
    ///         Sell held BUCK first; draw credit only on persistent advice.
    function monetaryOperation() external override returns (uint8 quadrant) {
        OpsParams memory op = opsParams;
        if (!op.enabled || turnoverRemaining == 0) revert MonetaryIdle();
        address dir = _monDir();
        if (dir == address(0)) revert DirectorUnset();
        uint32 epoch = IMonetaryDirector(dir).epochNow();
        if (lastMonetaryEpoch == epoch + 1) revert StepAlreadyDone();
        (int32 effort, bool outright) = IMonetaryDirector(dir).monetaryEffort();
        if (effort == 0) revert NoAdvice();
        uint256 nav = _deskNav();
        uint256 size = nav * uint256(effort < 0 ? -int256(effort) : int256(effort)) / 10000;
        _collectRelief();
        _syncAccount();
        _mark();
        if (netValue() < capitalFloor) revert MonetaryBound();
        uint256 positionCap = nav * op.maxPositionBp / 10000;
        uint256 debtCap = nav * op.maxOutrightBp / 10000;
        int256 beforeAccount = _bh().signedBalanceOf(address(this));
        bool selling = effort > 0;
        uint256 room;
        if (selling) {
            // Each operation either sells inventory or draws credit. Never
            // cross through zero merely because advice asked for more.
            if (monetaryBuckHeld > 0) { room = monetaryBuckHeld; quadrant = 3; }
            else {
                if (!outright) revert MonetaryIdle();
                uint256 debt = uint256(monetaryOutstanding);
                room = debtCap > debt ? debtCap - debt : 0;
                quadrant = 4;
            }
            room = _min(room, _bh().balanceOf(address(this)));
        } else {
            uint256 held = monetaryBuckHeld;
            room = uint256(monetaryOutstanding) + (positionCap > held ? positionCap - held : 0);
            quadrant = beforeAccount < 0 ? 2 : 1;
        }
        size = _min(size, _min(room, turnoverRemaining));
        if (size == 0) revert MonetaryBound();
        lastMonetaryEpoch = epoch + 1;
        uint256 cost = _trade(selling, size, op);
        if (cost == 0) revert MonetaryIdle();
        if (executionGuard != address(0)
            && !IMonetaryExecutionGuard(executionGuard).executionAllowed(effort)) revert MonetaryBound();
        turnoverRemaining -= cost;
        _syncAccount();
        _mark();
        // Postconditions use actual receipts (including BUCK's arrival fee),
        // not swap return values or intended operation sizes.
        if ((!selling && monetaryBuckHeld > positionCap)
            || (selling && uint256(monetaryOutstanding) > debtCap && quadrant == 4)
            || netValue() < capitalFloor) revert MonetaryBound();
        if (quadrant == 4 && uint256(monetaryOutstanding) > _kLimit()) revert MonetaryBound();
        int256 delta = _bh().signedBalanceOf(address(this)) - beforeAccount;
        uint256 actual = uint256(delta < 0 ? -delta : delta);
        emit MonetaryOperation(quadrant, effort, outright, actual, monetaryOutstanding, monetaryBuckHeld);
    }

    function _kLimit() private view returns (uint256) {
        return IBuckKSource(address(controller)).currentBuckK() * credit.markOf(creditId) / 1e18;
    }

    /// @dev Active V3 depth and price marks size the trade; ERC20 pool
    ///      balances include inactive liquidity and are not V3 reserves.
    function _trade(bool sellBuck, uint256 size, OpsParams memory op) private returns (uint256 cost) {
        uint256 n = constituents.length;
        if (n == 0) return 0;
        uint256 share = size / n;
        for (uint256 i; i < n; ++i) {
            IBuckBasketVenue.Marks memory m = _venueMarks(i);
            uint256 want = _min(share, m.depth * op.maxLegBp / 10000);
            if (want == 0) continue;
            if (sellBuck) {
                (uint256 spent, uint256 got) = _dv().monetaryLeg(i, true, want);
                monetaryTokenHeld[i] += got;
                cost += spent;
            } else {
                uint256 one = 10 ** constituents[i].decimals;
                uint256 tokens = _min(monetaryTokenHeld[i], want * one / m.pHigh);
                if (tokens == 0) continue;
                (uint256 spent,) = _dv().monetaryLeg(i, false, tokens);
                monetaryTokenHeld[i] -= spent;
                // Round cost up: splitting work must not mint free budget.
                cost += (spent * m.pHigh + one - 1) / one;
            }
        }
    }

    function _min(uint256 a, uint256 b) private pure returns (uint256) { return a < b ? a : b; }

    // --- MonetaryDesk's hooks -------------------------------------------------- //

    /// @dev The bounds are sized in the basket's equity, as they were sized in
    ///      the shell's NAV when the desk was a mixin.
    function _deskNav() internal view override returns (uint256 nav) {
        nav = basket.equity();
        if (nav == 0) revert NoValue();
    }

    function _deskNavSafe() internal view override returns (uint256) {
        try basket.equity() returns (uint256 e) { return e; } catch { return 0; }
    }

    function _deskPrices() internal view override returns (uint256[] memory prices) {
        uint256 n = constituents.length;
        prices = new uint256[](n);
        for (uint256 i = 0; i < n; i++) prices[i] = _venueMarks(i).pTwap;
    }

    // --- Dispatch: the venue facet (its swaps, callbacks and marks) ------------- //

    fallback() external {
        address v = address(venue);
        if (v == address(0)) revert VenueUnset();
        assembly {
            calldatacopy(0, 0, calldatasize())
            let ok := delegatecall(gas(), v, 0, calldatasize(), 0, 0)
            returndatacopy(0, 0, returndatasize())
            switch ok
            case 0 { revert(0, returndatasize()) }
            default { return(0, returndatasize()) }
        }
    }
}
