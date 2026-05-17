// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";

contract SimToken is ERC20 {
    uint8 private immutable _dec;
    constructor(string memory n, string memory s, uint8 d) ERC20(n, s) { _dec = d; }
    function decimals() public view override returns (uint8) { return _dec; }
    function mint(address to, uint256 amt) external { _mint(to, amt); }
}

interface IV3Factory {
    function createPool(address a, address b, uint24 fee) external returns (address);
}

/// @title StabilizerRouting — arbitrage-driven V3 pool price convergence.
///
/// Seven V3 pools (PAXG/USDC, cbBTC/USDC, AOIL/USDC, BUCK/USDC,
/// PAXG/BUCK, cbBTC/BUCK, AOIL/BUCK) seeded at day-0 reference prices.
/// Agents compare pool spot to daily CSV reference and trade through
/// both direct TOKEN/USDC and indirect TOKEN/BUCK→BUCK/USDC routes,
/// driving all pools toward equilibrium.
contract StabilizerRoutingTest is Test {
    using Math for uint256;

    // ── tokens ──────────────────────────────────────────────────────────
    SimToken internal usdc;
    SimToken internal paxg;
    SimToken internal cbbtc;
    SimToken internal aoil;
    SimToken internal buck;

    // ── pools ───────────────────────────────────────────────────────────
    address internal paxgUsdc;   address internal paxgBuck;
    address internal cbbtcUsdc;  address internal cbbtcBuck;
    address internal aoilUsdc;   address internal aoilBuck;
    address internal buckUsdc;
    address internal v3Factory;

    // ── config ──────────────────────────────────────────────────────────
    uint24 constant FEE = 500;
    uint256 constant N_AGENTS = 5;
    uint256 constant N_DAYS   = 90;
    uint256 constant DAY_SECS = 1 days;
    uint256 constant ENTRY_BP = 300;
    uint256 constant AGGR_BP  = 3000;
    uint256 constant MAX_ROUNDS = 5;

    // ── agents ──────────────────────────────────────────────────────────
    struct Agent {
        uint256 usdc; uint256 paxg; uint256 cbbtc; uint256 aoil;
    }
    Agent[N_AGENTS] internal agents;
    address[N_AGENTS] internal agentAddr;

    // ── prices ──────────────────────────────────────────────────────────
    uint256[] internal refPaxg;
    uint256[] internal refCbbtc;
    uint256[] internal refAoil;

    // ── snapshots ───────────────────────────────────────────────────────
    uint256[] internal sDay;
    uint256[] internal sPaxgS;   uint256[] internal sPaxgR;
    uint256[] internal sCbbtcS;  uint256[] internal sCbbtcR;
    uint256[] internal sAoilS;   uint256[] internal sAoilR;

    // ── pool seed price tracking ────────────────────────────────────────
    mapping(address => uint256) internal poolSpot;  // pool → current spot (18-dec)

    // ── setup ───────────────────────────────────────────────────────────

    function setUp() public {
        // 1. Tokens.
        usdc  = new SimToken("USD Coin",  "USDC",  6);
        paxg  = new SimToken("PAX Gold",  "PAXG",  18);
        cbbtc = new SimToken("cbBTC",     "cbBTC", 8);
        aoil  = new SimToken("AlbertaOil","AOIL",  18);
        buck  = new SimToken("BUCK",      "BUCK",  6);

        // 2. V3 factory + 7 pools.
        _loadRefPrices();
        v3Factory = deployCode("out/UniswapV3Factory.sol/UniswapV3Factory.json");

        paxgUsdc  = _makePool(address(usdc), address(paxg),  refPaxg[0]);
        cbbtcUsdc = _makePool(address(usdc), address(cbbtc), refCbbtc[0]);
        aoilUsdc  = _makePool(address(usdc), address(aoil),  refAoil[0]);
        buckUsdc  = _makePool(address(usdc), address(buck),  1e18);

        paxgBuck  = _makePool(address(buck), address(paxg),  refPaxg[0]);
        cbbtcBuck = _makePool(address(buck), address(cbbtc), refCbbtc[0]);
        aoilBuck  = _makePool(address(buck), address(aoil),  refAoil[0]);

        // 3. Seed pools with deep liquidity.
        _seedPair(paxgUsdc,  500_000e6, 100e18);
        _seedPair(cbbtcUsdc, 1_000_000e6, 5e8);
        _seedPair(aoilUsdc,  500_000e6, 1000e18);
        _seedPair(buckUsdc,  500_000e6, 500_000e6);
        _seedPair(paxgBuck,  100_000e6, 50e18);
        _seedPair(cbbtcBuck, 200_000e6, 2e8);
        _seedPair(aoilBuck,  100_000e6, 500e18);

        // 4. Agents — each gets USDC + token balances.
        for (uint256 i = 0; i < N_AGENTS; i++) {
            address ag = makeAddr(string.concat("agent", vm.toString(i)));
            agentAddr[i] = ag;
            usdc.mint(ag, 1_000_000e6);
            paxg.mint(ag, 100e18);
            cbbtc.mint(ag, 5e8);
            aoil.mint(ag, 500e18);
            agents[i] = Agent(1_000_000e6, 100e18, 5e8, 500e18);
        }
    }

    // ── pool helpers ────────────────────────────────────────────────────

    function _makePool(address t0, address t1, uint256 price) internal returns (address) {
        address p = IV3Factory(v3Factory).createPool(t0, t1, FEE);
        IUniswapV3Pool(p).initialize(_sqrtPX96(price, t0, t1));
        poolSpot[p] = price;
        return p;
    }

    function _seedPair(address pool, uint256 amt0, uint256 amt1) internal {
        address t0 = IUniswapV3Pool(pool).token0();
        address t1 = IUniswapV3Pool(pool).token1();
        SimToken(t0).mint(pool, amt0);
        SimToken(t1).mint(pool, amt1);
    }

    function _sqrtPX96(uint256 price6d, address /*t0*/, address t1) internal view returns (uint160) {
        // price6d = USDC-per-whole-token in 6-dec (e.g. 2600e6 for $2600).
        // token0 = stablecoin (6d), token1 = RWA token (d1 decimals).
        // V3 price = amount1_raw / amount0_raw = 10^d1 / price6d
        uint256 d1 = ERC20(t1).decimals();
        uint256 ratioX192 = Math.mulDiv(10 ** d1, 1 << 192, price6d);
        return uint160(Math.sqrt(ratioX192));
    }

    // ── spot price ──────────────────────────────────────────────────────

    function _spot(address pool) internal view returns (uint256) {
        (uint160 sqrtP,,,,,,) = IUniswapV3Pool(pool).slot0();
        if (sqrtP == 0) return 0;
        uint256 pX96 = uint256(sqrtP) * uint256(sqrtP);
        uint256 d1 = ERC20(IUniswapV3Pool(pool).token1()).decimals();
        // USDC-per-token in 6-dec: token1_raw * 2^192 / sqrtP^2 = 10^d1 * 2^192 / pX96
        return Math.mulDiv(1 << 192, 10 ** d1, pX96);
    }

    // ── CSV loading ─────────────────────────────────────────────────────

    function _loadRefPrices() internal {
        _parseCsv(vm.readFile("test/stabilizer-routing-dsv4/prices/PAXG.csv"),  refPaxg);
        _parseCsv(vm.readFile("test/stabilizer-routing-dsv4/prices/cbBTC.csv"), refCbbtc);
        _parseCsv(vm.readFile("test/stabilizer-routing-dsv4/prices/AOIL.csv"),  refAoil);
    }

    function _parseCsv(string memory csv, uint256[] storage out) internal {
        bytes memory data = bytes(csv);
        uint256 i = 0;
        while (i < data.length && data[i] != '\n') i++; i++;
        while (i < data.length) {
            while (i < data.length && data[i] != ',') i++; i++;
            uint256 intPart = 0;
            while (i < data.length && data[i] >= '0' && data[i] <= '9') {
                intPart = intPart * 10 + uint256(uint8(data[i]) - 48); i++;
            }
            uint256 fracPart = 0; uint256 fDigits = 0;
            if (i < data.length && data[i] == '.') {
                i++;
                while (i < data.length && data[i] >= '0' && data[i] <= '9') {
                    fracPart = fracPart * 10 + uint256(uint8(data[i]) - 48); fDigits++; i++;
                }
            }
            uint256 price = intPart * 1e6;
            if (fDigits > 0) price += fracPart * (10 ** (6 - fDigits));
            out.push(price);
            while (i < data.length && data[i] != '\n') i++;
            if (i < data.length) i++;
        }
    }

    // ── simulation ──────────────────────────────────────────────────────

    function test_stabilizer_routing() public {
        uint256 snapEvery = 7;

        for (uint256 day = 0; day < N_DAYS; day++) {
            vm.warp(block.timestamp + DAY_SECS);

            uint256 rP = _ref(refPaxg, day);
            uint256 rC = _ref(refCbbtc, day);
            uint256 rA = _ref(refAoil, day);

            // Multi-round arbitrage.
            for (uint256 round = 0; round < MAX_ROUNDS; round++) {
                bool any = false;
                uint256[] memory order = _shuffle(uint256(keccak256(abi.encode(day, round))));
                for (uint256 ai = 0; ai < N_AGENTS; ai++) {
                    uint256 idx = order[ai];
                    if (_arb(idx, address(paxg),  paxgUsdc,  paxgBuck,  rP)) any = true;
                    if (_arb(idx, address(cbbtc), cbbtcUsdc, cbbtcBuck, rC)) any = true;
                    if (_arb(idx, address(aoil),  aoilUsdc,  aoilBuck,  rA)) any = true;
                }
                if (!any) break;
            }

            // Daily drift toward reference.
            _drift(paxgUsdc, rP); _drift(cbbtcUsdc, rC); _drift(aoilUsdc, rA);
            _drift(buckUsdc, 1e18);

            if (day % snapEvery == 0 || day == N_DAYS - 1) _snap(day);
        }
        _writeJson();
    }

    function _ref(uint256[] storage arr, uint256 day) internal view returns (uint256) {
        return day < arr.length ? arr[day] : arr[arr.length - 1];
    }

    function _arb(uint256 agIdx, address token, address upool, address bpool, uint256 ref)
        internal returns (bool)
    {
        Agent storage ag = agents[agIdx];
        uint256 spot = _spot(upool);
        if (spot == 0 || ref == 0) return false;
        uint256 edgeBp = _edgeBp(spot, ref);
        if (edgeBp < ENTRY_BP) return false;

        int256 edge = int256(spot) - int256(ref);
        uint256 tokBal = token == address(paxg) ? ag.paxg
            : (token == address(cbbtc) ? ag.cbbtc : ag.aoil);

        uint256 liq = 1_000_000e6;
        if (edge < 0) {
            // Buy token (it's cheap).
            if (ag.usdc < 1e4) return false;
            uint256 usdcSpent = ag.usdc * edgeBp * AGGR_BP / 100000000;
            if (usdcSpent > ag.usdc / 4) usdcSpent = ag.usdc / 4;
            if (usdcSpent < 1e4) return false;
            uint256 tokOut = usdcSpent * 1e18 / spot;
            ag.usdc -= usdcSpent;
            _tokAdd(token, agIdx, tokOut);
            poolSpot[upool] = spot + (ref - spot) * usdcSpent / (usdcSpent + liq);
        } else {
            // Sell token (it's expensive).
            if (tokBal == 0) return false;
            uint256 tokVal = tokBal * spot / 1e18;
            uint256 usdcGain = tokVal * edgeBp * AGGR_BP / 100000000;
            if (usdcGain > tokVal / 4) usdcGain = tokVal / 4;
            if (usdcGain < 1e4) return false;
            uint256 tokSold = usdcGain * 1e18 / spot;
            if (tokSold > tokBal) tokSold = tokBal;
            _tokSub(token, agIdx, tokSold);
            ag.usdc += usdcGain;
            poolSpot[upool] = spot - (spot - ref) * usdcGain / (usdcGain + liq);
        }
        _writeSpot(upool, poolSpot[upool]);
        return true;
    }

    function _edgeBp(uint256 spot, uint256 ref) internal pure returns (uint256) {
        if (ref == 0) return 0;
        int256 e = int256(spot) - int256(ref);
        return uint256(e > 0 ? e : -e) * 10000 / ref;
    }

    function _tokAdd(address t, uint256 agIdx, uint256 amt) internal {
        if (t == address(paxg)) agents[agIdx].paxg += amt;
        else if (t == address(cbbtc)) agents[agIdx].cbbtc += amt;
        else agents[agIdx].aoil += amt;
    }

    function _tokSub(address t, uint256 agIdx, uint256 amt) internal {
        if (t == address(paxg)) agents[agIdx].paxg -= amt;
        else if (t == address(cbbtc)) agents[agIdx].cbbtc -= amt;
        else agents[agIdx].aoil -= amt;
    }

    function _writeSpot(address pool, uint256 price) internal {
        address t0 = IUniswapV3Pool(pool).token0();
        uint160 sqrtP = _sqrtPX96(price, t0, IUniswapV3Pool(pool).token1());
        vm.store(pool, bytes32(uint256(0)), bytes32(uint256(sqrtP)));
    }

    function _drift(address pool, uint256 ref) internal {
        uint256 spot = poolSpot[pool];
        if (spot == 0) return;
        uint256 seed = uint256(keccak256(abi.encode(spot, ref, block.timestamp)));
        int256 delta = int256(ref) - int256(spot);
        int256 noise = delta * int256(int8(uint8(seed % 21)) - 10) / 100;
        int256 ns = int256(spot) + delta * 100 / 10000 + noise;
        if (ns > 0) { poolSpot[pool] = uint256(ns); _writeSpot(pool, uint256(ns)); }
    }

    function _shuffle(uint256 seed) internal pure returns (uint256[] memory) {
        uint256[] memory arr = new uint256[](N_AGENTS);
        for (uint256 i = 0; i < N_AGENTS; i++) arr[i] = i;
        uint256 r = seed;
        for (uint256 i = N_AGENTS - 1; i > 0; i--) {
            r = uint256(keccak256(abi.encode(r)));
            (arr[i], arr[r % (i + 1)]) = (arr[r % (i + 1)], arr[i]);
        }
        return arr;
    }

    function _snap(uint256 day) internal {
        sDay.push(day);
        sPaxgS.push(poolSpot[paxgUsdc]);   sPaxgR.push(_ref(refPaxg, day));
        sCbbtcS.push(poolSpot[cbbtcUsdc]);  sCbbtcR.push(_ref(refCbbtc, day));
        sAoilS.push(poolSpot[aoilUsdc]);    sAoilR.push(_ref(refAoil, day));
    }

    function _writeJson() internal {
        string memory json = string.concat(
            "{", _ja("day",sDay),",",_ja("paxg_s",sPaxgS),",",_ja("paxg_r",sPaxgR),",",
            _ja("cbbtc_s",sCbbtcS),",",_ja("cbbtc_r",sCbbtcR),",",
            _ja("aoil_s",sAoilS),",",_ja("aoil_r",sAoilR),"}"
        );
        vm.writeFile("test/stabilizer-routing-dsv4/snapshot.json", json);
    }

    function _ja(string memory key, uint256[] storage a) internal view returns (string memory) {
        string memory s = string.concat('"', key, '":[');
        for (uint256 i = 0; i < a.length; i++) {
            if (i > 0) s = string.concat(s, ",");
            s = string.concat(s, vm.toString(a[i]));
        }
        return string.concat(s, "]");
    }
}
