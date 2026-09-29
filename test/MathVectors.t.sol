// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";

import {BuckCredit} from "../src/BuckCredit.sol";
import {Buck} from "../src/Buck.sol";
import {BuckKControllerDirect} from "../src/BuckKControllerDirect.sol";
import {toBuckQtySigned, toBuckSeconds} from "../src/BuckTypes.sol";

/// @title MathVectors -- golden-vector generator for the buck-math kernel.
///
/// The Solidity contracts are the mathematical specification
/// (alberta-buck-platform.org, Layer 2).  This test drives the REAL
/// deployed-code paths -- BuckCredit._depreciate, Buck._feeOwing,
/// Buck._carryingTransfer, BuckKControllerDirect.compute()/fundingFactor()
/// -- across input grids and writes test/vectors/math-vectors.json, which
/// the Rust (cargo), Python (pytest), and JS (node --test) suites all
/// assert byte-equally against.
///
/// All numbers are serialized as DECIMAL STRINGS (values exceed 2^53; JS
/// JSON numbers would lose precision).  Regenerate with:
///
///     make nix-match-MathVectors
///
/// A changed vector file in git diff means the on-chain math changed --
/// treat exactly like an ABI break.

contract DepHarness is BuckCredit {
    function depreciate(uint256 face, uint8 depType, uint32 rate,
                        uint256 floor_, uint48 startAt)
        external view returns (uint256)
    {
        return _depreciate(face, DepreciationType(depType), rate, floor_,
                           startAt);
    }
}

contract FeeHarness is Buck {
    constructor() Buck(address(1), address(2), address(3), address(4)) {}

    function setState(address a, int256 raw, uint256 bs, uint40 ts) external {
        // _setBalanceSigned keeps the private _totalSupply consistent (the
        // carrying path debits it); then plant the demurrage history.
        _setBalanceSigned(a, raw);
        _state[a].buckSeconds = toBuckSeconds(bs);
        _state[a].timestamp = ts;
    }

    // feeOwing(address) is already Buck's public demurrage view -- the
    // vectors call the real entry point directly.

    function carrying(address from, address to, uint256 value) external {
        _carryingTransfer(from, to, value);
    }

    function stateOf(address a)
        external view returns (int256 raw, uint256 bs, uint40 ts)
    {
        AccountState storage s = _state[a];
        return (s.balance.asInt(), s.buckSeconds.asUint(), s.timestamp);
    }
}

contract MockBasketRef {
    int256 public v = 1e18;
    function set(int256 _v) external { v = _v; }
    function basketValueInBuck() external view returns (int256) { return v; }
}

contract PidHarness is BuckKControllerDirect {
    constructor(int256 kp, int256 ki, int256 kd, uint256 dt,
                uint256 kmin, uint256 kmax, uint256 k0)
        BuckKControllerDirect(kp, ki, kd, dt, kmin, kmax, k0, msg.sender) {}

    function setRefs(int256 b, int256 p) external {
        lastBasketCost = b;
        lastBuckPrice = p;
    }
}

contract MathVectorsTest is Test {
    uint256 constant T0 = 1_750_000_000;           // fixed epoch for all rows
    string constant OUT = "test/vectors/math-vectors.json";

    string json;

    function _row(string memory section, string memory body) internal {
        json = string.concat(json, bytes(json).length > 0 ? ",\n" : "",
                             "  {\"s\":\"", section, "\",", body, "}");
    }

    function _kv(string memory k, uint256 v) internal pure
        returns (string memory)
    {
        return string.concat("\"", k, "\":\"", vm.toString(v), "\"");
    }

    function _kvi(string memory k, int256 v) internal pure
        returns (string memory)
    {
        return string.concat("\"", k, "\":\"", vm.toString(v), "\"");
    }

    function test_generate_math_vectors() public {
        _depreciationRows();
        _feeRows();
        _carryingRows();
        _fundingFactorRows();
        _pidRows();
        vm.writeFile(OUT, string.concat(
            "[\n", json, "\n]\n"));
    }

    // -- BuckCredit._depreciate over a type x face x rate x floor x t grid --

    function _depreciationRows() internal {
        DepHarness h = new DepHarness();
        uint256[3] memory faces =
            [uint256(1e6), 987_654_321_012_345, 1_208_925_819_614_629_174_706_175];
        uint32[3] memory rates = [uint32(100), 1500, 9999];
        uint256[7] memory elapseds = [uint256(0), 1, 15_778_800, 31_557_600,
                                      31_557_601, 78_894_000, 3_155_760_000];

        for (uint8 t = 1; t <= 2; t++) {                 // LINEAR, DECLINING
            for (uint256 f = 0; f < faces.length; f++) {
                for (uint256 r = 0; r < rates.length; r++) {
                    for (uint256 fl = 0; fl < 2; fl++) {
                        uint256 floor_ = fl == 0 ? 0 : faces[f] / 10;
                        for (uint256 e = 0; e < elapseds.length; e++) {
                            _oneDep(h, faces[f], t, rates[r], floor_,
                                    elapseds[e]);
                        }
                    }
                }
            }
        }
        // NONE + rate edge cases (0 bp, 10000 bp) on the declining path.
        _oneDep(h, 1e12, 0, 5000, 0, 31_557_600);
        _oneDep(h, 1e12, 2, 0, 17, 31_557_600);
        _oneDep(h, 1e12, 2, 10_000, 17, 1);
        _oneDep(h, 5, 2, 9999, 0, 63_115_200);          // v -> 0 -> floor
    }

    function _oneDep(DepHarness h, uint256 face, uint8 t, uint32 rate,
                     uint256 floor_, uint256 elapsed) internal {
        vm.warp(T0 + elapsed);
        uint256 v = h.depreciate(face, t, rate, floor_, uint48(T0));
        _row("dep", string.concat(
            _kv("face", face), ",", _kv("dep_type", t), ",",
            _kv("rate_bp", rate), ",", _kv("floor", floor_), ",",
            _kv("elapsed", elapsed), ",", _kv("value", v)));
    }

    // -- Buck._feeOwing (demurrage) over bs x raw x elapsed ----------------

    function _feeRows() internal {
        FeeHarness h = new FeeHarness();
        address a = address(0xFEE);
        uint256[4] memory bss = [uint256(0), 1, 1e18,
                                 1_329_227_995_784_915_872_903_807_060_280_344_575];
        int256[3] memory raws = [int256(0), 1e6, 604_462_909_807_314_587_353_087];
        uint256[5] memory elapseds =
            [uint256(0), 1, 86_400, 31_557_600, 315_576_000];

        for (uint256 b = 0; b < bss.length; b++) {
            for (uint256 r = 0; r < raws.length; r++) {
                for (uint256 e = 0; e < elapseds.length; e++) {
                    vm.warp(T0 + elapseds[e]);
                    h.setState(a, raws[r], bss[b], uint40(T0));
                    uint256 fee = h.feeOwing(a);
                    _row("fee", string.concat(
                        _kv("buck_seconds", bss[b]), ",",
                        _kvi("raw", raws[r]), ",",
                        _kv("elapsed", elapseds[e]), ",", _kv("fee", fee)));
                }
            }
        }
    }

    // -- Buck._carryingTransfer buckSeconds apportionment ------------------

    function _carryingRows() internal {
        // (fromRaw, fromBs, fromElapsed, toRaw, toBs, toElapsed, value)
        // A recipient below zero holds issuance-seconds and is repaid net of
        // the carried fee; a receipt that repays the whole lien pays its
        // relief (the last row: a lien carried a year).  Each harness plants
        // a Jubilee fund that covers the relief, as the fund does by
        // construction (it accrues on the BUCK issued).
        int256[7][9] memory cases = [
            [int256(1e12), 5e15, 3600, 0, 0, 0, 4e11],
            [int256(1e12), 5e15, 3600, 2e11, 7e14, 7200, 4e11],
            [int256(1e12), 0, 86_400, -3e11, 0, 500, 1e12],
            [int256(1e12), 0, 86_400, -3e12, 0, 500, 1e12],
            [int256(7), 13, 1, 1, 1, 1, 3],
            [int256(1e6), 1, 0, 0, 0, 0, 1e6],
            [int256(9e11), 123_456_789, 55, 1e11, 987_654_321, 66, 899_999_999_999],
            [int256(2e12), 1e15, 31_557_600, 1e12, 1e15, 31_557_600, 1],
            [int256(1e12), 0, 86_400, -3e11, 9_467_280_000_000_000_000, 0, 1e12]
        ];
        for (uint256 i = 0; i < cases.length; i++) {
            // Fresh harness per case: clean state, clean totals.
            FeeHarness h = new FeeHarness();
            address from = address(0xF00);
            address to = address(0x700);
            int256[7] memory c = cases[i];
            vm.warp(T0 + 1_000_000);
            h.setState(address(h), 1e15, 0, uint40(T0 + 1_000_000));   // the fund
            h.setState(from, c[0], uint256(c[1]),
                       uint40(T0 + 1_000_000 - uint256(c[2])));
            h.setState(to, c[3], uint256(c[4]),
                       uint40(T0 + 1_000_000 - uint256(c[5])));
            h.carrying(from, to, uint256(c[6]));
            (int256 fRaw, uint256 fBs,) = h.stateOf(from);
            (int256 tRaw, uint256 tBs,) = h.stateOf(to);
            _row("carry", string.concat(
                _kvi("from_raw", c[0]), ",", _kv("from_bs", uint256(c[1])),
                ",", _kv("from_elapsed", uint256(c[2])), ",",
                _kvi("to_raw", c[3]), ",", _kv("to_bs", uint256(c[4])), ",",
                _kv("to_elapsed", uint256(c[5])), ",",
                _kv("value", uint256(c[6])), ",",
                _kvi("from_raw_after", fRaw), ",",
                _kv("from_bs_after", fBs), ",",
                _kvi("to_raw_after", tRaw), ",", _kv("to_bs_after", tBs)));
        }
    }

    // -- fundingFactor over (basket, buck) reference pairs -----------------

    function _fundingFactorRows() internal {
        PidHarness h = new PidHarness(0, 1, 0, 600, 0, 1e18, 5e17);
        // 1e18-scale pairs (legacy embodiment) and ppm pairs (direct).
        int256[2][10] memory pairs = [
            [int256(1e18), 1e18],
            [int256(105e16), 1e18],
            [int256(95e16), 1e18],
            [int256(11e17), 1e18],
            [int256(9e17), 1e18],
            [int256(0), 1e18],
            [int256(-5), 1e18],
            [int256(1_000_000), 1_000_000],
            [int256(1_050_000), 1_000_000],
            [int256(909_090), 1_000_000]
        ];
        for (uint256 i = 0; i < pairs.length; i++) {
            h.setRefs(pairs[i][0], pairs[i][1]);
            uint256 ff = h.fundingFactor();
            _row("ff", string.concat(
                _kvi("basket", pairs[i][0]), ",", _kvi("buck", pairs[i][1]),
                ",", _kv("factor", ff)));
        }
    }

    // -- BuckKControllerDirect.compute() scripted scenario -----------------

    function _pidRows() internal {
        int256 KP = 5e9;            // real 0.005   (per ppm error)
        int256 KI = 2e6;            // real 2e-6    (per ppm*s)
        int256 KD = 1e12;           // real 1.0     (per ppm/s)
        uint256 DT = 600;
        uint256 DTMAX = 86_400;
        uint256 KMIN = 0;
        uint256 KMAX = 95e16;
        uint256 K0 = 75e16;

        vm.warp(T0);
        PidHarness h = new PidHarness(KP, KI, KD, DT, KMIN, KMAX, K0);
        MockBasketRef basket = new MockBasketRef();
        h.setBasket(address(basket));
        h.setDTMax(DTMAX);
        _row("pid_params", string.concat(
            _kvi("kp", KP), ",", _kvi("ki", KI), ",", _kvi("kd", KD), ",",
            _kv("dt", DT), ",", _kv("dt_max", DTMAX), ",",
            _kv("kmin", KMIN), ",", _kv("kmax", KMAX), ",", _kv("k0", K0),
            ",", _kv("t0", T0)));

        // (dt_seconds, basketValue 1e18): drift, cache-hit, dtMax clamp,
        // deep rich excursion to the KMIN rail (anti-windup), recovery.
        uint256[12] memory dts = [uint256(3600), 3600, 100, 3600, 200_000,
                                  3600, 3600, 3600, 3600, 3600, 200_000, 3600];
        int256[12] memory bvs = [int256(1e18), 102e16, 105e16, 105e16,
                                 98e16, 160e16, 160e16, 160e16, 90e16,
                                 90e16, 90e16, 1e18];
        uint256 t = T0;
        for (uint256 i = 0; i < dts.length; i++) {
            t += dts[i];
            vm.warp(t);
            basket.set(bvs[i]);
            uint256 k = h.compute();
            _row("pid", string.concat(
                _kv("t", t), ",", _kvi("basket", bvs[i]), ",",
                _kv("buck_k", k), ",", _kvi("p", h.P()), ",",
                _kvi("i", h.I()), ",", _kvi("d", h.D()), ",",
                _kv("last_update", h.lastUpdate()), ",",
                _kv("ff", h.fundingFactor())));
        }
    }
}
