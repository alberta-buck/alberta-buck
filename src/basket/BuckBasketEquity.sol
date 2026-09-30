// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20}             from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {UniswapV3OracleLib} from "../lib/UniswapV3OracleLib.sol";
import {IBuckKController}   from "../IBuckKController.sol";
import {BuckBasketReceipt}  from "./BuckBasketReceipt.sol";
import {IBuckBasketVenue}   from "./IBuckBasketVenue.sol";
import {IUniswapV3Factory, IBuckMintBurn} from "./BuckBasketStorage.sol";
import {BuckBasketEquityStorage, IEquityDeskPosition, IMarkedCredit} from "./BuckBasketEquityStorage.sol";

/// @notice The components facet's entry points (BuckBasketEquityWheel).
interface IEquityWheel {
    function wheelDue(uint8 kind, uint256 i) external view returns (bool);
    function wheelStep(uint8 kind, uint256 i) external returns (uint256 work);
}

/// @title BuckBasketEquity -- the BuckBasket as equity: shares, one pooled
///        lien, BUCK payouts, a wallet the work wheel places.
///
/// @notice (alberta-buck-ethereum.org, "BuckBasketEquity: the Basket as a Credit Holder".)
///
///         The basket is an ordinary credit holder: one self-issued MARKED
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
///         cost basis (as shares), and pays the rest in BUCK: its value at the
///         exiter's marks (each pool at the lower of spot and TWAP), less the
///         same charge, spent from what the account can spend once marked at
///         the equity that remains.  An exit that cannot be paid so is paid IN
///         KIND: its fraction of every position and of the wallet's TOKEN, by
///         transfer, with no swap.  The BUCK its positions return repay its
///         share of the lien; what is left of them is paid in BUCK -- or, if
///         the basket is under water and cannot spend it, left in the receipt
///         as shares.  Short of its lien share, it leaves TOKEN behind.
///
///         No margin calls: a K cut lowers the limit, never the lien.
///
/// # Dispatch
///
///         Two delegatecall facets over the shared storage: the components
///         (`equityWheel`: wheelDue / wheelStep) and the AMM venue (every
///         other selector, the V3 callbacks included), as the pro-rata shell
///         does.
contract BuckBasketEquity is BuckBasketEquityStorage, IEquityDeskPosition {

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

    function setEquityDirector(address d) external onlyGov {
        equityDirector = d;
        emit DirectorSet(d);
    }

    function setEquityParams(EquityParams calldata p) external onlyGov {
        if (p.bandBp >= 10000 || p.stepBp == 0 || p.stepBp > 10000
            || p.exitFeeBp > 1000 || p.flowDays == 0 || p.swapCapBp == 0
            || p.swapCapBp > 1000 || p.ceilBp < p.floorBp || p.ceilBp > 10000) revert Bp10000();
        eq = p;
    }

    /// @notice Open the basket's credit: a MARKED BuckCredit it issues itself
    ///         (the basket is its own insurer; zero premium), whose face caps
    ///         all it may ever issue at K x face.  Once, by governance, with
    ///         the basket bound as a public, non-Carrying identity.  It is
    ///         activated at the first mark above zero.
    function openCredit(address credit_, uint256 face) external onlyGov {
        if (address(credit) != address(0)) revert AlreadyPresent();
        IMarkedCredit c = IMarkedCredit(credit_);
        c.setCreditIssuer(address(this), true);
        creditId = c.createCredit(address(this), 0, face, 0, DEP_MARKED, 0, 0, 0);
        credit = c;
        emit CreditOpened(credit_, creditId, face);
    }

    /// @notice Register a constituent (the pro-rata shell's rule: declared
    ///         weights renormalized to 10000, each existing constituent's price
    ///         kept).
    function addBasketToken(address token, uint8 decimals, uint256 initialPriceInBuck,
                            uint256 weightBp, uint24 feeTier)
        external onlyGov returns (address pool)
    {
        if (!(token != address(0) && token != address(buck))) revert BadToken();
        if (!(weightBp <= 10000)) revert BadWeight();
        if (!(indexOf[token] == 0)) revert AlreadyPresent();
        if (!(initialPriceInBuck > 0)) revert BadPrice();

        uint256 N = constituents.length + 1;
        uint256 newW = weightBp > 0 ? weightBp : 10000 / N;
        uint256 oldW = 10000 - newW;
        uint256 oldSumW = 0;
        for (uint256 i = 0; i < constituents.length; i++) {
            Constituent storage c = constituents[i];
            c.targetWeightBp = UniswapV3OracleLib.mulDiv(c.targetWeightBp, oldW, 10000);
            if (!(c.targetWeightBp > 0 && c.targetWeightBp < 10000)) revert InvalidRescale();
            oldSumW += c.targetWeightBp;
            c.basketAmount = UniswapV3OracleLib.mulDiv(c.basketAmount, oldW, 10000);
        }
        if (constituents.length > 0) newW = 10000 - oldSumW;

        uint256 basketAmount = UniswapV3OracleLib.mulDiv(
            UniswapV3OracleLib.mulDiv(newW, 1e18, 10000), 1e18, initialPriceInBuck);
        (address p, int24 lo, int24 hi, bool b0) =
            _venue().setupPool(token, decimals, initialPriceInBuck, feeTier);
        pool = p;
        constituents.push(Constituent({
            token: token, decimals: decimals, basketAmount: basketAmount,
            initialPriceInBuck: initialPriceInBuck, feeTier: feeTier, pool: p,
            tickLower: lo, tickUpper: hi, buckIsToken0: b0, targetWeightBp: newW,
            treasuryLiquidity: 0
        }));
        indexOf[token] = constituents.length;
        controller.reprime();
        emit BasketTokenAdded(token, newW, initialPriceInBuck, p);
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
        public returns (uint256 id)
    {
        if (amount == 0) revert Amount0();
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
        uint256 value = asset == address(buck) ? amount : 0;
        uint256 feeMax = 0;
        for (uint256 i = 0; i < n; i++) {
            IBuckBasketVenue.Marks memory m = sn.m[i];
            if (defaultMaxDeviationBp > 0 && m.pLow > 0
                && (m.pHigh - m.pLow) * 10000 > m.pLow * defaultMaxDeviationBp) revert Slippage();
            if (constituents[i].feeTier > feeMax) feeMax = constituents[i].feeTier;
            if (i == idx) value = amount * m.pLow / (10 ** constituents[i].decimals);
        }
        if (value == 0) revert NoValue();

        uint256 k = sn.k;
        uint256 charge = asset == address(buck)
            ? feeMax * 1e12 * (1e18 + k) / 2e18
            : (k < 1e18 ? uint256(constituents[idx].feeTier) * 1e12 * (1e18 - k) / 2e18 : 0);
        uint256 net = value * (1e18 - charge) / 1e18;
        uint256 S = totalShares;
        uint256 eHigh = _equityS(sn, MARK_HIGH);
        uint256 shares = (S == 0 || eHigh == 0) ? net : net * S / eHigh;
        if (shares < minShares) revert MinOut();

        IERC20(asset).transferFrom(msg.sender, address(this), amount);
        if (asset != address(buck)) idleToken[idx] += amount;   // BUCK: straight to the account

        id = receipt.mint(msg.sender);
        holdings[id] = Holding(uint128(shares), uint128(value));
        deposits[id] = Deposit({buckPrincipal: value, tokenPrincipal:
                                asset == address(buck) ? 0 : amount,
                                token: asset, depositTime: uint64(block.timestamp)});
        totalShares = S + shares;
        dayFlow += int256(value);
        _account(sn);
        _markS(sn);                                     // the deposit's K, as credit
        // The pro-rata shells' event (the sim reads it): buckMinted is the
        // credit the deposit brings, K x its value.
        emit Deposited(msg.sender, id, asset, amount, k * value / 1e18, 0);
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

        Snap memory sn = _snap();
        (uint256 cut, uint256 cutValue) = _treasuryCut(sn, shares, basis);
        uint256 left;
        (paid, left) = _exit(shares - cut, sn, msg.sender);
        uint256 remainingBp = _retire(id, h, shares, left, basis);
        if (paid < minBuck) revert MinOut();
        // The pro-rata shells' event: nothing burned, BUCK paid, the cut's value.
        emit Redeemed(msg.sender, id, 0, paid, cutValue, remainingBp);
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
        h.shares -= uint128(gone);
        h.basis -= uint128(basis * gone / shares);
        remainingBp = h.shares == 0 ? 0 : uint256(h.shares) * 10000 / (uint256(h.shares) + gone);
        if (h.shares == 0) receipt.burn(id);
    }

    /// @notice The treasury's shares leave by the same door, with no cut.
    function redeemTreasury(uint256 bp, address to) external onlyGov returns (uint256 paid) {
        if (to == address(0)) revert To0();
        uint256 out = treasuryShares * bp / 10000;
        if (out == 0) revert RedeemZero();
        treasuryShares -= out;
        uint256 left;
        (paid, left) = _exit(out, _snap(), to);
        treasuryShares += left;
    }

    /// @dev Retire `out` shares, paying `to`.  In BUCK when the account can
    ///      spend it once marked at the equity that stays; else in kind.  The
    ///      exit fee (the stress fee's hook) retires the shares but leaves
    ///      its fraction of the assets.  Returns the BUCK paid and the shares
    ///      NOT retired (see `_exitInKind`).
    function _exit(uint256 out, Snap memory sn, address to)
        internal returns (uint256 pay, uint256 left)
    {
        uint256 S = totalShares;
        uint256 fWad = (out - out * eq.exitFeeBp / 10000) * 1e18 / S;
        uint256 eLow = _equityS(sn, MARK_LOW);
        uint256 value = eLow * fWad / 1e18;
        if (value == 0) revert Underwater();
        pay = value * (1e18 - _chargeBuck(sn.k)) / 1e18;

        // Mark the equity that stays, then pay from what Buck lets it spend.
        (, int256 deskValue) = _desk(sn.relief, sn.lien);
        int256 stays = int256(eLow - pay) + deskValue;
        _markAt(stays > 0 ? uint256(stays) : 0);
        if (pay <= _bk().balanceOf(address(this))) {
            totalShares = S - out;
            IERC20(address(buck)).transfer(to, pay);
            dayFlow -= int256(pay);
            return (pay, 0);
        }
        (pay, left) = _exitInKind(out, fWad, sn, to);
    }

    /// @dev The pro-rata exit, in kind: fraction `fWad` of every position and
    ///      of the wallet's TOKEN goes to `to` by transfer -- no swap, no
    ///      market moved.  The BUCK its positions return repay its share of
    ///      the lien (the account is one signed balance), and what they
    ///      returned beyond that share is paid in BUCK.  Two corners:
    ///        * short of its lien share (leverage above one), it leaves TOKEN
    ///          behind worth the shortfall, at the low marks;
    ///        * past what an under-water account can spend, the rest stays in
    ///          the receipt as shares at the price that remains -- the exit
    ///          completes, and nobody's claim moves ahead of anybody's.
    function _exitInKind(uint256 out, uint256 fWad, Snap memory sn, address to)
        internal returns (uint256 pay, uint256 left)
    {
        uint256 S = totalShares;
        uint256 n = constituents.length;
        uint256[] memory toks = new uint256[](n);
        int256 signed0 = _bk().signedBalanceOf(address(this));
        for (uint256 i = 0; i < n; i++) {
            uint256 tok = idleToken[i] * fWad / 1e18;
            idleToken[i] -= tok;
            uint128 l = uint128(uint256(liquidityOf[i]) * fWad / 1e18);
            if (l > 0) {
                (uint256 t,) = _venue().positionBurn(i, l);
                liquidityOf[i] -= l;
                tok += t;
            }
            toks[i] = tok;
        }
        // What its positions returned (net of the fee their BUCK carried),
        // less its share of equity's BUCK -- the lien, net of relief.
        int256 residue = (_bk().signedBalanceOf(address(this)) - signed0)
                       + sn.buckEq * int256(fWad) / 1e18;
        uint256 flow = 0;
        if (residue < 0) {
            uint256 tv = 0;
            for (uint256 i = 0; i < n; i++) tv += toks[i] * sn.m[i].pLow / (10 ** constituents[i].decimals);
            uint256 shortV = uint256(-residue);
            for (uint256 i = 0; i < n; i++) {
                uint256 keep = tv == 0 ? 0 : (shortV >= tv ? toks[i] : toks[i] * shortV / tv);
                idleToken[i] += keep;
                toks[i] -= keep;
            }
        } else if (residue > 0) {
            uint256 spend = _bk().balanceOf(address(this));
            pay = uint256(residue) < spend ? uint256(residue) : spend;
            uint256 unpaid = uint256(residue) - pay;
            if (unpaid > 0) {
                // Value what stays from a FRESH read: the positions just burned
                // are no longer in the snapshot's marks.
                uint256 eAfter = _equityS(_snap(), MARK_LOW);
                uint256 stay = S - out;
                left = eAfter > unpaid ? unpaid * stay / (eAfter - unpaid) : 0;
            }
            if (pay > 0) IERC20(address(buck)).transfer(to, pay);
            flow = pay;
        }
        for (uint256 i = 0; i < n; i++) {
            if (toks[i] == 0) continue;
            IERC20(constituents[i].token).transfer(to, toks[i]);
            flow += toks[i] * sn.m[i].pLow / (10 ** constituents[i].decimals);
            emit PaidInKind(to, constituents[i].token, toks[i]);
        }
        totalShares = S - out + left;
        dayFlow -= int256(flow);
        _markS(_snap());                                  // fresh: its positions are gone
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
    function creditDepositors(uint256 i, uint256 amount) external returns (uint128, uint256) {
        if (msg.sender != wheel || wheel == address(0)) revert NotWheel();
        IERC20(constituents[i].token).transferFrom(msg.sender, address(this), amount);
        idleToken[i] += amount;
        emit WalletCredited(constituents[i].token, amount);
        return (0, 0);
    }

    /// @notice BUCK the wheel captured, into the basket's account.
    function creditTreasury(uint256 amount) external {
        if (msg.sender != wheel || wheel == address(0)) revert NotWheel();
        IERC20(address(buck)).transferFrom(msg.sender, address(this), amount);
        emit WalletCredited(address(buck), amount);     // repays the lien, or is held
    }

    // --- Views ------------------------------------------------------------------ //

    function gross() external view returns (uint256) { return _gross(MARK_TWAP); }
    function grossAt(uint8 mark) external view returns (uint256) { return _gross(mark); }
    function equity() external view returns (uint256) { return _equity(MARK_TWAP); }
    function kNow() external view returns (uint256) { return _k(); }
    /// @notice The basket's lien: the BUCK it has issued (0 when it holds BUCK).
    function lien() external view returns (uint256) {
        int256 s_ = _bk().signedBalanceOf(address(this));
        return s_ < 0 ? uint256(-s_) : 0;
    }
    /// @notice Relief accrued on the lien and not yet paid.
    function reliefAccrued() external view returns (uint256) { return _bk().reliefOf(address(this)); }
    /// @notice The mark the credit carries now (the equity it was last marked at).
    function markNow() external view returns (uint256) {
        return address(credit) == address(0) ? 0 : credit.markOf(creditId);
    }
    /// @notice K x the mark, less the lien: the room to issue (< 0: under water).
    function headroom() external view returns (int256) {
        int256 s_ = _bk().signedBalanceOf(address(this));
        uint256 m = address(credit) == address(0) ? 0 : credit.markOf(creditId);
        int256 lim = int256(_k() * m / 1e18);
        return s_ < 0 ? lim + s_ : lim;
    }
    /// @notice What the account can spend: the BUCK held plus the headroom.
    function liquidity() external view returns (uint256) { return _bk().balanceOf(address(this)); }
    function liquidityTarget() external view returns (uint256) { return _target(); }

    function sharePrice() public view returns (uint256) {
        uint256 S = totalShares;
        return S == 0 ? 1e18 : _equity(MARK_TWAP) * 1e18 / S;
    }

    function valueOf(uint256 id) external view returns (uint256) {
        return uint256(holdings[id].shares) * sharePrice() / 1e18;
    }

    function weightsBp() external view returns (uint256[] memory w) {
        uint256 n = constituents.length;
        w = new uint256[](n);
        uint256 tot = 0;
        for (uint256 i = 0; i < n; i++) {
            w[i] = _marks(i).posTwap;
            tot += w[i];
        }
        for (uint256 i = 0; i < n; i++) w[i] = tot == 0 ? 0 : w[i] * 10000 / tot;
    }

    // --- The desk's book (none here; BuckBasketEquityOps answers) ------------- //

    function deskPosition(uint256 relief, uint256 lien_)
        external view virtual returns (int256, int256)
    {
        relief; lien_;
        return (0, 0);
    }

    /// @notice Assign the desk its share of relief just paid.  Only the basket
    ///         itself (the components facet, on its Daily).
    function deskRelief(uint256 relief, uint256 lien_) external virtual {
        if (msg.sender != address(this)) revert NotSelf();
        relief; lien_;
    }

    // --- Dispatch: the components facet, else the venue ------------------------- //

    fallback() external {
        address v = (msg.sig == IEquityWheel.wheelStep.selector
                     || msg.sig == IEquityWheel.wheelDue.selector)
            ? equityWheel : address(venue);
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
