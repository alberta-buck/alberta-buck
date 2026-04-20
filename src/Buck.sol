// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

import {BN254} from "./BN254.sol";
import {IdentityRegistry} from "./IdentityRegistry.sol";

/// @title Buck — identity-bound ERC-20 minted against aggregated BUCK_CREDIT value.
/// @notice
///   * mint(amount) — caller must be identity-verified; mint is bounded by
///     BuckCredit aggregated value scaled by BUCK_K, with a quadratic
///     utilization premium routed to the insurance pool.
///   * approve(spender, amount, E_bob, pi) — bilateral identity binding.
///     The plain ERC-20 approve is blocked: the identity-bound overload is
///     mandatory so allowances always carry a Chaum-Pedersen receipt.
///   * transfer / transferFrom — every counterparty pair must be identity-bound
///     (via prior approve receipt) or the recipient must be a public account.
///
/// @dev   Receipt fragments are keccak256 commitments over E_recipient.  They
///        let Buck cheaply re-check identity binding on every transfer without
///        re-running the Chaum-Pedersen NIZK; auditors recompute the hash from
///        their off-chain ciphertext copies.
interface IBuckK {
    function currentBuckK() external view returns (uint256);
}

interface IBuckCredit {
    function totalCurrentValue(address holder) external view returns (uint256);
}

contract Buck is ERC20 {

    IBuckCredit       public immutable buckCredit;
    IBuckK            public immutable buckK;
    IdentityRegistry  public immutable identity;
    address           public immutable insurancePool;

    uint256 internal constant PRECISION = 1e18;

    // ---- premium model -----------------------------------------------------

    /// @notice Base premium rate (basis points) at zero utilization.
    uint256 public constant BASE_RATE  = 50;     // 0.50%
    /// @notice Additional rate at 100% utilization, scaled quadratically.
    uint256 public constant SCALE_RATE = 450;    // +4.50%
    uint256 internal constant BP = 10000;

    // ---- per-account state -------------------------------------------------

    /// @notice Highest-ever credit limit observed for this account.  mint()
    ///         only ratchets it upward; BUCK_K-driven tightening shows up as a
    ///         premium spike rather than retroactive limit reduction.
    mapping(address => uint256) public storedLimit;

    /// @dev Receipt fragment per (from, to): keccak256 over the recipient's
    ///      identity ciphertext (E_to) sent at approve() time.  A non-zero
    ///      fragment proves Alice has performed the Chaum-Pedersen binding
    ///      to `to` at least once.  Cleared by setReceiptDirty().
    mapping(address => mapping(address => bytes32)) internal _receiptFragments;

    // ---- events ------------------------------------------------------------

    event Minted(
        address indexed account,
        uint256 amount,
        uint256 premium,
        uint256 creditValue,
        uint256 buckKValue,
        uint256 newLimit
    );

    event ApproveReceipt(address indexed owner, address indexed spender, bytes32 receiptHash);

    event BuckTransferReceipt(
        address indexed from,
        address indexed to,
        uint256 amount,
        bytes32 fromCipherHash,
        bytes32 toCipherHash
    );

    constructor(
        address _buckCredit,
        address _buckK,
        address _identity,
        address _insurancePool
    ) ERC20("Alberta Buck", "BUCK") {
        require(_buckCredit    != address(0), "buckCredit=0");
        require(_buckK         != address(0), "buckK=0");
        require(_identity      != address(0), "identity=0");
        require(_insurancePool != address(0), "insurancePool=0");
        buckCredit    = IBuckCredit(_buckCredit);
        buckK         = IBuckK(_buckK);
        identity      = IdentityRegistry(_identity);
        insurancePool = _insurancePool;
    }

    // ---- mint / burn -------------------------------------------------------

    /// @notice Mint BUCKs against the caller's aggregated BUCK_CREDIT value.
    function mint(uint256 amount) external {
        require(identity.isVerified(msg.sender), "BUCK: sender not verified");

        uint256 totalCreditValue = buckCredit.totalCurrentValue(msg.sender);
        uint256 currentBuckK     = buckK.currentBuckK();
        uint256 maxLimit         = totalCreditValue * currentBuckK / PRECISION;

        if (maxLimit > storedLimit[msg.sender]) {
            storedLimit[msg.sender] = maxLimit;
        }

        uint256 limit = storedLimit[msg.sender];
        require(balanceOf(msg.sender) + amount <= limit, "BUCK: exceeds credit limit");

        uint256 premium = _computePremium(msg.sender, amount, limit);
        _mint(msg.sender, amount - premium);
        if (premium > 0) {
            _mint(insurancePool, premium);
        }

        emit Minted(msg.sender, amount, premium, totalCreditValue, currentBuckK, limit);
    }

    function burn(uint256 amount) external {
        _burn(msg.sender, amount);
    }

    // ---- identity-bound approve --------------------------------------------

    /// @notice Identity-bound approve.  Sets ERC-20 allowance and stores a
    ///         Chaum-Pedersen receipt that `E_bob` re-encrypts the caller's
    ///         identity point M under spender's identity public key.
    function approve(
        address spender,
        uint256 amount,
        IdentityRegistry.ElGamalCT calldata E_bob,
        IdentityRegistry.CPProof calldata pi_CP
    ) external returns (bool) {
        require(identity.isVerified(msg.sender), "BUCK: sender not verified");
        require(
            identity.isVerified(spender) || identity.isPublic(spender),
            "BUCK: spender not verified"
        );

        bytes32 receipt;
        if (identity.isPublic(spender)) {
            receipt = _publicReceiptHash(spender);
        } else {
            require(
                identity.verifyApprove(msg.sender, spender, E_bob, pi_CP),
                "BUCK: bad CP proof"
            );
            receipt = _ciphertextHash(E_bob);
        }
        _receiptFragments[msg.sender][spender] = receipt;
        emit ApproveReceipt(msg.sender, spender, receipt);

        _approve(msg.sender, spender, amount);
        return true;
    }

    /// @notice Block the parameterless ERC-20 approve.  Identity-bound
    ///         approve is mandatory so every allowance carries a CP receipt.
    function approve(address, uint256) public pure override returns (bool) {
        revert("BUCK: use identity-bound approve");
    }

    function receiptFragment(address from, address to) external view returns (bytes32) {
        return _receiptFragments[from][to];
    }

    // ---- identity-checked transfers ----------------------------------------

    function transfer(address to, uint256 amount) public override returns (bool) {
        _identityCheckedTransfer(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount)
        public override returns (bool)
    {
        _spendAllowance(from, msg.sender, amount);
        _identityCheckedTransfer(from, to, amount);
        return true;
    }

    function _identityCheckedTransfer(address from, address to, uint256 amount) internal {
        require(identity.isVerified(from), "BUCK: sender not verified");
        require(
            identity.isVerified(to) || identity.isPublic(to),
            "BUCK: recipient not verified"
        );

        bytes32 toHash;
        if (identity.isPublic(to)) {
            toHash = _publicReceiptHash(to);
        } else {
            toHash = _receiptFragments[from][to];
            require(toHash != bytes32(0), "BUCK: missing identity receipt");
        }

        bytes32 fromHash = identity.isPublic(from)
            ? _publicReceiptHash(from)
            : _receiptFragments[to][from];

        _transfer(from, to, amount);
        emit BuckTransferReceipt(from, to, amount, fromHash, toHash);
    }

    // ---- helpers -----------------------------------------------------------

    function _ciphertextHash(IdentityRegistry.ElGamalCT calldata E)
        internal pure returns (bytes32)
    {
        return keccak256(abi.encode(E.R.X, E.R.Y, E.C.X, E.C.Y));
    }

    function _publicReceiptHash(address account) internal view returns (bytes32) {
        IdentityRegistry.ElGamalCT memory E = identity.ciphertextOf(account);
        return keccak256(abi.encode(E.R.X, E.R.Y, E.C.X, E.C.Y, "PUBLIC"));
    }

    /// @dev Premium = mintAmount * (BASE_RATE + util^2 * SCALE_RATE) / BP.
    function _computePremium(
        address account,
        uint256 mintAmount,
        uint256 limit
    ) internal view returns (uint256) {
        if (limit == 0) return 0;
        uint256 newBalance  = balanceOf(account) + mintAmount;
        uint256 utilization = newBalance * PRECISION / limit;
        uint256 utilSq      = utilization * utilization / PRECISION;
        uint256 rate        = BASE_RATE + utilSq * SCALE_RATE / PRECISION;
        return mintAmount * rate / BP;
    }
}
