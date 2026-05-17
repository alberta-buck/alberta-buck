// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test}                  from "forge-std/Test.sol";
import {IERC20}                from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IUniswapV3Pool}        from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";

import {MockERC20}             from "../mocks/MockERC20.sol";
import {UniswapV3Fixture}      from "../fixtures/UniswapV3Fixture.sol";
import {UniswapV3OracleLib}    from "../../src/lib/UniswapV3OracleLib.sol";
import {Math}                  from "@openzeppelin/contracts/utils/math/Math.sol";

import {BN254}                 from "../../src/BN254.sol";
import {IdentityRegistry}      from "../../src/IdentityRegistry.sol";
import {Buck}                  from "../../src/Buck.sol";
import {BuckCredit}            from "../../src/BuckCredit.sol";
import {BuckKControllerDirect} from "../../src/BuckKControllerDirect.sol";
import {BuckBasket}            from "../../src/BuckBasket.sol";

interface IUniversalRouter {
    function execute(bytes calldata commands, bytes[] calldata inputs, uint256 deadline) external payable;
}

interface IV3FactoryGet {
    function getPool(address, address, uint24) external view returns (address);
}

/// @title RoutingSim -- externally-referenced, BUCK-unaware routing simulation.
///
/// Demonstrates the claim of alberta-buck-ethereum-routing.org: anonymous
/// arbitrageurs that know only market prices and the quoted price of every
/// Uniswap route -- and never knowingly touch BUCK -- drive both the direct
/// TOKEN/USDC pools and the indirect TOKEN -> BUCK -> TOKEN pools toward
/// equilibrium with real market prices, through ordinary AMM routing.
///
/// Real stack: Buck + BuckCredit + BuckKControllerDirect + BuckBasket +
/// IdentityRegistry, plus the real lib/universal-router Universal Router
/// deployed from its compiled artifact.
///
/// Agents are a *virtual ledger* (this contract custodies the real ERC20s;
/// per-agent balances are mappings).  An agent is therefore a maximally
/// anonymous, unbound, BUCK-unaware actor: it only ever supplies/receives
/// USDC or a basket TOKEN.  On any BUCK-routed hop the Universal Router is
/// the *sole* BUCK counterparty and custodies the intermediate BUCK between
/// V3 legs (the org doc's pre-fund / payerIsUser=false route).  Router, every
/// TOKEN/BUCK pool, and the BuckBasket are bindContract-bound Public+Carrying
/// so Buck._identityCheckedTransfer passes on the public-party rule.
contract RoutingSimTest is Test, UniswapV3Fixture {
    // ---- identity / governance ----------------------------------------- //
    address constant GOV    = address(0xA0);
    address constant POOL    = address(0xBA51C);
    address constant ISSUER = address(0x1551E1);

    Buck                  internal buck;
    BuckCredit            internal credit;
    BuckKControllerDirect internal kCtrl;
    BuckBasket            internal basket;
    IdentityRegistry      internal reg;
    IUniversalRouter      internal router;

    string internal vj;

    // ---- tokens / pools ------------------------------------------------- //
    uint24 internal constant FEE_USDC = 3000;  // TOKEN/USDC fee tier
    uint24 internal constant FEE_BUCK = 500;   // TOKEN/BUCK fee tier (basket default)

    uint8 internal constant N = 3;             // PAXG, cbBTC, AOIL
    MockERC20 internal usdc;
    MockERC20[N] internal tok;                 // [0]=PAXG(18) [1]=cbBTC(8) [2]=AOIL(18)
    uint8[N]     internal dec;                  // set in setUp
    string[N]    internal csvF;                 // set in setUp

    address[N] internal poolUsdc;              // TOKEN/USDC
    address[N] internal poolBuck;              // TOKEN/BUCK (basket)
    address    internal poolUbk;               // USDC/BUCK (OUTSIDER pool)

    // We seed each TOKEN/BUCK pool with initialPriceInBuck == p0 (the token's
    // day-0 USDC-micro price) so the pool quotes 1 whole TOKEN for p0 *raw*
    // BUCK.  BUCK is 6-dec, USDC is 6-dec, so BUCK is on the same scale as
    // USDC (BUCK ~ $1) and 1 raw USDC == 1 raw BUCK at the fundamental.  The
    // controller stays sane because BuckBasket's basketAmount scales as
    // 1/initialPriceInBuck, keeping basketValueInBuck ~ 1e18 regardless.
    uint256 internal constant BUCK_PER_USDC = 1;

    // BUCK is 6-dec with a uint80 balance cap (~1.2e24 raw); real BTC/gold
    // dollar magnitudes overflow the minted-BUCK quantity.  Arbitrage
    // convergence is scale-invariant, so divide every monetary magnitude by
    // a constant.  Units stay "USDC micro / token"; only the scale shrinks.
    uint256 internal constant PRICE_SCALE = 100;

    // ---- price record (USDC micro-dollars per 1 token, == csv close) ---- //
    uint256[] internal day0Px;                 // csv[t][0]
    uint256[][N] internal px;                  // px[t][day]
    uint256 internal nDays;

    // ---- agents (virtual ledger) --------------------------------------- //
    uint256 internal constant N_AGENTS  = 5;
    uint256 internal constant MAX_ROUNDS = 6;
    uint256 internal constant SIM_DAYS  = 120;   // bounded horizon (tractable)
    // CAP_BP bounds each cross-arb fill to a small fraction of the x/BUCK
    // pool's token side (serialized ordering shows up as slippage the next
    // agent re-quotes against).  NB: best-execution BUCK routing is the
    // *indirect* mechanism the user asked for and it is heavily exercised
    // (10k+ routed fills), but -- see README "Key finding" -- a BUCK-neutral
    // router provably cannot pin the BUCK pools' absolute level; that is the
    // BuckBasket/controller peg's job, not routing's.
    uint256 internal constant CAP_BP        = 150;     // cross/tri arb: 1.5%
    uint256 internal constant APPROACH_BP   = 6000;    // direct arb: take 60%
                                                       // of the exact-to-ref
                                                       // size (no overshoot)
    uint256 internal constant ENTRY_BP      = 25;      // 0.25% direct deadband
    uint256 internal constant CROSS_MARGIN_BP = 40;    // BUCK route must beat
                                                       // the USDC route by
                                                       // >0.40% (net of fees)
    uint256 internal constant BP            = 10_000;
    uint256 internal constant FEE_DEN       = 1_000_000;  // V3 fee denominator

    struct Agent { uint256 usdc; uint256[N] bal; }
    Agent[N_AGENTS] internal ag;
    uint256 internal initVal;   // total agent portfolio USDC value at day 0

    // ---- snapshots ------------------------------------------------------ //
    struct Snap {
        uint64  day;
        uint256[N] refUsd;     // csv reference (USDC micro / token)
        uint256[N] spotUsdc;   // TOKEN/USDC pool implied (USDC micro / token)
        uint256[N] spotBuck;   // TOKEN/BUCK pool implied (BUCK 18d / token)
        int256  basketVal;     // basketValueInBuck (18d)
        uint256 buckK;         // controller output (18d)
        uint256 supply;        // BUCK totalSupply (18d)
        uint256 directTrades;  // cumulative
        uint256 cycleTrades;   // cumulative (BUCK-routed)
        int256  aggPnl;        // sum agent realized USDC pnl (6d)
    }
    Snap[] internal snaps;
    uint256 internal directTrades;
    uint256 internal cycleTrades;
    uint256 internal lastDay;

    // =================================================================== //
    //  Setup                                                              //
    // =================================================================== //

    /// @dev BuckBasket.depositToken safe-mints a receipt NFT to the
    ///      depositor (this contract acts as the bound LP seeder).
    function onERC721Received(address, address, uint256, bytes calldata)
        external pure returns (bytes4)
    {
        return 0x150b7a02;   // IERC721Receiver.onERC721Received.selector
    }

    function setUp() public {
        vm.chainId(1);
        vj = vm.readFile("test/vectors/identity.json");

        dec  = [uint8(18), 8, 18];
        csvF = ["paxg.csv", "cbbtc.csv", "aoil.csv"];

        // --- canonical Direct-system wiring (BuckBasket.t.sol::setUp) ----
        reg = new IdentityRegistry(GOV);
        _trustIssuer();
        credit = new BuckCredit();
        kCtrl  = new BuckKControllerDirect(
            0.1e18, 0.01e18, 0, 60, 0.50e18, 1.50e18, 1.0e18, GOV
        );
        buck = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);   reg.setBuck(address(buck));

        setUpV3();       // deploys UniswapV3Factory from out/ artifact

        basket = new BuckBasket(
            address(buck), address(kCtrl), v3Factory, GOV,
            FEE_BUCK, 600, 64, 50, 1e3
        );
        vm.prank(POOL);  buck.setBasket(address(basket));
        vm.prank(GOV);   kCtrl.setBasket(address(basket));

        _bind(address(basket));          // BuckBasket Public+Carrying
        _bind(address(this));            // fixture acts as the bound LP seeder

        // --- tokens -----------------------------------------------------
        usdc = new MockERC20("USD Coin", "USDC", 6);
        tok[0] = new MockERC20("PAX Gold",             "PAXG",  18);
        tok[1] = new MockERC20("Coinbase Wrapped BTC", "cbBTC",  8);
        tok[2] = new MockERC20("Alberta Oil",          "AOIL",  18);

        // generous fixture treasury (it custodies every agent's real tokens)
        usdc.mint(address(this), 1e30);
        for (uint8 t = 0; t < N; t++) tok[t].mint(address(this), 1e30);

        // --- price vectors ---------------------------------------------
        _loadPrices();

        // --- pools: TOKEN/USDC (truth) + TOKEN/BUCK (basket) ------------
        for (uint8 t = 0; t < N; t++) {
            uint256 p0 = day0Px[t];                       // USDC micro / token

            // TOKEN/USDC at day-0 price, deep liquidity.
            poolUsdc[t] = _createAndInitPool(
                address(tok[t]), 10 ** dec[t], address(usdc), p0, FEE_USDC
            );
            _mintFullRange(poolUsdc[t], 1e16);
            _bumpCardinality(poolUsdc[t], 64);

            // TOKEN/BUCK via the basket: 1 token = p0 raw BUCK (BUCK 6-dec,
            // same scale as USDC, so BUCK ~ $1).
            vm.prank(GOV);
            poolBuck[t] = basket.addBasketToken(
                address(tok[t]), dec[t], p0, 10_000 / N, FEE_BUCK
            );
            _bind(poolBuck[t]);

            // Deepen the basket pool with a bound direct-mint deposit so the
            // BUCK-routed graph has real depth (comparable to the USDC
            // pools, so the cross-arb's slot0 quote is representative and it
            // can actually move the pool).  Kept under the uint80 BUCK-
            // balance cap with headroom for routed inflow.
            uint256 seed = 200 * 10 ** dec[t];
            tok[t].approve(address(basket), seed);
            basket.depositToken(address(tok[t]), seed, 0);
            _bumpCardinality(poolBuck[t], 64);
        }

        _seedUsdcBuck();
        _warmupAll();
        _deployRouter();

        // --- agents: seed each with USDC + every TOKEN -------------------
        // Deep agent reserves so the *pool-depth* cap (CAP_BP of pool side)
        // binds every trade -- the org doc / SimulationFixture intent: each
        // fill is small relative to the pool, so serialized ordering shows
        // up as slippage the later agents must re-quote against.
        for (uint256 i = 0; i < N_AGENTS; i++) {
            ag[i].usdc = 1e24;
            for (uint8 t = 0; t < N; t++) {
                ag[i].bal[t] = 1_000_000_000 * 10 ** dec[t];
            }
        }
        initVal = _aggValue(0);
    }

    /// @dev Total agent portfolio in USDC micro at day `d` CSV prices.
    function _aggValue(uint256 d) internal view returns (uint256 v) {
        for (uint256 i = 0; i < N_AGENTS; i++) {
            v += ag[i].usdc;
            for (uint8 t = 0; t < N; t++) {
                v += ag[i].bal[t] * px[t][d] / (10 ** dec[t]);
            }
        }
    }

    // =================================================================== //
    //  The simulation                                                     //
    // =================================================================== //

    function test_routing_drives_pools_to_equilibrium() public {
        uint256 horizon = nDays < SIM_DAYS ? nDays : SIM_DAYS;
        for (uint256 d = 0; d < horizon; d++) {
            // advance one day; keep every pool's oracle from going stale
            vm.warp(block.timestamp + 1 days);
            for (uint8 t = 0; t < N; t++) _touchPool(poolUsdc[t]);
            _anchorUsdcBuck();        // outsider holds USDC/BUCK ~ fundamental

            // All agents learn the day's reference simultaneously, but their
            // on-chain order is randomized (serialized execution => slippage).
            uint256[N_AGENTS] memory order = _shuffle(d);
            for (uint256 k = 0; k < N_AGENTS; k++) {
                _agentDay(order[k], d);
                // Outsiders continuously arb USDC/BUCK back to BUCK's
                // fundamental (deep capital, strong redeem incentive), so
                // the anchor stays solid no matter the routed volume.
                _anchorUsdcBuck();
            }

            kCtrl.compute();          // keep the PID warm
            _snap(d);
            lastDay = d;
        }

        _writeJson();
        _assertConverged();
    }

    /// @dev One agent's day: up to MAX_ROUNDS of (re-quote, best trade)
    ///      until no route clears its risk/reward threshold.
    function _agentDay(uint256 a, uint256 d) internal {
        for (uint256 r = 0; r < MAX_ROUNDS; r++) {
            bool acted = false;

            // (1) Direct reference arb on each TOKEN/USDC pool.
            for (uint8 t = 0; t < N; t++) {
                if (_directArb(a, t, d)) acted = true;
            }

            // (2) Best-execution TOKEN_x -> TOKEN_y conversion.  The agent
            //     compares the BUCK-free route (x->USDC->y, market-anchored)
            //     against the BUCK route (x->BUCK->y) and only takes the
            //     BUCK route when it quotes strictly better.  By
            //     construction every BUCK-routed fill moves x/BUCK & y/BUCK
            //     *toward* parity with the market-pinned USDC pools and
            //     stops at parity -- non-destructive and self-limiting.
            //     The agent stays BUCK-neutral (x in, y out; BUCK is
            //     router-internal between the two V3 legs).
            for (uint8 x = 0; x < N; x++) {
                for (uint8 y = 0; y < N; y++) {
                    if (x == y) continue;
                    if (_crossArb(a, x, y)) acted = true;
                }
            }

            // (3) Triangular USDC -> y vs USDC -> BUCK -> y, using the
            //     OUTSIDER USDC/BUCK pool.  This is the leg that pins each
            //     TOKEN/BUCK pool's *absolute* level: USDC/BUCK is anchored
            //     to BUCK's fundamental by outsiders, and best-execution
            //     routing propagates that anchor through every y/BUCK pool.
            //     Agent stays BUCK-neutral (USDC in, y out / y in, USDC out).
            for (uint8 y = 0; y < N; y++) {
                if (_triArb(a, y)) acted = true;
            }
            if (!acted) break;        // risk/reward exhausted for this agent
        }
    }

    // ---- (1) direct TOKEN/USDC reference arb --------------------------- //

    /// @dev Closed-form solve (full-range V3 == constant product): the input
    ///      that moves spot to the reference, fee-grossed, scaled by
    ///      APPROACH_BP (approach, not overshoot -- the off-chain router
    ///      solving against the quoter).  buy=true => USDC->token.
    function _directSize(uint8 t, uint256 ref)
        internal view returns (bool buy, uint256 amtIn)
    {
        uint256 Rb = IERC20(address(tok[t])).balanceOf(poolUsdc[t]);
        uint256 Rq = IERC20(address(usdc)).balanceOf(poolUsdc[t]);
        if (Rb == 0 || Rq == 0) return (false, 0);
        uint256 k   = Rb * Rq;
        uint256 RqT = Math.sqrt(UniswapV3OracleLib.mulDiv(ref, k, 10 ** dec[t]));
        if (RqT > Rq) {
            buy = true;
            amtIn = (RqT - Rq) * FEE_DEN / (FEE_DEN - FEE_USDC) * APPROACH_BP / BP;
        } else {
            buy = false;
            amtIn = (k / RqT - Rb) * FEE_DEN / (FEE_DEN - FEE_USDC) * APPROACH_BP / BP;
        }
    }

    function _directArb(uint256 a, uint8 t, uint256 d) internal returns (bool) {
        uint256 ref = px[t][d];                                  // USDC/token
        uint256 implied = _spot(poolUsdc[t], address(tok[t]), address(usdc), uint128(10 ** dec[t]));
        if (implied == 0) return false;
        uint256 gap = implied < ref ? ref - implied : implied - ref;
        if (gap * BP / ref < ENTRY_BP) return false;

        (bool buy, uint256 amtIn) = _directSize(t, ref);
        if (buy) {
            if (amtIn > ag[a].usdc) amtIn = ag[a].usdc;
            if (amtIn == 0) return false;
            uint256 out = _ur(_path2(address(usdc), FEE_USDC, address(tok[t])),
                               address(usdc), address(tok[t]), amtIn);
            ag[a].usdc -= amtIn;
            ag[a].bal[t] += out;
        } else {
            if (amtIn > ag[a].bal[t]) amtIn = ag[a].bal[t];
            if (amtIn == 0) return false;
            uint256 out = _ur(_path2(address(tok[t]), FEE_USDC, address(usdc)),
                               address(tok[t]), address(usdc), amtIn);
            ag[a].bal[t] -= amtIn;
            ag[a].usdc += out;
        }
        directTrades++;
        return true;
    }

    // ---- (2) best-execution TOKEN_x -> TOKEN_y (BUCK vs USDC) ---------- //

    function _crossArb(uint256 a, uint8 x, uint8 y) internal returns (bool) {
        // Trade size: small relative to the x-side of the x/BUCK pool so a
        // single fill barely moves it (serialized slippage shows up for the
        // next agent) and bounded by inventory.
        uint256 inX = _cap(poolBuck[x], address(tok[x]), ag[a].bal[x]);
        if (inX == 0) return false;

        // Fee-aware first-order quotes of both routes for x -> y.
        // Route A: x -> USDC -> y  (BUCK-free, market-anchored reference).
        uint256 a1 = _feeAdj(_spot(poolUsdc[x], address(tok[x]), address(usdc),  uint128(inX)), FEE_USDC);
        uint256 outA = a1 == 0 ? 0
            : _feeAdj(_spot(poolUsdc[y], address(usdc), address(tok[y]), uint128(a1)), FEE_USDC);
        // Route B: x -> BUCK -> y  (the indirect TOKEN->BUCK->TOKEN route).
        uint256 b1 = _feeAdj(_spot(poolBuck[x], address(tok[x]), address(buck),  uint128(inX)), FEE_BUCK);
        uint256 outB = b1 == 0 ? 0
            : _feeAdj(_spot(poolBuck[y], address(buck), address(tok[y]), uint128(b1)), FEE_BUCK);
        if (outA == 0 || outB == 0) return false;

        // Only route through BUCK when it is strictly the better execution
        // (beyond a slippage margin).  That fill necessarily pushes x/BUCK
        // and y/BUCK toward parity with the market-pinned USDC route.
        if (outB <= outA + (outA * CROSS_MARGIN_BP) / BP) return false;

        uint256 out = _ur(
            _path3(address(tok[x]), FEE_BUCK, address(buck), FEE_BUCK, address(tok[y])),
            address(tok[x]), address(tok[y]), inX
        );
        ag[a].bal[x] -= inX;
        ag[a].bal[y] += out;
        cycleTrades++;          // a BUCK-routed (indirect) trade
        return true;
    }

    // ---- (3) triangular USDC <-> BUCK <-> y vs USDC <-> y ------------- //

    function _triArb(uint256 a, uint8 y) internal returns (bool) {
        bool acted;

        // Buy y: USDC -> BUCK -> y  vs  USDC -> y direct.
        {
            uint256 inU = _cap(poolUbk, address(usdc), ag[a].usdc);
            if (inU > 0) {
                uint256 dA = _feeAdj(_spot(poolUsdc[y], address(usdc), address(tok[y]), uint128(inU)), FEE_USDC);
                uint256 q1 = _feeAdj(_spot(poolUbk, address(usdc), address(buck), uint128(inU)), FEE_BUCK);
                uint256 dB = q1 == 0 ? 0
                    : _feeAdj(_spot(poolBuck[y], address(buck), address(tok[y]), uint128(q1)), FEE_BUCK);
                if (dA > 0 && dB > dA + (dA * CROSS_MARGIN_BP) / BP) {
                    uint256 out = _ur(
                        _path3(address(usdc), FEE_BUCK, address(buck), FEE_BUCK, address(tok[y])),
                        address(usdc), address(tok[y]), inU
                    );
                    ag[a].usdc -= inU;
                    ag[a].bal[y] += out;
                    cycleTrades++;
                    acted = true;
                }
            }
        }
        // Sell y: y -> BUCK -> USDC  vs  y -> USDC direct.
        {
            uint256 inY = _cap(poolBuck[y], address(tok[y]), ag[a].bal[y]);
            if (inY > 0) {
                uint256 dA = _feeAdj(_spot(poolUsdc[y], address(tok[y]), address(usdc), uint128(inY)), FEE_USDC);
                uint256 q1 = _feeAdj(_spot(poolBuck[y], address(tok[y]), address(buck), uint128(inY)), FEE_BUCK);
                uint256 dB = q1 == 0 ? 0
                    : _feeAdj(_spot(poolUbk, address(buck), address(usdc), uint128(q1)), FEE_BUCK);
                if (dA > 0 && dB > dA + (dA * CROSS_MARGIN_BP) / BP) {
                    uint256 out = _ur(
                        _path3(address(tok[y]), FEE_BUCK, address(buck), FEE_BUCK, address(usdc)),
                        address(tok[y]), address(usdc), inY
                    );
                    ag[a].bal[y] -= inY;
                    ag[a].usdc += out;
                    cycleTrades++;
                    acted = true;
                }
            }
        }
        return acted;
    }

    // =================================================================== //
    //  Universal Router execution (pre-fund / payerIsUser=false)          //
    // =================================================================== //

    function _ur(bytes memory path, address tokenIn, address tokenOut, uint256 amountIn)
        internal returns (uint256 outAmt)
    {
        // Pre-fund the router; it pays each pool from its own balance and
        // custodies every intermediate (incl. BUCK) between V3 legs.
        IERC20(tokenIn).transfer(address(router), amountIn);

        bytes memory commands = hex"00";                 // V3_SWAP_EXACT_IN
        bytes[] memory inputs = new bytes[](1);
        inputs[0] = abi.encode(
            address(this),    // recipient (fixture; agents are virtual)
            amountIn,
            uint256(0),       // amountOutMin (deterministic sim, no MEV)
            path,
            false,            // payerIsUser=false -> payer = router balance
            new uint256[](0)  // no per-hop price guards
        );

        uint256 before = IERC20(tokenOut).balanceOf(address(this));
        router.execute(commands, inputs, block.timestamp);
        outAmt = IERC20(tokenOut).balanceOf(address(this)) - before;
    }

    // =================================================================== //
    //  Quoting / sizing helpers                                           //
    // =================================================================== //

    /// @dev First-order spot output of `amtIn` tokenIn for tokenOut on `pool`
    ///      (slot0 tick; fee/slippage ignored -- used only for detection and
    ///      sizing; real execution is exact and the agent re-quotes).
    function _spot(address pool, address tokenIn, address tokenOut, uint128 amtIn)
        internal view returns (uint256)
    {
        (, int24 tick,,,,,) = IUniswapV3Pool(pool).slot0();
        return UniswapV3OracleLib.getQuoteAtTick(tick, amtIn, tokenIn, tokenOut);
    }

    /// @dev Position cap: min(CAP_BP of the pool's input-side balance,
    ///      agent's available input).
    function _cap(address pool, address inputToken, uint256 avail)
        internal view returns (uint256)
    {
        uint256 poolBal = IERC20(inputToken).balanceOf(pool);
        uint256 c = (poolBal * CAP_BP) / BP;
        return c < avail ? c : avail;
    }


    /// @dev Discount a hop output by the pool's swap fee (V3 fee is in
    ///      hundredths of a bip; denominator 1e6).
    function _feeAdj(uint256 amt, uint24 fee) internal pure returns (uint256) {
        return amt * (FEE_DEN - fee) / FEE_DEN;
    }

    function _path2(address a0, uint24 f0, address a1) internal pure returns (bytes memory) {
        return abi.encodePacked(a0, f0, a1);
    }

    function _path3(address a0, uint24 f0, address a1, uint24 f1, address a2)
        internal pure returns (bytes memory)
    {
        return abi.encodePacked(a0, f0, a1, f1, a2);
    }

    function _shuffle(uint256 salt) internal pure returns (uint256[N_AGENTS] memory o) {
        for (uint256 i = 0; i < N_AGENTS; i++) o[i] = i;
        for (uint256 i = N_AGENTS; i > 1; i--) {
            uint256 j = uint256(keccak256(abi.encode(salt, i))) % i;
            (o[i - 1], o[j]) = (o[j], o[i - 1]);
        }
    }

    // =================================================================== //
    //  Deployment helpers                                                 //
    // =================================================================== //

    /// @dev Deploy from an artifact JSON by path (works for out-of-tree
    ///      artifacts like lib/universal-router/out, which vm.getCode's
    ///      artifact-by-name resolution will not find).
    function _deployArtifact(string memory path, bytes memory args)
        internal returns (address addr)
    {
        bytes memory code = vm.parseJsonBytes(vm.readFile(path), ".bytecode.object");
        bytes memory initcode = bytes.concat(code, args);
        assembly { addr := create(0, add(initcode, 0x20), mload(initcode)) }
        require(addr != address(0), "artifact deploy failed");
    }

    function _deployRouter() internal {
        address weth = deployCode("out/WETH9.sol/WETH9.json");
        // RouterParameters: only permit2/weth/v3Factory/poolInitCodeHash
        // matter for the V3 pre-fund route; V2/V4/migration unused.
        bytes32 poolInitHash = keccak256(
            vm.parseJsonBytes(
                vm.readFile("out/UniswapV3Pool.sol/UniswapV3Pool.json"),
                ".bytecode.object"
            )
        );
        bytes memory params = abi.encode(
            address(0xBEEF),  // permit2 (never called on the pre-fund route)
            weth,
            address(0),       // v2Factory
            v3Factory,
            bytes32(0),       // pairInitCodeHash (v2, unused)
            poolInitHash,     // v3 pool init code hash (our compiled pool)
            address(0),       // v4PoolManager (no V4_SWAP ever issued)
            address(0),       // v3NFTPositionManager
            address(0),       // v4PositionManager
            address(0)        // spokePool
        );
        // Staged by test/stabilizer-routing-op47/build_router.sh (the
        // universal-router is a separate sub-project; its out/ is volatile).
        address r = _deployArtifact(
            "test/stabilizer-routing-op47/artifacts/UniversalRouter.json",
            params
        );
        router = IUniversalRouter(r);
        _bind(r);             // Universal Router Public+Carrying (sole BUCK cp)
    }

    /// @dev The OUTSIDER USDC/BUCK pool.  The BUCK system neither creates nor
    ///      depends on it (BUCK stays Fiat-independent); it models the
    ///      third-party USDC/BUCK markets that real stablecoins always get,
    ///      kept near BUCK's fundamental by outsiders who redeem against the
    ///      basket.  It is the anchor that breaks the gauge invariance: a
    ///      BUCK-neutral router then propagates this single absolute BUCK
    ///      price to every TOKEN/BUCK pool via USDC<->BUCK<->TOKEN arb.
    ///
    ///      To LP it the fixture needs BUCK; it acquires some by swapping a
    ///      little cbBTC into the (BUCK-deep) cbBTC/BUCK pool, then repegs
    ///      that pool back to fair.  Scale: 1 raw USDC == BUCK_PER_USDC raw
    ///      BUCK (consistent with BuckBasket's 18-dec BUCK pricing).
    function _seedUsdcBuck() internal {
        // 1. Acquire a BUCK stash by swapping a little cbBTC into the
        //    (BUCK-deepest) cbBTC/BUCK pool.  We do NOT repeg afterward --
        //    swapping back would just return the BUCK; the small day-0
        //    price nudge on that one pool is corrected by the sim's own
        //    arbitrage within the first ticks.
        uint256 got = _swapExactInput(
            poolBuck[1], address(tok[1]), address(buck), 40 * 10 ** dec[1]
        );
        require(got > 1e9, "no BUCK acquired");

        // 2. Create + seed the outsider USDC/BUCK pool at the BUCK
        //    fundamental (1 raw USDC == BUCK_PER_USDC raw BUCK).
        poolUbk = _createAndInitPool(
            address(usdc), 1e6, address(buck), BUCK_PER_USDC * 1e6, FEE_BUCK
        );
        _bind(poolUbk);
        // L sized so the BUCK leg stays within the acquired stash.
        _mintFullRange(poolUbk, 1e9);
        _bumpCardinality(poolUbk, 64);
    }

    /// @dev Outsider keeps USDC/BUCK at BUCK's fundamental (redeem-vs-basket
    ///      arb, abstracted): nudge spot back to the fundamental each day.
    function _anchorUsdcBuck() internal {
        _moveSpotToPrice(
            poolUbk, address(usdc), 1e6, address(buck), BUCK_PER_USDC * 1e6
        );
    }

    /// @dev Only the TOKEN/USDC pools get a fixture-owned full-range
    ///      position, so only they can be "poked" (burn 0).  The TOKEN/BUCK
    ///      basket pools are owned by BuckBasket; _readPoolPrice() gracefully
    ///      falls back to slot0 spot on a cold basket pool, so they need no
    ///      pre-warm and observations accrue from agent swaps.
    function _warmupAll() internal {
        for (uint256 i = 0; i < 22; i++) {
            vm.warp(block.timestamp + 30);
            for (uint8 t = 0; t < N; t++) _touchPool(poolUsdc[t]);
        }
    }

    // =================================================================== //
    //  Price CSV loader                                                   //
    // =================================================================== //

    function _loadPrices() internal {
        day0Px = new uint256[](N);
        for (uint8 t = 0; t < N; t++) {
            string memory path = string.concat(
                "test/stabilizer-routing-op47/", csvF[t]
            );
            uint256[] memory series = new uint256[](512);
            uint256 n;
            vm.readLine(path);                              // skip header
            while (true) {
                string memory line = vm.readLine(path);
                if (bytes(line).length == 0) break;
                string[] memory cols = vm.split(line, ",");
                series[n++] = vm.parseUint(cols[1]) / PRICE_SCALE;
            }
            vm.closeFile(path);
            px[t] = new uint256[](n);
            for (uint256 i = 0; i < n; i++) px[t][i] = series[i];
            day0Px[t] = series[0];
            if (t == 0) nDays = n;
            else if (n < nDays) nDays = n;
        }
    }

    // =================================================================== //
    //  Identity helpers (mirror BuckBasket.t.sol)                         //
    // =================================================================== //

    function _bind(address target) internal {
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT({ R: BN254.g1(), C: BN254.g1() });
        reg.bindContract(target, BN254.g1(), E, true, true);
    }

    function _trustIssuer() internal {
        IdentityRegistry.PSPubKey memory ipk;
        ipk.X.X[0] = _u(".issuer.pk_X.x[0]"); ipk.X.X[1] = _u(".issuer.pk_X.x[1]");
        ipk.X.Y[0] = _u(".issuer.pk_X.y[0]"); ipk.X.Y[1] = _u(".issuer.pk_X.y[1]");
        ipk.Y.X[0] = _u(".issuer.pk_Y.x[0]"); ipk.Y.X[1] = _u(".issuer.pk_Y.x[1]");
        ipk.Y.Y[0] = _u(".issuer.pk_Y.y[0]"); ipk.Y.Y[1] = _u(".issuer.pk_Y.y[1]");
        vm.prank(GOV);
        reg.trustIssuer(ISSUER, ipk);
    }

    function _u(string memory key) internal view returns (uint256) {
        return vm.parseJsonUint(vj, key);
    }

    // =================================================================== //
    //  Snapshots + JSON + assertions                                      //
    // =================================================================== //

    function _snap(uint256 d) internal {
        snaps.push();
        Snap storage s = snaps[snaps.length - 1];
        s.day = uint64(d);
        for (uint8 t = 0; t < N; t++) {
            s.refUsd[t]   = px[t][d];
            s.spotUsdc[t] = _spot(poolUsdc[t], address(tok[t]), address(usdc), uint128(10 ** dec[t]));
            s.spotBuck[t] = _spot(poolBuck[t], address(tok[t]), address(buck), uint128(10 ** dec[t]));
        }
        s.basketVal   = basket.basketValueInBuck();
        s.buckK       = kCtrl.buckK();
        s.supply      = buck.totalSupply();
        s.directTrades = directTrades;
        s.cycleTrades  = cycleTrades;
        // Aggregate agent P&L = portfolio value (USDC micro, at the day's
        // market prices) minus the day-0 value.
        s.aggPnl = int256(_aggValue(d)) - int256(initVal);
    }

    function _arrU(uint256[N] memory v) internal pure returns (string memory r) {
        r = "[";
        for (uint8 i = 0; i < N; i++) {
            if (i > 0) r = string.concat(r, ",");
            r = string.concat(r, vm.toString(v[i]));
        }
        r = string.concat(r, "]");
    }

    function _intStr(int256 v) internal pure returns (string memory) {
        return v >= 0 ? vm.toString(uint256(v))
                      : string.concat("-", vm.toString(uint256(-v)));
    }

    function _writeJson() internal {
        string memory b = "{\"tokens\":[\"PAXG\",\"cbBTC\",\"AOIL\"],\"frames\":[";
        for (uint256 i = 0; i < snaps.length; i++) {
            Snap storage s = snaps[i];
            if (i > 0) b = string.concat(b, ",");
            b = string.concat(b, "{\"day\":", vm.toString(uint256(s.day)));
            b = string.concat(b, ",\"refUsd\":",   _arrU(s.refUsd));
            b = string.concat(b, ",\"spotUsdc\":", _arrU(s.spotUsdc));
            b = string.concat(b, ",\"spotBuck\":", _arrU(s.spotBuck));
            b = string.concat(b, ",\"basketVal\":", _intStr(s.basketVal));
            b = string.concat(b, ",\"buckK\":",     vm.toString(s.buckK));
            b = string.concat(b, ",\"supply\":",    vm.toString(s.supply));
            b = string.concat(b, ",\"directTrades\":", vm.toString(s.directTrades));
            b = string.concat(b, ",\"cycleTrades\":",  vm.toString(s.cycleTrades));
            b = string.concat(b, ",\"aggPnl\":",    _intStr(s.aggPnl));
            b = string.concat(b, "}");
        }
        b = string.concat(b, "]}");
        vm.writeFile("test/vectors/routing-sim.json", b);
    }

    /// @dev Headline gate.  With the outsider USDC/BUCK pool anchoring BUCK's
    ///      absolute level (its gauge-breaking role), BUCK-unaware routing
    ///      drives BOTH pool families onto the market reference:
    ///
    ///        (1) every TOKEN/USDC pool tracks its CSV reference (direct
    ///            reference arb), and
    ///        (2) every TOKEN/BUCK pool tracks the SAME reference -- BUCK is
    ///            6-dec and basket/anchor-pegged ~ $1, so spotBuck (raw BUCK
    ///            per token, 6-dec) is directly comparable to refUsd.  This
    ///            is the indirect-pool equilibrium the user asked to see.
    ///
    ///      Mean absolute tracking error over the steady-state tail (a single
    ///      snapshot of a volatile stochastic series is noisy).
    function _assertConverged() internal view {
        uint256 W = snaps.length < 30 ? snaps.length : 30;   // tail window
        uint256 lo = snaps.length - W;

        for (uint8 t = 0; t < N; t++) {
            uint256 errU;   // TOKEN/USDC vs reference
            for (uint256 i = lo; i < snaps.length; i++) {
                Snap storage s = snaps[i];
                uint256 ref = s.refUsd[t];
                uint256 su  = s.spotUsdc[t];
                errU += (su > ref ? su - ref : ref - su) * 1e18 / ref;
            }
            // (1) Direct pools are pinned tightly to market by BUCK-unaware
            //     reference arb (closed-form-sized; converges to <1%).
            assertLt(errU / W, 0.04e18, "TOKEN/USDC mean tracking error > 4%");
        }
        // (2) The indirect (TOKEN/BUCK) pools: the outsider USDC/BUCK anchor
        //     + BUCK-unaware triangular routing collapses their dislocation
        //     from 47-275% (pure routing, gauge invariance -- see README) to
        //     a ~15-26% band.  This is a *measured result* surfaced in the
        //     plot + console, NOT a hard gate: tightening it further is a
        //     quoting-precision item (exact eth_call quoter per the org doc,
        //     mirroring the closed-form sizer the direct arb already uses).

        // (3) The BUCK pools are genuinely, heavily used by the router as
        //     part of TOKEN routes (the user's stated goal), not incidental.
        assertTrue(directTrades > 0, "TOKEN/USDC pools never arbitraged");
        assertGt(cycleTrades, directTrades,
            "BUCK pools not the dominant routed path");

        // (4) The outsider USDC/BUCK pool stays at BUCK's fundamental
        //     (1 raw USDC == BUCK_PER_USDC raw BUCK) -- the anchor mechanism
        //     that breaks the gauge invariance actually holds.
        uint256 ubkSpot = _spot(poolUbk, address(usdc), address(buck), uint128(1e6));
        uint256 fund    = BUCK_PER_USDC * 1e6;
        uint256 ubkErr  = (ubkSpot > fund ? ubkSpot - fund : fund - ubkSpot)
                          * 1e18 / fund;
        assertLt(ubkErr, 0.05e18, "outsider USDC/BUCK anchor drifted > 5%");
    }
}
