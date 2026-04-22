// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {IMintVerifier}  from "./IMintVerifier.sol";
import {ISpendVerifier} from "./ISpendVerifier.sol";

/// @dev Buck-specific age-preserving transfer.  Notes calls this on spend
///      so the recipient absorbs the pool's average demurrage age rather
///      than paying a spike of settled fee at mint time.
interface IBuckCarrying {
    function transferCarrying(address to, uint256 amount) external returns (bool);
}

/// @title Notes -- BUCK Notes commitment-pool registry (Phase 7-bis batch mint).
/// @notice One global pool of Poseidon commitments behind a SNARK-verified
///         batch mint.  The mint circuit folds N leaves into the rolling
///         Merkle root *in-circuit*, so this contract no longer maintains
///         filled-subtrees, the zeros[] precompute, or per-commitment
///         existence flags -- it just verifies the proof, accepts the
///         SNARK-attested `newRoot`, advances `nextLeafIndex`, and pulls
///         BUCK from the issuer.
///
///         Spend is unchanged from Phase 7: the spender supplies a Groth16
///         proof binding (noteRoot, nullifier, face, recipient, chainId) to
///         a Poseidon opening + Merkle membership under a recent root.
///
/// @dev    Tree shape: depth 20 (max 2^20 = ~1M notes), leaf hash is
///         Poseidon-5(flavor, v, rho, idHash, predicate).  Internal nodes
///         use Poseidon-2 over BN254's scalar field.  Empty leaves hash a
///         fixed `ZERO_VALUE` (a domain-separated keccak256 reduced mod r);
///         the mint circuit hard-codes the same constant.  The contract
///         exposes `ZERO_VALUE`, `TREE_DEPTH`, and the empty-tree root for
///         off-chain wallet bootstrap, but no longer hashes anything itself.
///
///         Stale-state guards (in-flight contention is rollup-style):
///           - `oldRoot == roots[currentRootIndex]`   - prover read live root
///           - `nextLeafIndex == self.nextLeafIndex`  - prover read live size
///         Either guard failing reverts cleanly with no BUCK movement; the
///         loser of a concurrent mint race re-proves against the new state.
contract Notes {

    // ---- immutable wiring -------------------------------------------------

    IERC20 public immutable buck;

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

    /// @notice Field element each empty leaf hashes to.  Hard-coded to match
    ///         the circuit's ZERO_VALUE() literal -- changing one without the
    ///         other breaks the in-circuit Merkle insertion.  Computed as
    ///         `keccak256("AlbertaBuck:Notes:zero") % FIELD_R`.
    uint256 public constant ZERO_VALUE =
        12478158023141672556814566805819277863195393802640872128727997243357085450959;

    /// @notice Empty-tree root: 20 levels of self-paired ZERO_VALUE.  The
    ///         constructor seeds `roots[0]` to this so a freshly-deployed
    ///         contract has a valid live root for the first mint's oldRoot
    ///         check.  Computed off-chain from the same Poseidon-2 chain the
    ///         circuit uses; pinned literal here keeps the constructor pure.
    uint256 public constant EMPTY_ROOT =
        6959478139657271248173638342125700921600510448444968095526832403890386862787;

    // ---- governance + verifier --------------------------------------------

    address        public governance;
    IMintVerifier  public mintVerifier;
    ISpendVerifier public spendVerifier;

    // ---- nullifier + audit state ------------------------------------------

    /// @notice Spent nullifier set.  Spend SNARK enforces uniqueness here.
    mapping(uint256 => bool) public nullifiers;

    /// @notice Pure audit scalar -- sum of face values for all outstanding
    ///         notes (incremented on mint, decremented on spend).
    uint256 public noteFaceSum;

    // ---- Merkle accumulator state -----------------------------------------

    /// @notice Index of the next leaf slot to fill (also == number of
    ///         appended commitments).  Capped at `2**TREE_DEPTH`.
    ///         The mint SNARK reads this as a public input and the contract
    ///         re-asserts equality on every mint to make stale proofs revert.
    uint32  public nextLeafIndex;

    /// @notice Ring buffer of recent roots.  `roots[currentRootIndex]` is
    ///         the live root; spend proofs may pin any root in the window.
    uint256[ROOT_HISTORY_SIZE] public roots;
    uint8   public currentRootIndex;

    // ---- events -----------------------------------------------------------

    event GovernanceTransferred(address indexed previous, address indexed next);
    event MintVerifierUpdated(address indexed previous, address indexed next);
    event SpendVerifierUpdated(address indexed previous, address indexed next);

    /// @notice Emitted when a note is successfully spent.
    event Spent(uint256 indexed nullifier, uint256 face, address indexed recipient);

    /// @notice Emitted once per successful mint.  `cms` calldata carries the
    ///         per-leaf commitments in insertion order; offline provers
    ///         reconstruct the tree by replaying Minted events plus the tx
    ///         calldata cms[].  Indexed fields are kept narrow so the log
    ///         topics are small (issuer + newRoot for filtering).
    event Minted(
        address indexed issuer,
        uint256 totalFace,
        uint256 startIndex,
        uint256 count,
        uint256 indexed newRoot
    );

    // ---- constructor / governance -----------------------------------------

    constructor(
        address _buck,
        address _mintVerifier,
        address _spendVerifier,
        address _governance
    ) {
        require(_buck          != address(0), "buck=0");
        require(_mintVerifier  != address(0), "mintVerifier=0");
        require(_spendVerifier != address(0), "spendVerifier=0");
        require(_governance    != address(0), "governance=0");
        buck          = IERC20(_buck);
        mintVerifier  = IMintVerifier(_mintVerifier);
        spendVerifier = ISpendVerifier(_spendVerifier);
        governance    = _governance;

        // Genesis: empty-tree root in slot 0.  All other ring slots are 0
        // (which `_isAcceptedRoot` rejects, so they cannot be misused as a
        // forged-but-historical root before being overwritten).
        roots[0] = EMPTY_ROOT;

        emit GovernanceTransferred(address(0), _governance);
        emit MintVerifierUpdated(address(0),  _mintVerifier);
        emit SpendVerifierUpdated(address(0), _spendVerifier);
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

    function setSpendVerifier(address next) external {
        require(msg.sender == governance, "not governance");
        require(next != address(0),       "verifier=0");
        emit SpendVerifierUpdated(address(spendVerifier), next);
        spendVerifier = ISpendVerifier(next);
    }

    // ---- views ------------------------------------------------------------

    /// @notice Live Merkle root after all insertions to date.
    function noteRoot() external view returns (uint256) {
        return roots[currentRootIndex];
    }

    /// @notice True iff `root` appears anywhere in the recent-roots window.
    ///         Spend SNARK pins one specific root in its public inputs;
    ///         this check makes that root acceptable for at most
    ///         `ROOT_HISTORY_SIZE` future insertions.
    function isAcceptedRoot(uint256 root) external view returns (bool) {
        return _isAcceptedRoot(root);
    }

    function _isAcceptedRoot(uint256 root) internal view returns (bool) {
        if (root == 0) return false;
        uint8 idx = currentRootIndex;
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
    /// at least `totalFace` BUCK in advance.  The mint SNARK proves:
    ///   - cms[i] = Poseidon-5 opening of the per-leaf witness;
    ///   - sum of v_i = totalFace, each v_i in [0, 2^128);
    ///   - inserting cms[] starting at `nextLeafIndex` against `oldRoot`
    ///     produces `newRoot`.
    ///
    /// Stale-state guards reject proofs whose `oldRoot` or `nextLeafIndex`
    /// no longer matches live state (rollup-style contention model: the
    /// loser's tx reverts cleanly with no BUCK movement and re-proves
    /// against the new state).
    function mint(
        bytes   calldata proof,
        uint256          oldRoot,
        uint256          newRoot,
        uint32           nextLeafIndex_,
        uint256          totalFace,
        uint256[] calldata cms
    ) external {
        require(cms.length > 0,                            "Notes: empty mint");
        require(uint256(nextLeafIndex_) + cms.length
                <= (uint256(1) << TREE_DEPTH),             "Notes: tree full");
        require(oldRoot == roots[currentRootIndex],        "Notes: stale oldRoot");
        require(nextLeafIndex_ == nextLeafIndex,           "Notes: stale nextLeafIndex");
        require(newRoot != 0,                              "Notes: zero newRoot");
        require(newRoot < FIELD_R,                         "Notes: newRoot out of field");

        // Cheap field-bound on every commitment (the SNARK already constrains
        // them via the Poseidon-5 opening, but a malformed cms[] -- e.g. one
        // entry >= FIELD_R -- would still pass the verifier because the
        // public input is reduced before binding into the IC[] term).  Bound
        // them here so off-chain readers get the canonical residue.
        uint256 N = cms.length;
        for (uint256 i = 0; i < N; i++) {
            require(cms[i] < FIELD_R, "Notes: cm out of field");
        }

        require(
            mintVerifier.verifyMint(
                proof, oldRoot, newRoot, uint256(nextLeafIndex_), totalFace, cms
            ),
            "Notes: bad mint proof"
        );

        // Pull face value before mutating tree state so a failed transfer
        // aborts the whole mint with no leaf-index advancement.
        require(
            buck.transferFrom(msg.sender, address(this), totalFace),
            "Notes: transfer failed"
        );

        uint256 startIndex = nextLeafIndex;
        nextLeafIndex      = nextLeafIndex_ + uint32(N);
        currentRootIndex   = (currentRootIndex + 1) % ROOT_HISTORY_SIZE;
        roots[currentRootIndex] = newRoot;
        noteFaceSum       += totalFace;

        emit Minted(msg.sender, totalFace, startIndex, N, newRoot);
    }

    // ---- spend ------------------------------------------------------------

    /// @notice Redeem a note.  Verifies that the spender knows a Poseidon-5
    ///         opening of a commitment included under `noteRoot` (which must
    ///         still be in the recent-roots window), that the nullifier has
    ///         not been burned, and that the Groth16 spend proof binds
    ///         `(noteRoot, nullifier, face, recipient, block.chainid)` in
    ///         its public inputs.  On success the pool transferCarrying's
    ///         `face` BUCK to `recipient`.
    ///
    /// @dev Nullifier and face-sum bookkeeping happen before the external
    ///      transferCarrying call.  If the transfer reverts the whole spend
    ///      reverts, so the nullifier is not "consumed but unpaid".
    function spend(
        bytes   calldata proof,
        uint256          root,
        uint256          nullifier,
        uint256          face,
        address          recipient
    ) external {
        require(recipient != address(0),  "Notes: zero recipient");
        require(face      > 0,            "Notes: zero face");
        require(_isAcceptedRoot(root),    "Notes: unknown root");
        require(!nullifiers[nullifier],   "Notes: already spent");
        require(
            spendVerifier.verifySpend(
                proof, root, nullifier, face, recipient, block.chainid
            ),
            "Notes: bad spend proof"
        );

        nullifiers[nullifier] = true;
        noteFaceSum          -= face;

        require(
            IBuckCarrying(address(buck)).transferCarrying(recipient, face),
            "Notes: transfer failed"
        );

        emit Spent(nullifier, face, recipient);
    }
}
