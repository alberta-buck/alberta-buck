// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IBasketView} from "./IBasketView.sol";
import {IBasketReference} from "./IBasketReference.sol";
import {IArbMarkets} from "./BasketMarketOps.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import {UniswapV3OracleLib as V3} from "../lib/UniswapV3OracleLib.sol";
import {CarryRatio} from "../lib/CarryRatio.sol";

/// @notice The price book, and the parity reference: what each constituent,
///         and the reference basket, would fetch if sold now by its best route,
///         in full-value net BUCK and in USDC, at the pools' TWAPs. The wheel
///         samples it first each turn; K (through the venue facet), the parity
///         signal and the desk policy read the sample; execution reads live.
///
///         A TOKEN sold for BUCK goes into its own pool, or for USDC and the
///         USDC into the dollar pool; either way the BUCK leaves a Carrying
///         pool and arrives at that pool's net / gross (lib/CarryRatio.sol).
///         A TOKEN sold for USDC goes directly, or into its own pool and the
///         BUCK into the dollar pool: that BUCK passes the Carrying router at
///         nominal, so no ratio applies. The bids are mid prices: fees are
///         execution costs. While the basket reads nominally (its comparison
///         switch) the BUCK bids are the direct quoted prices, the measure
///         before 2026-11-15.
///
///         The routes through USDC come from the wheel's arbitrage triangles
///         (setMarkets); unset, only the direct routes price. Each publication
///         is one coherent observation: a constituent's own pool that is empty
///         or whose spot has left its TWAP refuses the whole reading; a route
///         through USDC that cannot be read the same way is skipped, and the
///         direct route prices that TOKEN. The book counts the consecutive days
///         it has published; a missed day, a failed read or a changed
///         constituent list starts the count again.
contract BasketReference is IBasketReference {
    /// @notice One TOKEN unit (10**decimals) by its best route, raw units.
    struct Bid {
        uint112 buck;       // net BUCK it fetches
        uint112 usdc;       // USDC it fetches; 0 without a USDC route
        bool buckViaUsdc;   // the BUCK bid went TOKEN -> USDC -> BUCK
        bool usdcViaBuck;   // the USDC bid went TOKEN -> BUCK -> USDC
    }

    /// @notice A reading of the whole book.
    struct Book {
        int256 twap;        // the basket in net BUCK at the TWAPs (rawValue, net BVIB)
        int256 spot;        // the same at the spot prices
        uint256 usdc;       // the basket in USDC at the TWAPs; 0 unless every TOKEN has a USDC route
        uint256 buckBid;    // USDC one full-value BUCK (10**decimals) fetches; 0 without the dollar pool
        uint256 buckAsk;    // USDC one net BUCK costs: bought, it arrives at the dollar pool's ratio
        Bid[] bids;         // per constituent
    }

    /// @dev The dollar pool and each constituent's USDC pool, as read once per book.
    struct Markets {
        address buck;
        address usdc;
        address dollar;
        bool dollarOk;
        int24 dollarTwap;
        int24 dollarSpot;
        uint256 gross;      // the dollar pool's carry (CarryRatio.read)
        uint256 net;
        address[] usd;      // by constituent index; 0: no USDC route
    }

    /// @dev One constituent's direct route and USDC quotes, in memory (the stack).
    struct Leg {
        address token;
        address buck;
        address pool;
        uint128 unit;
        uint256 pB;         // TOKEN -> BUCK on its own pool, quoted (nominal): TWAP, spot
        uint256 pBs;
        uint256 pU;         // TOKEN -> USDC on its USDC pool: TWAP, spot; 0 unreadable
        uint256 pUs;
    }

    IBasketView public immutable basket;
    uint32 public immutable sampleInterval;
    uint32 public immutable maxAge;
    uint16 public immutable maxDeviationBp;
    uint64 public lastDay;
    uint64 public lastSampleAt;
    uint256 public lastAttemptBlock;
    uint256 public sampledConstituents;
    uint32 public consecutiveDays;
    bool public valid;
    int256 public rawValue;
    /// @notice The wheel whose arbitrage triangles name the USDC routes; 0: direct routes only.
    address public markets;
    uint256 public valueUsdc;
    uint256 public buckBid;
    uint256 public buckAsk;
    Bid[] public bids;

    error InvalidObservation();
    error NotGovernance();
    event Observed(uint64 indexed at, uint64 indexed day, int256 raw, bool restarted);
    event ObservationUnavailable(uint256 indexed at);
    event MarketsSet(address wheel);

    constructor(address basket_, uint32 interval_, uint32 maxAge_, uint16 deviationBp_) {
        require(basket_ != address(0) && interval_ > 0 && interval_ <= 1 days
                && maxAge_ >= interval_ && maxAge_ <= 1 days
                && deviationBp_ > 0 && deviationBp_ <= 1000, "signal params");
        basket = IBasketView(basket_);
        sampleInterval = interval_;
        maxAge = maxAge_;
        maxDeviationBp = deviationBp_;
    }

    /// @notice Price the routes through USDC from this wheel's triangles (0: direct only),
    ///         and widen the oracles of their pools.
    function setMarkets(address wheel) external {
        if (msg.sender != basket.governance()) revert NotGovernance();
        markets = wheel;
        emit MarketsSet(wheel);
        widen();
    }

    /// @notice Let the dollar pool and the USDC pools keep as many observations as the
    ///         basket's own (a TWAP over the window needs them). Anyone may pay for it;
    ///         call again after the wheel's triangles change.
    function widen() public {
        IArbMarkets w = IArbMarkets(markets);
        if (address(w) == address(0)) return;
        uint16 slots = basket.observationCardinality();
        address dollar = w.poolUb();
        if (dollar != address(0)) IUniswapV3Pool(dollar).increaseObservationCardinalityNext(slots);
        uint256 k = w.triangleCount();
        for (uint256 j; j < k; ++j) {
            (,, address usd,) = w.triangles(j);
            if (usd != address(0)) IUniswapV3Pool(usd).increaseObservationCardinalityNext(slots);
        }
    }

    function fresh() public view returns (bool) {
        return valid && block.timestamp <= uint256(lastSampleAt) + maxAge
            && sampledConstituents == basket.constituentsLength();
    }

    function isDue() public view returns (bool) {
        return lastAttemptBlock != block.number
            && (!fresh() || block.timestamp >= uint256(lastSampleAt) + sampleInterval);
    }

    /// @notice Strict live reference for K or an execution-time direction check.
    ///         No inventory offset is added to the parity error.
    function basketValueInBuck() external view returns (int256 raw) {
        (raw,) = marketValues();
    }

    /// @notice The reference basket in net BUCK by each TOKEN's best route, at
    ///         the TWAPs and at the spots (quoted, direct, while the basket reads
    ///         nominally).
    function marketValues() public view returns (int256 twap, int256 spot) {
        Book memory b = prices();
        return (b.twap, b.spot);
    }

    /// @notice The whole book, live.
    function prices() public view returns (Book memory b) {
        uint256 n = basket.constituentsLength();
        uint32 window = basket.twapWindow();
        if (n == 0 || window == 0) revert InvalidObservation();
        Markets memory m = _markets(n, window);
        bool net = !basket.nominalReference();
        bool usdcComplete = true;
        b.bids = new Bid[](n);
        for (uint256 i; i < n; ++i) {
            (uint256 amount, uint256 spotBuck, Bid memory bid) = _price(i, window, net, m);
            b.bids[i] = bid;
            b.twap += int256(V3.mulDiv(amount, bid.buck, 1e18));
            b.spot += int256(V3.mulDiv(amount, spotBuck, 1e18));
            if (bid.usdc == 0) usdcComplete = false;
            b.usdc += V3.mulDiv(amount, bid.usdc, 1e18);
        }
        if (b.twap <= 0) revert InvalidObservation();
        if (!usdcComplete) b.usdc = 0;
        if (m.dollarOk) {
            b.buckBid = V3.getQuoteAtTick(m.dollarTwap, uint128(10 ** IERC20Metadata(m.buck).decimals()), m.buck, m.usdc);
            b.buckAsk = CarryRatio.toNominal(m.gross, m.net, b.buckBid);
        }
    }

    /// @notice A pool's TWAP and spot ticks; reverts if it is empty or has no
    ///         history over the window. External, so a route may try it.
    function ticks(address pool, uint32 window) public view returns (int24 twap, int24 spot) {
        if (IUniswapV3Pool(pool).liquidity() == 0) revert InvalidObservation();
        twap = V3.consult(pool, window);
        (, spot,,,,,) = IUniswapV3Pool(pool).slot0();
    }

    function _markets(uint256 n, uint32 window) private view returns (Markets memory m) {
        m.usd = new address[](n);
        IArbMarkets w = IArbMarkets(markets);
        if (address(w) == address(0)) return m;
        m.dollar = w.poolUb();
        if (m.dollar == address(0)) return m;
        (m.buck, m.usdc) = (w.arbBuck(), w.arbUsdc());
        try this.ticks(m.dollar, window) returns (int24 t, int24 s) {
            if (_near(V3.getQuoteAtTick(t, 1e18, m.buck, m.usdc), V3.getQuoteAtTick(s, 1e18, m.buck, m.usdc))) {
                (m.dollarOk, m.dollarTwap, m.dollarSpot) = (true, t, s);
                (m.gross, m.net) = CarryRatio.read(m.buck, m.dollar);
            }
        } catch {}
        uint256 k = w.triangleCount();
        for (uint256 j; j < k; ++j) {
            (address token, address own, address usd, uint256 index) = w.triangles(j);
            if (index >= n) continue;
            (address t,,,,, address pool,,,,,) = basket.constituents(index);
            if (t == token && pool == own) m.usd[index] = usd;
        }
    }

    /// @dev One constituent: its basket amount (1e18-scaled), its net BUCK bid at
    ///      the spots, and its bids at the TWAPs.
    function _price(uint256 i, uint32 window, bool net, Markets memory m)
        private view returns (uint256 amount, uint256 spotBuck, Bid memory bid)
    {
        Leg memory l;
        amount = _direct(i, window, l);
        if (m.usd[i] != address(0)) {
            try this.ticks(m.usd[i], window) returns (int24 t, int24 s) {
                (l.pU, l.pUs) = (V3.getQuoteAtTick(t, l.unit, l.token, m.usdc), V3.getQuoteAtTick(s, l.unit, l.token, m.usdc));
                if (!_near(l.pU, l.pUs)) (l.pU, l.pUs) = (0, 0);
            } catch {}
        }
        // In USDC: directly, or through BUCK, which passes the Carrying router at nominal.
        uint256 usdc = l.pU;
        if (m.dollarOk) {
            uint256 viaBuck = V3.getQuoteAtTick(m.dollarTwap, SafeCast.toUint128(l.pB), l.buck, m.usdc);
            if (viaBuck > usdc) (usdc, bid.usdcViaBuck) = (viaBuck, true);
        }
        bid.usdc = SafeCast.toUint112(usdc);
        // In net BUCK: from its own pool at its ratio, or through USDC from the dollar pool at its ratio.
        if (!net) {
            bid.buck = SafeCast.toUint112(l.pB);
            return (amount, l.pBs, bid);
        }
        (uint256 g, uint256 n) = CarryRatio.read(l.buck, l.pool);
        uint256 buck = CarryRatio.toNet(g, n, l.pB);
        spotBuck = CarryRatio.toNet(g, n, l.pBs);
        if (m.dollarOk && l.pU != 0) {
            uint256 via = CarryRatio.toNet(m.gross, m.net,
                V3.getQuoteAtTick(m.dollarTwap, SafeCast.toUint128(l.pU), m.usdc, l.buck));
            if (via > buck) (buck, bid.buckViaUsdc) = (via, true);
            via = CarryRatio.toNet(m.gross, m.net,
                V3.getQuoteAtTick(m.dollarSpot, SafeCast.toUint128(l.pUs), m.usdc, l.buck));
            if (via > spotBuck) spotBuck = via;
        }
        bid.buck = SafeCast.toUint112(buck);
    }

    /// @dev The direct route, TOKEN -> BUCK on the constituent's own pool, quoted;
    ///      the pool must read and its spot stay near its TWAP.
    function _direct(uint256 i, uint32 window, Leg memory l) private view returns (uint256 amount) {
        uint8 dec;
        bool b0;
        (l.token, dec, amount,,, l.pool,,, b0,,) = basket.constituents(i);
        l.unit = uint128(10 ** dec);
        l.buck = b0 ? IUniswapV3Pool(l.pool).token0() : IUniswapV3Pool(l.pool).token1();
        (int24 t, int24 s) = ticks(l.pool, window);
        l.pB = V3.getQuoteAtTick(t, l.unit, l.token, l.buck);
        l.pBs = V3.getQuoteAtTick(s, l.unit, l.token, l.buck);
        if (!_near(l.pB, l.pBs)) revert InvalidObservation();
    }

    /// @dev A spot within the allowed deviation of its TWAP price (both quoted).
    function _near(uint256 twap, uint256 spot) private view returns (bool) {
        uint256 delta = twap > spot ? twap - spot : spot - twap;
        return twap != 0 && delta * 10000 <= twap * maxDeviationBp;
    }

    function observe() external {
        if (!isDue()) return;
        lastAttemptBlock = block.number;
        try this.prices() returns (Book memory b) {
            uint64 today = uint64(block.timestamp / 1 days);
            uint256 n = b.bids.length;
            bool restart = !valid || sampledConstituents != n || today > lastDay + 1;
            if (restart || today != lastDay) {
                consecutiveDays = restart ? 1 : consecutiveDays + 1;
                lastDay = today;
            }
            rawValue = b.twap;
            (valueUsdc, buckBid, buckAsk) = (b.usdc, b.buckBid, b.buckAsk);
            for (uint256 i; i < n; ++i) {
                if (i < bids.length) bids[i] = b.bids[i];
                else bids.push(b.bids[i]);
            }
            while (bids.length > n) bids.pop();
            sampledConstituents = n;
            lastSampleAt = uint64(block.timestamp);
            valid = true;
            emit Observed(lastSampleAt, today, b.twap, restart);
        } catch {
            valid = false;
            consecutiveDays = 0;
            emit ObservationUnavailable(block.timestamp);
        }
    }
}
