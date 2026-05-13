// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BuckKController}    from "../../src/BuckKController.sol";
import {UniswapV3OracleLib} from "../../src/lib/UniswapV3OracleLib.sol";
import {MockERC20}          from "../mocks/MockERC20.sol";
import {UniswapV3Fixture}   from "./UniswapV3Fixture.sol";
import {IUniswapV3Pool}     from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";

/// @title SimulationFixture -- Bob/Alice multi-actor PID simulation harness.
///
/// Phase (a) of the arbitrage validation: pool-only.  No Buck or Identity
/// contracts; "mints" and "burns" are synthetic transfers in/out of a dead
/// address.  What this validates is the BuckKController + arbitrageur dynamics
/// against a realistic stream of Bob lifecycle events.
///
/// Accounting: this fixture's address holds all real ERC20 balances.  Per-
/// actor BUCK and USDT balances are virtual (mappings on this contract).
/// Pool callbacks already pull from address(this); the bookkeeping just
/// reassigns ownership of those tokens between Alice / Bobs / the LP / the
/// "dead" pile.  The invariant is that for each token X,
///     ERC20(X).balanceOf(address(this))
///   ==  sum(actor_balance_X) + tokens_locked_in_LP_position
/// at all times outside of an active swap callback.
///
/// Bob lifecycle (random):
///   t=arrive   -- bob is "minted" mintAmount BUCK (received from the BUCK
///                 reserve mintable to address(this)), then immediately swaps
///                 (1 - keepFraction) of it for USDT in the BUCK/USDT pool.
///   t=retire   -- bob needs to retire mintAmount BUCK.  His USDT balance
///                 swaps for the deficit (= mintAmount - currentBuck).  If
///                 BUCK has appreciated, he pays MORE USDT than he received
///                 -- a loss.  If BUCK has depreciated, he pays less -- a
///                 gain.  Either way the retirement swap pushes BUCK price
///                 toward parity (always a buy).  The retired BUCK is
///                 transferred to address(0xdead).
///
/// Alice strategy (threshold-based):
///   - Read TWAP and basket cost.  Compute err = (basket - twap) / basket.
///   - Entry threshold 3 %: if err > +3 % AND alice has USDT reserve, deploy
///     ARB_FRACTION of reserve to buy BUCK.  Track entry price.
///   - Mirror entry: err < -3 % AND alice has BUCK reserve -> sell BUCK.
///   - Exit threshold 0.5 %: if abs(err) < 0.5 % AND alice has an open arb
///     position, close it (swap back).  Realized PnL goes to her USDT P&L.
abstract contract SimulationFixture is Test, UniswapV3Fixture {
    // -------------------------------------------------------------------- //
    //  Tokens / pools / controller                                           //
    // -------------------------------------------------------------------- //

    BuckKController public ctrl;
    BuckKController public alicePid;

    MockERC20 public usdt;
    MockERC20 public usdc;
    MockERC20 public xaut;
    MockERC20 public paxg;
    MockERC20 public cbbtc;
    MockERC20 public wbtc;
    MockERC20 public buck;

    address public xautUsdt;
    address public paxgUsdc;
    address public cbbtcUsdc;
    address public wbtcUsdt;
    address public buckUsdt;

    address public governance;
    address public deadAddr = address(0xdead);

    uint32  internal constant TWAP        = 600;     // 10-min window
    uint256 internal constant W_EACH      = 0.25e18; // 25% per basket pool
    uint256 internal constant GOLD_USD    = 4000;
    uint256 internal constant BTC_USD     = 100000;

    // -------------------------------------------------------------------- //
    //  Per-actor virtual accounting                                          //
    // -------------------------------------------------------------------- //

    struct Bob {
        uint256 mintAmount;     // BUCK liability owed at retirement (18-dec)
        uint256 keepFractionBp; // 0..10000; fraction of mint kept as BUCK
        uint64  arriveTime;     // block.timestamp at first action
        uint64  retireTime;     // block.timestamp when he closes out
        uint8   state;          // 0=pending 1=alive 2=retired
        uint8   kind;           // 0=Bob (farmer), 1=Fred (builder)
        uint256 buckBalance;    // virtual BUCK held (18-dec)
        uint256 usdtBalance;    // virtual USDT held (6-dec)
    }
    Bob[] public bobs;

    struct AliceState {
        uint256 buckReserve;   // not in LP, available for arb-sell positions
        uint256 usdtReserve;   // not in LP, available for arb-buy positions
        // Open-arb tracking.  At most one direction open at a time.
        // direction: 0 = flat, 1 = long BUCK (bought), 2 = short BUCK (sold).
        uint8   arbDirection;
        uint256 arbBuckEntered;  // BUCK acquired (long) or owed back (short)
        uint256 arbUsdtSpent;    // USDT spent (long) or received (short)
        // Realized PnL across closed arb cycles, in USDT units (6-dec).
        int256  realizedUsdtPnl;
        uint256 arbCount;        // number of completed round-trips
    }
    AliceState public alice;

    // -------------------------------------------------------------------- //
    //  Snapshots                                                             //
    // -------------------------------------------------------------------- //

    struct Snapshot {
        uint64  t;            // simulation time (seconds since start)
        int256  spotBuckUsd;  // 18-dec: spot BUCK price in USDT
        int256  twapBuckUsd;  // 18-dec: TWAP BUCK price in USDT
        int256  basketCost;   // 18-dec: basket cost
        uint256 buckK;        // 18-dec: official PID output
        uint256 aliceK;       // 18-dec: Alice's faster PID output
        uint256 aliceBuck;    // alice virtual BUCK reserve
        uint256 aliceUsdt;    // alice virtual USDT reserve
        int256  aliceRealizedPnl;
        uint8   aliceArbDir;
        uint32  bobsAlive;
        uint32  bobsRetired;
        uint32  fredsAlive;
        uint32  fredsRetired;
    }
    Snapshot[] internal snapshots;
    uint64 internal _simStart;

    // -------------------------------------------------------------------- //
    //  Setup helpers                                                         //
    // -------------------------------------------------------------------- //

    /// @dev Deploys the V3 factory, all tokens, all 5 pools, mints LP into
    ///      every pool (basket pools tiny, BUCK/USDT 100K/100K = L=1e17),
    ///      bumps cardinality to 64, warms TWAP for >TWAP seconds, then
    ///      deploys the controller wired with twapInterval=TWAP on every
    ///      pool.  Alice's reserves are seeded from the fixture's mint pool.
    function setUpSim(address _governance) internal {
        governance = _governance;
        setUpV3();

        usdt  = new MockERC20("Tether USD",            "USDT",  6);
        usdc  = new MockERC20("USD Coin",              "USDC",  6);
        xaut  = new MockERC20("Tether Gold",           "XAUT",  6);
        paxg  = new MockERC20("PAX Gold",              "PAXG", 18);
        cbbtc = new MockERC20("Coinbase Wrapped BTC",  "cbBTC", 8);
        wbtc  = new MockERC20("Wrapped BTC",           "WBTC",  8);
        buck  = new MockERC20("Alberta Buck",          "BUCK", 18);

        xautUsdt  = _createAndInitPool(address(xaut),  1e6,  address(usdt), GOLD_USD * 1e6, 3000);
        paxgUsdc  = _createAndInitPool(address(paxg),  1e18, address(usdc), GOLD_USD * 1e6, 3000);
        cbbtcUsdc = _createAndInitPool(address(cbbtc), 1e8,  address(usdc), BTC_USD  * 1e6, 3000);
        wbtcUsdt  = _createAndInitPool(address(wbtc),  1e8,  address(usdt), BTC_USD  * 1e6, 3000);
        buckUsdt  = _createAndInitPool(address(buck),  1e18, address(usdt), 1 * 1e6,        3000);

        // Mint the fixture a generous treasury for liquidity, reserves, and
        // simulated mints.  Sized to 1e30 of each so we never run out.
        usdt.mint (address(this), 1e32);
        usdc.mint (address(this), 1e32);
        xaut.mint (address(this), 1e32);
        paxg.mint (address(this), 1e32);
        cbbtc.mint(address(this), 1e32);
        wbtc.mint (address(this), 1e32);
        buck.mint (address(this), 1e32);

        // Seed liquidity in every pool (small on basket; large on BUCK/USDT).
        _mintFullRange(xautUsdt,  1e15);
        _mintFullRange(paxgUsdc,  1e15);
        _mintFullRange(cbbtcUsdc, 1e15);
        _mintFullRange(wbtcUsdt,  1e15);
        _mintFullRange(buckUsdt,  1e19);  // ~10M BUCK / 10M USDT

        // Cardinality + TWAP warmup.
        _bumpCardinality(xautUsdt,  64);
        _bumpCardinality(paxgUsdc,  64);
        _bumpCardinality(cbbtcUsdc, 64);
        _bumpCardinality(wbtcUsdt,  64);
        _bumpCardinality(buckUsdt,  64);
        for (uint i = 0; i < 22; i++) {
            vm.warp(block.timestamp + 30);
            _touchPool(xautUsdt);
            _touchPool(paxgUsdc);
            _touchPool(cbbtcUsdc);
            _touchPool(wbtcUsdt);
            _touchPool(buckUsdt);
        }

        // Deploy controller with TWAP enabled.
        ctrl = new BuckKController(
            0.1e18, 0.01e18, 0,        // Kp, Ki, Kd
            60,                         // dT
            0.50e18, 1.50e18,           // bounds
            1.0e18,                     // initial buckK
            buckUsdt, TWAP,             // BUCK price oracle pool + window
            governance
        );
        vm.prank(governance);
        ctrl.setBuckPriceOracle(buckUsdt, address(buck), address(usdt), 6, TWAP);

        vm.startPrank(governance);
        ctrl.addBasketPool(xautUsdt,  address(xaut),  address(usdt),
            _scaleWeight(W_EACH, GOLD_USD * 1e18), 6,  6, TWAP);
        ctrl.addBasketPool(paxgUsdc,  address(paxg),  address(usdc),
            _scaleWeight(W_EACH, GOLD_USD * 1e18), 18, 6, TWAP);
        ctrl.addBasketPool(cbbtcUsdc, address(cbbtc), address(usdc),
            _scaleWeight(W_EACH, BTC_USD  * 1e18), 8,  6, TWAP);
        ctrl.addBasketPool(wbtcUsdt,  address(wbtc),  address(usdt),
            _scaleWeight(W_EACH, BTC_USD  * 1e18), 8,  6, TWAP);
        // Long-gap clamp to one hour.
        ctrl.setDTMax(3600);
        vm.stopPrank();

        // Alice's private faster PID (5x gains).  Same oracle pool wiring.
        alicePid = new BuckKController(
            0.5e18, 0.05e18, 0,
            60,
            0.50e18, 1.50e18,
            1.0e18,
            buckUsdt, TWAP,
            governance
        );
        vm.prank(governance);
        alicePid.setBuckPriceOracle(buckUsdt, address(buck), address(usdt), 6, TWAP);
        vm.startPrank(governance);
        alicePid.addBasketPool(xautUsdt,  address(xaut),  address(usdt),
            _scaleWeight(W_EACH, GOLD_USD * 1e18), 6,  6, TWAP);
        alicePid.addBasketPool(paxgUsdc,  address(paxg),  address(usdc),
            _scaleWeight(W_EACH, GOLD_USD * 1e18), 18, 6, TWAP);
        alicePid.addBasketPool(cbbtcUsdc, address(cbbtc), address(usdc),
            _scaleWeight(W_EACH, BTC_USD  * 1e18), 8,  6, TWAP);
        alicePid.addBasketPool(wbtcUsdt,  address(wbtc),  address(usdt),
            _scaleWeight(W_EACH, BTC_USD  * 1e18), 8,  6, TWAP);
        alicePid.setDTMax(3600);
        vm.stopPrank();

        vm.warp(block.timestamp + 61);
        ctrl.compute();
        alicePid.compute();

        _simStart = uint64(block.timestamp);
    }

    function _scaleWeight(uint256 usdShare, uint256 pricePerUnit18d) internal pure returns (uint256) {
        return (usdShare * 1e18) / pricePerUnit18d;
    }

    /// @dev Seed Alice's off-LP reserves.  The amounts are deducted from the
    ///      fixture's free balance (the rest of which still backs Bob mints,
    ///      LP positions, and the dead pile).
    function _seedAlice(uint256 buckAmt, uint256 usdtAmt) internal {
        alice.buckReserve = buckAmt;
        alice.usdtReserve = usdtAmt;
    }

    // -------------------------------------------------------------------- //
    //  Bob lifecycle                                                         //
    // -------------------------------------------------------------------- //

    function _addBob(
        uint256 mintAmount,
        uint256 keepFractionBp,
        uint64  arriveTime,
        uint64  retireTime
    ) internal returns (uint256 id) {
        return _addBobKind(mintAmount, keepFractionBp, arriveTime, retireTime, 0);
    }

    function _addBobKind(
        uint256 mintAmount,
        uint256 keepFractionBp,
        uint64  arriveTime,
        uint64  retireTime,
        uint8   kind
    ) internal returns (uint256 id) {
        require(retireTime > arriveTime, "bob:retire<=arrive");
        require(keepFractionBp <= 10000, "bob:keep>1");
        bobs.push(Bob({
            mintAmount:     mintAmount,
            keepFractionBp: keepFractionBp,
            arriveTime:     arriveTime,
            retireTime:     retireTime,
            state:          0,
            kind:           kind,
            buckBalance:    0,
            usdtBalance:    0
        }));
        id = bobs.length - 1;
    }

    /// @dev Bob arrives: receives mintAmount BUCK, dumps (1-keep) for USDT.
    ///      Also seeded with an off-pool USDT reserve equal to his mint
    ///      amount (translated 18-dec -> 6-dec) -- this represents the
    ///      real-world collateral that backs his BUCK credit.  In a
    ///      deflationary scenario this reserve absorbs the retirement-
    ///      side loss; in inflationary scenarios it grows.  Without this
    ///      reserve, the first wave of dumps leaves later retirees
    ///      underwater whenever any arbitrageur lifts BUCK back to parity.
    function _bobArrive(uint256 i) internal {
        Bob storage b = bobs[i];
        require(b.state == 0, "bob:already arrived");

        // Real-world collateral cushion (1:1 with mint amount, in USDT 6-dec).
        b.usdtBalance += b.mintAmount / 1e12;

        b.buckBalance = b.mintAmount;
        uint256 dumpAmount = b.mintAmount * (10000 - b.keepFractionBp) / 10000;
        if (dumpAmount > 0) {
            uint256 usdtOut = _swapExactInput(buckUsdt, address(buck), address(usdt), dumpAmount);
            b.buckBalance -= dumpAmount;
            b.usdtBalance += usdtOut;
        }
        b.state = 1;
        ctrl.compute();
        alicePid.compute();
    }

    /// @dev Bob retires: covers his liability via USDT->BUCK swap if short,
    ///      then "burns" mintAmount by sending to dead.
    function _bobRetire(uint256 i) internal {
        Bob storage b = bobs[i];
        require(b.state == 1, "bob:not alive");

        if (b.buckBalance < b.mintAmount) {
            uint256 deficit = b.mintAmount - b.buckBalance;
            // Use exact-output swap so we land precisely at the deficit.
            uint256 usdtIn = _swapExactOutput(buckUsdt, address(usdt), address(buck), deficit);
            require(b.usdtBalance >= usdtIn, "bob:underwater"); // Bob unable to retire
            b.usdtBalance -= usdtIn;
            b.buckBalance += deficit;
        }
        // If Bob has surplus BUCK (kept more than minted, unlikely), dump it.
        if (b.buckBalance > b.mintAmount) {
            uint256 surplus = b.buckBalance - b.mintAmount;
            uint256 usdtOut = _swapExactInput(buckUsdt, address(buck), address(usdt), surplus);
            b.buckBalance -= surplus;
            b.usdtBalance += usdtOut;
        }
        // "Burn" the mintAmount (transfer to dead).
        require(b.buckBalance == b.mintAmount, "bob:retirement math");
        buck.transfer(deadAddr, b.mintAmount);
        b.buckBalance = 0;
        b.state = 2;
        ctrl.compute();
        alicePid.compute();
    }

    // -------------------------------------------------------------------- //
    //  Alice arbitrage policy                                                //
    // -------------------------------------------------------------------- //

    // Alice's policy parameters.
    //
    // Position size is the minimum of three bounds:
    //   (1) ARB_RESERVE_FRACTION_BP of remaining reserve on the input side
    //       (governance: how aggressively to deploy capital per cycle).
    //   (2) ARB_TARGET_SLIPPAGE_BP of the pool's input-side balance
    //       (sized so the round-trip slippage is comfortably narrower than
    //       the entry threshold, otherwise her own swap immediately erases
    //       the edge she just spotted).
    //   (3) POOL_DEPTH_CAP_BP of the pool's input-side balance (hard
    //       envelope; "5 % even for a very wealthy Alice").
    //
    // For the default 3 % entry threshold and 1 % target slippage the
    // expected per-cycle PnL is roughly +1 % of position when the price
    // recovers cleanly to parity, more on overshoots.
    uint256 internal constant ARB_RESERVE_FRACTION_BP =  5000; // 50 % of reserve
    uint256 internal constant ARB_TARGET_SLIPPAGE_BP  =   100; // 1 % of pool side
    uint256 internal constant POOL_DEPTH_CAP_BP       =   500; // 5 % hard ceiling
    // PID-spread thresholds: signal = aliceK - buckK (both 18-dec PID outputs).
    uint256 internal constant ALICE_ENTRY_SIGNAL      = 0.003e18;   // 0.3 %
    uint256 internal constant ALICE_EXIT_SIGNAL       = 0.0005e18;  // 0.05 %
    uint256 internal constant ALICE_FULL_SIZE_SIGNAL  = 0.02e18;    // 2 % saturates

    /// @dev Combined cap on a single arb swap.  See policy parameters above.
    function _aliceArbSize(uint256 reserve, address inputToken) internal view returns (uint256) {
        uint256 fromReserve = (reserve * ARB_RESERVE_FRACTION_BP) / 10000;
        uint256 poolBal     = MockERC20(inputToken).balanceOf(buckUsdt);
        uint256 fromTarget  = (poolBal * ARB_TARGET_SLIPPAGE_BP) / 10000;
        uint256 fromCap     = (poolBal * POOL_DEPTH_CAP_BP)      / 10000;

        uint256 r = fromReserve < fromTarget ? fromReserve : fromTarget;
        return r < fromCap ? r : fromCap;
    }

    /// @dev Run one tick of Alice's PID-spread arbitrage strategy.
    ///
    /// signal = aliceK - buckK (both 18-dec PID outputs).
    ///   > 0  =>  Alice's faster PID has accumulated more "BUCK undervalued"
    ///            pressure than the slow official controller.  She expects
    ///            BUCK to be pushed UP over the next many cycles.  Buy BUCK.
    ///   < 0  =>  mirror; sell BUCK.
    ///
    /// Position size scales linearly with |signal| up to ALICE_FULL_SIZE_SIGNAL,
    /// capped by reserve / pool-target-slippage / pool-depth.
    ///
    /// Exit: |signal| decays back below ALICE_EXIT_SIGNAL (PIDs reconverged)
    /// OR sign-flip past entry threshold in the opposite direction.
    function _aliceTick() internal {
        uint256 aliceK = alicePid.compute();
        int256  signal = int256(aliceK) - int256(ctrl.buckK());
        uint256 sigMag = uint256(signal >= 0 ? signal : -signal);

        if (alice.arbDirection == 0) {
            if (sigMag < ALICE_ENTRY_SIGNAL) return;
            if (signal > 0) {
                uint256 usdtIn = _aliceSize(alice.usdtReserve, address(usdt), sigMag);
                if (usdtIn == 0) return;
                uint256 buckOut = _swapExactInput(buckUsdt, address(usdt), address(buck), usdtIn);
                alice.usdtReserve   -= usdtIn;
                alice.buckReserve   += buckOut;
                alice.arbBuckEntered = buckOut;
                alice.arbUsdtSpent   = usdtIn;
                alice.arbDirection   = 1;
            } else {
                uint256 buckIn = _aliceSize(alice.buckReserve, address(buck), sigMag);
                if (buckIn == 0) return;
                uint256 usdtOut = _swapExactInput(buckUsdt, address(buck), address(usdt), buckIn);
                alice.buckReserve   -= buckIn;
                alice.usdtReserve   += usdtOut;
                alice.arbBuckEntered = buckIn;
                alice.arbUsdtSpent   = usdtOut;
                alice.arbDirection   = 2;
            }
            ctrl.compute();
            alicePid.compute();
        } else {
            bool converged = sigMag <= ALICE_EXIT_SIGNAL;
            bool flipped = (alice.arbDirection == 1 && signal <= -int256(ALICE_ENTRY_SIGNAL))
                        || (alice.arbDirection == 2 && signal >=  int256(ALICE_ENTRY_SIGNAL));
            if (!converged && !flipped) return;

            if (alice.arbDirection == 1) {
                uint256 usdtOut = _swapExactInput(
                    buckUsdt, address(buck), address(usdt), alice.arbBuckEntered);
                alice.buckReserve -= alice.arbBuckEntered;
                alice.usdtReserve += usdtOut;
                alice.realizedUsdtPnl += int256(usdtOut) - int256(alice.arbUsdtSpent);
            } else {
                uint256 usdtIn = _swapExactOutput(
                    buckUsdt, address(usdt), address(buck), alice.arbBuckEntered);
                alice.usdtReserve -= usdtIn;
                alice.buckReserve += alice.arbBuckEntered;
                alice.realizedUsdtPnl += int256(alice.arbUsdtSpent) - int256(usdtIn);
            }
            alice.arbBuckEntered = 0;
            alice.arbUsdtSpent   = 0;
            alice.arbDirection   = 0;
            alice.arbCount      += 1;
            ctrl.compute();
            alicePid.compute();
        }
    }

    /// @dev Position size scaled by signal magnitude, capped by reserve / pool.
    function _aliceSize(uint256 reserve, address inputToken, uint256 sigMag)
        internal view returns (uint256)
    {
        uint256 cap = _aliceArbSize(reserve, inputToken);
        if (sigMag >= ALICE_FULL_SIZE_SIGNAL) return cap;
        return (cap * sigMag) / ALICE_FULL_SIZE_SIGNAL;
    }

    // -------------------------------------------------------------------- //
    //  Snapshots + JSON                                                      //
    // -------------------------------------------------------------------- //

    function _snap() internal {
        snapshots.push();
        Snapshot storage s = snapshots[snapshots.length - 1];
        s.t                = uint64(block.timestamp - _simStart);
        s.spotBuckUsd      = _getBuckSpotExternal();
        s.twapBuckUsd      = _getBuckTwapExternal();
        s.basketCost       = _getBasketCostExternal();
        s.buckK            = ctrl.buckK();
        s.aliceK           = alicePid.buckK();
        s.aliceBuck        = alice.buckReserve;
        s.aliceUsdt        = alice.usdtReserve;
        s.aliceRealizedPnl = alice.realizedUsdtPnl;
        s.aliceArbDir      = alice.arbDirection;
        (uint32 ba, uint32 br, uint32 fa, uint32 fr) = _actorCounts();
        s.bobsAlive    = ba;
        s.bobsRetired  = br;
        s.fredsAlive   = fa;
        s.fredsRetired = fr;
    }

    function _bobCounts() internal view returns (uint32 alive, uint32 retired) {
        for (uint i = 0; i < bobs.length; i++) {
            if (bobs[i].kind != 0) continue;
            if (bobs[i].state == 1) alive++;
            else if (bobs[i].state == 2) retired++;
        }
    }

    function _actorCounts() internal view
        returns (uint32 ba, uint32 br, uint32 fa, uint32 fr)
    {
        for (uint i = 0; i < bobs.length; i++) {
            uint8 st = bobs[i].state;
            if (bobs[i].kind == 0) {
                if (st == 1) ba++; else if (st == 2) br++;
            } else {
                if (st == 1) fa++; else if (st == 2) fr++;
            }
        }
    }


    function _writeSnapshotsJson(string memory path) internal {
        string memory body = "[";
        for (uint i = 0; i < snapshots.length; i++) {
            if (i > 0) body = string.concat(body, ",");
            body = string.concat(body, _snapJson(snapshots[i]));
        }
        body = string.concat(body, "]");

        string memory bobsJson = "[";
        for (uint i = 0; i < bobs.length; i++) {
            if (i > 0) bobsJson = string.concat(bobsJson, ",");
            bobsJson = string.concat(bobsJson, _bobJson(i, bobs[i]));
        }
        bobsJson = string.concat(bobsJson, "]");

        string memory full = string.concat(
            "{\"snapshots\":", body,
            ",\"bobs\":",      bobsJson,
            ",\"arbCount\":",  vm.toString(alice.arbCount),
            "}"
        );
        vm.writeFile(path, full);
    }

    function _snapJson(Snapshot memory s) internal pure returns (string memory r) {
        r = string.concat("{\"t\":",    vm.toString(uint256(s.t)));
        r = string.concat(r, ",\"spot\":",   _intStr(s.spotBuckUsd));
        r = string.concat(r, ",\"twap\":",   _intStr(s.twapBuckUsd));
        r = string.concat(r, ",\"basket\":", _intStr(s.basketCost));
        r = string.concat(r, ",\"buckK\":",  vm.toString(s.buckK));
        r = string.concat(r, ",\"aliceK\":", vm.toString(s.aliceK));
        r = string.concat(r, ",\"aliceBuck\":", vm.toString(s.aliceBuck));
        r = string.concat(r, ",\"aliceUsdt\":", vm.toString(s.aliceUsdt));
        r = string.concat(r, ",\"aliceRealizedPnl\":", _intStr(s.aliceRealizedPnl));
        r = string.concat(r, ",\"aliceArbDir\":", vm.toString(uint256(s.aliceArbDir)));
        r = string.concat(r, ",\"bobsAlive\":",   vm.toString(uint256(s.bobsAlive)));
        r = string.concat(r, ",\"bobsRetired\":", vm.toString(uint256(s.bobsRetired)));
        r = string.concat(r, ",\"fredsAlive\":",  vm.toString(uint256(s.fredsAlive)));
        r = string.concat(r, ",\"fredsRetired\":",vm.toString(uint256(s.fredsRetired)));
        r = string.concat(r, "}");
    }

    function _bobJson(uint256 id, Bob memory b) internal view returns (string memory r) {
        r = string.concat("{\"id\":", vm.toString(id));
        r = string.concat(r, ",\"kind\":",       vm.toString(uint256(b.kind)));
        r = string.concat(r, ",\"mintAmount\":", vm.toString(b.mintAmount));
        r = string.concat(r, ",\"keepBp\":",     vm.toString(b.keepFractionBp));
        r = string.concat(r, ",\"arrive\":",     vm.toString(uint256(b.arriveTime - _simStart)));
        r = string.concat(r, ",\"retire\":",     vm.toString(uint256(b.retireTime - _simStart)));
        r = string.concat(r, ",\"state\":",      vm.toString(uint256(b.state)));
        r = string.concat(r, "}");
    }

    function _intStr(int256 v) internal pure returns (string memory) {
        if (v >= 0) return vm.toString(uint256(v));
        return string.concat("-", vm.toString(uint256(-v)));
    }

    // -------------------------------------------------------------------- //
    //  Externalised oracle reads (replicate controller internals for tests)  //
    // -------------------------------------------------------------------- //

    function _getBuckSpotExternal() internal view returns (int256) {
        (, int24 tick,,,,,) = IUniswapV3Pool(buckUsdt).slot0();
        uint256 q = UniswapV3OracleLib.getQuoteAtTick(
            tick, uint128(1e18), address(buck), address(usdt));
        return int256(q * 10 ** 12);
    }

    function _getBuckTwapExternal() internal view returns (int256) {
        int24 tick = UniswapV3OracleLib.consult(buckUsdt, TWAP);
        uint256 q = UniswapV3OracleLib.getQuoteAtTick(
            tick, uint128(1e18), address(buck), address(usdt));
        return int256(q * 10 ** 12);
    }

    function _getBasketCostExternal() internal view returns (int256) {
        int256 total;
        for (uint i = 0; i < ctrl.basketPoolsLength(); i++) {
            (address pool, address baseT, address quoteT, uint256 weight,
             uint8 baseDec, uint8 quoteDec, uint32 twap) = ctrl.basketPools(i);
            int24 tick;
            if (twap == 0) {
                (, tick,,,,,) = IUniswapV3Pool(pool).slot0();
            } else {
                tick = UniswapV3OracleLib.consult(pool, twap);
            }
            uint256 q = UniswapV3OracleLib.getQuoteAtTick(
                tick, uint128(10 ** baseDec), baseT, quoteT);
            int256 normalized = int256(q * 10 ** (18 - quoteDec));
            total += normalized * int256(weight) / int256(uint256(1e18));
        }
        return total;
    }

    // -------------------------------------------------------------------- //
    //  Time advance                                                          //
    // -------------------------------------------------------------------- //

    function _advance(uint256 secs) internal {
        vm.warp(block.timestamp + secs);
    }

    /// @dev Touch the BUCK pool so its TWAP keeps up; called every tick to
    ///      avoid the consult-extrapolation degenerate where stale obs
    ///      makes consult equal to current tick.
    function _maintainTwap() internal {
        _touchPool(buckUsdt);
        // Basket pools don't drift in this fixture (their spot is initialised
        // at parity prices and never moved), so they self-maintain via the
        // initial warmup populated observations.
    }
}
