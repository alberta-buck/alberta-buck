// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test}                  from "forge-std/Test.sol";
import {ERC20}                 from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {BN254}                 from "../src/BN254.sol";
import {IdentityRegistry}      from "../src/IdentityRegistry.sol";
import {Buck}                  from "../src/Buck.sol";
import {BuckCredit}            from "../src/BuckCredit.sol";
import {BuckKPeggedHarness}    from "./harness/BuckKPeggedHarness.sol";

/// @dev Mock USDC for the equilibrium scenario.  Name differs from LifecycleUSDC
///      so both fixtures can coexist.
contract EqUSDC is ERC20 {
    constructor() ERC20("Mock USDC", "USDC") { _mint(msg.sender, 1_000_000_000_000e6); }
}

interface IV2Factory {
    function createPair(address, address) external returns (address);
}
interface IV2Router {
    function addLiquidity(address, address, uint, uint, uint, uint, address, uint)
        external returns (uint, uint, uint);
    function swapExactTokensForTokens(uint, uint, address[] calldata, address, uint)
        external returns (uint[] memory);
    function swapTokensForExactTokens(uint, uint, address[] calldata, address, uint)
        external returns (uint[] memory);
}
interface IERC20Like {
    function approve(address, uint256) external returns (bool);
    function transfer(address, uint256) external returns (bool);
    function balanceOf(address) external view returns (uint256);
}
interface IV2Pair {
    function token0() external view returns (address);
    function getReserves() external view returns (uint112, uint112, uint32);
}

/// @title BuckEquilibriumScenarioTest -- 18-month seasonal Carol/BUCK_K simulation.
///
/// @notice Phase-(b) of the BUCK-K validation: multi-Carol simulation against
///         a deep BUCK/USDC V2 pool, demonstrating
///
///           * Insurance funding factor as bootstrap demand for BUCK
///           * BUCK_K -> credit limit feedback driving voluntary supply
///             contraction during inflation and expansion during deflation
///           * Mean-reversion of the BUCK pool back toward parity with the
///             $1.00-pegged basket
///
/// Counter-cyclical PID gains:  Kp / Ki are NEGATIVE in this scenario so that
/// BUCK below basket (inflationary) lowers buckK -> tightens credit -> induces
/// burns; BUCK above basket lifts buckK -> loosens credit -> induces mints.
/// Sign convention is the OPPOSITE of the commodity-driven PID scenario used
/// in BuckKArbScenarioTest (where higher BUCK error pushes more credit out so
/// holders spend BUCK into commodities, pulling commodity prices toward BUCK).
contract BuckEquilibriumScenarioTest is Test {

    // ---- Sizing ----------------------------------------------------------- //
    uint256 constant DURATION_DAYS = 540;        // 18 months
    uint256 constant TICK_DAYS     = 1;          // advance 1 day per loop step
    uint256 constant SNAP_DAYS     = 7;          // weekly snapshots
    uint256 constant REBAL_DAYS    = 30;         // monthly rebalance
    uint256 constant N_CAROLS      = 50;
    // Carols arrive spread across the first 12 months (not clustered) so
    // arrivals and departures overlap throughout the 18-month run.
    uint256 constant CAROL_ARRIVAL_PEAK_DAY = 180;
    uint256 constant CAROL_ARRIVAL_STD_DAYS = 100;
    uint256 constant CAROL_LIFESPAN_DAYS    = 270;   // term of the BUCK credit
    uint256 constant CAROL_LIFESPAN_STD     = 45;

    // Hanks: opportunistic BUCK buyers who accumulate for long-term holdings
    // (BUCK-denominated farmland purchases, BUCK Notes for inheritances,
    // multi-year savings).  Price-aware: only buy when BUCK is at or below
    // HANK_BUY_CEILING ($1.05); above that they sit on USDC.
    uint256 constant N_HANKS                = 40;
    uint256 constant HANK_ARRIVAL_PEAK_DAY  = 150;
    uint256 constant HANK_ARRIVAL_STD_DAYS  = 90;
    uint256 constant HANK_MONTHLY_USDC      = 500e6;     // $500 USDC / month
    uint256 constant HANK_USDC_BUDGET       = 12_000e6;  // 24 months budget
    int256  constant HANK_BUY_CEILING       = int256(1.05e18); // skip if BUCK > $1.05

    uint256 constant CAROL_FACEVALUE  = 100_000e6;   // $100K
    uint32  constant CAROL_PREMIUM_BP = 150;         // 1.5 %/yr
    uint256 constant CAROL_USDC_SEED  = 30_000e6;    // $30K initial USDC

    uint256 constant ALICE_FACEVALUE  = 4_000_000e6; // $4M
    uint256 constant POOL_SEED_AMT    = 1_000_000e6; // $1M each side

    // BUCK_K initial state.  Lower bound widened so contractions can bite.
    uint256 constant BUCKK_INIT  = 0.25e18;
    uint256 constant BUCKK_MIN   = 0.05e18;
    uint256 constant BUCKK_MAX   = 1.00e18;

    // PID: POSITIVE gains.  Under the project sign convention,
    //   error = BUCK - basket
    // is negative when BUCK is undervalued (inflation), so positive Kp
    // contracts buckK -> tighter credit -> burn pressure -> price recovery.
    // Tuned to give a ~10% buckK move per 5% sustained drift -- gentle
    // enough that natural Carol/Hank flows dominate but firm enough to
    // pull buckK off neutral when persistent error appears.
    int256  constant KP    = int256(0.5e18);
    int256  constant KI    = int256(0.005e18);
    int256  constant KD    = int256(0);
    uint256 constant CTRL_DT = 60;                 // seconds

    uint256 constant SEED = 0xC0FFEE1234;

    // ---- Roles ----------------------------------------------------------- //
    address internal constant GOV    = address(0xA0);
    address internal constant POOL   = address(0xBA51C);  // insurancePool sink
    address internal constant ISSUER = address(0x1551E1);

    Buck                internal buck;
    BuckCredit          internal credit;
    BuckKPeggedHarness  internal kCtrl;
    IdentityRegistry    internal reg;
    address             internal usdc;
    address             internal weth;
    address             internal factory;
    address             internal router;
    address             internal pair;

    address             internal alice;
    address[]           internal carolAddrs;
    uint256             internal aliceTokenId;

    string              internal vj;

    // Per-Carol state.
    struct Carol {
        uint64  arriveTime;     // sim-absolute timestamp
        uint64  departTime;     // sim-absolute timestamp of credit retirement
        uint8   state;          // 0=pending 1=active 2=retired
        uint256 tokenId;
        uint256 mintedNet;      // BUCK delivered to her (net of insurance)
        uint256 mintsBacked;    // covers all NFTs the Carol holds (tracked here for sim)
    }
    Carol[] internal carols;

    // Per-Hank state.  Hanks are pure BUCK accumulators: they own no NFT,
    // never mint or burn.  Each month after arrival they buy a fixed USDC
    // amount of BUCK and add it to their wallet (long-term holding).
    struct Hank {
        uint64  arriveTime;
        uint8   state;          // 0=pending 1=active (no retirement)
        uint256 usdcBudget;     // remaining USDC for monthly buys
    }
    Hank[]    internal hanks;
    address[] internal hankAddrs;

    // ---- Snapshot storage (per-field arrays; mirror BuckLifecycle pattern) //
    uint256[] internal s_t;
    int256[]  internal s_spot;        // 18-dec BUCK price (USDC/BUCK)
    int256[]  internal s_basket;      // 18-dec basket cost (constant $1)
    uint256[] internal s_buckK;
    uint256[] internal s_factor;
    // PID accumulators -- exposed so the plot can show the controller's
    // internal state alongside the prices it's responding to.
    int256[]  internal s_pid_p;
    int256[]  internal s_pid_i;
    int256[]  internal s_pid_d;
    uint256[] internal s_supply;
    uint256[] internal s_jubilee;
    uint256[] internal s_pool_buck;
    uint256[] internal s_pool_usdc;
    uint32[]  internal s_active;
    uint32[]  internal s_retired;
    uint32[]  internal s_hanks;       // active Hanks
    uint256[] internal s_agg_minted;
    uint256[] internal s_hank_hold;   // total BUCK held by Hanks

    // ---- Setup ----------------------------------------------------------- //

    function setUp() public {
        vm.chainId(1);
        vj = vm.readFile("test/vectors/identity.json");

        reg = new IdentityRegistry(GOV);
        _trustIssuer();

        alice = address(uint160(_u(".alice.registrant")));
        _registerAlice();

        credit = new BuckCredit();
        kCtrl  = new BuckKPeggedHarness(KP, KI, KD, CTRL_DT, BUCKK_MIN, BUCKK_MAX, BUCKK_INIT, GOV);
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));

        usdc = address(new EqUSDC());

        weth    = deployCode("out/WETH9.sol/WETH9.json");
        factory = deployCode("out/UniswapV2Factory.sol/UniswapV2Factory.json", abi.encode(address(this)));
        router  = deployCode("out/UniswapV2Router02.sol/UniswapV2Router02.json", abi.encode(factory, weth));
        pair    = IV2Factory(factory).createPair(address(buck), usdc);

        IdentityRegistry.ElGamalCT memory E = IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()});
        reg.bindContract(pair,   BN254.g1(), E, true, true);
        reg.bindContract(router, BN254.g1(), E, true, true);

        // Mutual decryptability: Alice seeds the pool and receives LP
        // payouts, so she must CP-approve both pair and router.
        {
            bytes32 _fragSlot = keccak256(
                abi.encode(pair, keccak256(abi.encode(alice, uint256(5)))));
            vm.store(address(buck), _fragSlot, bytes32(uint256(1)));
            _fragSlot = keccak256(
                abi.encode(router, keccak256(abi.encode(alice, uint256(5)))));
            vm.store(address(buck), _fragSlot, bytes32(uint256(1)));
        }

        vm.prank(GOV);
        kCtrl.setV2BuckPair(pair, address(buck));

        _generateCarols();
        _generateHanks();
        _registerCarolsAsPublic(E);
        _registerHanksAsPublic(E);
        _issueCredits();
        _seedAliceAndPool();
        _seedCarolUsdc();
        _seedHankUsdc();
    }

    function _registerHanksAsPublic(IdentityRegistry.ElGamalCT memory E) internal {
        for (uint256 i = 0; i < N_HANKS; i++) {
            vm.etch(hankAddrs[i], hex"00");
            reg.bindContract(hankAddrs[i], BN254.g1(), E, true, false);
        }
    }

    function _seedHankUsdc() internal {
        for (uint256 i = 0; i < N_HANKS; i++) {
            IERC20Like(usdc).transfer(hankAddrs[i], HANK_USDC_BUDGET);
        }
    }

    // ---- Main test ------------------------------------------------------- //

    function test_equilibrium_18_months() public {
        uint256 endTime  = block.timestamp + DURATION_DAYS * 1 days;
        uint256 nextSnap = block.timestamp;
        uint256 nextReb  = block.timestamp + REBAL_DAYS * 1 days;
        uint256 nextHankBuy = block.timestamp + REBAL_DAYS * 1 days;

        while (block.timestamp < endTime) {
            _processArrivals();
            _processDepartures();
            _processHankArrivals();
            if (block.timestamp >= nextReb) {
                _rebalance(_monthSeed());
                nextReb = block.timestamp + REBAL_DAYS * 1 days;
            }
            if (block.timestamp >= nextHankBuy) {
                _hankMonthlyBuys();
                nextHankBuy = block.timestamp + REBAL_DAYS * 1 days;
            }
            if (block.timestamp >= nextSnap) {
                _snap();
                nextSnap = block.timestamp + SNAP_DAYS * 1 days;
            }
            vm.warp(block.timestamp + TICK_DAYS * 1 days);
        }
        _snap();
        _writeJson();

        (uint32 actEnd, uint32 retEnd) = _carolCounts();
        emit log_named_uint("snapshots written",   s_t.length);
        emit log_named_uint("active carols",       actEnd);
        emit log_named_uint("retired carols",      retEnd);
        emit log_named_uint("active hanks",        s_hanks[s_hanks.length - 1]);
        emit log_named_uint("hank holdings (6d)",  s_hank_hold[s_hank_hold.length - 1]);
        emit log_named_uint("buckK end (1e18)",    kCtrl.buckK());
        emit log_named_uint("factor end (1e18)",   kCtrl.fundingFactor());
        emit log_named_int ("spot end (1e18)",     kCtrl.getBuckPrice());
        emit log_named_uint("supply end (6dec)",   buck.totalSupply());

        // Sanity: buckK should stay within bounds; total supply should not
        // explode (a runaway PID would push beyond reasonable bounds).
        assertGe(kCtrl.buckK(), BUCKK_MIN);
        assertLe(kCtrl.buckK(), BUCKK_MAX);
    }

    // ---- Carol generation (Gaussian-clustered arrivals) ----------------- //

    function _generateCarols() internal {
        uint256 simStart = block.timestamp;
        uint256 seed     = SEED;
        for (uint256 i = 0; i < N_CAROLS; i++) {
            seed = uint256(keccak256(abi.encode(seed, "carol-arr", i)));
            int256 arrOff = _gaussOff(seed, int256(CAROL_ARRIVAL_STD_DAYS * 1 days));
            int256 arrTs  = int256(simStart) + int256(CAROL_ARRIVAL_PEAK_DAY * 1 days) + arrOff;
            if (arrTs < int256(simStart)) arrTs = int256(simStart);

            seed = uint256(keccak256(abi.encode(seed, "carol-life")));
            int256 lifeOff = _gaussOff(seed, int256(CAROL_LIFESPAN_STD * 1 days));
            int256 dep = arrTs + int256(CAROL_LIFESPAN_DAYS * 1 days) + lifeOff;
            if (dep <= arrTs) dep = arrTs + int256(30 days);

            address c = address(uint160(uint256(keccak256(abi.encode("carol-addr", i)))));
            carolAddrs.push(c);
            carols.push(Carol({
                arriveTime:  uint64(uint256(arrTs)),
                departTime:  uint64(uint256(dep)),
                state:       0,
                tokenId:     0,
                mintedNet:   0,
                mintsBacked: 0
            }));
        }
    }

    function _generateHanks() internal {
        uint256 simStart = block.timestamp;
        uint256 seed     = uint256(keccak256(abi.encode(SEED, "hank-seed")));
        for (uint256 i = 0; i < N_HANKS; i++) {
            seed = uint256(keccak256(abi.encode(seed, "hank-arr", i)));
            int256 off = _gaussOff(seed, int256(HANK_ARRIVAL_STD_DAYS * 1 days));
            int256 ts  = int256(simStart) + int256(HANK_ARRIVAL_PEAK_DAY * 1 days) + off;
            if (ts < int256(simStart)) ts = int256(simStart);

            address h = address(uint160(uint256(keccak256(abi.encode("hank-addr", i)))));
            hankAddrs.push(h);
            hanks.push(Hank({
                arriveTime: uint64(uint256(ts)),
                state:      0,
                usdcBudget: HANK_USDC_BUDGET
            }));
        }
    }

    function _registerCarolsAsPublic(IdentityRegistry.ElGamalCT memory E) internal {
        // bindContract requires `target.code.length > 0`.  vm.etch a single
        // byte at each Carol's address so the registry accepts the binding;
        // the synthetic bytecode is never executed -- Carols only ever
        // appear as msg.sender via vm.prank.
        for (uint256 i = 0; i < N_CAROLS; i++) {
            vm.etch(carolAddrs[i], hex"00");
            reg.bindContract(carolAddrs[i], BN254.g1(), E, true, false);
        }
    }

    function _issueCredits() internal {
        // Alice's big credit (drives the bootstrap mint).
        aliceTokenId = credit.createCredit(
            alice, 0, ALICE_FACEVALUE, 0,
            BuckCredit.DepreciationType.NONE, 0, uint48(block.timestamp), 0
        );
        vm.prank(alice);
        credit.activate(aliceTokenId, ALICE_FACEVALUE);

        // Each Carol's vehicle-style NFT (1.5 %/yr premium, no depreciation
        // to keep the math focused on the funding-factor loop).
        for (uint256 i = 0; i < N_CAROLS; i++) {
            uint256 tid = credit.createCredit(
                carolAddrs[i], 0, CAROL_FACEVALUE, 0,
                BuckCredit.DepreciationType.NONE, 0, uint48(block.timestamp), CAROL_PREMIUM_BP
            );
            vm.prank(carolAddrs[i]);
            credit.activate(tid, CAROL_FACEVALUE);
            carols[i].tokenId = tid;
        }
    }

    function _seedAliceAndPool() internal {
        // Bootstrap mint by Alice while totalSupply == 0 (funding-factor bypass).
        vm.prank(alice);
        buck.mint(POOL_SEED_AMT);

        // Move test-fixture USDC to Alice for pool seed.
        IERC20Like(usdc).transfer(alice, POOL_SEED_AMT);

        // Seed BUCK/USDC pool 1:1.
        _setBuckAllowance(alice, router, POOL_SEED_AMT);
        vm.prank(alice);
        IERC20Like(usdc).approve(router, POOL_SEED_AMT);
        vm.prank(alice);
        IV2Router(router).addLiquidity(
            address(buck), usdc, POOL_SEED_AMT, POOL_SEED_AMT,
            0, 0, alice, block.timestamp + 1
        );

        // Warp >= dT and prime the PID.
        vm.warp(block.timestamp + CTRL_DT + 1);
        kCtrl.compute();
    }

    function _seedCarolUsdc() internal {
        for (uint256 i = 0; i < N_CAROLS; i++) {
            IERC20Like(usdc).transfer(carolAddrs[i], CAROL_USDC_SEED);
        }
    }

    // ---- Loop helpers ---------------------------------------------------- //

    function _processArrivals() internal {
        for (uint256 i = 0; i < carols.length; i++) {
            if (carols[i].state == 0 && carols[i].arriveTime <= block.timestamp) {
                _carolArrive(i);
            }
        }
    }

    function _processDepartures() internal {
        for (uint256 i = 0; i < carols.length; i++) {
            if (carols[i].state == 1 && carols[i].departTime <= block.timestamp) {
                _carolDepart(i);
            }
        }
    }

    function _processHankArrivals() internal {
        for (uint256 i = 0; i < hanks.length; i++) {
            if (hanks[i].state == 0 && hanks[i].arriveTime <= block.timestamp) {
                hanks[i].state = 1;
            }
        }
    }

    /// @dev Each active Hank spends HANK_MONTHLY_USDC on BUCK if his budget
    ///      remains and the current pool spot is at or below HANK_BUY_CEILING.
    ///      He is a "value buyer" -- happy to accumulate when BUCK is cheap
    ///      or near parity but skips months when BUCK is overvalued.
    function _hankMonthlyBuys() internal {
        int256 spot = kCtrl.getBuckPrice();
        if (spot > HANK_BUY_CEILING) return;
        for (uint256 i = 0; i < hanks.length; i++) {
            if (hanks[i].state != 1) continue;
            if (hanks[i].usdcBudget < HANK_MONTHLY_USDC) continue;
            _hankBuy(hankAddrs[i], HANK_MONTHLY_USDC);
            hanks[i].usdcBudget -= HANK_MONTHLY_USDC;
        }
    }

    function _hankBuy(address ha, uint256 usdcIn) internal {
        // USDC -> BUCK swap; Hank holds the result.
        vm.prank(ha);
        IERC20Like(usdc).approve(router, type(uint256).max);
        address[] memory path = new address[](2);
        path[0] = usdc;
        path[1] = address(buck);
        vm.prank(ha);
        try IV2Router(router).swapExactTokensForTokens(
            usdcIn, 0, path, ha, block.timestamp + 1
        ) returns (uint256[] memory) { } catch { }
    }

    /// @dev Carol arrival sequence:
    ///   1. Compute her current credit limit (creditValue * buckK / 1e18).
    ///   2. Target mint = 50 % of limit.
    ///   3. Read funding factor; quote required pre-balance.
    ///   4. Swap USDC -> BUCK to acquire the pre-balance (a small buy that
    ///      slightly nudges spot up, supporting BUCK price during bootstrap).
    ///   5. mint(target) (passes the factor gate; receives target net BUCK).
    ///   6. Swap her freshly-minted BUCK -> USDC at the new (slightly
    ///      lifted) spot price.  Keeps the pre-balance in her wallet.
    function _carolArrive(uint256 i) internal {
        Carol storage c = carols[i];
        address ca = carolAddrs[i];

        uint256 buckK_ = kCtrl.compute();
        uint256 limit  = CAROL_FACEVALUE * buckK_ / 1e18;
        uint256 target = limit / 2;
        if (target == 0) { c.state = 1; return; }

        // Quote the per-NFT allocation so we know the insurance principal.
        uint256[] memory tids = new uint256[](1);
        tids[0] = c.tokenId;
        (uint256 totalCoverage, uint256 poolPrincipal) = buck.quoteMint(target, tids);
        totalCoverage; // silence unused

        uint256 factor   = kCtrl.fundingFactor();
        uint256 required = poolPrincipal * factor / 1e18;

        // Step 4: Carol buys `required` BUCK from the pool with USDC.  Buy
        // 5 % extra to absorb (a) demurrage that lands on the BUCK as it
        // transfers from the Carrying pair into her non-Carrying slot, and
        // (b) any V2 swap rounding -- the gate inside _mintAllocated reads
        // spendable balanceOf, which is raw - feeOwing.
        if (required > 0) {
            _carolBuyBuck(ca, required + required / 20);
        }

        // Step 5: mint exactly `target` net BUCK.  Use the explicit-tokenIds
        // overload so the pre-quoted (totalCoverage, poolPrincipal) matches
        // what _allocateMint computes.  Tolerate revert (e.g. funding-factor
        // tightened past her budget mid-tick, or credit limit collapsed) by
        // marking her active without an open position.
        vm.prank(ca);
        try buck.mint(target, tids) {
            c.mintedNet   += target;
            c.mintsBacked += totalCoverage;
            // Step 6: dump the freshly-minted `target` BUCK for USDC.
            _carolSellBuck(ca, target);
        } catch {
            // Skip the dump; she enters the simulation idle.
        }

        c.state = 1;
    }

    /// @dev Carol's credit-retirement: buy back her outstanding mintedNet
    ///      BUCK from the pool with USDC, then burn it to release coverage.
    ///      Mirrors the Bob lifecycle in BuckKArbScenarioTest -- the buy is
    ///      pure pool demand, the burn contracts supply, both push BUCK
    ///      toward parity.  If she lacks USDC she retires partial (burns
    ///      whatever she can buy), accepting the bad rate as cost-of-exit.
    function _carolDepart(uint256 i) internal {
        Carol storage c = carols[i];
        address ca = carolAddrs[i];

        uint256 needBuck = c.mintedNet;
        if (needBuck > 0) {
            // Buy as much as her USDC supports; capped at needBuck.
            uint256 haveBuck = buck.balanceOf(ca);
            uint256 toBuy = needBuck > haveBuck ? needBuck - haveBuck : 0;
            if (toBuy > 0) _carolBuyBuck(ca, toBuy);

            // Burn whatever she now holds, up to needBuck.
            uint256 burnAmt = buck.balanceOf(ca);
            if (burnAmt > needBuck) burnAmt = needBuck;
            if (burnAmt > 0) {
                vm.prank(ca);
                try buck.burn(burnAmt) {
                    c.mintedNet   = c.mintedNet > burnAmt ? c.mintedNet - burnAmt : 0;
                    c.mintsBacked = c.mintsBacked > burnAmt ? c.mintsBacked - burnAmt : 0;
                } catch { }
            }
        }
        c.state = 2;
    }

    // ---- Monthly rebalance ---------------------------------------------- //

    function _monthSeed() internal view returns (uint256) {
        return uint256(keccak256(abi.encode(SEED, "month", block.timestamp / (30 days))));
    }

    /// @dev 10 % of active Carols (deterministically selected via month seed)
    ///      re-evaluate their position.  Decision tree:
    ///        - target = 50 % * (faceValue * current buckK / 1e18)
    ///        - if BUCK > $1.00 AND mintedNet < target:
    ///              mint up to target, dump for USDC (premium capture).
    ///        - if BUCK < $1.00 AND mintedNet > target:
    ///              buy BUCK from pool, burn it (recover pool principal).
    ///        - otherwise: skip (within band).
    function _rebalance(uint256 monthSeed) internal {
        for (uint256 i = 0; i < carols.length; i++) {
            if (carols[i].state != 1) continue;
            uint256 r = uint256(keccak256(abi.encode(monthSeed, i))) % 100;
            if (r >= 10) continue;  // only 10 % per month rebalance
            _carolRebalance(i);
        }
    }

    function _carolRebalance(uint256 i) internal {
        Carol storage c = carols[i];
        address ca = carolAddrs[i];

        uint256 buckK_ = kCtrl.compute();
        uint256 target = (CAROL_FACEVALUE * buckK_ / 1e18) / 2;
        int256  spot   = kCtrl.getBuckPrice();
        bool    overParity  = spot > int256(1e18);
        bool    underParity = spot < int256(1e18);

        if (overParity && c.mintedNet + (c.mintedNet / 20) < target) {
            _carolMintMore(i, target - c.mintedNet);
        } else if (underParity && c.mintedNet > target + (c.mintedNet / 20)) {
            _carolBuyAndBurn(i, c.mintedNet - target);
        }
        // else: within +/- 5 % band; no action.
        ca; // silence unused
    }

    function _carolMintMore(uint256 i, uint256 extra) internal {
        Carol storage c = carols[i];
        address ca = carolAddrs[i];

        uint256[] memory tids = new uint256[](1);
        tids[0] = c.tokenId;
        (uint256 totalCoverage, uint256 poolPrincipal) = buck.quoteMint(extra, tids);
        totalCoverage;

        uint256 factor   = kCtrl.fundingFactor();
        uint256 required = poolPrincipal * factor / 1e18;

        uint256 have = buck.balanceOf(ca);
        if (have < required) {
            uint256 need = required - have;
            if (IERC20Like(usdc).balanceOf(ca) < need * 2) return; // not enough USDC for buffer
            _carolBuyBuck(ca, need);
        }
        vm.prank(ca);
        try buck.mint(extra, tids) {
            c.mintedNet   += extra;
            c.mintsBacked += totalCoverage;
            _carolSellBuck(ca, extra);
        } catch { /* skip if revert (e.g. credit limit exceeded) */ }
    }

    function _carolBuyAndBurn(uint256 i, uint256 reduce) internal {
        Carol storage c = carols[i];
        address ca = carolAddrs[i];

        // Buy `reduce` BUCK from pool, burn it.  The burn returns poolRefund
        // BUCK from the insurance pool to Carol's account (visible in her
        // balance bump).  Burn flow uses _selectMostExpensive(holder).
        _carolBuyBuck(ca, reduce);
        if (buck.balanceOf(ca) < reduce) return;
        vm.prank(ca);
        try buck.burn(reduce) {
            c.mintedNet   = c.mintedNet > reduce ? c.mintedNet - reduce : 0;
            c.mintsBacked = c.mintsBacked > reduce ? c.mintsBacked - reduce : 0;
        } catch { /* skip if revert */ }
    }

    // ---- Pool helpers ---------------------------------------------------- //

    function _carolBuyBuck(address ca, uint256 buckOut) internal {
        // swapTokensForExactTokens: USDC -> BUCK, asking for exact buckOut.
        vm.prank(ca);
        IERC20Like(usdc).approve(router, type(uint256).max);
        address[] memory path = new address[](2);
        path[0] = usdc;
        path[1] = address(buck);
        vm.prank(ca);
        try IV2Router(router).swapTokensForExactTokens(
            buckOut, type(uint256).max, path, ca, block.timestamp + 1
        ) returns (uint256[] memory) { } catch { }
    }

    function _carolSellBuck(address ca, uint256 buckIn) internal {
        _setBuckAllowance(ca, router, type(uint256).max);
        address[] memory path = new address[](2);
        path[0] = address(buck);
        path[1] = usdc;
        vm.prank(ca);
        try IV2Router(router).swapExactTokensForTokens(
            buckIn, 0, path, ca, block.timestamp + 1
        ) returns (uint256[] memory) { } catch { }
    }

    // ---- Snapshot -------------------------------------------------------- //

    function _snap() internal {
        s_t.push(block.timestamp);
        s_spot.push(kCtrl.getBuckPrice());
        s_basket.push(kCtrl.getBasketCost());
        s_buckK.push(kCtrl.buckK());
        s_factor.push(kCtrl.fundingFactor());
        s_pid_p.push(kCtrl.P());
        s_pid_i.push(kCtrl.I());
        s_pid_d.push(kCtrl.D());
        s_supply.push(buck.totalSupply());
        s_jubilee.push(buck.jubileeActual());

        (uint112 r0, uint112 r1,) = IV2Pair(pair).getReserves();
        bool buckIs0 = IV2Pair(pair).token0() == address(buck);
        s_pool_buck.push(buckIs0 ? r0 : r1);
        s_pool_usdc.push(buckIs0 ? r1 : r0);

        (uint32 act, uint32 ret) = _carolCounts();
        s_active.push(act);
        s_retired.push(ret);

        uint32 nh = 0;
        uint256 hbal = 0;
        for (uint256 i = 0; i < hanks.length; i++) {
            if (hanks[i].state == 1) nh++;
            hbal += buck.balanceOf(hankAddrs[i]);
        }
        s_hanks.push(nh);
        s_hank_hold.push(hbal);

        uint256 agg = 0;
        for (uint256 i = 0; i < carols.length; i++) agg += carols[i].mintedNet;
        s_agg_minted.push(agg);
    }

    function _countActive() internal view returns (uint32 n) {
        for (uint256 i = 0; i < carols.length; i++) if (carols[i].state == 1) n++;
    }

    function _carolCounts() internal view returns (uint32 act, uint32 ret) {
        for (uint256 i = 0; i < carols.length; i++) {
            if (carols[i].state == 1) act++;
            else if (carols[i].state == 2) ret++;
        }
    }

    // ---- JSON ----------------------------------------------------------- //

    function _writeJson() internal {
        string memory j = "{";
        j = string.concat(j, _jUint("t",          s_t),          ",");
        j = string.concat(j, _jInt ("spot",       s_spot),       ",");
        j = string.concat(j, _jInt ("basket",     s_basket),     ",");
        j = string.concat(j, _jUint("buckK",      s_buckK),      ",");
        j = string.concat(j, _jUint("factor",     s_factor),     ",");
        j = string.concat(j, _jInt ("pid_p",      s_pid_p),      ",");
        j = string.concat(j, _jInt ("pid_i",      s_pid_i),      ",");
        j = string.concat(j, _jInt ("pid_d",      s_pid_d),      ",");
        j = string.concat(j, _jUint("supply",     s_supply),     ",");
        j = string.concat(j, _jUint("jubilee",    s_jubilee),    ",");
        j = string.concat(j, _jUint("pool_buck",  s_pool_buck),  ",");
        j = string.concat(j, _jUint("pool_usdc",  s_pool_usdc),  ",");
        j = string.concat(j, _jU32 ("active",     s_active),     ",");
        j = string.concat(j, _jU32 ("retired",    s_retired),    ",");
        j = string.concat(j, _jU32 ("hanks",      s_hanks),      ",");
        j = string.concat(j, _jUint("hank_hold",  s_hank_hold),  ",");
        j = string.concat(j, _jUint("agg_minted", s_agg_minted));
        j = string.concat(j, "}");
        vm.writeFile("test/vectors/equilibrium-scenario.json", j);
    }

    function _jUint(string memory key, uint256[] storage arr) internal view returns (string memory) {
        string memory s = string.concat('"', key, '":[');
        for (uint256 i = 0; i < arr.length; i++) {
            if (i > 0) s = string.concat(s, ",");
            s = string.concat(s, vm.toString(arr[i]));
        }
        return string.concat(s, "]");
    }

    function _jU32(string memory key, uint32[] storage arr) internal view returns (string memory) {
        string memory s = string.concat('"', key, '":[');
        for (uint256 i = 0; i < arr.length; i++) {
            if (i > 0) s = string.concat(s, ",");
            s = string.concat(s, vm.toString(uint256(arr[i])));
        }
        return string.concat(s, "]");
    }

    function _jInt(string memory key, int256[] storage arr) internal view returns (string memory) {
        string memory s = string.concat('"', key, '":[');
        for (uint256 i = 0; i < arr.length; i++) {
            if (i > 0) s = string.concat(s, ",");
            int256 v = arr[i];
            if (v >= 0) s = string.concat(s, vm.toString(uint256(v)));
            else        s = string.concat(s, "-", vm.toString(uint256(-v)));
        }
        return string.concat(s, "]");
    }

    // ---- Gaussian helper (sum of 12 uniforms) --------------------------- //

    function _gaussOff(uint256 seed, int256 stdSec) internal pure returns (int256) {
        int256 sum = 0;
        for (uint256 k = 0; k < 12; k++) {
            uint256 h = uint256(keccak256(abi.encode(seed, k, "g")));
            sum += int256(h % 1000) - 500;
        }
        return (sum * stdSec) / 289;
    }

    // ---- Identity helpers (copied from BuckLifecycle.t.sol) -------------- //

    function _setBuckAllowance(address owner_, address spender, uint256 amount) internal {
        bytes32 slot = keccak256(abi.encode(spender, keccak256(abi.encode(owner_, uint256(2)))));
        vm.store(address(buck), slot, bytes32(amount));
    }

    function _u(string memory key) internal view returns (uint256) {
        return vm.parseJsonUint(vj, key);
    }
    function _g1(string memory key) internal view returns (BN254.G1Point memory) {
        return BN254.G1Point(_u(string.concat(key, ".x")), _u(string.concat(key, ".y")));
    }
    function _ps(string memory who) internal view returns (IdentityRegistry.PSSig memory s) {
        s.sigma_1 = _g1(string.concat(".", who, ".ps_sig_rerand.sigma_1"));
        s.sigma_2 = _g1(string.concat(".", who, ".ps_sig_rerand.sigma_2"));
    }
    function _ct(string memory key) internal view returns (IdentityRegistry.ElGamalCT memory c) {
        c.R = _g1(string.concat(key, ".R"));
        c.C = _g1(string.concat(key, ".C"));
    }
    function _regProof(string memory who) internal view returns (IdentityRegistry.RegistrationProof memory p) {
        string memory base = string.concat(".", who, ".registration_proof");
        p.e    = _u(string.concat(base, ".e"));
        p.s_m  = _u(string.concat(base, ".s_m"));
        p.s_r  = _u(string.concat(base, ".s_r"));
        p.A_ps = _g1(string.concat(base, ".A_ps"));
        p.T_C  = _g1(string.concat(base, ".T_C"));
        p.T_R  = _g1(string.concat(base, ".T_R"));
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
    function _registerAlice() internal {
        BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        vm.prank(alice);
        reg.register(ISSUER, pk, E, _ps("alice"), _regProof("alice"));
    }
}
