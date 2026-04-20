// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {IMintVerifier} from "./IMintVerifier.sol";

/// @title Notes — BUCK Notes commitment-pool registry (Phase 1).
/// @notice One global pool of Poseidon commitments behind a SNARK-verified
///         mint.  Phase 1 implements the on-chain note pool (commitment store
///         + nullifier set + audit scalar) and the mint entry-point against a
///         pluggable IMintVerifier.  A-spend / B-spend deposit and the
///         age-preserving transfer primitive arrive in Phases 2-4.
///
/// @dev    The Merkle tree of commitments is built off-chain by provers in
///         Phase 1; the contract stores the leaf list and emits an
///         `Appended(cm, leafIndex)` event per insertion.  When a real
///         verifier is plugged in (Phase 2+), the corresponding incremental
///         Poseidon Merkle accumulator is the natural successor data
///         structure -- the leaf-list view persists either way as the
///         authoritative ordered set of valid commitments.
contract Notes {

    // ---- immutable wiring -------------------------------------------------

    IERC20 public immutable buck;

    // ---- governance + verifier --------------------------------------------

    address       public governance;
    IMintVerifier public mintVerifier;

    // ---- commitment / nullifier state -------------------------------------

    /// @notice Ordered list of all minted commitments.  `leafIndex` in events
    ///         maps to the position in this array.
    uint256[] public commitments;

    /// @notice Membership view -- O(1) check for a commitment without
    ///         scanning `commitments`.  Useful for off-chain provers that
    ///         want to confirm a leaf was published.
    mapping(uint256 => bool) public commitmentExists;

    /// @notice Spent nullifier set.
    mapping(uint256 => bool) public nullifiers;

    /// @notice Pure audit scalar -- sum of face values for all outstanding
    ///         notes (incremented on mint, decremented on deposit in Phase 2+).
    uint256 public noteFaceSum;

    // ---- events -----------------------------------------------------------

    event GovernanceTransferred(address indexed previous, address indexed next);
    event MintVerifierUpdated(address indexed previous, address indexed next);

    /// @notice One per minted commitment.  `leafIndex` is the future Merkle
    ///         leaf position (0-indexed insertion order).
    event Appended(uint256 indexed cm, uint256 indexed leafIndex);

    /// @notice One per successful mint call.  `count` commitments were
    ///         appended starting at `startIndex`; `totalFace` BUCK was pulled
    ///         from `issuer`.
    event Minted(
        address indexed issuer,
        uint256 totalFace,
        uint256 startIndex,
        uint256 count
    );

    // ---- constructor / governance -----------------------------------------

    constructor(address _buck, address _verifier, address _governance) {
        require(_buck       != address(0), "buck=0");
        require(_verifier   != address(0), "verifier=0");
        require(_governance != address(0), "governance=0");
        buck         = IERC20(_buck);
        mintVerifier = IMintVerifier(_verifier);
        governance   = _governance;
        emit GovernanceTransferred(address(0), _governance);
        emit MintVerifierUpdated(address(0), _verifier);
    }

    function transferGovernance(address next) external {
        require(msg.sender == governance, "not governance");
        require(next != address(0),       "governance=0");
        emit GovernanceTransferred(governance, next);
        governance = next;
    }

    function setMintVerifier(address next) external {
        require(msg.sender == governance, "not governance");
        require(next != address(0),       "verifier=0");
        emit MintVerifierUpdated(address(mintVerifier), next);
        mintVerifier = IMintVerifier(next);
    }

    // ---- views ------------------------------------------------------------

    /// @notice Number of commitments minted so far (== future leaf count).
    function commitmentCount() external view returns (uint256) {
        return commitments.length;
    }

    // ---- mint -------------------------------------------------------------

    /// @notice Mint a batch of notes.
    ///
    /// The caller is the issuer.  They must have approved this contract for
    /// at least `totalFace` BUCK in advance (BUCK's identity-bound approve
    /// requires this contract to be flagged `isPublic` in IdentityRegistry,
    /// or the caller to provide an identity-bound approve receipt -- the
    /// pool is "a regular account" per the design doc).
    ///
    /// On success: SNARK proof verifies, BUCK is pulled from the issuer to
    /// this contract, every commitment is recorded, and `noteFaceSum` is
    /// incremented by `totalFace`.
    function mint(
        bytes calldata proof,
        uint256[] calldata cms,
        uint256 totalFace
    ) external {
        require(cms.length > 0, "Notes: empty mint");
        require(
            mintVerifier.verifyMint(proof, totalFace, cms, msg.sender),
            "Notes: bad mint proof"
        );

        // Pull face value first so a failed transfer aborts the whole mint
        // (no commitments leak into the tree on payment failure).
        require(
            buck.transferFrom(msg.sender, address(this), totalFace),
            "Notes: transfer failed"
        );

        uint256 startIndex = commitments.length;
        for (uint256 i = 0; i < cms.length; i++) {
            uint256 cm = cms[i];
            require(cm != 0,                  "Notes: zero commitment");
            require(!commitmentExists[cm],    "Notes: duplicate commitment");
            commitmentExists[cm] = true;
            commitments.push(cm);
            emit Appended(cm, startIndex + i);
        }
        noteFaceSum += totalFace;

        emit Minted(msg.sender, totalFace, startIndex, cms.length);
    }
}
