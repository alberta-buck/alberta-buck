// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IBuckKController}   from "../IBuckKController.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {IBuckJubilee} from "../IBuckJubilee.sol";
import {BuckTypes} from "../BuckTypes.sol";
import {IBuckBasketVenue}   from "./IBuckBasketVenue.sol";
import {IBuckMintBurn}      from "./BuckBasketStorage.sol";
import {IBuckHolder, IMarkedCredit, IBuckKSource, IBuckNetAccount, IBasketCreditFund}
    from "./BuckBasketEquityStorage.sol";
import {BasketJubileeMath} from "./BasketJubileeMath.sol";
import {MonetaryDesk}       from "./MonetaryDesk.sol";
import {IBasketView}        from "./IBasketView.sol";
import {IDeskPolicy}        from "./IDeskPolicy.sol";

/// @title BasketDesk -- the monetary desk as its own BUCK credit holder,
///        beside the equity basket.
///
/// @notice The desk reuses MonetaryDesk's configuration and stabilizer views,
///         and replaces its privileged mint/burn operations with actual
///         credit-holder accounting. It runs beside a
///         BuckBasketEquity and shares nothing with it but the pools it
///         trades in:
///
///           * its own ordinary public account at Buck. Canonical mode keeps
///             cash and actual NFT principal separate, even when both exist;
///           * its own self-issued MARKED BuckCredit, marked at its own net
///             value -- its TOKEN at net low marks, plus cash less principal.
///             Marked unpaid relief enters netValue, never collateral. Buck holds issuance within
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
///           D1  refresh/mark do not issue. New draws fit actual activated
///               collateral; final trades cannot worsen the K shortfall
///           D2  nothing the desk holds or owes enters the basket's books,
///               mark or limit: they are two accounts
///           D3  only an actual fund reduction retires principal/supply.
///               Its cash cost, relief and shared liquidity caps are enforced
///           D4  one financial lock excludes nested trade/refresh/reduction;
///               any failure rolls back swaps and all monetary/book effects
///
///         Explicit canonical mode is required before opening against Buck.
///         The temporary signed branch supports labelled legacy fixtures.
contract BasketDesk is MonetaryDesk {
    using SafeERC20 for IERC20;

    IBasketView public basket;
    IMarkedCredit     public credit;
    uint256           public creditId;
    bool              public creditLive;

    /// @notice Finite gross intervention programme, in native BUCK units.
    ///         Neither repayments nor opposite-side trades replenish it.
    ///         Off until governance allocates a programme and a capital floor.
    uint256 public turnoverRemaining;
    uint256 public capitalFloor;
    address public executionGuard;

    bool public netAccounting;
    bool private transient _entered;
    uint256 private constant Q = 1e36;
    uint256 private constant T = 50 * (365 days + 6 hours);

    struct Account {
        uint256 cash;
        uint256 principal;
        uint256 assets; // own TOKEN at net LOW marks, plus actual net cash
    }

    error DeskReentrant();
    error InvalidNetAccounting();
    error NetAccountingRequired();
    error CreditCashLimit();
    error CreditShortfallIncreased();
    error InconsistentExecution();

    modifier financialLock() {
        if (_entered) revert DeskReentrant();
        _entered = true;
        _;
        _entered = false;
    }

    uint8 internal constant DEP_MARKED = 3;              // BuckCredit.DepreciationType.MARKED

    event CreditOpened(address indexed credit, uint256 indexed tokenId, uint256 face);
    event DeskMarked(uint256 value);
    event RiskBudgetSet(uint256 turnover, uint256 capitalFloor, uint256 executionDeviationBp);
    event ExecutionGuardSet(address indexed guard);
    event NetAccountingEnabled();
    event CreditRetired(uint256 indexed tokenId, uint256 principal,
                        uint256 reliefScaled, uint256 cashSpentScaled);

    constructor(address _buck, address _controller, address _basket, address _governance) {
        buck       = IBuckMintBurn(_buck);
        controller = IBuckKController(_controller);
        basket     = IBasketView(_basket);
        governance = _governance;
    }

    // --- Wiring (governance) ------------------------------------------------ //

    /// @dev Pre: no facility has been opened; Buck uses the canonical model
    ///      and this desk is an ordinary account. Post: observations and
    ///      financial operations use actual cash and NFT principal. No money
    ///      is issued, no coverage is activated, and no legacy fallback occurs.
    function enableNetAccounting() external onlyGov {
        if (netAccounting || address(credit) != address(0)) revert AlreadyPresent();
        if (!_hasNetModel()
            || !IBuckJubilee(address(buck)).identity().isVerified(address(this))
            || !IBuckJubilee(address(buck)).identity().isPublicIdentity(address(this))
            || IBuckJubilee(address(buck)).identity().isCarrying(address(this))) revert InvalidNetAccounting();
        netAccounting = true;
        emit NetAccountingEnabled();
    }

    function _hasNetModel() private view returns (bool) {
        (bool ok, bytes memory data) = address(buck).staticcall(abi.encodeWithSignature("ACCOUNTING_MODEL()"));
        return ok && data.length == 32
            && abi.decode(data, (bytes32)) == keccak256("Buck/compounded-cash/per-facility-jubilee/v1");
    }

    function setVenue(address v) external onlyGov {
        venue = IBuckBasketVenue(v);
        emit VenueSet(v);
    }

    /// @dev Credit only TOKEN actually delivered; never borrow a depositor's
    ///      book or allow a capital callback during another financial action.
    function capitalizeMonetary(uint256 i, uint256 amount) public override onlyGov financialLock {
        if (!netAccounting) { super.capitalizeMonetary(i, amount); return; }
        if (i >= constituents.length) revert NotInBasket();
        if (amount == 0) revert Amount0();
        IERC20 asset = IERC20(constituents[i].token);
        uint256 beforeBalance = asset.balanceOf(address(this));
        asset.safeTransferFrom(msg.sender, address(this), amount);
        uint256 received = asset.balanceOf(address(this)) - beforeBalance;
        if (received == 0 || received > amount) revert InconsistentExecution();
        monetaryTokenHeld[i] += received;
        emit MonetaryCapitalized(i, received);
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
    ///         (zero premium). Canonical free backing is activated by the first
    ///         financial operation; opening or marking creates no currency.
    function openCredit(address credit_, uint256 face) external onlyGov {
        if (address(credit) != address(0)) revert AlreadyPresent();
        if (!netAccounting && _hasNetModel()) revert NetAccountingRequired();
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
        IBuckBasketVenue.Marks memory m = IBuckBasketVenue(address(this)).marks(i, 0);
        if (!netAccounting) return m;
        (uint256 gross, uint256 net) = IBuckNetAccount(address(buck)).accountBalancesScaled(constituents[i].pool);
        if (gross == 0) return IBuckBasketVenue.Marks(0, 0, 0, 0, 0, 0, 0);
        m.pLow = Math.mulDiv(m.pLow, net, gross);
        m.pTwap = Math.mulDiv(m.pTwap, net, gross);
        m.pHigh = Math.mulDiv(m.pHigh, net, gross);
        m.depth = Math.mulDiv(m.depth, net, gross);
        return m;
    }

    /// @notice Live net cash, actual NFT principal, marked unpaid relief,
    ///         bare collateral and marked equity, all in raw BUCK units.
    /// @dev Relief is a marked asset, never spendable cash or new collateral.
    function accountState() external view returns (uint256 cash, uint256 principal,
        uint256 relief, uint256 collateral, uint256 marked)
    {
        if (!netAccounting) revert NetAccountingRequired();
        Account memory a = _account();
        relief = BasketJubileeMath.claim(address(buck), creditId, a.principal, a.assets);
        return (a.cash, a.principal, relief, _equity(a, 0), _equity(a, relief));
    }

    /// @dev No fund valuation is needed to observe bare assets or set a
    ///      collateral mark. Only explicit marked-equity views read the claim.
    function _account() internal view returns (Account memory a) {
        a.cash = IBuckNetAccount(address(buck)).netBalanceOf(address(this));
        a.principal = _principal();
        a.assets = a.cash;
        for (uint256 i; i < constituents.length; ++i) {
            uint256 held = monetaryTokenHeld[i];
            if (held != 0) a.assets += Math.mulDiv(held, _venueMarks(i).pLow, 10 ** constituents[i].decimals);
        }
    }

    function _principal() internal view returns (uint256 principal) {
        if (address(credit) != address(0)) (principal,) = credit.issuanceOf(creditId);
    }

    function _equity(Account memory a, uint256 relief) internal pure returns (uint256) {
        if (a.assets >= a.principal) return a.assets - a.principal + relief;
        uint256 shortfall = a.principal - a.assets;
        return relief > shortfall ? relief - shortfall : 0;
    }

    function collateralValue() public view returns (uint256) {
        return netAccounting ? _equity(_account(), 0) : netValue();
    }

    /// @notice The desk's net value in BUCK at the pools' low marks: its TOKEN,
    ///         plus cash less principal and marked unpaid relief. Canonical
    ///         credit is marked at collateralValue, excluding that relief.
    function netValue() public view returns (uint256) {
        if (netAccounting) {
            Account memory a = _account();
            return _equity(a, BasketJubileeMath.claim(address(buck), creditId, a.principal, a.assets));
        }
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

    /// @dev Canonical marks describe bare collateral without issuing currency,
    ///      activating coverage or realizing relief. The signed fixture branch
    ///      alone activates its first positive mark through legacy mint(max).
    function _mark() internal {
        IMarkedCredit c = credit;
        if (address(c) == address(0)) return;
        uint256 value = collateralValue();
        if (c.markOf(creditId) != value) {
            c.mark(creditId, value);
            emit DeskMarked(value);
        }
        if (!netAccounting && !creditLive && value > 0) {
            // One mint for all the credit can give activates its whole face:
            // zero premium, so no deposit.
            uint256[] memory ids = new uint256[](1);
            ids[0] = creditId;
            _bh().mint(type(uint256).max, ids);
            creditLive = true;
        }
    }

    /// @dev Legacy fixture only. Canonical relief is paid through _reduce.
    function _collectRelief() internal {
        if (netAccounting) return; // Actual relief requires principal retirement.
        int256 s0 = _bh().signedBalanceOf(address(this));
        if (s0 >= 0 || _bh().reliefOf(address(this)) == 0) return;
        _bh().settleRelief();
    }

    /// @notice Canonical cash and principal coexist and are observed separately.
    ///         The legacy fixture alone nets them into one signed account.
    function _syncAccount() internal {
        if (netAccounting) {
            monetaryBuckHeld = IBuckNetAccount(address(buck)).netBalanceOf(address(this));
            uint256 principal = _principal();
            monetaryOutstanding = int256(principal);
            creditLive = principal != 0;
            return;
        }
        int256 s = _bh().signedBalanceOf(address(this));
        monetaryBuckHeld = s > 0 ? uint256(s) : 0;
        monetaryOutstanding = s < 0 ? -s : int256(0);
    }

    function refresh() external financialLock {
        _collectRelief();
        _syncAccount();
        _mark();
    }

    function netInventory() public view override returns (int256) {
        if (netAccounting) {
            uint256 cash = IBuckNetAccount(address(buck)).netBalanceOf(address(this));
            uint256 principal = _principal();
            return int256(cash) - int256(principal);
        }
        return _bh().signedBalanceOf(address(this));
    }

    function monetaryDeviationOffset() public view override returns (int256) {
        if (!netAccounting) return super.monetaryDeviationOffset();
        return int256(IBuckNetAccount(address(buck)).netBalanceOf(address(this))) - int256(monetaryTokenValue());
    }

    function capacity() public view override returns (uint256) {
        if (turnoverRemaining == 0) return 0;
        try this.collateralValue() returns (uint256 value) {
            if (value <= capitalFloor) return 0;
        } catch { return 0; }
        if (!netAccounting) return super.capacity();
        if (!opsParams.enabled) return 0;
        uint256 nav = _deskNavSafe();
        uint256 cash = IBuckNetAccount(address(buck)).netBalanceOf(address(this));
        uint256 principal = _principal();
        return Math.min(_room(nav * opsParams.maxPositionBp / 10000, cash),
                        _room(nav * opsParams.maxOutrightBp / 10000, principal));
    }

    /// @notice The credit-holder implementation replaces the legacy mint/burn
    ///         quadrants. Buy receipts retire only debt that actually exists;
    ///         positive receipts are held even when the policy is persistent.
    ///         Sell held BUCK first; draw credit only on persistent advice.
    function monetaryOperation() external override financialLock returns (uint8 quadrant) {
        if (netAccounting) return _netOperation();
        return _legacyOperation();
    }

    function _legacyOperation() internal returns (uint8 quadrant) {
        OpsParams memory op = opsParams;
        if (!op.enabled || turnoverRemaining == 0) revert MonetaryIdle();
        address dir = _monDir();
        if (dir == address(0)) revert DirectorUnset();
        uint32 epoch = IDeskPolicy(dir).epochNow();
        if (lastMonetaryEpoch == epoch + 1) revert StepAlreadyDone();
        (int32 effort, bool outright) = IDeskPolicy(dir).monetaryEffort();
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
            && !IDeskPolicy(executionGuard).executionAllowed(effort)) revert MonetaryBound();
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

    /// @dev Preconditions: ordinary verified desk, self-insured zero-premium
    ///      NFT, current K/marks and the financial lock. Post: one bounded
    ///      swap programme, with actual cash/principal observations and no
    ///      worse actual K shortfall. New currency is issued only immediately
    ///      before its sale. A purchase against debt performs a real reduction;
    ///      any liquidity/refund/market/postcondition failure rolls back all
    ///      swaps, fees, issuance, reduction, epochs and turnover together.
    function _netOperation() internal returns (uint8 quadrant) {
        OpsParams memory op = opsParams;
        if (!op.enabled || turnoverRemaining == 0) revert MonetaryIdle();
        address dir = _monDir();
        if (dir == address(0)) revert DirectorUnset();
        uint32 epoch = IDeskPolicy(dir).epochNow();
        if (lastMonetaryEpoch == epoch + 1) revert StepAlreadyDone();
        (int32 effort, bool outright) = IDeskPolicy(dir).monetaryEffort();
        if (effort == 0) revert NoAdvice();
        controller.compute();
        _syncAccount();
        _mark();
        uint256 openingShortfall = _shortfall();
        if (collateralValue() < capitalFloor) revert MonetaryBound();
        _activateBacking();
        uint256 nav = _deskNav();
        uint256 positionCap_ = Math.mulDiv(nav, op.maxPositionBp, 10000);
        uint256 debtCap = Math.mulDiv(nav, op.maxOutrightBp, 10000);
        bool selling = effort > 0;
        uint256 room;
        if (selling) {
            if (monetaryBuckHeld != 0) { room = monetaryBuckHeld; quadrant = 3; }
            else {
                if (!outright) revert MonetaryIdle();
                uint256 debt = uint256(monetaryOutstanding);
                room = Math.min(debtCap > debt ? debtCap - debt : 0, _issuable());
                quadrant = 4;
            }
        } else {
            room = uint256(monetaryOutstanding)
                 + (positionCap_ > monetaryBuckHeld ? positionCap_ - monetaryBuckHeld : 0);
            quadrant = monetaryOutstanding != 0 ? 2 : 1;
        }
        uint256 effort_ = uint256(effort < 0 ? -int256(effort) : int256(effort));
        uint256 size = Math.min(Math.mulDiv(nav, effort_, 10000), Math.min(room, turnoverRemaining));
        if (size == 0) revert MonetaryBound();
        lastMonetaryEpoch = epoch + 1;
        (uint256 cost, uint256 bought, uint256 issued) = _netTrade(selling, quadrant == 4, size, op);
        if (cost == 0 || (!selling && bought / Q == 0)) revert MonetaryIdle();
        if (executionGuard != address(0)
            && !IDeskPolicy(executionGuard).executionAllowed(effort)) revert MonetaryBound();
        turnoverRemaining -= cost;
        uint256 retired;
        if (quadrant == 2) {
            // Retire at most the newly acquired whole BUCK principal. Actual
            // marked relief remains cash, not a second unrecorded retirement.
            retired = Math.min(_principal(), bought / Q);
            if (retired == 0) revert MonetaryIdle();
            _reduce(retired, retired, 0);
        }
        _syncAccount();
        _mark();
        if ((!selling && monetaryBuckHeld > positionCap_)
            || (quadrant == 4 && uint256(monetaryOutstanding) > debtCap)
            || collateralValue() < capitalFloor) revert MonetaryBound();
        if (_shortfall() > openingShortfall) revert CreditShortfallIncreased();
        uint256 actual = quadrant == 2 ? retired : quadrant == 4 ? issued : selling ? cost : bought / Q;
        emit MonetaryOperation(quadrant, effort, outright, actual, monetaryOutstanding, monetaryBuckHeld);
    }

    /// @notice Settle exact principal using the existing liquid Jubilee fund.
    /// @dev Governor/wheel only; no swap or LP redemption. Max cost is raw
    ///      BUCK, minimum relief is scaled net BUCK. The guard excludes trading
    ///      and refresh callbacks but allows complete read-only valuations.
    function retireCredit(uint256 principal, uint256 maxCashCost, uint256 minimumReliefScaled)
        external financialLock returns (uint256 reliefScaled, uint256 cashSpentScaled)
    {
        if (msg.sender != governance && msg.sender != wheel) revert NotWheel();
        if (!netAccounting || address(credit) == address(0)) revert NetAccountingRequired();
        return _reduce(principal, maxCashCost, minimumReliefScaled);
    }

    function _reduce(uint256 principal, uint256 maxCashCost, uint256 minimumReliefScaled)
        internal returns (uint256 reliefScaled, uint256 cashSpentScaled)
    {
        if (principal == 0) revert Amount0();
        controller.compute();
        _mark();
        uint256 openingShortfall = _shortfall();
        IBuckNetAccount token = IBuckNetAccount(address(buck));
        (, uint256 beforeNet) = token.accountBalancesScaled(address(this));
        token.authorizeReduction(creditId, principal, minimumReliefScaled, uint64(block.timestamp));
        (reliefScaled,) = IBasketCreditFund(token.jubilee()).reduce(creditId, principal);
        (, uint256 afterNet) = token.accountBalancesScaled(address(this));
        cashSpentScaled = beforeNet > afterNet ? beforeNet - afterNet : 0;
        if (Math.ceilDiv(cashSpentScaled, Q) > maxCashCost) revert CreditCashLimit();
        _syncAccount();
        _mark();
        if (_shortfall() > openingShortfall) revert CreditShortfallIncreased();
        emit CreditRetired(creditId, principal, reliefScaled, cashSpentScaled);
    }

    function _kLimit() private view returns (uint256) {
        if (address(credit) == address(0)) return 0;
        uint256 value = netAccounting ? credit.currentValue(creditId) : credit.markOf(creditId);
        return Math.mulDiv(IBuckKSource(address(controller)).currentBuckK(), value, 1e18);
    }

    function _shortfall() private view returns (uint256) {
        uint256 principal = _principal();
        uint256 limit = _kLimit();
        return principal > limit ? principal - limit : 0;
    }

    /// @dev Pure backing activation never creates cash or claim age. Mature
    ///      facilities and a pending maturity book cannot acquire new backing.
    function _activateBacking() private {
        if (address(credit) == address(0)) return;
        IBuckNetAccount token = IBuckNetAccount(address(buck));
        if (token.maturitiesPending()) return;
        (uint256 principal, uint256 age) = credit.issuanceOf(creditId);
        if (principal != 0 && age == principal * T) return;
        (uint256 face, uint256 active, uint32 rate) = credit.creditInfo(creditId);
        if (rate != 0) revert InvalidNetAccounting();
        if (face != active && credit.markOf(creditId) != 0) token.issue(creditId, face - active, 0, 0, address(0));
    }

    /// @dev Full free backing is activated before funding. Unpaid claims are
    ///      excluded, and principal/account/supply/maturity bounds all apply.
    function _issuable() private view returns (uint256 room) {
        if (address(credit) == address(0)) return 0;
        IBuckNetAccount token = IBuckNetAccount(address(buck));
        if (token.maturitiesPending()) return 0;
        (uint256 principal, uint256 age) = credit.issuanceOf(creditId);
        if (principal != 0 && age == principal * T) return 0;
        uint256 limit = _kLimit();
        if (principal >= limit) return 0;
        room = Math.min(limit - principal, BuckTypes.MAX_BALANCE - principal);
        (uint256 gross,) = token.accountBalancesScaled(address(this));
        room = Math.min(room, (BuckTypes.MAX_BALANCE * Q - gross) / Q);
        room = Math.min(room, type(uint256).max / Q - token.totalIssued());
    }

    function _fundNet(uint256 desired) private returns (uint256 funded, uint256 issued) {
        _mark();
        IBuckNetAccount token = IBuckNetAccount(address(buck));
        uint256 cash = token.netBalanceOf(address(this));
        funded = Math.min(desired, cash + _issuable());
        issued = funded > cash ? funded - cash : 0;
        if (issued != 0) token.issue(creditId, 0, issued, 0, address(0));
    }

    /// @dev Net receipts are measured independently of nominal V3 returns.
    ///      Each new draw must be entirely sold; partial execution reverts
    ///      rather than silently leaving newly issued surplus cash/debt.
    function _netTrade(bool sellBuck, bool mayIssue, uint256 size, OpsParams memory op)
        private returns (uint256 cost, uint256 bought, uint256 issued)
    {
        uint256 n = constituents.length;
        if (n == 0) return (0, 0, 0);
        uint256 share = size / n;
        IBuckNetAccount token = IBuckNetAccount(address(buck));
        for (uint256 i; i < n; ++i) {
            IBuckBasketVenue.Marks memory m = _venueMarks(i);
            uint256 want = Math.min(share, Math.mulDiv(m.depth, op.maxLegBp, 10000));
            if (want == 0 || m.pHigh == 0) continue;
            uint256 draw;
            if (sellBuck && mayIssue) (want, draw) = _fundNet(want);
            if (want == 0) continue;
            IERC20 asset = IERC20(constituents[i].token);
            uint256 one = 10 ** constituents[i].decimals;
            uint256 input = sellBuck ? want : Math.min(monetaryTokenHeld[i], Math.mulDiv(want, one, m.pHigh));
            if (input == 0) continue;
            uint256 beforeToken = asset.balanceOf(address(this));
            (, uint256 beforeNet) = token.accountBalancesScaled(address(this));
            (uint256 spent, uint256 received) = _dv().monetaryLeg(i, sellBuck, input);
            uint256 afterToken = asset.balanceOf(address(this));
            (, uint256 afterNet) = token.accountBalancesScaled(address(this));
            if (sellBuck) {
                if (spent > input || beforeNet < afterNet || beforeNet - afterNet != spent * Q
                    || afterToken < beforeToken || afterToken - beforeToken != received
                    || (draw != 0 && spent != input)) revert InconsistentExecution();
                monetaryTokenHeld[i] += received;
                cost += spent;
                issued += draw;
            } else {
                if (spent > input || afterToken > beforeToken || beforeToken - afterToken != spent
                    || afterNet <= beforeNet) revert InconsistentExecution();
                monetaryTokenHeld[i] -= spent;
                bought += afterNet - beforeNet;
                cost += Math.mulDiv(spent, m.pHigh, one, Math.Rounding.Ceil);
            }
        }
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
        assembly ("memory-safe") {
            let ptr := mload(0x40)
            calldatacopy(ptr, 0, calldatasize())
            let ok := delegatecall(gas(), v, ptr, calldatasize(), 0, 0)
            returndatacopy(ptr, 0, returndatasize())
            switch ok
            case 0 { revert(ptr, returndatasize()) }
            default { return(ptr, returndatasize()) }
        }
    }
}
