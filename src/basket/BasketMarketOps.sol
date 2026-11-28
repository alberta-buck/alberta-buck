// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {IBuckJubilee} from "../IBuckJubilee.sol";
import {IBuckNetView} from "../IBuckNetView.sol";
import {CarryRatio} from "../lib/CarryRatio.sol";
import {UniswapV3OracleLib as V3} from "../lib/UniswapV3OracleLib.sol";
import {IBuckBasketVenue} from "./IBuckBasketVenue.sol";
import {IBuckFlash} from "../IBuckFlashBorrower.sol";
import {IBasketArb} from "./IBasketArb.sol";
import {IBasketUpkeep} from "./IBasketUpkeep.sol";

interface IMarketCash is IBuckNetView {
    function approvedCarryingIssuer(address account) external view returns (bool);
}

interface ISignedCash {
    function signedBalanceOf(address account) external view returns (int256);
}

interface IArbMarkets {
    function arbBuck() external view returns (address);
    function arbUsdc() external view returns (address);
    function poolUb() external view returns (address);
    function creditee() external view returns (address);
    function capBp() external view returns (uint16);
    function minEdgeBp() external view returns (uint16);
    function triangleCount() external view returns (uint256);
    function triangles(uint256 i) external view returns (address token, address own, address usd, uint256 index);
}

interface IArbBasket {
    function constituents(uint256 i) external view returns (address, uint8, uint256, uint256, uint24,
        address, int24, int24, bool, uint256, uint128);
    function creditId() external view returns (uint256);
    function liquidityOf(uint256 i) external view returns (uint128);
    function liquidityTarget() external view returns (uint256);
    function v3Factory() external view returns (address);
    function twapWindow() external view returns (uint32);
}

/// @notice One shared linked helper, used by the existing upkeep facets.
/// PRE: execution is delegatecalled by a locked, ordinary canonical basket.
/// Quotes assume constant active liquidity; actual execution independently
/// measures net cash + LP principal + collected fees at frozen TOKEN marks.
/// Neither a nominal gain, old uncollected income, a changed marking price
/// nor a transfer from the wheel's reserve can stand in for portfolio return.
library BasketMarketOps {
    uint256 private constant Q96 = 2 ** 96;
    uint256 private constant Q = 1e36;

    struct Pool {
        address pool;
        address token0;
        address token1;
        uint160 sqrt;
        uint128 liquidity;
        uint24 fee;
        uint8 protocol;
        uint256 gross;
        uint256 net;
    }
    struct Market {
        uint256 index;
        address token;
        address own;
        address usd;
        int24 lower;
        int24 upper;
        uint128 owned;
        uint256 one;
        uint256 mark;
    }
    struct Context {
        address buck;
        address usd;
        address ub;
        Market[2] markets;
        uint8 count;
        uint16 cap;
        uint16 edge;
        uint256 budget;
        uint256 walletGross;
        uint256 walletNet;
        uint256 reserveFloor;
        bool flash;
    }
    struct Route {
        Pool[4] pools;
        address[4] input;
        uint8 length;
    }

    error InvalidArbMarket();
    error InvalidArbRoute();
    error ArbBudget();
    error NoPortfolioGain();
    error IntermediateInventory();
    error NetCashUnavailable();
    error Underwater();                 // the same selector as the shell's

    event PaidInKind(address indexed to, address indexed token, uint256 amount);

    event BasketArbitrage(uint256 indexed route, uint8 direction, uint256 input,
                         int256 cashReturn, int256 lpReturn, uint256 lpFees, int256 totalReturn);
    event PriorArbFeesCollected(uint256 indexed route, uint256 fees);
    event ArbBuckFlow(address indexed pool, bool buckInput, uint256 nominal, uint256 netScaled,
                      uint256 basketRatioBefore, uint256 basketRatioAfter,
                      uint256 poolRatioBefore, uint256 poolRatioAfter);

    /// @notice Shared read-only check before the existing setup facet creates
    /// a basket facility. Carrying is a service permission, not an accounting
    /// exemption. No NFT, cash, approval or registry state changes here.
    function validateCredit(address buck, address credit, bool netAccounting) public view {
        (bool ok, bytes memory data) = buck.staticcall(abi.encodeWithSignature("ACCOUNTING_MODEL()"));
        bool canonical = ok && data.length == 32
            && abi.decode(data, (bytes32)) == keccak256("Buck/compounded-cash/per-facility-jubilee/v1");
        if (netAccounting) {
            if (!canonical) revert IBasketUpkeep.InvalidCreditModel();
            IBuckJubilee token = IBuckJubilee(buck);
            if (!token.identity().isVerified(address(this)) || !token.identity().isPublicIdentity(address(this))
                || (token.identity().isCarrying(address(this))
                    && !IMarketCash(buck).approvedCarryingIssuer(address(this)))) revert IBasketUpkeep.InvalidCreditIdentity();
            if (credit != address(token.buckCredit()) || token.buckCredit().buck() != buck) {
                revert IBasketUpkeep.WrongCreditContract();
            }
        } else if (canonical) revert IBasketUpkeep.InvalidCreditModel();
    }

    /// @notice Nominal ERC-20 units needed to deliver at least value net raw
    /// BUCK. Fresh issuance and all incoming transfers share one pooled basis.
    /// PRE: value fits deliverable cash. POST: integer nominal transfer covers
    /// the requested net amount; rounding cannot borrow fractional inventory.
    function nominalForNet(address token, uint256 value) public view returns (uint256 nominal) {
        if (value == 0) return 0;
        (uint256 gross, uint256 net) = CarryRatio.read(token, address(this));
        if (net == 0) revert NetCashUnavailable();
        nominal = CarryRatio.toNominal(gross, net, value);
        if (nominal > IERC20(token).balanceOf(address(this))) revert NetCashUnavailable();
    }

    function spendableNet(address token) public view returns (uint256) {
        (uint256 gross, uint256 net) = CarryRatio.read(token, address(this));
        return CarryRatio.toNet(gross, net, IERC20(token).balanceOf(address(this)));
    }

    /// @notice Policy amounts are net BUCK; the existing venue remains in
    /// nominal ERC-20 units. TOKEN amounts retain native units. Return actual
    /// cash changes, rounding costs up and receipts down in raw BUCK.
    function monetaryLeg(address token, uint256 i, bool sellBuck, uint256 value)
        public returns (uint256 spent, uint256 received)
    {
        (, uint256 beforeNet) = IMarketCash(token).accountBalancesScaled(address(this));
        (spent, received) = IBuckBasketVenue(address(this)).monetaryLeg(
            i, sellBuck, sellBuck ? nominalForNet(token, value) : value);
        (, uint256 afterNet) = IMarketCash(token).accountBalancesScaled(address(this));
        if (sellBuck) spent = Math.ceilDiv(beforeNet - afterNet, Q);
        else received = (afterNet - beforeNet) / Q;
    }

    /// @dev The upkeep has prepared the credit draw and selected its authenticated pool
    /// payment callbacks. Only withdrawn TOKEN and freshly issued BUCK go back into the LP.
    function refreshPosition(mapping(uint256 => uint256) storage idle, mapping(uint256 => uint128) storage liquidity,
                             uint256 i, uint128 slice, uint256 budget) public returns (uint256 issued)
    {
        IBuckBasketVenue venue = IBuckBasketVenue(address(this));
        (uint256 tokens, uint256 bucks) = venue.positionBurn(i, slice);
        uint128 restored;
        uint256 paired;
        (restored, paired, issued) = venue.positionMint(i, tokens, Math.min(bucks, budget));
        if (restored == 0) revert NetCashUnavailable();
        liquidity[i] = liquidity[i] - slice + restored;
        idle[i] += tokens - paired;
    }

    /// @notice A receipt's exit from the positions (BuckBasketEquity._exitInKind): withdraw
    ///         fraction `fWad` of every position and of the idle TOKEN; paid in BUCK, sell that
    ///         TOKEN through each constituent's own pool (the leaver bearing the slippage; the
    ///         displaced price is the wheel's arbitrage), else deliver it in kind. `residue` is
    ///         the BUCK its positions released and its TOKEN fetched, plus its share of equity's
    ///         BUCK (`buckEqShare`: cash less lien plus relief, negative when levered). Sold short
    ///         of its lien share it cannot leave (`Underwater`); in kind, it leaves TOKEN worth
    ///         the shortfall at the low marks. Returns the residue and the value delivered in kind.
    /// PRE: delegatecalled by the basket under its position lock. POST: positions and idle TOKEN
    ///      reduced; the shell pays the residue, retires shares and books the flow.
    function exitAssets(mapping(uint256 => uint256) storage idle, mapping(uint256 => uint128) storage liq,
                        address token, bool net, uint256 fWad, int256 buckEqShare, address to, bool inKind,
                        uint256 n)
        external returns (int256 residue, uint256 flow)
    {
        uint256[] memory toks = new uint256[](n);
        address[] memory tokens = new address[](n);
        uint256[] memory units = new uint256[](n);
        uint256[] memory pLow = new uint256[](n);
        int256 before = _held(token, net);
        for (uint256 i; i < n; i++) {
            // The low marks the exit is valued at, read before this position moves.
            pLow[i] = IBuckBasketVenue(address(this)).marks(i, liq[i]).pLow;
            uint8 dec;
            (tokens[i], dec,,,,,,,,,) = IArbBasket(address(this)).constituents(i);
            units[i] = 10 ** dec;
            uint256 tok = idle[i] * fWad / 1e18;
            idle[i] -= tok;
            // casting to 'uint128' is safe because fWad <= 1e18, so l <= liq[i]
            // forge-lint: disable-next-line(unsafe-typecast)
            uint128 l = uint128(uint256(liq[i]) * fWad / 1e18);
            if (l > 0) {
                (uint256 t,) = IBuckBasketVenue(address(this)).positionBurn(i, l);
                liq[i] -= l;
                tok += t;
            }
            if (!inKind && tok > 0) {
                IBuckBasketVenue(address(this)).monetaryLeg(i, false, tok);
                tok = 0;
            }
            toks[i] = tok;
        }
        residue = _held(token, net) - before + buckEqShare;
        if (residue < 0) {
            if (!inKind) revert Underwater();
            uint256 tv;
            for (uint256 i; i < n; i++) tv += toks[i] * pLow[i] / units[i];
            // casting to 'uint256' is safe because residue < 0 here
            // forge-lint: disable-next-line(unsafe-typecast)
            uint256 shortV = uint256(-residue);
            for (uint256 i; i < n; i++) {
                uint256 keep = tv == 0 ? 0 : (shortV >= tv ? toks[i] : toks[i] * shortV / tv);
                idle[i] += keep;
                toks[i] -= keep;
            }
        }
        for (uint256 i; i < n; i++) {
            if (toks[i] == 0) continue;
            // forge-lint: disable-next-line(erc20-unchecked-transfer)
            IERC20(tokens[i]).transfer(to, toks[i]);
            flow += toks[i] * pLow[i] / units[i];
            emit PaidInKind(to, tokens[i], toks[i]);
        }
    }

    function _held(address token, bool net) private view returns (int256) {
        return net ? int256(IMarketCash(token).netBalanceOf(address(this)))
                   : ISignedCash(token).signedBalanceOf(address(this));
    }

    /// @notice Net-valued redemption. Carrying keeps nominal transfer rules;
    /// only this basket policy grosses up its payout to honor its net quote.
    function payNet(address token, address to, uint256 netBuck) public {
        if (!IERC20(token).transfer(to, nominalForNet(token, netBuck))) revert NetCashUnavailable();
    }

    /// @notice At most 12 ternary refinements per direction on one route.
    /// Reads are pinned to this call; repeated candidate evaluations are pure.
    /// This is bounded numerical sizing, not a claim of an exact global optimum.
    function plan(address buck, address wheel, uint256 route) public view returns (IBasketArb.ArbPlan memory best) {
        Context memory c = _context(buck, wheel, route);
        for (uint8 d; d < 2; d++) {
            Route memory r = _route(c, d);
            uint256 cap = _cap(c, r);
            uint256 lo;
            uint256 hi = cap;
            for (uint256 j; j < 12 && hi > lo + 2; j++) {
                uint256 a = lo + (hi - lo) / 3;
                uint256 b = hi - (hi - lo) / 3;
                IBasketArb.ArbPlan memory pa = _estimate(c, r, a, d);
                IBasketArb.ArbPlan memory pb = _estimate(c, r, b, d);
                best = _better(c, best, pa);
                best = _better(c, best, pb);
                if (pa.totalReturn < pb.totalReturn) lo = a;
                else hi = b;
            }
            best = _better(c, best, _estimate(c, r, (lo + hi) / 2, d));
            best = _better(c, best, _estimate(c, r, cap, d));
        }
    }

    /// @dev Old fees are collected before the baseline; new fees after the
    /// cycle. LP principal is valued on both sides at the same frozen marks.
    /// Every intermediate spend must be complete. Existing custody cannot
    /// finance an unreported remainder. A revert undoes all collections/swaps.
    /// @notice One cycle, funded from the wallet or, with the wheel's arbFlash, by a flash on the basket's
    /// credit line (Buck.flashIssue calls the basket back, which runs flashLeg).
    function run(address buck, address wheel, uint256 creditId, uint256 route, uint8 direction, uint256 amount,
                 uint256 minimumGain, mapping(uint256 => uint256) storage idle)
        public returns (IBasketArb.ArbPlan memory)
    {
        if (!flashing(wheel)) return execute(buck, wheel, route, direction, amount, minimumGain, 0, idle);
        return abi.decode(IBuckFlash(buck).flashIssue(creditId, amount, abi.encode(route, direction, minimumGain)),
                          (IBasketArb.ArbPlan));
    }

    /// @notice The cycle inside a flash: the borrowed leg is the cycle's input, kept out of the reserve.
    function flashLeg(address buck, address wheel, uint256 amount, bytes memory data,
                      mapping(uint256 => uint256) storage idle) public returns (bytes memory)
    {
        (uint256 route, uint8 direction, uint256 minimumGain) = abi.decode(data, (uint256, uint8, uint256));
        return abi.encode(execute(buck, wheel, route, direction, amount, minimumGain, amount, idle));
    }

    /// `borrowed`: BUCK the basket holds by flash for this cycle (repaid after it), so excluded from the
    /// reserve it must keep.
    function execute(address buck, address wheel, uint256 route, uint8 direction, uint256 amount,
                     uint256 minimumGain, uint256 borrowed, mapping(uint256 => uint256) storage idle)
        public returns (IBasketArb.ArbPlan memory result)
    {
        if (direction > 1 || amount == 0) revert InvalidArbRoute();
        Context memory c = _context(buck, wheel, route);
        uint256 priorFees = _sync(c, idle);
        if (priorFees != 0) emit PriorArbFeesCollected(route, priorFees);
        c = _context(buck, wheel, route);
        c.reserveFloor = Math.min(c.reserveFloor, spendableNet(buck) - borrowed);
        Route memory r = _route(c, direction);
        if (amount > _cap(c, r)) revert ArbBudget();
        uint256 required = Math.max(minimumGain, _minimum(c, amount));
        result = _estimate(c, r, amount, direction);
        if (result.totalReturn < int256(required)) revert NoPortfolioGain();
        uint256 beforeValue = _value(c);
        (, uint256 cash) = IMarketCash(buck).accountBalancesScaled(address(this));
        uint256 stable = IERC20(c.usd).balanceOf(address(this));
        uint256 flowing = amount;
        for (uint256 k; k < r.length; k++) {
            address out = k + 1 == r.length ? buck : r.input[k + 1];
            flowing = _swapLeg(c, r.pools[k], r.input[k], out, flowing);
        }
        if (IERC20(c.usd).balanceOf(address(this)) != stable) revert IntermediateInventory();
        (, uint256 afterCash) = IMarketCash(buck).accountBalancesScaled(address(this));
        result.cashReturn = _netDifference(afterCash, cash);
        result.lpFees = _sync(c, idle);
        if (spendableNet(buck) < c.reserveFloor + borrowed) revert ArbBudget();
        result.totalReturn = _difference(_value(c), beforeValue);
        result.lpReturn = result.totalReturn - result.cashReturn;
        if (result.totalReturn < int256(required)) revert NoPortfolioGain();
        emit BasketArbitrage(route, direction, amount, result.cashReturn,
                             result.lpReturn, result.lpFees, result.totalReturn);
    }

    function _swapLeg(Context memory c, Pool memory p, address input, address output, uint256 amount)
        private returns (uint256 received)
    {
        bool buckLeg = input == c.buck || output == c.buck;
        uint256 wg; uint256 wn;
        if (buckLeg) (wg, wn) = IMarketCash(c.buck).accountBalancesScaled(address(this));
        received = IBuckBasketVenue(address(this)).arbSwap(p.pool, input, output, amount);
        if (buckLeg) {
            (uint256 ag, uint256 an) = IMarketCash(c.buck).accountBalancesScaled(address(this));
            (uint256 pg, uint256 pn) = IMarketCash(c.buck).accountBalancesScaled(p.pool);
            bool paying = input == c.buck;
            emit ArbBuckFlow(p.pool, paying, paying ? amount : received,
                paying ? wn - an : an - wn, _ratio(wn, wg), _ratio(an, ag),
                _ratio(p.net, p.gross), _ratio(pn, pg));
        }
    }

    function _minimum(Context memory c, uint256 amount) private pure returns (uint256) {
        // Fractional/raw rounding cannot turn an unchanged portfolio into
        // paid work. This is a two-raw-unit dust floor, not a gas/bounty hurdle.
        uint256 netInput = CarryRatio.toNet(c.walletGross, c.walletNet, amount);
        return Math.max(2, Math.mulDiv(netInput, c.edge, 10000, Math.Rounding.Ceil));
    }

    function _better(Context memory c, IBasketArb.ArbPlan memory old, IBasketArb.ArbPlan memory candidate)
        private pure returns (IBasketArb.ArbPlan memory)
    {
        return candidate.amount != 0 && candidate.totalReturn >= int256(_minimum(c, candidate.amount))
            && candidate.totalReturn > old.totalReturn ? candidate : old;
    }

    function _context(address buck, address wheel, uint256 route) private view returns (Context memory c) {
        IArbMarkets w = IArbMarkets(wheel);
        if (w.arbBuck() != buck || w.creditee() != address(this)) revert InvalidArbMarket();
        c.buck = buck;
        c.usd = w.arbUsdc();
        c.ub = w.poolUb();
        c.cap = w.capBp();
        c.edge = w.minEdgeBp();
        if (c.cap > 500 || c.edge > 10000 || c.usd == buck || c.usd == address(0)) revert InvalidArbMarket();
        uint256 n = w.triangleCount();
        if (n == 0 || n > 32 || route >= n + n * (n - 1) / 2) revert InvalidArbRoute();
        uint256 i = route;
        uint256 j;
        c.count = 1;
        if (route >= n) {
            uint256 pair = route - n;
            i = 0;
            while (pair >= n - i - 1) { pair -= n - i - 1; ++i; }
            j = i + 1 + pair;
            c.count = 2;
        }
        c.markets[0] = _market(c, w, i);
        if (c.count == 2) {
            c.markets[1] = _market(c, w, j);
            if (c.markets[0].index == c.markets[1].index
                || c.markets[0].token == c.markets[1].token) revert InvalidArbMarket();
        }
        (c.walletGross, c.walletNet) = IMarketCash(buck).accountBalancesScaled(address(this));
        uint256 cash = spendableNet(buck);
        uint256 target = IArbBasket(address(this)).liquidityTarget();
        // An atomic cycle may temporarily use the basket's own liquid cash.
        // Preserve the net reserve at completion, or never worsen a reserve
        // deficit that already exists. No outside financing is introduced.
        c.reserveFloor = Math.min(cash, target);
        c.budget = c.walletNet == 0 ? 0 : IERC20(buck).balanceOf(address(this));
        // Flash arbitrage (owner, 2026-11-20): the wheel's arbFlash borrows each cycle's BUCK leg fresh
        // from the basket's credit line for the cycle's length (Buck.flashIssue), so the line's room
        // to issue, not the wallet, bounds it.
        c.flash = flashing(wheel);
        if (c.flash) c.budget = Math.max(c.budget, IBuckFlash(buck).flashRoom(IArbBasket(address(this)).creditId()));
    }

    /// @notice Whether the wheel has the basket borrow its cycles' BUCK by flash (a wheel without the
    ///         setting does not).
    function flashing(address wheel) public view returns (bool) {
        (bool ok, bytes memory r) = wheel.staticcall(abi.encodeWithSignature("arbFlash()"));
        return ok && r.length == 32 && abi.decode(r, (bool));
    }

    function _market(Context memory c, IArbMarkets w, uint256 index) private view returns (Market memory m) {
        (m.token, m.own, m.usd, m.index) = w.triangles(index);
        (address token, uint8 decimals,,,, address own, int24 lo, int24 hi,,,) =
            IArbBasket(address(this)).constituents(m.index);
        if (token != m.token || own != m.own || token == c.usd || m.own == m.usd
            || m.own == c.ub || m.usd == c.ub || decimals > 38) revert InvalidArbMarket();
        m.lower = lo; m.upper = hi; m.one = 10 ** decimals;
        (m.owned,,,,) = IUniswapV3Pool(own).positions(keccak256(abi.encodePacked(address(this), lo, hi)));
        if (m.owned != IArbBasket(address(this)).liquidityOf(m.index)) revert InvalidArbMarket();
        uint32 window = IArbBasket(address(this)).twapWindow();
        if (window == 0) revert InvalidArbMarket();
        // Use the existing guarded TWAP convention, frozen before execution.
        // No spot fallback and no re-marking a TOKEN to manufacture return.
        int24 tick = V3.consult(own, window);
        uint256 nominal = V3.getQuoteAtTick(tick, uint128(m.one), token, c.buck);
        (uint256 gross, uint256 net) = CarryRatio.read(c.buck, own);
        if (gross == 0 || net == 0) revert InvalidArbMarket();
        m.mark = CarryRatio.toNet(gross, net, nominal);
        if (m.mark == 0) revert InvalidArbMarket();
    }

    function _route(Context memory c, uint8 direction) private view returns (Route memory r) {
        Market memory a = c.markets[direction == 0 || c.count == 1 ? 0 : 1];
        r.length = c.count == 1 ? 3 : 4;
        if (c.count == 1 && direction == 1) {
            r.pools[0] = _pool(c, c.ub, c.buck, c.usd);
            r.pools[1] = _pool(c, a.usd, c.usd, a.token);
            r.pools[2] = _pool(c, a.own, a.token, c.buck);
            r.input = [c.buck, c.usd, a.token, address(0)];
        } else {
            r.pools[0] = _pool(c, a.own, c.buck, a.token);
            r.pools[1] = _pool(c, a.usd, a.token, c.usd);
            r.input = [c.buck, a.token, c.usd, address(0)];
            if (c.count == 1) r.pools[2] = _pool(c, c.ub, c.usd, c.buck);
            else {
                Market memory b = c.markets[direction == 0 ? 1 : 0];
                r.pools[2] = _pool(c, b.usd, c.usd, b.token);
                r.pools[3] = _pool(c, b.own, b.token, c.buck);
                r.input[3] = b.token;
            }
        }
    }

    function _pool(Context memory c, address pool, address input, address output) private view returns (Pool memory p) {
        IUniswapV3Pool v = IUniswapV3Pool(pool);
        p.pool = pool; p.token0 = v.token0(); p.token1 = v.token1();
        if (!((p.token0 == input && p.token1 == output) || (p.token1 == input && p.token0 == output))
            || v.factory() != IArbBasket(address(this)).v3Factory()) revert InvalidArbMarket();
        (p.sqrt,,,,, p.protocol,) = v.slot0();
        p.liquidity = v.liquidity(); p.fee = v.fee();
        if (p.sqrt == 0 || p.liquidity == 0) revert InvalidArbMarket();
        if (input == c.buck || output == c.buck) {
            if (!IBuckJubilee(c.buck).identity().isCarrying(pool)
                || !IBuckJubilee(c.buck).identity().isVerified(pool)) revert InvalidArbMarket();
            (p.gross, p.net) = IMarketCash(c.buck).accountBalancesScaled(pool);
            if (p.gross == 0 || p.net == 0) revert InvalidArbMarket();
        }
    }

    function _cap(Context memory c, Route memory r) private pure returns (uint256) {
        Pool memory p = r.pools[0];
        uint256 reserve = p.token0 == c.buck ? Math.mulDiv(p.liquidity, Q96, p.sqrt)
                                           : Math.mulDiv(p.liquidity, p.sqrt, Q96);
        return Math.min(c.budget, Math.mulDiv(reserve, c.cap, 10000));
    }

    function _estimate(Context memory c, Route memory r, uint256 amount, uint8 direction)
        private pure returns (IBasketArb.ArbPlan memory q)
    {
        if (amount == 0) return q;
        q.amount = amount; q.direction = direction;
        uint256 flowing = amount;
        uint256 finalNet;
        uint256 cashFees;
        for (uint256 k; k < r.length; k++) {
            Pool memory p = r.pools[k];
            (Pool memory afterPool, uint256 out, uint256 fee, uint256 receivedNet) = _step(c, p, r.input[k], flowing);
            if (out == 0) { q.totalReturn = -int256(amount); return q; }
            for (uint256 j; j < c.count; j++) {
                Market memory m = c.markets[j];
                if (m.own != p.pool) continue;
                uint256 ownedFee = Math.mulDiv(fee, m.owned, p.liquidity);
                uint256 feeValue = r.input[k] == c.buck ? CarryRatio.toNet(afterPool.gross, afterPool.net, ownedFee)
                    : Math.mulDiv(ownedFee, m.mark, m.one);
                q.lpFees += feeValue;
                if (r.input[k] == c.buck) cashFees += feeValue;
                q.lpReturn += _difference(_lpValue(c.buck, m, afterPool), _lpValue(c.buck, m, p)) + int256(feeValue);
            }
            flowing = out;
            if (k + 1 == r.length) finalNet = receivedNet;
        }
        q.cashReturn = _netDifference(finalNet, CarryRatio.toNetScaled(c.walletGross, c.walletNet, amount));
        q.totalReturn = q.cashReturn + q.lpReturn;
        if (int256(c.walletNet / Q) + q.cashReturn + int256(cashFees) < int256(c.reserveFloor)) {
            q.totalReturn = -1;
        }
    }

    function _step(Context memory c, Pool memory p, address input, uint256 amount)
        private pure returns (Pool memory a, uint256 output, uint256 lpFee, uint256 netOutput)
    {
        // Construct a fresh object: candidates must not mutate the original
        // memory pool and thereby change later estimates in the same search.
        a = Pool(p.pool, p.token0, p.token1, p.sqrt, p.liquidity, p.fee, p.protocol, p.gross, p.net);
        bool zero = p.token0 == input;
        uint256 effective = Math.mulDiv(amount, 1e6 - p.fee, 1e6);
        if (effective == 0) return (a, 0, 0, 0);
        uint256 s = zero
            ? Math.mulDiv(uint256(p.liquidity) << 96, p.sqrt,
                (uint256(p.liquidity) << 96) + effective * p.sqrt, Math.Rounding.Ceil)
            : uint256(p.sqrt) + Math.mulDiv(effective, Q96, p.liquidity);
        if (s <= 4295128739 || s >= 1461446703485210103287273052203988822378723970342) return (a, 0, 0, 0);
        a.sqrt = uint160(s);
        uint256 nominal = zero ? V3.getAmount1ForLiquidity(a.sqrt, p.sqrt, p.liquidity)
                               : V3.getAmount0ForLiquidity(p.sqrt, a.sqrt, p.liquidity);
        lpFee = amount - effective;
        uint8 protocol = zero ? p.protocol & 15 : p.protocol >> 4;
        if (protocol != 0) lpFee -= lpFee / protocol;
        output = nominal;
        if (input == c.buck) {
            a.gross += amount * Q;
            a.net += CarryRatio.toNetScaled(c.walletGross, c.walletNet, amount);
        }
        else if ((zero ? p.token1 : p.token0) == c.buck) {
            if (nominal * Q > p.gross) return (a, 0, 0, 0);
            uint256 netOut = CarryRatio.toNetScaled(p.gross, p.net, nominal);
            a.gross -= nominal * Q; a.net -= netOut;
            netOutput = netOut;
        }
    }

    function _lpValue(address buck, Market memory m, Pool memory p) private pure returns (uint256) {
        uint160 low = V3.getSqrtRatioAtTick(m.lower);
        uint160 high = V3.getSqrtRatioAtTick(m.upper);
        uint160 s = p.sqrt < low ? low : p.sqrt > high ? high : p.sqrt;
        uint256 a0 = V3.getAmount0ForLiquidity(s, high, m.owned);
        uint256 a1 = V3.getAmount1ForLiquidity(low, s, m.owned);
        (uint256 b, uint256 t) = p.token0 == buck ? (a0, a1) : (a1, a0);
        return CarryRatio.toNet(p.gross, p.net, b) + Math.mulDiv(t, m.mark, m.one);
    }

    function _value(Context memory c) private view returns (uint256 value) {
        value = IMarketCash(c.buck).netBalanceOf(address(this));
        for (uint256 j; j < c.count; j++) {
            Market memory m = c.markets[j];
            Pool memory p = _pool(c, m.own, c.buck, m.token);
            value += _lpValue(c.buck, m, p) + Math.mulDiv(IERC20(m.token).balanceOf(address(this)), m.mark, m.one);
        }
    }

    function _sync(Context memory c, mapping(uint256 => uint256) storage idle) private returns (uint256 value) {
        for (uint256 j; j < c.count; j++) {
            Market memory m = c.markets[j];
            uint256 beforeCash = IMarketCash(c.buck).netBalanceOf(address(this));
            (uint256 t,) = IBuckBasketVenue(address(this)).positionSync(m.index);
            idle[m.index] += t;
            value += IMarketCash(c.buck).netBalanceOf(address(this)) - beforeCash + Math.mulDiv(t, m.mark, m.one);
        }
    }

    function _difference(uint256 afterValue, uint256 beforeValue) private pure returns (int256) {
        if (afterValue > uint256(type(int256).max) || beforeValue > uint256(type(int256).max)) revert InvalidArbMarket();
        return int256(afterValue) - int256(beforeValue);
    }

    function _netDifference(uint256 afterValue, uint256 beforeValue) private pure returns (int256) {
        return afterValue >= beforeValue ? int256((afterValue - beforeValue) / Q)
            : -int256(Math.ceilDiv(beforeValue - afterValue, Q));
    }

    function _ratio(uint256 net, uint256 gross) private pure returns (uint256) {
        return gross == 0 ? 0 : Math.mulDiv(net, Q, gross);
    }
}
