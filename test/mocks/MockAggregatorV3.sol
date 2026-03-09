// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "@chainlink/contracts/src/v0.8/shared/interfaces/AggregatorV3Interface.sol";

/// @notice Minimal mock Chainlink aggregator for testing.
///         Owner can set the price; decimals fixed at construction.
contract MockAggregatorV3 is AggregatorV3Interface {
    int256 private _price;
    uint8  private _decimals;
    string private _description;
    uint256 private _updatedAt;
    uint80  private _roundId;

    constructor(string memory desc, uint8 dec, int256 initialPrice) {
        _description = desc;
        _decimals = dec;
        _price = initialPrice;
        _updatedAt = block.timestamp;
        _roundId = 1;
    }

    function setPrice(int256 price) external {
        _price = price;
        _updatedAt = block.timestamp;
        _roundId++;
    }

    function decimals() external view override returns (uint8) {
        return _decimals;
    }

    function description() external view override returns (string memory) {
        return _description;
    }

    function version() external pure override returns (uint256) {
        return 4;
    }

    function getRoundData(uint80) external view override
        returns (uint80 roundId, int256 answer, uint256 startedAt, uint256 updatedAt, uint80 answeredInRound)
    {
        return (_roundId, _price, _updatedAt, _updatedAt, _roundId);
    }

    function latestRoundData() external view override
        returns (uint80 roundId, int256 answer, uint256 startedAt, uint256 updatedAt, uint80 answeredInRound)
    {
        return (_roundId, _price, _updatedAt, _updatedAt, _roundId);
    }
}
