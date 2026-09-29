// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20}             from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {UniswapV3OracleLib} from "../lib/UniswapV3OracleLib.sol";
import {IBuckKController}   from "../IBuckKController.sol";
import {BuckBasketReceipt}  from "./BuckBasketReceipt.sol";
import {IBuckBasketVenue}   from "./IBuckBasketVenue.sol";
import {IUniswapV3Factory, IBuckMintBurn} from "./BuckBasketStorage.sol";
import {BuckBasketEquityStorage} from "./BuckBasketEquityStorage.sol";

/// @notice The components facet's entry points (BuckBasketEquityWheel).
interface IEquityWheel {
    function wheelDue(uint8 kind, uint256 i) external view returns (bool);
    function wheelStep(uint8 kind, uint256 i) external returns (uint256 work);
}

/// @title BuckBasketEquity -- the BuckBasket as equity: shares, one pooled
///        debt, BUCK payouts, a wallet the work wheel places.
///
/// @notice doc/BASKET-EQUITY.org section 13.6 (the design as ruled, 2026-09-29).
///
///         A DEPOSIT of TOKEN or BUCK is equity valued in BUCK: at the lower of
///         its pool's spot and TWAP, against the basket at the higher of each
///         pool's, less the pool fee on the swap the wheel will make to place
///         it.  It buys shares, and the basket mints K of the moment x its
///         value at once -- the deposit's own credit, whatever the pool's
///         leverage.  The asset and the BUCK land in the WALLET; the wheel's
///         components (the facet) place them.
///
///         A REDEMPTION gives the treasury 25% of the receipt's gain over its
///         cost basis (as shares), and pays the rest in BUCK: its value at
///         the exiter's marks (each pool at the lower of spot and TWAP), less
///         the same charge.  The wallet's BUCK pays first, then BUCK minted on
///         demand within K x equity - debt; the exit's share of the debt and
///         any mint become `owed`, which the wheel's Trim sells positions to
///         burn.  An exit neither covers goes pro rata: its fraction of the
///         wallet and of every position, the TOKEN sold, its debt share
///         burned, the rest paid.
///
///         No margin calls: a K cut changes only what new deposits bring.
///
/// # Dispatch
///
///         Two delegatecall facets over the shared storage: the components
///         (`equityWheel`: wheelDue / wheelStep) and the AMM venue (every
///         other selector, the V3 callbacks included), as the pro-rata shell
///         does.
contract BuckBasketEquity is BuckBasketEquityStorage {

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
        uint256 k = _k();

        // One pass over the pools: the guard, the deposit's value, the charge,
        // and the basket at the incumbents' (high) marks.
        uint256 n = constituents.length;
        uint256 value = asset == address(buck) ? amount : 0;
        uint256 feeMax = 0;
        uint256 grossHigh = idleBuck;
        for (uint256 i = 0; i < n; i++) {
            Constituent storage c = constituents[i];
            IBuckBasketVenue.Marks memory m = _marks(i);
            if (defaultMaxDeviationBp > 0 && m.pLow > 0
                && (m.pHigh - m.pLow) * 10000 > m.pLow * defaultMaxDeviationBp) revert Slippage();
            if (c.feeTier > feeMax) feeMax = c.feeTier;
            grossHigh += m.posHigh;
            uint256 one = 10 ** c.decimals;
            if (idleToken[i] > 0) grossHigh += idleToken[i] * m.pHigh / one;
            if (i == idx) value = amount * m.pLow / one;
        }
        if (value == 0) revert NoValue();

        uint256 charge = asset == address(buck)
            ? feeMax * 1e12 * (1e18 + k) / 2e18
            : (k < 1e18 ? uint256(constituents[idx].feeTier) * 1e12 * (1e18 - k) / 2e18 : 0);
        uint256 net = value * (1e18 - charge) / 1e18;
        uint256 S = totalShares;
        uint256 eHigh = grossHigh > debt ? grossHigh - debt : 0;
        uint256 shares = (S == 0 || eHigh == 0) ? net : net * S / eHigh;
        if (shares < minShares) revert MinOut();

        IERC20(asset).transferFrom(msg.sender, address(this), amount);
        if (asset == address(buck)) idleBuck += amount;
        else idleToken[idx] += amount;
        uint256 credit = k * value / 1e18;
        _mint(credit);                                  // the deposit's own K

        id = receipt.mint(msg.sender);
        holdings[id] = Holding(uint128(shares), uint128(value));
        deposits[id] = Deposit({buckPrincipal: value, tokenPrincipal:
                                asset == address(buck) ? 0 : amount,
                                token: asset, depositTime: uint64(block.timestamp)});
        totalShares = S + shares;
        dayFlow += int256(value);
        _settle();
        emit EquityDeposited(msg.sender, id, asset, amount, value, shares, credit);
    }

    // --- Redemption ----------------------------------------------------------- //

    function redeem(uint256 id, uint256 bp) external returns (uint256) {
        return redeem(id, bp, 0);
    }

    function redeem(uint256 id, uint256 bp, uint256 minBuck) public returns (uint256 paid) {
        if (receipt.ownerOf(id) != msg.sender) revert NotOwner();
        if (bp == 0 || bp > 10000) revert RedeemZero();
        Holding storage h = holdings[id];
        uint256 shares = uint256(h.shares) * bp / 10000;
        uint256 basis = uint256(h.basis) * bp / 10000;
        if (shares == 0) revert RedeemZero();

        // The treasury's cut: LAMBDA of the gain over the cost basis, in shares.
        Snap memory sn = _snap();
        uint256 S = totalShares;
        uint256 e = _equityS(sn, MARK_TWAP);
        uint256 worth = shares * e / S;
        uint256 cut = worth > basis ? (worth - basis) * LAMBDA_BP / 10000 * S / e : 0;
        treasuryShares += cut;

        bool proRata;
        (paid, proRata) = _exit(shares - cut, sn);
        h.shares -= uint128(shares);
        h.basis -= uint128(basis);
        if (h.shares == 0) receipt.burn(id);
        if (paid < minBuck) revert MinOut();
        IERC20(address(buck)).transfer(msg.sender, paid);
        emit EquityRedeemed(msg.sender, id, shares, cut, paid, proRata);
    }

    /// @notice The treasury's shares leave by the same door, with no cut.
    function redeemTreasury(uint256 bp, address to) external onlyGov returns (uint256 paid) {
        if (to == address(0)) revert To0();
        uint256 out = treasuryShares * bp / 10000;
        if (out == 0) revert RedeemZero();
        treasuryShares -= out;
        (paid,) = _exit(out, _snap());
        IERC20(address(buck)).transfer(to, paid);
    }

    /// @dev Retire `out` shares and return the BUCK to pay (held by the basket,
    ///      out of the wallet's books).  The exit fee (the stress fee's hook)
    ///      retires the shares but leaves its fraction of the assets.
    function _exit(uint256 out, Snap memory sn) internal returns (uint256 pay, bool proRata) {
        uint256 S = totalShares;
        uint256 fWad = (out - out * eq.exitFeeBp / 10000) * 1e18 / S;
        uint256 value = _equityS(sn, MARK_LOW) * fWad / 1e18;
        if (value == 0) revert Underwater();
        pay = value * (1e18 - _chargeBuck(sn.k)) / 1e18;
        uint256 dShare = debt * fWad / 1e18;

        uint256 use = idleBuck < pay ? idleBuck : pay;
        uint256 need = pay - use;
        uint256 eT = _equityS(sn, MARK_TWAP);
        uint256 cap = sn.k * (eT > pay ? eT - pay : 0) / 1e18;
        if (need == 0 || debt + need <= cap) {
            _mint(need);
            idleBuck -= pay;
            owed += dShare + need;
        } else {
            pay = _exitProRata(fWad, dShare);
            proRata = true;
        }
        totalShares = S - out;
        dayFlow -= int256(pay);
        _settle();
    }

    function _exitProRata(uint256 fWad, uint256 dShare) internal returns (uint256 pay) {
        uint256 b = idleBuck * fWad / 1e18;
        idleBuck -= b;
        uint256 n = constituents.length;
        for (uint256 i = 0; i < n; i++) {
            uint256 tok = idleToken[i] * fWad / 1e18;
            idleToken[i] -= tok;
            uint128 l = uint128(uint256(liquidityOf[i]) * fWad / 1e18);
            if (l > 0) {
                (uint256 t, uint256 bk) = _venue().positionBurn(i, l);
                liquidityOf[i] -= l;
                tok += t;
                b += bk;
            }
            if (tok > 0) {
                if (!_venue().poolLive(i)) revert EmptyPool();
                (, uint256 got) = _venue().monetaryLeg(i, false, tok);
                b += got;
            }
        }
        if (b < dShare) revert Underwater();
        buck.burnFromBasket(dShare);
        debt -= dShare;
        burnedTotal += dShare;
        owed = owed * (1e18 - fWad) / 1e18;
        pay = b - dShare;
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

    /// @notice BUCK the wheel captured, into the wallet.
    function creditTreasury(uint256 amount) external {
        if (msg.sender != wheel || wheel == address(0)) revert NotWheel();
        IERC20(address(buck)).transferFrom(msg.sender, address(this), amount);
        idleBuck += amount;
        _settle();
        emit WalletCredited(address(buck), amount);
    }

    // --- Views ------------------------------------------------------------------ //

    function gross() external view returns (uint256) { return _gross(MARK_TWAP); }
    function grossAt(uint8 mark) external view returns (uint256) { return _gross(mark); }
    function equity() external view returns (uint256) { return _equity(MARK_TWAP); }
    function kNow() external view returns (uint256) { return _k(); }
    function headroom() external view returns (int256) { return _headroom(); }
    function liquidity() external view returns (uint256) { return _liquidity(); }
    function liquidityTarget() external view returns (uint256) { return _target(); }
    function keepBuck() external view returns (uint256) { return _keep(); }

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
