// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

library PortfolioMath {
    /// @dev Largest remainder apportionment; ties go to the lowest index.
    function weights(uint256[] memory values) internal pure returns (uint256[] memory w) {
        uint256 total;
        uint256 n = values.length;
        w = new uint256[](n);
        for (uint256 i; i < n; ++i) total += values[i];
        if (total == 0) return w;
        uint256 assigned;
        uint256[] memory rem = new uint256[](n);
        for (uint256 i; i < n; ++i) {
            w[i] = values[i] * 10000 / total;
            rem[i] = values[i] * 10000 % total;
            assigned += w[i];
        }
        for (; assigned < 10000; ++assigned) {
            uint256 best;
            for (uint256 i = 1; i < n; ++i) if (rem[i] > rem[best]) best = i;
            ++w[best];
            rem[best] = 0;
        }
    }

    /// @dev exp(y), clamped to [-4,4], via exp(y/16)^16.
    function expWad(int256 y) internal pure returns (uint256) {
        if (y > 4e18) y = 4e18;
        if (y < -4e18) y = -4e18;
        int256 x = y / 16;
        int256 term = 1e18;
        int256 sum = 1e18;
        for (int256 j = 1; j <= 6; ++j) { term = term * x / (j * 1e18); sum += term; }
        uint256 r = uint256(sum);
        for (uint256 j; j < 4; ++j) r = r * r / 1e18;
        return r;
    }

    /// @dev ln(x), x in 1e18 and above zero: halve or double x into
    /// [1/sqrt(2), sqrt(2)], then 2 atanh((x - 1) / (x + 1)) by its series.
    function lnWad(uint256 x) internal pure returns (int256) {
        require(x > 0, "ln of zero");
        int256 halvings;
        while (x >= 1414213562373095049) { x /= 2; ++halvings; }
        while (x < 707106781186547524) { x *= 2; --halvings; }
        int256 z = (int256(x) - 1e18) * 1e18 / (int256(x) + 1e18);
        int256 square = z * z / 1e18;
        int256 term = z;
        int256 sum = z;
        for (int256 n = 3; n <= 9; n += 2) { term = term * square / 1e18; sum += term / n; }
        return 2 * sum + halvings * 693147180559945309;
    }

    function min(uint256 a, uint256 b) internal pure returns (uint256) { return a < b ? a : b; }
}
