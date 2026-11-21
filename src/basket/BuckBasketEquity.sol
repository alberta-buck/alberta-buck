// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20}             from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20}          from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {IBuckKController}   from "../IBuckKController.sol";
import {BuckBasketReceipt}  from "./BuckBasketReceipt.sol";
import {IBuckBasketVenue}   from "./IBuckBasketVenue.sol";
import {IUniswapV3Factory, IBuckMintBurn} from "./BuckBasketStorage.sol";
import {BuckBasketEquityStorage, IMarkedCredit, IBuckNetAccount, IBasketCreditFund} from "./BuckBasketEquityStorage.sol";
import {PortfolioMath} from "./PortfolioMath.sol";
import {IBasketUpkeep} from "./IBasketUpkeep.sol";
import {IBuckBasketPosition} from "./IBuckBasketPosition.sol";
import {IBasketArb} from "./IBasketArb.sol";
import {IBuckFlashBorrower} from "../IBuckFlashBorrower.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BasketMarketOps} from "./BasketMarketOps.sol";
import {IBuckNetView} from "../IBuckNetView.sol";


/// @title BuckBasketEquity -- the BuckBasket as equity: shares, one pooled
///        lien, BUCK payouts, a wallet the work wheel places.
///
/// @notice (alberta-buck-ethereum.org, "BuckBasketEquity: the Basket as a Credit Holder".)
///
///         Canonical snapshots distinguish net cash, actual NFT principal
///         and a fee-aware marked Jubilee claim. LP prices include the claim;
///         new-credit collateral excludes it. Canonical credit opening checks
///         the public service identity, Carrying issuer approval where needed,
///         token model and matching credit NFT.
///         Canonical spending/repayment is explicit, and an exit may not make the
///         K shortfall of its activated backing larger. The implicit leveraged
///         flow described below is the legacy fixture path alone.
///
///         The canonical basket is an approved Carrying service. Nominal
///         ERC-20 flow is separate from net cash/ROI/collateral and payouts.
///         It owns one self-issued MARKED
///         BuckCredit (`openCredit`), marked at its equity before it spends,
///         so its limit is K x equity and Buck enforces it.  Its debt is its
///         lien, which earns Jubilee relief like any other.
///
///         A DEPOSIT of TOKEN or BUCK is equity valued in BUCK: at the lower of
///         its pool's spot and TWAP, against the basket at the higher of each
///         pool's, less the pool fee on the swap the wheel will make to place
///         it.  It buys shares, and raises the mark: K x its value of new
///         credit, unless the basket is under water, when it restores the
///         limit first (an under-water account stops issuing).  The asset
///         lands in the WALLET; the wheel's components (the facet) place it,
///         spending the credit.
///
///         A REDEMPTION gives the treasury 25% of the receipt's gain over its
///         cost basis (as shares), and pays the rest in BUCK (owner, 2026-11-14):
///         its value at the exiter's marks (each pool at the lower of spot and
///         TWAP), less the same charge, from the wallet when it can pay that and
///         still fund the principal the exit may have to retire.  Otherwise its
///         fraction of every position and of the wallet's TOKEN is withdrawn and
///         the TOKEN sold through each constituent's own pool
///         (`BasketMarketOps.exitAssets`): the exiter bears the slippage, bounded
///         by its minimum, and the displaced price is the wheel's arbitrage for
///         the holders who stay.  It is paid what its positions released and its
///         TOKEN fetched less its share of the lien; sold short of that share it
///         cannot leave (`Underwater`).  `redeemInKind` delivers the TOKEN by
///         transfer instead, leaving TOKEN behind for its lien share.
///
///         An exit is never refused for being under water.  The TOKEN back the
///         lien, so after the exit the basket retires as much principal as keeps
///         its K shortfall from growing (none while it has room), from the debt
///         share the exiter left as cash; the fund may refuse a retirement larger
///         than its relief liquidity, and the exit then reverts so a smaller one
///         may follow.
///
///         No margin calls: a K cut lowers the limit, never the lien.
///
/// # Dispatch
///
///         Two delegatecall facets over the shared storage: the components
///         (`equityWheel`: wheelDue / wheelStep) and the AMM venue (every
///         other selector, the V3 callbacks included), as the pro-rata shell
///         does.
contract BuckBasketEquity is BuckBasketEquityStorage, IBuckBasketPosition {
    using SafeERC20 for IERC20;

    error PositionReentrant();
    error PositionAssetMismatch();
    error PositionOverflow();
    error PositionSharesExceeded();
    error NetCreditUnavailable();
    error InvalidNetToken();
    error CreditCashLimit();

    event PositionIncreased(address indexed who, uint256 indexed receiptId, address indexed asset,
                            uint256 nominalAmount, uint256 received, uint256 sharesAdded);
    event PositionReduced(address indexed who, uint256 indexed receiptId, uint256 requestedShares,
                          uint256 paid, uint256 sharesRetired, uint256 sharesRemaining);
    event NetAccountingEnabled();
    event CreditRetired(uint256 indexed tokenId, uint256 principal,
                        uint256 reliefScaled, uint256 cashSpentScaled);

    modifier positionLock() {
        if (_positionEntered || _creditEntered) revert PositionReentrant();
        _positionEntered = true;
        _;
        _positionEntered = false;
    }

    /// @dev A credit operation runs outside the position lock. Both locks
    ///      exclude each other and reject mutating upkeep callbacks; the
    ///      fund may still read the complete, unchanged position inventory.
    modifier creditLock() {
        if (_positionEntered || _creditEntered) revert PositionReentrant();
        _creditEntered = true;
        _;
        _creditEntered = false;
    }

    constructor(
        address _buck,
        address _controller,
        address _v3Factory,
        address _governance,
        uint24  _defaultFeeTier,
        uint32  _twapWindow,
        uint16  _observationCardinality,
        uint256 _defaultMaxDeviationBp,
        uint256 _minSeedLiquidity
    ) {
        require(_buck != address(0) && _controller != address(0)
                && _v3Factory != address(0) && _governance != address(0), "zero addr");
        buck                   = IBuckMintBurn(_buck);
        controller             = IBuckKController(_controller);
        v3Factory              = IUniswapV3Factory(_v3Factory);
        governance             = _governance;
        defaultFeeTier         = _defaultFeeTier;
        twapWindow             = _twapWindow;
        observationCardinality = _observationCardinality;
        defaultMaxDeviationBp  = _defaultMaxDeviationBp;
        minSeedLiquidity       = _minSeedLiquidity;
        eq = EquityParams({floorBp: 100, flowZx100: 200, bandBp: 5000, parkBp: 30000,
                           flowDays: 30, stepBp: 500, weightBandBp: 200, grainPpm: 100,
                           exitFeeBp: 0, swapCapBp: 100, ceilBp: 2000});
        refreshFloorBp = 9900;
        refreshTopBp = 9950;
        response = ResponseParams({leashBp: 3000, interval: 1800, tiltBp: 0, leanCapBp: 0});
        receipt = new BuckBasketReceipt(address(this));
    }

    function _venue() internal view returns (IBuckBasketVenue) {
        return IBuckBasketVenue(address(this));
    }

    // --- Governance ------------------------------------------------------- //

    function setGovernance(address g) external onlyGov {
        if (g == address(0)) revert Gov0();
        governance = g;
    }

    function setVenue(address v) external onlyGov {
        venue = IBuckBasketVenue(v);
        emit VenueSet(v);
    }

    function setEquityWheel(address w) external onlyGov { equityWheel = w; }

    event ArbitrageCreditorSet(address indexed creditor);

    function setArbitrageCreditor(address source) external onlyGov {
        arbitrageCreditor = source;
        emit ArbitrageCreditorSet(source);
    }

    event ResponseConfigured(ResponseParams params);

    /// @dev A target never leans as far as the leash, so the safeguard that
    ///      keeps a constituent inside its leash cannot fight the lean.
    function setRefreshBand(uint16 floorBp, uint16 topBp) external onlyGov {
        require(floorBp <= topBp && topBp <= 10000, "refresh band");
        refreshFloorBp = floorBp;
        refreshTopBp = topBp;
    }

    function setResponse(ResponseParams calldata p) external onlyGov {
        require(p.leashBp > 0 && p.leashBp <= 10000 && p.interval > 0
                && p.tiltBp <= 10000 && p.leanCapBp < p.leashBp, "response params");
        response = p;
        emit ResponseConfigured(p);
    }

    event NetMarkSet(bool on);

    /// @notice Mark the credit so that K bounds the lien net of the BUCKs the
    ///         basket still holds in its positions (on), or the whole lien
    ///         (off, as it was made). The next verb or wheel step re-marks.
    function setNetMark(bool on) external onlyGov {
        netMark = on;
        emit NetMarkSet(on);
    }

    event ReferencePriceSet(uint256 indexed i, uint256 priceInBuck);

    /// @notice Set the price constituent i is taken to return to, in BUCK
    ///         units per whole TOKEN as the venue quotes it; zero restores
    ///         its anchor price. It moves rebalancing targets only: the
    ///         reference basket K steers by keeps its quantities.
    function setReferencePrice(uint256 i, uint256 priceInBuck) external onlyGov {
        require(i < constituents.length, "constituent");
        referencePrice[i] = priceInBuck;
        emit ReferencePriceSet(i, priceInBuck);
    }

    function setEquityParams(EquityParams calldata p) external onlyGov {
        if (p.bandBp >= 10000 || p.stepBp == 0 || p.stepBp > 10000
            || p.exitFeeBp > 1000 || p.flowDays == 0 || p.swapCapBp == 0
            || p.swapCapBp > 1000 || p.ceilBp < p.floorBp || p.ceilBp > 10000) revert Bp10000();
        eq = p;
    }

    /// @notice Select the experimental net-token integration before any
    ///         deposits or credit are opened. This mode can invest existing
    ///         BUCK. Credit opening additionally validates the canonical model,
    ///         public service identity and credit binding. Draws are explicit.
    function enableNetAccounting() external onlyGov {
        if (netAccounting || totalShares != 0 || address(credit) != address(0)) revert AlreadyPresent();
        (bool ok, bytes memory data) = address(buck).staticcall(
            abi.encodeWithSelector(IBuckNetView.netBalanceOf.selector, address(this))
        );
        if (!ok || data.length != 32) revert InvalidNetToken();
        netAccounting = true;
        emit NetAccountingEnabled();
    }

    /// @notice Open the basket's credit: a MARKED BuckCredit it issues itself
    ///         (the basket is its own insurer; zero premium), whose face caps
    ///         all it may ever issue at K x face.  Once, by governance, with
    ///         the basket bound as a public, non-Carrying identity.  It is
    ///         activated at the first mark above zero.
    function openCredit(address credit_, uint256 face) external onlyGov {
        _openCredit(credit_, face);
    }

    function _openCredit(address credit_, uint256 face) internal {
        IBasketUpkeep(address(this)).initializeEquityCredit(credit_, face);
    }

    /// @notice Register a constituent (the pro-rata shell's rule: declared
    ///         weights renormalized to 10000, each existing constituent's price
    ///         kept).
    function addBasketToken(address token, uint8 decimals, uint256 initialPriceInBuck,
                            uint256 weightBp, uint24 feeTier)
        external onlyGov returns (address pool)
    {
        return _venue().registerToken(token, decimals, initialPriceInBuck, weightBp, feeTier);
    }

    function constituentsLength() external view returns (uint256) {
        return constituents.length;
    }

    // --- Deposit ------------------------------------------------------------ //

    /// @notice The pro-rata shell's name and arguments (the sim's agents call
    ///         it); the deviation bound is the basket's own guard.
    function depositToken(address token, uint256 amount, uint256)
        external returns (uint256)
    {
        return deposit(token, amount, 0);
    }

    function deposit(address asset, uint256 amount, uint256 minShares)
        public override positionLock returns (uint256 id)
    {
        (id,,) = _deposit(0, asset, amount, 0, minShares);
    }

    /// @notice Add to an existing receipt without creating another NFT.
    /// @dev The same original asset is required so the receipt metadata has
    ///      one unambiguous TOKEN principal. BUCK is credited at its increase
    ///      in the basket's net equity, not the sender's nominal amount.
    function increasePosition(uint256 id, address asset, uint256 amount, uint256 minReceived, uint256 minShares)
        external override positionLock returns (uint256 positionId, uint256 received, uint256 sharesAdded)
    {
        if (id == 0) revert EmptyDeposit();
        return _deposit(id, asset, amount, minReceived, minShares);
    }

    function _deposit(uint256 id, address asset, uint256 amount, uint256 minReceived, uint256 minShares)
        internal returns (uint256 positionId, uint256 received, uint256 shares)
    {
        if (amount == 0) revert Amount0();
        bool existing = id != 0;
        if (existing) {
            if (receipt.ownerOf(id) != msg.sender) revert NotOwner();
            if (deposits[id].token != asset) revert PositionAssetMismatch();
        }
        uint256 idx = type(uint256).max;
        if (asset != address(buck)) {
            idx = indexOf[asset];
            if (idx == 0) revert NotInBasket();
            idx -= 1;
        }
        Snap memory sn = _snap();

        // One pass over the pools: the guard, the deposit's value, the charge,
        // and the basket at the incumbents' (high) marks.
        uint256 n = constituents.length;
        uint256 feeMax = 0;
        for (uint256 i = 0; i < n; i++) {
            IBuckBasketVenue.Marks memory m = sn.m[i];
            if (defaultMaxDeviationBp > 0 && m.pLow > 0
                && (m.pHigh - m.pLow) * 10000 > m.pLow * defaultMaxDeviationBp) revert Slippage();
            if (constituents[i].feeTier > feeMax) feeMax = constituents[i].feeTier;
        }

        uint256 k = sn.k;
        uint256 charge = asset == address(buck)
            ? feeMax * 1e12 * (1e18 + k) / 2e18
            : (k < 1e18 ? uint256(constituents[idx].feeTier) * 1e12 * (1e18 - k) / 2e18 : 0);
        uint256 S = totalShares;
        uint256 eHigh = _equityS(sn, MARK_HIGH);
        // A live, zero-equity basket has no defined fair entry price. Issuing
        // one share per BUCK would give its existing worthless shares a
        // windfall at the new depositor's expense.
        if (S != 0 && eHigh == 0) revert Underwater();
        if (asset == address(buck)) {
            IERC20(asset).safeTransferFrom(msg.sender, address(this), amount);
            if (netAccounting) {
                // A deposit can revalue the mutual claim. Credit only cash
                // actually delivered; reprice the claim after issuing shares.
                uint256 cash = IBuckNetAccount(address(buck)).netBalanceOf(address(this));
                if (cash <= sn.cash) revert NoValue();
                received = cash - sn.cash;
            } else {
                int256 beforeBuckEquity = sn.buckEq;
                _account(sn);
                if (sn.buckEq <= beforeBuckEquity) revert NoValue();
                received = uint256(sn.buckEq - beforeBuckEquity);
            }
        } else {
            uint256 beforeBalance = IERC20(asset).balanceOf(address(this));
            IERC20(asset).safeTransferFrom(msg.sender, address(this), amount);
            received = IERC20(asset).balanceOf(address(this)) - beforeBalance;
            idleToken[idx] += received;
        }
        if (received == 0 || received > amount) revert NoValue();
        if (received < minReceived) revert MinOut();
        uint256 value = asset == address(buck)
            ? received : received * sn.m[idx].pLow / (10 ** constituents[idx].decimals);
        if (value == 0) revert NoValue();
        uint256 net = value * (1e18 - charge) / 1e18;
        shares = S == 0 ? net : net * S / eHigh;
        if (shares == 0 || shares < minShares) revert MinOut();

        if (!existing) id = receipt.nextId();
        Holding storage h = holdings[id];
        if (shares > type(uint128).max - uint256(h.shares)
            || value > type(uint128).max - uint256(h.basis)) revert PositionOverflow();
        h.shares += uint128(shares);
        h.basis += uint128(value);
        Deposit storage d = deposits[id];
        d.buckPrincipal = h.basis;
        if (!existing) {
            d.token = asset;
            d.depositTime = uint64(block.timestamp);
        }
        if (asset != address(buck)) d.tokenPrincipal += received;
        totalShares = S + shares;
        dayFlow += int256(value);
        _account(sn);
        _markS(sn);                                     // the deposit's K, as credit
        if (!existing) assert(receipt.mint(msg.sender) == id);
        // The pro-rata shells' event (the sim reads it): buckMinted is the
        // credit the deposit brings, K x its value.
        emit Deposited(msg.sender, id, asset, amount, k * value / 1e18, 0);
        if (existing) emit PositionIncreased(msg.sender, id, asset, amount, received, shares);
        return (id, received, shares);
    }

    // --- Redemption ----------------------------------------------------------- //

    function redeem(uint256 id, uint256 bp) external returns (uint256) {
        return redeem(id, bp, 0);
    }

    /// @notice Redeem `bp` of receipt `id` (0: all of it, the pro-rata shells'
    ///         convention); revert unless at least `minBuck` is paid.
    function redeem(uint256 id, uint256 bp, uint256 minBuck) public returns (uint256 paid) {
        if (receipt.ownerOf(id) != msg.sender) revert NotOwner();
        if (bp == 0) bp = 10000;
        if (bp > 10000) revert Bp10000();
        Holding storage h = holdings[id];
        uint256 shares = uint256(h.shares) * bp / 10000;
        if (shares == 0) revert RedeemZero();
        uint256 basis = uint256(h.basis) * bp / 10000;

        (paid,,) = _redeemShares(id, h, shares, basis, minBuck, false);
    }

    /// @notice Redeem exactly `shares`; unlike basis-point redemption, zero
    ///         never denotes a full exit. Returns zero ID only if burned.
    function redeemShares(uint256 id, uint256 shares, uint256 minBuck)
        external override returns (uint256, uint256, uint256, uint256)
    {
        return _redeemExact(id, shares, minBuck, false);
    }

    /// @notice The same exit, delivered in kind on the holder's request.
    function redeemInKind(uint256 id, uint256 shares)
        external override returns (uint256, uint256, uint256, uint256)
    {
        return _redeemExact(id, shares, 0, true);
    }

    function _redeemExact(uint256 id, uint256 shares, uint256 minBuck, bool inKind)
        internal returns (uint256 positionId, uint256 paid, uint256 sharesRetired, uint256 sharesRemaining)
    {
        if (receipt.ownerOf(id) != msg.sender) revert NotOwner();
        Holding storage h = holdings[id];
        uint256 priorShares = h.shares;
        if (shares == 0) revert RedeemZero();
        if (shares > priorShares) revert PositionSharesExceeded();
        uint256 basis = uint256(h.basis) * shares / priorShares;
        (paid, sharesRetired, sharesRemaining) = _redeemShares(id, h, shares, basis, minBuck, inKind);
        positionId = sharesRemaining == 0 ? 0 : id;
        emit PositionReduced(msg.sender, id, shares, paid, sharesRetired, sharesRemaining);
    }

    /// @dev The exit runs under the position lock; the principal it must retire to hold the
    ///      basket's K shortfall is retired after the lock is released (a credit operation, which
    ///      the fund values against the complete, settled inventory).
    function _redeemShares(uint256 id, Holding storage h, uint256 shares, uint256 basis, uint256 minBuck,
                           bool inKind) internal returns (uint256 paid, uint256 retired, uint256 remaining)
    {
        if (_positionEntered || _creditEntered) revert PositionReentrant();
        _positionEntered = true;
        (Snap memory sn, uint256 before) = _exitSnapshot();
        (uint256 cut, uint256 cutValue) = _treasuryCut(sn, shares, basis);
        uint256 left;
        (paid, left) = _exit(shares - cut, sn, msg.sender, inKind);
        uint256 remainingBp = _retire(id, h, shares, left, basis);
        if (paid < minBuck) revert MinOut();
        _positionEntered = false;
        // The fund's own receipt may have changed: the mark below values its final shares.
        _holdShortfall(before);
        // The pro-rata shells' event: nothing burned, BUCK paid, the cut's value.
        emit Redeemed(msg.sender, id, 0, paid, cutValue, remainingBp);
        return (paid, shares - left, h.shares);
    }

    /// @dev The treasury's cut: LAMBDA of the gain over the cost basis, in
    ///      shares; returns them and their value.
    function _treasuryCut(Snap memory sn, uint256 shares, uint256 basis)
        internal returns (uint256 cut, uint256 cutValue)
    {
        uint256 S = totalShares;
        uint256 e = _equityS(sn, MARK_TWAP);
        uint256 worth = shares * e / S;
        cut = worth > basis ? (worth - basis) * LAMBDA_BP / 10000 * S / e : 0;
        treasuryShares += cut;
        cutValue = cut * e / S;
    }

    /// @dev Take `shares` off the holding, less the `left` an under-water
    ///      basket could not pay in BUCK; returns what remains, in bp.
    function _retire(uint256 id, Holding storage h, uint256 shares, uint256 left, uint256 basis)
        internal returns (uint256 remainingBp)
    {
        uint256 gone = shares - left;
        uint256 priorShares = h.shares;
        h.shares -= uint128(gone);
        h.basis -= uint128(basis * gone / shares);
        Deposit storage d = deposits[id];
        d.buckPrincipal = h.basis;
        d.tokenPrincipal -= d.tokenPrincipal * gone / priorShares;
        remainingBp = h.shares == 0 ? 0 : uint256(h.shares) * 10000 / (uint256(h.shares) + gone);
        if (h.shares == 0) receipt.burn(id);
    }

    /// @notice The treasury's shares leave by the same door, with no cut.
    function redeemTreasury(uint256 bp, address to) external onlyGov returns (uint256 paid) {
        if (to == address(0)) revert To0();
        uint256 out = treasuryShares * bp / 10000;
        if (out == 0) revert RedeemZero();
        if (_positionEntered || _creditEntered) revert PositionReentrant();
        _positionEntered = true;
        (Snap memory sn, uint256 before) = _exitSnapshot();
        treasuryShares -= out;
        uint256 left;
        (paid, left) = _exit(out, sn, to, false);
        treasuryShares += left;
        _positionEntered = false;
        _holdShortfall(before);
    }

    /// @dev Pre: position lock held. Fresh K and complete inventory; returns the
    ///      basket's K shortfall (lien less K x bare collateral) before the exit.
    ///      An exit is never refused for being under water: it may not make
    ///      the shortfall larger (`_holdShortfall`).
    function _exitSnapshot() internal returns (Snap memory s, uint256 shortfall) {
        if (netAccounting) controller.compute();
        s = _snap();
        shortfall = _shortfallS(s);
    }

    /// @dev Post-exit, outside the position lock: retire the principal by which
    ///      the exit made the shortfall larger (from the basket's cash: the debt
    ///      share the leaver left), then require it no larger and mark afresh.
    ///      The fund may refuse a retirement larger than its relief liquidity:
    ///      the exit then reverts, and a smaller one may follow.
    function _holdShortfall(uint256 before) internal {
        if (!netAccounting) return;
        Snap memory s = _snap();
        uint256 grown = _shortfallS(s);
        if (grown > before) {
            this.retireCredit(grown - before, type(uint256).max, 0);
            s = _snap();
            if (_shortfallS(s) > before) revert CreditShortfallIncreased();
        }
        _markS(s);
    }

    /// @dev Retire `out` shares, paying `to`.  In BUCK from cash when the
    ///      account can spend it and still fund the principal the exit may
    ///      have to retire (at most K x the payment); else from its share of
    ///      the positions (`_exitInKind`): sold for BUCK, or delivered in kind
    ///      on request.  The exit fee (the stress fee's hook) retires the
    ///      shares but leaves its fraction of the assets.  Returns the BUCK
    ///      paid and the shares NOT retired.
    function _exit(uint256 out, Snap memory sn, address to, bool inKind)
        internal returns (uint256 pay, uint256 left)
    {
        uint256 S = totalShares;
        uint256 fWad = (out - out * eq.exitFeeBp / 10000) * 1e18 / S;
        uint256 eLow = _equityS(sn, MARK_LOW);
        uint256 value = eLow * fWad / 1e18;
        if (value == 0) revert Underwater();
        pay = value * (1e18 - _chargeBuck(sn.k)) / 1e18;

        // Legacy Buck constrains an implicit draw through this prospective
        // mark. Canonical Buck spends cash only; its final actual collateral
        // and unchanged principal are checked after receipt retirement.
        if (!netAccounting) _markAt(_creditValueS(sn, eLow - pay));
        if (!inKind && pay + (netAccounting ? pay * sn.k / 1e18 : 0) <= _cashAvailable()) {
            totalShares = S - out;
            _payCash(to, pay);
            dayFlow -= int256(pay);
            return (pay, 0);
        }
        (pay, left) = _exitInKind(out, fWad, sn, to, inKind || !netAccounting);
    }

    /// @dev The pro-rata exit, in kind: fraction `fWad` of every position and
    ///      of the wallet's TOKEN goes to `to` by transfer -- no swap, no
    ///      market moved. Canonical LP proceeds remain cash, never automatic
    ///      repayment. Net equity is paid by retaining TOKEN for a negative
    ///      BUCK residue (released cash + its fraction of cash-principal+claim).
    ///      Actual principal stays fixed; _exitSnapshot guards final backing.
    ///      Legacy Buck alone treats proceeds as implicit repayment. Corners:
    ///        * short of its lien share (leverage above one), it leaves TOKEN
    ///          behind worth the shortfall, at the low marks;
    ///        * legacy: past what an under-water account can spend, the rest stays in
    ///          the receipt as shares at the price that remains -- the exit
    ///          completes, and nobody's claim moves ahead of anybody's.
    function _exitInKind(uint256 out, uint256 fWad, Snap memory sn, address to, bool inKind)
        internal returns (uint256 pay, uint256 left)
    {
        uint256 S = totalShares;
        (int256 residue, uint256 flow) = BasketMarketOps.exitAssets(idleToken, liquidityOf, address(buck),
            netAccounting, fWad, sn.buckEq * int256(fWad) / 1e18, to, inKind, constituents.length);
        if (residue > 0) {
            uint256 spend = _cashAvailable();
            pay = uint256(residue) < spend ? uint256(residue) : spend;
            uint256 unpaid = uint256(residue) - pay;
            if (unpaid > 0) {
                // Value what stays from a FRESH read: the positions just burned
                // are no longer in the snapshot's marks.
                uint256 eAfter = _equityS(_snap(), MARK_LOW);
                uint256 stay = S - out;
                left = eAfter > unpaid ? unpaid * stay / (eAfter - unpaid) : 0;
            }
            if (pay > 0) _payCash(to, pay);
            flow += pay;
        }
        totalShares = S - out + left;
        dayFlow -= int256(flow);
        // Canonical mode checks/marks after the receipt holding also changes.
        if (!netAccounting) _markS(_snap());
    }

    /// @notice (1+K)/2 of the dearest pool fee: what placing a BUCK deposit
    ///         costs, charged on BUCK in and BUCK out alike (1e18).
    function _chargeBuck(uint256 k) internal view returns (uint256) {
        uint256 feeMax = 0;
        for (uint256 i = 0; i < constituents.length; i++) {
            if (constituents[i].feeTier > feeMax) feeMax = constituents[i].feeTier;
        }
        return feeMax * 1e12 * (1e18 + k) / 2e18;
    }

    // --- The work wheel's credits (its arbitrage captures) ------------------------ //

    /// @notice TOKEN the wheel captured, into the wallet (equity for every
    ///         holder; Deploy places it).  The pro-rata shell's name, so the
    ///         wheel's arbitrage kind needs no change.
    function creditDepositors(uint256 i, uint256 amount) external positionLock returns (uint128, uint256) {
        _requireIncomeSource();
        IERC20(constituents[i].token).transferFrom(msg.sender, address(this), amount);
        idleToken[i] += amount;
        emit WalletCredited(constituents[i].token, amount);
        return (0, 0);
    }

    /// @notice BUCK the wheel captured, into the basket's account.
    function creditTreasury(uint256 amount) external positionLock {
        _requireIncomeSource();
        IERC20(address(buck)).transferFrom(msg.sender, address(this), amount);
        emit WalletCredited(address(buck), amount);     // canonical cash; legacy implicit repayment
    }

    function _requireIncomeSource() private view {
        if (msg.sender == address(0) || (msg.sender != wheel && msg.sender != arbitrageCreditor)) revert NotWheel();
    }

    /// @notice Retire an exact amount of this basket's issued NFT principal.
    /// @param principal Raw issued BUCK to extinguish, never a coverage-room amount.
    /// @param maxCashCost Maximum full-value wallet BUCK spent, in raw units.
    /// @param minimumReliefScaled Minimum actual net Jubilee relief, scaled by 1e36.
    /// @dev Pre: canonical basket, existing NFT and authorized governor/wheel
    ///      (or an internal self-call), with no position operation in progress.
    ///      K/collateral are refreshed before authorizing the fund; the fund
    ///      owns age, refund, valuation and shared-block liquidity enforcement.
    ///      Post: principal and supply fall only through that real settlement,
    ///      wallet cost fits maxCashCost, and collateral is marked afresh.
    ///      No LP is sold here. Any failure rolls back authorization, owner /
    ///      insurer / fund cash, claim seconds and principal together. A large
    ///      reduction may need smaller calls across blocks; zero never closes
    ///      an NFT or means maximum.
    function retireCredit(uint256 principal, uint256 maxCashCost, uint256 minimumReliefScaled)
        external creditLock returns (uint256 reliefScaled, uint256 cashSpentScaled)
    {
        if (msg.sender != governance && msg.sender != wheel && msg.sender != address(this)) revert NotWheel();
        if (!netAccounting || address(credit) == address(0)) revert NetCreditUnavailable();
        if (principal == 0) revert Amount0();
        controller.compute();
        _markS(_snap());
        IBuckNetAccount token = IBuckNetAccount(address(buck));
        (, uint256 beforeNet) = token.accountBalancesScaled(address(this));
        token.authorizeReduction(creditId, principal, minimumReliefScaled, uint64(block.timestamp));
        (reliefScaled,) = IBasketCreditFund(token.jubilee()).reduce(creditId, principal);
        // Settlement deactivates coverage with the principal; the basket's self-insured,
        // zero-premium backing is all free, so it is activated again (no cash, no principal).
        (uint256 face, uint256 active,) = credit.creditInfo(creditId);
        if (active < face) token.issue(creditId, face - active, 0, 0, address(0));
        (, uint256 afterNet) = token.accountBalancesScaled(address(this));
        cashSpentScaled = beforeNet > afterNet ? beforeNet - afterNet : 0;
        if (Math.ceilDiv(cashSpentScaled, 1e36) > maxCashCost) revert CreditCashLimit();
        Snap memory s = _snap();
        creditLive = s.lien != 0;
        _markS(s);
        emit CreditRetired(creditId, principal, reliefScaled, cashSpentScaled);
    }

    // --- Views ------------------------------------------------------------------ //

    function gross() external view returns (uint256) { return _gross(MARK_TWAP); }
    function grossAt(uint8 mark) external view returns (uint256) { return _gross(mark); }
    function equity() external view returns (uint256) { return _equity(MARK_TWAP); }
    function kNow() external view returns (uint256) { return _k(); }
    /// @notice Outstanding facility issuance, independent of wallet cash.
    function lien() external view returns (uint256) {
        if (netAccounting) {
            if (address(credit) == address(0)) return 0;
            (uint256 principal,) = credit.issuanceOf(creditId);
            return principal;
        }
        int256 s_ = _bk().signedBalanceOf(address(this));
        return s_ < 0 ? uint256(-s_) : 0;
    }
    /// @notice Relief accrued on the lien and not yet paid.
    function reliefAccrued() external view returns (uint256) {
        return netAccounting ? _snap().relief : _bk().reliefOf(address(this));
    }
    /// @notice The mark the credit carries now (the equity it was last marked at).
    function markNow() external view returns (uint256) {
        return address(credit) == address(0) ? 0 : credit.markOf(creditId);
    }
    /// @notice K x the mark, less the lien: the room to issue (< 0: under water).
    function headroom() external view returns (int256) {
        if (netAccounting) return _headroomS(_snap());
        int256 s_ = _bk().signedBalanceOf(address(this));
        uint256 m = address(credit) == address(0) ? 0 : credit.markOf(creditId);
        int256 lim = int256(_k() * m / 1e18);
        return s_ < 0 ? lim + s_ : lim;
    }
    /// @notice Actual canonical wallet cash; legacy fixtures include unused
    ///         headroom. Upkeep's potential issuance budget is separate.
    function liquidity() external view returns (uint256) { return _cashAvailable(); }
    function liquidityTarget() external view returns (uint256) { return _target(); }

    function sharePrice() public view returns (uint256) {
        uint256 S = totalShares;
        return S == 0 ? 1e18 : _equity(MARK_TWAP) * 1e18 / S;
    }

    function valueOf(uint256 id) external view returns (uint256) {
        uint256 S = totalShares;
        // One rounding, matching the fund's fixed-point receipt valuation.
        return S == 0 ? 0 : Math.mulDiv(holdings[id].shares, _equity(MARK_TWAP), S);
    }

    function weightsBp() external view returns (uint256[] memory w) {
        return _weights(false);
    }

    /// @notice The allocation including waiting deposits and harvested TOKEN.
    function responseWeightsBp() external view returns (uint256[] memory) {
        return _weights(true);
    }

    function _weights(bool includeIdle) private view returns (uint256[] memory) {
        uint256[] memory values = new uint256[](constituents.length);
        for (uint256 i; i < values.length; ++i) {
            IBuckBasketVenue.Marks memory m = _marks(i);
            values[i] = m.posTwap;
            if (includeIdle) values[i] += idleToken[i] * m.pTwap / (10 ** constituents[i].decimals);
        }
        return PortfolioMath.weights(values);
    }

    // --- Dispatch: the components facet, else the venue ------------------------- //

    fallback() external {
        bool arb = msg.sig == IBasketArb.executeBasketArb.selector
                || msg.sig == IBasketArb.basketArbPlan.selector
                || msg.sig == IBuckFlashBorrower.onBuckFlash.selector;
        bool setup = msg.sig == IBasketUpkeep.initializeEquityCredit.selector
                  || msg.sig == IBasketUpkeep.authorizeIdentityBinding.selector
                  || msg.sig == IBasketUpkeep.revokeIdentityBinding.selector;
        if ((_positionEntered || _creditEntered) && (setup || msg.sig == IBasketUpkeep.wheelStep.selector
            || msg.sig == bytes4(keccak256("setWheel(address)")))) revert PositionReentrant();
        address v = (setup || arb || msg.sig == IBasketUpkeep.wheelStep.selector
                     || msg.sig == IBasketUpkeep.wheelDue.selector)
            ? equityWheel : address(venue);
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
