// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

contract SimToken is ERC20 {
    uint8 private immutable _dec;
    constructor(string memory n, string memory s, uint8 d) ERC20(n, s) { _dec = d; }
    function decimals() public view override returns (uint8) { return _dec; }
    function mint(address to, uint256 amt) external { _mint(to, amt); }
}

/// @title StabilizerRouting — Arbitrage-driven pool price convergence.
///
/// Simulates 10 agents with USDC reserves arbitraging 3 RWA tokens
/// (PAXG, cbBTC, AOIL) against an exogenous daily CSV reference price.
/// Each day agents compare pool spot to reference and trade toward
/// equilibrium, limited by aggressiveness and multi-round convergence.
/// Records weekly snapshots as JSON for plotting.
contract StabilizerRoutingTest is Test {
    // ── tokens ──────────────────────────────────────────────────────────
    SimToken internal usdc;
    SimToken internal paxg;
    SimToken internal cbbtc;
    SimToken internal aoil;

    // ── pool state (simulated — no real V3 pools needed for Phase 0) ───
    // Each "pool" is just a spot price stored in contract storage.
    uint256 internal spotPaxgUsdc;
    uint256 internal spotCbbtcUsdc;
    uint256 internal spotAoilUsdc;

    // ── config ──────────────────────────────────────────────────────────
    uint256 constant N_AGENTS  = 10;
    uint256 constant N_DAYS     = 365;
    uint256 constant DAY_SECS   = 1 days;
    uint256 constant AGENT_USDC = 1_000_000e6;    // $1M each
    uint256 constant ENTRY_BP   = 300;             // 3% edge to enter
    uint256 constant EXIT_BP    = 50;              // 0.5% to stop
    uint256 constant MAX_ROUNDS = 5;
    uint256 constant AGGRESSION_BP = 3000;          // 30% of gap per trade

    // ── agents ──────────────────────────────────────────────────────────
    struct Agent {
        uint256 usdc;
        uint256 paxg;
        uint256 cbbtc;
        uint256 aoil;
    }
    Agent[N_AGENTS] internal agents;

    // ── reference prices ────────────────────────────────────────────────
    uint256[] internal refPaxg;
    uint256[] internal refCbbtc;
    uint256[] internal refAoil;

    // ── snapshots ───────────────────────────────────────────────────────
    uint256[] internal sDay;
    uint256[] internal sPaxgSpot;     uint256[] internal sPaxgRef;
    uint256[] internal sCbbtcSpot;    uint256[] internal sCbbtcRef;
    uint256[] internal sAoilSpot;     uint256[] internal sAoilRef;
    uint256[] internal sAgentUsdc;

    // ── setup ───────────────────────────────────────────────────────────

    function setUp() public {
        usdc  = new SimToken("USD Coin",  "USDC",  6);
        paxg  = new SimToken("PAX Gold",  "PAXG",  18);
        cbbtc = new SimToken("cbBTC",     "cbBTC", 8);
        aoil  = new SimToken("AlbertaOil","AOIL",  18);

        // Parse prices using Python helper output (simpler than Solidity CSV parser).
        _parseRefPrices();

        // Initialize pool spots to day-0 reference prices (18-dec, USDC per whole TOKEN).
        spotPaxgUsdc  = refPaxg[0];
        spotCbbtcUsdc = refCbbtc[0];
        spotAoilUsdc  = refAoil[0];

        // Mint USDC to agents.
        for (uint256 i = 0; i < N_AGENTS; i++) {
            agents[i] = Agent(AGENT_USDC, 0, 0, 0);
        }
    }

    /// @dev Parse the pre-computed 6-decimal integer prices from a simple format.
    function _parseRefPrices() internal {
        // Read the pre-parsed integer prices from a JSON file produced by gen_prices.py.
        // For simplicity, read the CSV directly and parse inline.
        string memory paxgCsv  = vm.readFile("test/stabilizer-routing-dsv4/prices/PAXG.csv");
        string memory cbbtcCsv = vm.readFile("test/stabilizer-routing-dsv4/prices/cbBTC.csv");
        string memory aoilCsv  = vm.readFile("test/stabilizer-routing-dsv4/prices/AOIL.csv");
        _parseCsv(paxgCsv,  refPaxg);
        _parseCsv(cbbtcCsv, refCbbtc);
        _parseCsv(aoilCsv,  refAoil);
    }

    /// @dev Parse a simple two-column CSV (date,price_usd) into uint256[] of 6-dec prices.
    function _parseCsv(string memory csv, uint256[] storage out) internal {
        bytes memory data = bytes(csv);
        uint256 i = 0;

        // Skip header line.
        while (i < data.length && data[i] != '\n') i++;
        i++; // past newline

        while (i < data.length) {
            // Skip date column (until comma).
            while (i < data.length && data[i] != ',') i++;
            i++; // past comma

            // Parse price: collect integer part, then optional fractional part.
            uint256 intPart = 0;
            while (i < data.length && data[i] >= '0' && data[i] <= '9') {
                intPart = intPart * 10 + uint256(uint8(data[i]) - 48);
                i++;
            }
            uint256 fracPart = 0;
            uint256 fracDigits = 0;
            if (i < data.length && data[i] == '.') {
                i++; // skip dot
                while (i < data.length && data[i] >= '0' && data[i] <= '9') {
                    fracPart = fracPart * 10 + uint256(uint8(data[i]) - 48);
                    fracDigits++;
                    i++;
                }
            }
            // Normalize to 6 decimals.
            uint256 price = intPart * 1e6;
            if (fracDigits > 0) {
                price += fracPart * (10 ** (6 - fracDigits));
            }
            out.push(price);

            // Skip to next line.
            while (i < data.length && data[i] != '\n') i++;
            if (i < data.length) i++; // past newline
        }
    }

    // ── simulation ──────────────────────────────────────────────────────

    function test_stabilizer_routing_12months() public {
        uint256 snapEvery = 7; // weekly

        for (uint256 day = 0; day < N_DAYS; day++) {
            vm.warp(block.timestamp + DAY_SECS);

            uint256 refP = refPaxg[day];
            uint256 refC = refCbbtc[day];
            uint256 refA = refAoil[day];

            // Multi-round arbitrage.
            for (uint256 round = 0; round < MAX_ROUNDS; round++) {
                bool any = false;

                // Randomize agent order each round.
                uint256 seed = uint256(keccak256(abi.encode(day, round)));
                uint256[] memory order = _shuffle(seed);

                for (uint256 ai = 0; ai < N_AGENTS; ai++) {
                    uint256 idx = order[ai];
                    if (_arbPaxg(idx, refP))   any = true;
                    if (_arbCbbtc(idx, refC))  any = true;
                    if (_arbAoil(idx, refA))   any = true;
                }
                if (!any) break;
            }

            // Drift pool spots toward reference by a random amount (simulating
            // external non-arb trade flow that imperfectly tracks reference).
            spotPaxgUsdc  = _driftToward(spotPaxgUsdc,  refP, 100);  // 1% daily drift
            spotCbbtcUsdc = _driftToward(spotCbbtcUsdc, refC, 100);
            spotAoilUsdc  = _driftToward(spotAoilUsdc,  refA, 100);

            if (day % snapEvery == 0 || day == N_DAYS - 1) {
                _snap(day);
            }
        }

        _writeJson();
    }

    function _driftToward(uint256 spot, uint256 ref, uint256 bpPerDay) internal returns (uint256) {
        uint256 seed = uint256(keccak256(abi.encode(spot, ref, block.timestamp)));
        int256 delta = int256(ref) - int256(spot);
        // Move a random fraction toward ref, scaled by bpPerDay.
        int256 move = delta * int256(bpPerDay) / 10000;
        // Add noise: +/- 20% of the move.
        int256 noise = move * int256(int8(uint8(seed % 41)) - 20) / 100;
        int256 newSpot = int256(spot) + move + noise;
        return newSpot > 0 ? uint256(newSpot) : spot;
    }

    function _edgeBp(uint256 spot, uint256 ref) internal pure returns (uint256) {
        if (ref == 0) return 0;
        int256 e = int256(spot) - int256(ref);
        return uint256(e > 0 ? e : -e) * 10000 / ref;
    }

    function _arbPaxg(uint256 agIdx, uint256 ref) internal returns (bool) {
        return _arb(agIdx, ref, 0);
    }
    function _arbCbbtc(uint256 agIdx, uint256 ref) internal returns (bool) {
        return _arb(agIdx, ref, 1);
    }
    function _arbAoil(uint256 agIdx, uint256 ref) internal returns (bool) {
        return _arb(agIdx, ref, 2);
    }

    function _arb(uint256 agIdx, uint256 refPrice, uint256 which)
        internal returns (bool)
    {
        Agent storage ag = agents[agIdx];
        uint256 spot;
        if (which == 0) spot = spotPaxgUsdc;
        else if (which == 1) spot = spotCbbtcUsdc;
        else spot = spotAoilUsdc;
        if (spot == 0 || refPrice == 0) return false;

        uint256 edgeBp = _edgeBp(spot, refPrice);
        if (edgeBp < ENTRY_BP) return false;

        uint256 tokenBal;
        if (which == 0) tokenBal = ag.paxg;
        else if (which == 1) tokenBal = ag.cbbtc;
        else tokenBal = ag.aoil;

        int256 edge = int256(spot) - int256(refPrice);
        uint256 liq = (which == 0) ? 100000e6 : (which == 1 ? 200000e6 : 50000e6);

        if (edge < 0) {
            // Buy: token is cheap.
            if (ag.usdc < 1e4) return false;
            uint256 usdcSpent = ag.usdc * edgeBp * AGGRESSION_BP / 100000000;
            if (usdcSpent > ag.usdc / 4) usdcSpent = ag.usdc / 4;
            if (usdcSpent < 1e4) return false;
            uint256 tokenOut = usdcSpent * 1e18 / spot;
            ag.usdc -= usdcSpent;
            if (which == 0) ag.paxg += tokenOut;
            else if (which == 1) ag.cbbtc += tokenOut;
            else ag.aoil += tokenOut;
            uint256 newSpot = spot + (refPrice - spot) * usdcSpent / (usdcSpent + liq);
            if (which == 0) spotPaxgUsdc = newSpot;
            else if (which == 1) spotCbbtcUsdc = newSpot;
            else spotAoilUsdc = newSpot;
        } else {
            // Sell: token is expensive.
            if (tokenBal == 0) return false;
            uint256 tokSold = tokenBal * edgeBp * AGGRESSION_BP / 100000000;
            if (tokSold > tokenBal / 4) tokSold = tokenBal / 4;
            if (tokSold == 0) return false;
            uint256 usdcOut = tokSold * spot / 1e18;
            if (which == 0) ag.paxg -= tokSold;
            else if (which == 1) ag.cbbtc -= tokSold;
            else ag.aoil -= tokSold;
            ag.usdc += usdcOut;
            uint256 newSpot = spot - (spot - refPrice) * tokSold / (tokSold + liq);
            if (which == 0) spotPaxgUsdc = newSpot;
            else if (which == 1) spotCbbtcUsdc = newSpot;
            else spotAoilUsdc = newSpot;
        }
        return true;
    }

    function _shuffle(uint256 seed) internal pure returns (uint256[] memory) {
        uint256[] memory arr = new uint256[](N_AGENTS);
        for (uint256 i = 0; i < N_AGENTS; i++) arr[i] = i;
        uint256 r = seed;
        for (uint256 i = N_AGENTS - 1; i > 0; i--) {
            r = uint256(keccak256(abi.encode(r)));
            uint256 j = r % (i + 1);
            (arr[i], arr[j]) = (arr[j], arr[i]);
        }
        return arr;
    }

    // ── snapshot / JSON ─────────────────────────────────────────────────

    function _snap(uint256 day) internal {
        sDay.push(day);
        sPaxgSpot.push(spotPaxgUsdc);   sPaxgRef.push(refPaxg[day]);
        sCbbtcSpot.push(spotCbbtcUsdc);  sCbbtcRef.push(refCbbtc[day]);
        sAoilSpot.push(spotAoilUsdc);    sAoilRef.push(refAoil[day]);
        uint256 totalUsdc = 0;
        for (uint256 i = 0; i < N_AGENTS; i++) {
            totalUsdc += agents[i].usdc;
        }
        sAgentUsdc.push(totalUsdc);
    }

    function _writeJson() internal {
        string memory json = string.concat(
            "{",
            _ja("day",        sDay),       ",",
            _ja("paxg_spot",  sPaxgSpot),  ",",
            _ja("paxg_ref",   sPaxgRef),   ",",
            _ja("cbbtc_spot", sCbbtcSpot), ",",
            _ja("cbbtc_ref",  sCbbtcRef),  ",",
            _ja("aoil_spot",  sAoilSpot),  ",",
            _ja("aoil_ref",   sAoilRef),   ",",
            _ja("agent_usdc", sAgentUsdc),
            "}"
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
