// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// A rebalance policy the tests drive by hand (rate, recipient, urgency), for the basket machine's
/// upkeep facet (BasketUpkeepFacet).
contract ControlledPairIntent {
    address public basket;
    function count() external view virtual returns (uint256) { return 3; }
    uint256 public constant maxRate = 0.693147e18;
    uint256 public rate = 1e18;
    uint256 public impacts;
    uint64 public lastExecutionAt;
    uint256 public onlyRecipient = type(uint256).max;
    constructor(address basket_) { basket = basket_; }
    function signals() external view returns (address) { return address(this); }
    function isDue() external pure returns (bool) { return true; }
    function observe() external {}
    function setRate(uint256 r) external { rate = r; }
    function setRecipient(uint256 j) external { onlyRecipient = j; }
    function desire(uint256, uint256 j) public view returns (uint256) {
        return onlyRecipient == type(uint256).max || onlyRecipient == j ? rate : 0;
    }
    function allocationRate(uint256 i, bool sell, uint256, uint256[] calldata weights)
        external view returns (uint256 result)
    {
        uint256 total;
        for (uint256 j; j < weights.length; ++j) {
            if (j == i) continue;
            result += weights[j] * (sell ? desire(i, j) : desire(j, i));
            total += weights[j];
        }
        return total == 0 ? 0 : result / total;
    }
    // The share of a gap to move: any rule will do for the facet's tests.
    function share(uint256, uint256 r) public view returns (uint256) {
        uint256 elapsed = block.timestamp - lastExecutionAt;
        if (elapsed > 1 days) elapsed = 1 days;
        uint256 u = r * elapsed / 1 days;
        return u * 1e18 / (1e18 + u);
    }
    function pairShare(uint256 i, uint256 r) external view returns (uint256) { return share(i, r); }
    uint256[] public asked;
    function recordRates(uint256[] calldata rates) external { require(msg.sender == basket); asked = rates; }
    function recordImpact(uint256, int24, int24) external { require(msg.sender == basket); ++impacts; }
    function recordExecution(uint256 donor) external {
        require(msg.sender == basket); lastExecutionAt = uint64(block.timestamp); acted = donor;
    }
    // Urgency, as the facet sees it: a mask the test sets, and the gaps it was last told.
    uint256 public patience;
    uint256 public urgentMask;
    uint256 public acted = type(uint256).max; // the donor of the last transfer
    int256[] public reported;
    function setUrgent(uint256 patience_, uint256 mask) external { patience = patience_; urgentMask = mask; }
    function urgent(int256[] calldata) external view returns (uint256) { return urgentMask; }
    function recordGaps(int256[] calldata gaps) external { require(msg.sender == basket); reported = gaps; }
}

/// The controlled policy over however many constituents its basket has: the shared fixtures' upkeep
/// (the basket machine's facet; the earlier components facet is retired, 2026-11-20). At rate 0 it
/// asks for no rebalancing, so a response only pairs what the basket holds.
contract BasketIntent is ControlledPairIntent {
    constructor(address basket_) ControlledPairIntent(basket_) {}
    function count() external view override returns (uint256) {
        (bool ok, bytes memory r) = basket.staticcall(abi.encodeWithSignature("constituentsLength()"));
        return ok && r.length == 32 ? abi.decode(r, (uint256)) : 0;
    }
}
