// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "./BuckCredit.sol";
import "./BuckKController.sol";

/// @title Buck — ERC-20 Token with Credit-Limit Minting
/// @notice Minted against aggregated BUCK_CREDIT values, with on-chain
///         credit-limit enforcement and default insurance premiums.
contract Buck is ERC20 {

    BuckCredit public immutable buckCredit;
    BuckKController public immutable buckK;
    address public immutable insurancePool;

    uint256 constant PRECISION = 1e18;

    // Default insurance premium parameters
    uint256 constant BASE_RATE  = 50;    // 0.50% base (basis points)
    uint256 constant SCALE_RATE = 450;   // 4.50% additional at 100% utilization
    uint256 constant BP = 10000;

    // Per-account stored credit limit
    mapping(address => uint256) public storedLimit;

    event Minted(address indexed account, uint256 amount, uint256 premium,
                 uint256 creditValue, uint256 buckKValue, uint256 newLimit);

    constructor(address _buckCredit, address _buckK, address _insurancePool)
        ERC20("Alberta Buck", "BUCK")
    {
        buckCredit = BuckCredit(_buckCredit);
        buckK = BuckKController(_buckK);
        insurancePool = _insurancePool;
    }

    /// @notice Mint BUCKs against the caller's aggregated BUCK_CREDIT value.
    function mint(uint256 amount) external {
        // 1. Aggregate all BUCK_CREDITs
        uint256 totalCreditValue = buckCredit.totalCurrentValue(msg.sender);

        // 2. Get current BUCK_K stabilization factor
        uint256 currentBuckK = buckK.compute();

        // 3. Compute maximum credit limit
        uint256 maxLimit = totalCreditValue * currentBuckK / PRECISION;

        // 4. Only increase stored limit (never decrease via mint)
        if (maxLimit > storedLimit[msg.sender]) {
            storedLimit[msg.sender] = maxLimit;
        }

        // 5. Check that mint doesn't exceed limit
        uint256 currentBalance = balanceOf(msg.sender);
        require(currentBalance + amount <= storedLimit[msg.sender], "Exceeds credit limit");

        // 6. Compute default insurance premium
        uint256 premium = _computePremium(msg.sender, amount, storedLimit[msg.sender]);

        // 7. Mint: net amount to client, premium to insurance pool
        _mint(msg.sender, amount - premium);
        if (premium > 0) {
            _mint(insurancePool, premium);
        }

        emit Minted(msg.sender, amount, premium,
                     totalCreditValue, currentBuckK, storedLimit[msg.sender]);
    }

    /// @notice Burn BUCKs to reduce outstanding balance.
    function burn(uint256 amount) external {
        _burn(msg.sender, amount);
    }

    /// @dev Premium scales quadratically with utilization.
    function _computePremium(
        address account,
        uint256 mintAmount,
        uint256 limit
    ) internal view returns (uint256) {
        if (limit == 0) return 0;

        uint256 newBalance = balanceOf(account) + mintAmount;
        uint256 utilization = newBalance * PRECISION / limit;

        uint256 utilSq = utilization * utilization / PRECISION;
        uint256 rate = BASE_RATE + utilSq * SCALE_RATE / PRECISION;

        return mintAmount * rate / BP;
    }
}
