// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

import {Buck} from "../Buck.sol";
import {toBuckQtySigned, toBuckSeconds} from "../BuckTypes.sol";

/// @title BuckWithBasketHooks -- Buck plus the pro-rata baskets' mint/burn
///        hooks.  SIMULATION AND TEST ONLY: never deploy it.
///
/// @notice Production Buck carries no basket code: a BuckBasket is an
///         ordinary credit holder -- a self-issued MARKED BuckCredit, its
///         debt a lien, relief like anyone's (alberta-buck-ethereum.org,
///         "Two Kinds of BUCK").
///         The pro-rata baskets that came before it (BuckBasketProRata,
///         BuckBasketOps, BuckBasketFence and the legacy BuckBasket) mint
///         and burn through these hooks instead, so the sims deploy this
///         subclass to keep their baselines and judged results reproducible.
///
///         The hooks issue BUCK against no lien, so Buck's supply identity
///         gains a term -- the hooks' net issuance, `basketIssued`:
///
///             totalSupply = sum(liens) + basketIssued + reliefRealized - feesRealized
///
///         The fund accrues on it, and on the liens.  A basket that burns BUCK
///         it bought (a desk's buy-back) retires more than it issued; then the
///         fund counts the liens alone, and the demurrage that retirement
///         owes is the basket's (`basketRelief` < 0), never the accounts'.
///         The basket's relief is recorded, not paid.
contract BuckWithBasketHooks is Buck {

    /// @notice The one contract that may call the hooks; set once.
    address public basket;
    /// @notice The hooks' net issuance: minted, less what burns retired.
    int256  public basketIssued;
    /// @dev    basketIssued integrated over time (BUCK-seconds, signed).
    int256  internal _basketIssuanceSeconds;
    uint64  internal _basketFoldedAt;

    constructor(address _buckCredit, address _buckK, address _identity, address _insurancePool)
        Buck(_buckCredit, _buckK, _identity, _insurancePool)
    {
        _basketFoldedAt = uint64(block.timestamp);
    }

    /// @notice One-shot wiring of the basket, by the insurance pool.
    function setBasket(address _basket) external {
        require(msg.sender == insurancePool, "BUCK: not insurancePool");
        require(basket == address(0), "BUCK: basket already set");
        require(_basket != address(0), "BUCK: basket=0");
        basket = _basket;
    }

    /// @notice Mint `amount` BUCK to `to`, against no lien.
    function mintFromBasket(address to, uint256 amount) external nonReentrant {
        require(msg.sender == basket && basket != address(0), "BUCK: not basket");
        if (amount == 0) return;
        _accrueJubilee();
        _foldBasket();
        basketIssued += int256(amount);
        _crystallize(to);
        // Fresh BUCK: no age rides in.  Credited like any receipt, so BUCK
        // minted to an account below zero repay its lien (and cross it).
        _credit(to, amount, 0);
        emit Transfer(address(0), to, amount);
    }

    /// @notice Burn `amount` BUCK from the basket's balance.  The burned BUCK
    ///         carry their share of the basket's age out with them, and pay
    ///         its fee: aged BUCK retire `amount - fee` of the issuance, as
    ///         aged BUCK repay `value - fee` of a lien.
    function burnFromBasket(uint256 amount) external nonReentrant {
        require(msg.sender == basket && basket != address(0), "BUCK: not basket");
        if (amount == 0) return;
        _accrueJubilee();
        _crystallize(msg.sender);
        AccountState memory s = _state[msg.sender];
        int256 raw = s.balance.asInt();
        require(raw > 0 && uint256(raw) >= amount, "BUCK: insufficient");
        uint256 bs    = s.buckSeconds.asUint();
        uint256 share = Math.mulDiv(bs, amount, uint256(raw));
        uint256 fee   = Math.mulDiv(share, BASE_RATE_PER_SEC, SCALE);
        if (fee > amount) fee = amount;
        s.buckSeconds = toBuckSeconds(bs - share);
        s.balance     = toBuckQtySigned(raw - int256(amount));
        _state[msg.sender] = s;
        _totalSupply -= amount;      // Carrying: the balance stays >= 0
        if (fee != 0) {
            feesRealized += fee;
            emit FeeRealized(msg.sender, fee);
        }
        _foldBasket();
        basketIssued -= int256(amount - fee);
        emit Transfer(msg.sender, address(0), amount);
    }

    /// @notice The relief the basket has earned on its net issuance (+), or
    ///         the demurrage it owes on BUCK it retired beyond it (-).
    function basketRelief() external view returns (int256) {
        int256 s = _basketIssuanceSeconds
                 + basketIssued * int256(block.timestamp - uint256(_basketFoldedAt));
        return s * int256(BASE_RATE_PER_SEC) / int256(SCALE);
    }

    /// @notice The liens, plus the hooks' net issuance where it is positive.
    function totalIssued() public view override returns (uint256) {
        int256 base = int256(_totalSupply) - int256(reliefRealized) + int256(feesRealized);
        if (basketIssued < 0) base -= basketIssued;
        return base > 0 ? uint256(base) : 0;
    }

    function _foldBasket() internal {
        uint256 elapsed = block.timestamp - uint256(_basketFoldedAt);
        if (elapsed != 0) {
            _basketIssuanceSeconds += basketIssued * int256(elapsed);
            _basketFoldedAt = uint64(block.timestamp);
        }
    }
}
