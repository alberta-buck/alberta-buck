// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {IMintVerifier} from "./IMintVerifier.sol";
import {IPoseidonT3}   from "./IPoseidonT3.sol";

/// @title Notes -- BUCK Notes commitment-pool registry.
/// @notice One global pool of Poseidon commitments behind a SNARK-verified
///         mint, plus an on-chain incremental Poseidon Merkle accumulator
///         whose root the spend SNARK opens against.  Off-chain provers
///         reconstruct sibling paths from the `Appended` event stream and
///         submit a `(noteRoot, nullifier)` pair the contract checks against
///         the recent-roots window before paying out.
///
/// @dev    Tree shape: depth 20 (max ~1M notes), leaf hash is the Poseidon-5
///         note commitment from `mint.circom`, internal nodes are
///         `Poseidon(left, right)` over BN254's scalar field.  Empty leaves
///         hash a fixed `ZERO_VALUE` (a domain-separated keccak256 reduced
///         mod r); all level-zero subtree roots are precomputed in the
///         constructor.  Insertion uses the Tornado-style filled-subtrees
///         technique -- exactly `depth` Poseidon hashes per leaf -- and
///         appends the resulting root to a fixed-size ring buffer that
///         `isAcceptedRoot` walks linearly during spend verification.
contract Notes {

    // ---- immutable wiring -------------------------------------------------

    IERC20       public immutable buck;
    IPoseidonT3  public immutable poseidon;

    /// @notice Field modulus of BN254's scalar field, mirrored from the
    ///         circuit so on-chain reductions stay consistent with the SNARK.
    uint256 public constant FIELD_R =
        21888242871839275222246405745257275088548364400416034343698204186575808495617;

    /// @notice Tree depth (max leaves = 2^TREE_DEPTH).  Changing this is a
    ///         hard fork -- it shifts every interior node and invalidates
    ///         in-flight spend proofs.
    uint8   public constant TREE_DEPTH        = 20;

    /// @notice Number of past roots a spend SNARK may reference.  Larger
    ///         windows tolerate longer prover latency at the cost of one
    ///         storage slot per accepted root.
    uint8   public constant ROOT_HISTORY_SIZE = 30;

    /// @notice Field element each empty leaf hashes to.  Domain-separated
    ///         from the Poseidon-5 commitment space so a malicious prover
    ///         cannot fabricate a "real" commitment that collides with an
    ///         empty slot (probability is already 2^-254 with random rho,
    ///         but the domain separation makes the gap formal).
    uint256 public immutable ZERO_VALUE;

    // ---- governance + verifier --------------------------------------------

    address       public governance;
    IMintVerifier public mintVerifier;

    // ---- commitment / nullifier state -------------------------------------

    /// @notice Ordered list of all minted commitments.  `leafIndex` in events
    ///         maps to the position in this array.  Off-chain provers can
    ///         reconstruct the full tree state from this list alone (or
    ///         equivalently from the `Appended` event stream).
    uint256[] public commitments;

    /// @notice Membership view -- O(1) check for a commitment without
    ///         scanning `commitments`.
    mapping(uint256 => bool) public commitmentExists;

    /// @notice Spent nullifier set.  Spend SNARK enforces uniqueness here.
    mapping(uint256 => bool) public nullifiers;

    /// @notice Pure audit scalar -- sum of face values for all outstanding
    ///         notes (incremented on mint, decremented on spend).
    uint256 public noteFaceSum;

    // ---- Merkle tree state ------------------------------------------------

    /// @notice Pre-computed empty-subtree roots, one per level.  `zeros[0]`
    ///         is `ZERO_VALUE`; `zeros[i] = Poseidon(zeros[i-1], zeros[i-1])`.
    uint256[TREE_DEPTH] public zeros;

    /// @notice Most-recently-seen left sibling at each level, used by the
    ///         filled-subtrees insertion scheme.  When the next leaf's
    ///         path bit at level `i` is 0 (left child), we stash our hash
    ///         here; when it's 1, we pair against this slot to climb.
    uint256[TREE_DEPTH] public filledSubtrees;

    /// @notice Index of the next leaf slot to fill (also == number of
    ///         appended commitments).  Capped at `2**TREE_DEPTH`.
    uint32  public nextLeafIndex;

    /// @notice Ring buffer of recent roots.  `roots[currentRootIndex]` is
    ///         the live root.
    uint256[ROOT_HISTORY_SIZE] public roots;
    uint8   public currentRootIndex;

    // ---- events -----------------------------------------------------------

    event GovernanceTransferred(address indexed previous, address indexed next);
    event MintVerifierUpdated(address indexed previous, address indexed next);

    /// @notice One per minted commitment.  `leafIndex` is the Merkle leaf
    ///         position (0-indexed insertion order).
    event Appended(uint256 indexed cm, uint256 indexed leafIndex);

    /// @notice Emitted once per mint call after every commitment in the
    ///         batch has been folded into the tree.  `newRoot` is the live
    ///         root after the batch (also `roots[currentRootIndex]`).
    event Minted(
        address indexed issuer,
        uint256 totalFace,
        uint256 startIndex,
        uint256 count,
        uint256 newRoot
    );

    // ---- constructor / governance -----------------------------------------

    constructor(
        address _buck,
        address _verifier,
        address _poseidon,
        address _governance
    ) {
        require(_buck       != address(0), "buck=0");
        require(_verifier   != address(0), "verifier=0");
        require(_poseidon   != address(0), "poseidon=0");
        require(_governance != address(0), "governance=0");
        buck         = IERC20(_buck);
        mintVerifier = IMintVerifier(_verifier);
        poseidon     = IPoseidonT3(_poseidon);
        governance   = _governance;

        // Domain-separated empty-leaf scalar.  Reducing mod r keeps it in
        // the field; the high bits lost by the reduction don't matter --
        // any field element distinct from the commitment space works.
        uint256 z = uint256(keccak256("AlbertaBuck:Notes:zero")) % FIELD_R;
        ZERO_VALUE = z;

        // Pre-compute the empty-subtree root at every level so insertion
        // never has to hash zeros against zeros at runtime.
        zeros[0]          = z;
        filledSubtrees[0] = z;
        for (uint256 i = 1; i < TREE_DEPTH; i++) {
            uint256 prev = zeros[i - 1];
            uint256 hash = IPoseidonT3(_poseidon).poseidon([prev, prev]);
            zeros[i]          = hash;
            filledSubtrees[i] = hash;
        }
        // Initial root is the empty-tree root.
        roots[0] = IPoseidonT3(_poseidon).poseidon(
            [zeros[TREE_DEPTH - 1], zeros[TREE_DEPTH - 1]]
        );

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

    /// @notice Live Merkle root after all insertions to date.
    function noteRoot() external view returns (uint256) {
        return roots[currentRootIndex];
    }

    /// @notice True iff `root` appears anywhere in the recent-roots window.
    ///         The spend SNARK pins one specific root in its public inputs;
    ///         this check makes that root acceptable for at most
    ///         `ROOT_HISTORY_SIZE` future insertions.
    function isAcceptedRoot(uint256 root) external view returns (bool) {
        if (root == 0) return false;
        uint8  idx = currentRootIndex;
        for (uint256 i = 0; i < ROOT_HISTORY_SIZE; i++) {
            if (roots[idx] == root) return true;
            if (idx == 0) idx = ROOT_HISTORY_SIZE - 1;
            else          idx -= 1;
        }
        return false;
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
    /// this contract, every commitment is appended to both the leaf list
    /// and the Merkle accumulator, the new root is recorded in the ring
    /// buffer, and `noteFaceSum` is incremented by `totalFace`.
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
        uint256 newRoot;
        for (uint256 i = 0; i < cms.length; i++) {
            uint256 cm = cms[i];
            require(cm != 0,                  "Notes: zero commitment");
            require(cm < FIELD_R,             "Notes: cm out of field");
            require(cm != ZERO_VALUE,         "Notes: cm == zero leaf");
            require(!commitmentExists[cm],    "Notes: duplicate commitment");
            commitmentExists[cm] = true;
            commitments.push(cm);
            emit Appended(cm, startIndex + i);
            newRoot = _insert(cm);
        }
        noteFaceSum += totalFace;

        emit Minted(msg.sender, totalFace, startIndex, cms.length, newRoot);
    }

    // ---- internal: incremental tree insertion -----------------------------

    /// @dev Tornado-style filled-subtrees insertion.  Exactly `TREE_DEPTH`
    ///      Poseidon-2 hashes per leaf; updates `filledSubtrees` along the
    ///      left-spine of the inserted leaf, advances `nextLeafIndex`, and
    ///      writes the new root into the ring buffer.  Returns the new root.
    function _insert(uint256 leaf) internal returns (uint256 root) {
        uint32 idx = nextLeafIndex;
        require(idx < uint32(1) << TREE_DEPTH, "Notes: tree full");

        uint256 cur = leaf;
        for (uint8 level = 0; level < TREE_DEPTH; level++) {
            uint256 left;
            uint256 right;
            if ((idx & 1) == 0) {
                // We are a left child: stash for the future right sibling
                // and pair with the empty-subtree root on the right.
                left  = cur;
                right = zeros[level];
                filledSubtrees[level] = cur;
            } else {
                // We are a right child: pair with our previously-stashed
                // left sibling.  filledSubtrees[level] is unchanged --
                // this subtree at this level is now sealed.
                left  = filledSubtrees[level];
                right = cur;
            }
            cur = poseidon.poseidon([left, right]);
            idx >>= 1;
        }

        nextLeafIndex     = nextLeafIndex + 1;
        currentRootIndex  = (currentRootIndex + 1) % ROOT_HISTORY_SIZE;
        roots[currentRootIndex] = cur;
        return cur;
    }
}
