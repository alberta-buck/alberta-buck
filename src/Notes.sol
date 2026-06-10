// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {IMintVerifier}                 from "./IMintVerifier.sol";
import {IMintVerifierA2}               from "./IMintVerifierA2.sol";
import {ISpendVerifier}                from "./ISpendVerifier.sol";
import {IIdentityMembershipVerifier}   from "./IIdentityMembershipVerifier.sol";
import {INoteBindingVerifier}          from "./INoteBindingVerifier.sol";
import {IdentityRegistry}              from "./IdentityRegistry.sol";
import {BN254}                         from "./BN254.sol";

// (Buck dispatches to the Carrying transfer path automatically when the
//  sender is registered with isCarrying = true in IdentityRegistry; the
//  Notes pool is registered as a Carrying service contract at deploy/bind
//  time.  No special Buck interface is needed -- Notes calls IERC20.transfer.)

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

    /// @notice Per-leaf issuer-mode labels, mirrored from the mint circuit's
    ///         `issuerMode` output (circuits/mint_batch.circom).  A leaf is
    ///         PUBLIC-mode (A1/B1 -> the issuer is a registered public Identity
    ///         bound by a Schnorr signature) or PRIVATE-mode (A2 -> the issuer
    ///         is a registered private Identity bound by an A2 re-encryption
    ///         proof).  Because a bearer (B1) leaf projects to PUBLIC in the
    ///         circuit, a private issuer cannot route a bearer note through the
    ///         PUBLIC-mode mint path -- the "bearer => public issuer" invariant.
    uint256 public constant MODE_PUBLIC  = 1;
    uint256 public constant MODE_PRIVATE = 2;

    // ---- governance + verifier --------------------------------------------

    address        public governance;
    IMintVerifier  public mintVerifier;

    /// @notice Private-issuer (A2) mint verifier -- the mint_batch_a2 circuit
    ///         family (src/MintBatchA2N*Groth16Verifier via MintVerifierA2Adapter).
    ///         Distinct from `mintVerifier` because A2 has a different public
    ///         arity (5N+4: per-leaf eIss exposed as outputs) and is used only by
    ///         the PRIVATE-mode mint path, where it ties each committed leaf to
    ///         its re-encryption binding's eIss (the collusion-resistant leaf-tie;
    ///         see alberta-buck-notes-decryptability.org, The Required Mint SNARK
    ///         Signal).  Optional at construction; governance wires it via
    ///         setA2MintVerifier before any A2 mint.
    IMintVerifierA2 public a2MintVerifier;

    ISpendVerifier public spendVerifier;

    /// @dev Storage slot retained as a placeholder.  This was
    ///      `ISpendAVerifier public spendAVerifier` -- the legacy account-pinned
    ///      A-spend path (spend_a.circom + spendACP), removed in the Identity-M
    ///      consolidation.  Kept (address-sized, like the verifier it replaced)
    ///      so the storage layout below -- identityRegistry, noteFaceSum, the
    ///      roots ring, ... -- is unperturbed.  Do not reuse without a migration.
    address private _removedSpendAVerifier;

    /// @notice Identity registry consulted by the Identity-M-bound spends for the
    ///         deposit-coupling / depositor-binding sigmas and the membership root.
    IdentityRegistry public identityRegistry;

    /// @notice Identity membership verifier (Phase 9 -- identity-axis).
    ///         Verifies a Groth16 proof that the counterparty identity point
    ///         is a member of the registry-Identity accumulator under
    ///         identityRegistry.identityRoot().  Required for the full
    ///         identity-binding spend path (A2 deposit coupling + B1
    ///         depositor binding).  Optional at construction; governance
    ///         wires it via setIdentityMembershipVerifier.  A zero address
    ///         means identity membership checks are skipped (backward-compat
    ///         during migration).
    IIdentityMembershipVerifier public identityMembershipVerifier;

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

    /// @notice Note<->eEnc re-encryption-tie verifier (see
    ///         INoteBindingVerifier; relation: circuits/note_binding.circom,
    ///         verified by NoteBindingGroth16Verifier behind
    ///         NoteBindingVerifierAdapter).  Binds the deposit-coupling
    ///         ciphertext `eEnc` to the SPECIFIC addressed (A1/A2) note being
    ///         spent, so a depositor cannot substitute a self-addressed
    ///         ciphertext for the note's committed one.  This closes the two
    ///         gaps the flavor-agnostic spend proof leaves open:
    ///           * addressed-binding — "only the recipient Identity M_rec can
    ///             spend an A1/A2 note"; and
    ///           * A2 collusion — "an un-nameable note is un-spendable".
    ///         Optional at construction; governance wires it via
    ///         setNoteBindingVerifier.  A zero address (or an empty per-spend
    ///         proof) SKIPS the tie (backward-compat) — and, crucially, while
    ///         skipped those two guarantees are NOT enforced on-chain.  Wired
    ///         only into the addressed spends; B1 (bearer) needs no tie (the
    ///         depositor binding names the depositor directly).
    /// @dev    Appended at the END of storage so the existing slot positions
    ///         (nullifiers, noteFaceSum, nextLeafIndex, roots, ...) that tests
    ///         reach via `vm.store` stay unperturbed.
    INoteBindingVerifier public noteBindingVerifier;

    // ---- events -----------------------------------------------------------

    event GovernanceTransferred(address indexed previous, address indexed next);
    event MintVerifierUpdated(address indexed previous, address indexed next);
    event A2MintVerifierUpdated(address indexed previous, address indexed next);
    event SpendVerifierUpdated(address indexed previous, address indexed next);
    event IdentityRegistryUpdated(address indexed previous, address indexed next);
    event IdentityMembershipVerifierUpdated(address indexed previous, address indexed next);
    event NoteBindingVerifierUpdated(address indexed previous, address indexed next);

    /// @notice Emitted on an identity-M-bound (unilateral) A2 deposit.  Publishes
    ///         the committed point `P_I = (piX, piY)` that the deposit-coupling
    ///         sigma constrained and the membership proof certified as a member of
    ///         the registry-Identity accumulator — so an auditor can re-check the
    ///         binding against the on-chain identityRoot.  Reveals no Identity:
    ///         `P_I` is the perfectly-hiding blind `M_iss + b·H`.
    event SpentCoupledA2(
        uint256 indexed nullifier,
        uint256 face,
        address indexed recipient,
        uint256 piX,
        uint256 piY
    );

    /// @notice Emitted on an identity-M-bound A1 deposit (addressed, public
    ///         issuer).  Same shape as SpentCoupledA2; here `P_I = (piX, piY)`
    ///         commits the *recipient* identity M_rec the membership certified.
    event SpentCoupledA1(
        uint256 indexed nullifier,
        uint256 face,
        address indexed recipient,
        uint256 piX,
        uint256 piY
    );

    /// @notice Emitted on an identity-M-bound B1 deposit (bearer, public issuer):
    ///         the depositor's binding-certified membership of M_dep, plus the
    ///         `eDepForIss` the issuer alone decrypts to name the depositor.  The
    ///         membership-bound counterpart of `SpentB`.
    event SpentCoupledB1(
        uint256 indexed nullifier,
        uint256 face,
        address indexed recipient,
        address indexed issuer,
        IdentityRegistry.ElGamalCT eDepForIss
    );

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

    /// @notice Emitted when a mint carries a verified public-issuer Schnorr
    ///         binding: the issuer's decrypted Identity is provably bound to
    ///         every leaf in the batch (Notes mutual-decryptability, Phase 1).
    event IssuerBound(address indexed issuer, uint256 indexed newRoot);

    /// @notice Emitted when a mint anchors `count` verified A2 (private-issuer)
    ///         recipient-blinded re-encryption bindings (Notes
    ///         mutual-decryptability, Phase 2; see verifyIssuerReenc).
    event IssuerReencBound(address indexed issuer, uint256 indexed newRoot, uint256 count);

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

    /// @notice Wire (or rotate) the A2 (private-issuer) mint verifier.  Required
    ///         before any PRIVATE-mode mint; passing `address(0)` disables A2
    ///         mints (the next private-mode mint reverts on the verifier check).
    function setA2MintVerifier(address next) external {
        require(msg.sender == governance, "not governance");
        emit A2MintVerifierUpdated(address(a2MintVerifier), next);
        a2MintVerifier = IMintVerifierA2(next);
    }

    function setSpendVerifier(address next) external {
        require(msg.sender == governance, "not governance");
        require(next != address(0),       "verifier=0");
        emit SpendVerifierUpdated(address(spendVerifier), next);
        spendVerifier = ISpendVerifier(next);
    }

    /// @notice Wire (or rotate) the identity registry consulted by the
    ///         Identity-M-bound spends (deposit-coupling / depositor-binding and
    ///         the membership root).  Passing `address(0)` disables those spends.
    function setIdentityRegistry(address next) external {
        require(msg.sender == governance, "not governance");
        emit IdentityRegistryUpdated(address(identityRegistry), next);
        identityRegistry = IdentityRegistry(next);
    }

    /// @notice Wire (or rotate) the identity membership verifier consulted
    ///         by every spend path when identityRegistry.identityRoot() is
    ///         non-zero.  Passing `address(0)` disables identity membership
    ///         checks (backward-compat during migration).  When the G1-tie
    ///         circuit lands, governance swaps the real verifier in.
    function setIdentityMembershipVerifier(address next) external {
        require(msg.sender == governance, "not governance");
        emit IdentityMembershipVerifierUpdated(
            address(identityMembershipVerifier), next);
        identityMembershipVerifier = IIdentityMembershipVerifier(next);
    }

    /// @notice Wire (or rotate) the note<->eEnc re-encryption-tie verifier
    ///         (INoteBindingVerifier) consulted by the addressed (A1/A2) spends.
    ///         Passing `address(0)` disables the tie (backward-compat skip) — and
    ///         while disabled the addressed-binding / A2-collusion guarantees are
    ///         NOT enforced.  The production verifier is the generated
    ///         NoteBindingGroth16Verifier behind NoteBindingVerifierAdapter.
    function setNoteBindingVerifier(address next) external {
        require(msg.sender == governance, "not governance");
        emit NoteBindingVerifierUpdated(address(noteBindingVerifier), next);
        noteBindingVerifier = INoteBindingVerifier(next);
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
    /// Minting is *gated-only*: every batch binds a nameable issuer Identity.
    /// A PUBLIC batch (A1/B1) routes through the (issuerMode + SchnorrProof)
    /// overload from a registered public issuer; a PRIVATE batch (A2) through the
    /// (issuerMode + A2Binding[]) overload from a registered private issuer.
    /// There is no unbound mint path -- an unnameable note cannot be created.

    /// @notice One A2 (addressed, private-issuer) leaf's recipient-blinded
    ///         re-encryption binding: the leaf ciphertext `eIss` (E_iss-for-rec)
    ///         plus its proof.  See IdentityRegistry.verifyIssuerReenc and
    ///         alberta_buck.wallet.issuer_reenc.
    struct A2Binding {
        IdentityRegistry.ElGamalCT        eIss;
        IdentityRegistry.IssuerReencProof proof;
    }

    /// @notice Mint a PUBLIC-issuer batch with per-leaf issuerMode gating
    ///         (Notes mutual-decryptability; The Required Mint SNARK Signal).
    ///         Every leaf must be PUBLIC-mode (issuerMode[i] == MODE_PUBLIC):
    ///         `msg.sender` must be a registered PUBLIC Identity and `issuerSig`
    ///         must bind their decrypted Identity to keccak256(cms).  Because a
    ///         bearer (B1) leaf projects to PUBLIC in the mint circuit, a bearer
    ///         note is minted through this path -- and a non-public issuer is
    ///         rejected here, enforcing "bearer => public issuer".
    ///
    /// @dev    The mint SNARK (mint_batch) binds `issuerMode[]` to each committed
    ///         flavor, so the verifier rejects any batch whose attested modes do
    ///         not match the `issuerMode[]` passed here; the contract then gates
    ///         on it (every leaf PUBLIC, registered public `msg.sender`, Schnorr).
    function mint(
        bytes   calldata proof,
        uint256          oldRoot,
        uint256          newRoot,
        uint32           nextLeafIndex_,
        uint256          totalFace,
        uint256[] calldata cms,
        uint256[] calldata issuerMode,
        IdentityRegistry.SchnorrProof calldata issuerSig
    ) external {
        _verifyMintOrRevert(proof, issuerMode, oldRoot, newRoot, nextLeafIndex_, totalFace, cms);
        require(address(identityRegistry) != address(0),
                "Notes: identity registry not set");
        (uint256 nPublic, uint256 nPrivate) = _classifyModes(issuerMode, cms.length);
        require(nPrivate == 0, "Notes: private leaf needs A2 overload");
        require(nPublic  > 0,  "Notes: no public leaves");
        require(identityRegistry.isPublicIdentity(msg.sender),
                "Notes: public-mode leaf needs public issuer");
        require(
            identityRegistry.verifyIssuerSchnorr(
                msg.sender, keccak256(abi.encodePacked(cms)), issuerSig),
            "Notes: bad issuer binding"
        );
        uint256 startIndex =
            _advanceAndPull(newRoot, nextLeafIndex_, totalFace, cms.length);
        emit Minted(msg.sender, totalFace, startIndex, cms.length, newRoot);
        emit IssuerBound(msg.sender, newRoot);
    }

    /// @notice Mint a PRIVATE-issuer (A2) batch with the collusion-resistant
    ///         per-leaf eIss leaf-tie.  Every leaf must be PRIVATE-mode
    ///         (issuerMode[i] == MODE_PRIVATE); `msg.sender` must be a registered
    ///         PRIVATE Identity and supply exactly one A2 re-encryption binding
    ///         per committed leaf.  The proof is verified by the *A2* mint
    ///         circuit (mint_batch_a2), which constrains every leaf to flavor ==
    ///         A2 and exposes each leaf's committed `eIss` as a public output; we
    ///         pass the bindings' `eIss` as that public input, so a Groth16
    ///         accept proves each binding's `eIss` *is* the committed leaf's --
    ///         the leaf-tie that closes the floating-/missing-binding collusion
    ///         sub-cases (per-batch count alone could not).
    ///
    /// @dev    What the leaf-tie does and does NOT close.  It binds each
    ///         committed leaf to a *verified* re-encryption of the issuer's
    ///         registered Identity, so no leaf is left without a binding and no
    ///         binding can float to a different leaf.  It does NOT force the
    ///         binding's `pk_rec` to be the addressed recipient's registered key
    ///         (verifyIssuerReenc binds `eIss` to the key committed in the
    ///         proof's `Q`, which the issuer chooses); a colluding issuer+
    ///         recipient can still encrypt `eIss` under a throwaway key, leaving
    ///         the issuer un-nameable while the note stays spendable.  Closing
    ///         that residual hole needs the eNote<->eIss recipient-key coupling
    ///         at mint -- see alberta-buck-notes-decryptability.org ("The A2
    ///         recipient-key coupling gap").  issuerMode here is a caller-facing
    ///         assertion (the A2 circuit independently constrains flavor == A2).
    function mint(
        bytes   calldata proof,
        uint256          oldRoot,
        uint256          newRoot,
        uint32           nextLeafIndex_,
        uint256          totalFace,
        uint256[] calldata cms,
        uint256[] calldata issuerMode,
        A2Binding[] calldata a2Bindings
    ) external {
        require(address(identityRegistry) != address(0),
                "Notes: identity registry not set");
        (uint256 nPublic, uint256 nPrivate) = _classifyModes(issuerMode, cms.length);
        require(nPublic  == 0, "Notes: public leaf needs Schnorr overload");
        require(nPrivate  > 0, "Notes: no private leaves");
        require(!identityRegistry.isPublicIdentity(msg.sender),
                "Notes: private-mode leaf needs private issuer");
        require(a2Bindings.length == nPrivate, "Notes: A2 binding count");

        // Leaf-tie: the bindings' eIss are the A2 circuit's public inputs, so a
        // valid proof ties each committed leaf to the binding answering for it.
        uint256[4][] memory eIss = new uint256[4][](a2Bindings.length);
        for (uint256 i = 0; i < a2Bindings.length; i++) {
            eIss[i][0] = a2Bindings[i].eIss.R.X;
            eIss[i][1] = a2Bindings[i].eIss.R.Y;
            eIss[i][2] = a2Bindings[i].eIss.C.X;
            eIss[i][3] = a2Bindings[i].eIss.C.Y;
        }
        _verifyA2MintOrRevert(proof, eIss, oldRoot, newRoot, nextLeafIndex_, totalFace, cms);

        // Each committed eIss must carry a valid re-encryption of the issuer's
        // registered Identity (soundness of the binding itself).
        for (uint256 i = 0; i < a2Bindings.length; i++) {
            require(
                identityRegistry.verifyIssuerReenc(
                    msg.sender, a2Bindings[i].eIss, a2Bindings[i].proof),
                "Notes: bad A2 binding"
            );
        }
        uint256 startIndex =
            _advanceAndPull(newRoot, nextLeafIndex_, totalFace, cms.length);
        emit Minted(msg.sender, totalFace, startIndex, cms.length, newRoot);
        emit IssuerReencBound(msg.sender, newRoot, nPrivate);
    }

    /// @dev Shared mint pre-flight: stale-state guards, per-commitment field
    ///      bound, and the Groth16 mint-proof check.  View-only -- any revert
    ///      here aborts before BUCK moves or tree state advances.
    ///
    ///      The field bound matters even though the SNARK constrains each cm[i]
    ///      via its Poseidon-5 opening: a malformed cms[] entry >= FIELD_R would
    ///      still pass the verifier (the public input is reduced before binding
    ///      into the IC[] term), so we bound it here for canonical off-chain
    ///      reads.
    function _verifyMintOrRevert(
        bytes   calldata proof,
        uint256[] memory  issuerMode,
        uint256          oldRoot,
        uint256          newRoot,
        uint32           nextLeafIndex_,
        uint256          totalFace,
        uint256[] calldata cms
    ) internal {
        require(cms.length > 0,                            "Notes: empty mint");
        require(issuerMode.length == cms.length,           "Notes: issuerMode/cms length");
        require(uint256(nextLeafIndex_) + cms.length
                <= (uint256(1) << TREE_DEPTH),             "Notes: tree full");
        require(oldRoot == roots[currentRootIndex],        "Notes: stale oldRoot");
        require(nextLeafIndex_ == nextLeafIndex,           "Notes: stale nextLeafIndex");
        require(newRoot != 0,                              "Notes: zero newRoot");
        require(newRoot < FIELD_R,                         "Notes: newRoot out of field");

        uint256 N = cms.length;
        for (uint256 i = 0; i < N; i++) {
            require(cms[i] < FIELD_R, "Notes: cm out of field");
        }

        require(
            mintVerifier.verifyMint(
                proof, issuerMode, oldRoot, newRoot, uint256(nextLeafIndex_), totalFace, cms
            ),
            "Notes: bad mint proof"
        );
    }

    /// @dev A2 mint pre-flight: the same stale-state guards as
    ///      `_verifyMintOrRevert`, plus the per-leaf `eIss` canonical bound, then
    ///      the A2 Groth16 check.  `eIss` is the per-leaf E_iss-for-rec the
    ///      bindings carry; passing it as the A2 circuit's public input makes the
    ///      verifier's accept the leaf-tie (the circuit exposes the *committed*
    ///      leaf's eIss, so equality with what we pass is a constraint, not a
    ///      contract-side compare).  View-only -- reverts here abort before BUCK
    ///      moves or tree state advances.
    function _verifyA2MintOrRevert(
        bytes   calldata proof,
        uint256[4][] memory eIss,
        uint256          oldRoot,
        uint256          newRoot,
        uint32           nextLeafIndex_,
        uint256          totalFace,
        uint256[] calldata cms
    ) internal {
        require(cms.length > 0,                            "Notes: empty mint");
        require(eIss.length == cms.length,                 "Notes: eIss/cms length");
        require(address(a2MintVerifier) != address(0),     "Notes: a2 verifier not set");
        require(uint256(nextLeafIndex_) + cms.length
                <= (uint256(1) << TREE_DEPTH),             "Notes: tree full");
        require(oldRoot == roots[currentRootIndex],        "Notes: stale oldRoot");
        require(nextLeafIndex_ == nextLeafIndex,           "Notes: stale nextLeafIndex");
        require(newRoot != 0,                              "Notes: zero newRoot");
        require(newRoot < FIELD_R,                         "Notes: newRoot out of field");

        uint256 N = cms.length;
        for (uint256 i = 0; i < N; i++) {
            require(cms[i] < FIELD_R, "Notes: cm out of field");
            // eIss is exposed by the SNARK reduced mod FIELD_R while the binding
            // uses the full base-field point; require the canonical representative
            // so the leaf-tie and the binding agree on one value.  (The snarkjs
            // verifier rejects public inputs >= FIELD_R regardless; honest ElGamal
            // coordinates are < FIELD_R with overwhelming probability.)
            require(eIss[i][0] < FIELD_R && eIss[i][1] < FIELD_R
                 && eIss[i][2] < FIELD_R && eIss[i][3] < FIELD_R,
                    "Notes: eIss out of field");
        }

        require(
            a2MintVerifier.verifyMint(
                proof, eIss, oldRoot, newRoot, uint256(nextLeafIndex_), totalFace, cms
            ),
            "Notes: bad mint proof"
        );
    }

    /// @dev Shared mint settlement: pull `totalFace` BUCK from the issuer before
    ///      mutating tree state (a failed transfer aborts with no leaf-index
    ///      advancement), advance `nextLeafIndex`, install the SNARK-attested
    ///      `newRoot`, and bump the audit sum.  Returns the first leaf index
    ///      this batch occupies.  Callers MUST run `_verifyMintOrRevert` and
    ///      their binding gate first.
    function _advanceAndPull(
        uint256 newRoot,
        uint32  nextLeafIndex_,
        uint256 totalFace,
        uint256 count
    ) internal returns (uint256 startIndex) {
        require(
            buck.transferFrom(msg.sender, address(this), totalFace),
            "Notes: transfer failed"
        );
        startIndex       = nextLeafIndex;
        nextLeafIndex    = nextLeafIndex_ + uint32(count);
        currentRootIndex = (currentRootIndex + 1) % ROOT_HISTORY_SIZE;
        roots[currentRootIndex] = newRoot;
        noteFaceSum     += totalFace;
    }

    /// @dev Validate the per-leaf `issuerMode[]` against `expectLen` (== cms
    ///      length) and tally the PUBLIC/PRIVATE leaves.  Reverts on a bad mode
    ///      value or a mixed-mode batch: one batch has one `msg.sender`, hence
    ///      one issuer class, so PUBLIC and PRIVATE leaves cannot co-occur.
    function _classifyModes(uint256[] calldata issuerMode, uint256 expectLen)
        internal pure returns (uint256 nPublic, uint256 nPrivate)
    {
        require(issuerMode.length == expectLen, "Notes: issuerMode/cms length");
        for (uint256 i = 0; i < expectLen; i++) {
            uint256 mode = issuerMode[i];
            if      (mode == MODE_PUBLIC)  nPublic++;
            else if (mode == MODE_PRIVATE) nPrivate++;
            else revert("Notes: bad issuerMode");
        }
        require(!(nPublic > 0 && nPrivate > 0), "Notes: mixed issuerMode batch");
    }

    // ---- identity membership (shared by the coupled spends) ------------

    /// @dev Shared identity membership check — called by the spend paths.
    ///      Verifies that the committed point `P_I = (px, py)` is a member of the
    ///      registry-Identity accumulator under the current root.  The point is
    ///      supplied by the caller (the coupled-A2 path passes the deposit-coupling
    ///      sigma's `dc.P_I`), so the membership proof is bound to the SAME point
    ///      the sigma decrypted `eIss` to — a colluding pair cannot answer the
    ///      coupling with one point and the membership with another.
    ///
    ///      Reverts if the verifier is wired and the proof is invalid or the
    ///      registry's identityRoot is zero (unseeded accumulator).  Silently
    ///      passes if the verifier is not set (address(0)) or the proof is empty
    ///      (backward-compat skip).  The generic spend paths pass `(0, 0)`: with
    ///      the stub verifier that is plumbing-only; the real G1-tie verifier has
    ///      no valid proof for the point (0, 0) and so fails closed there — the
    ///      bound membership is reachable only through `spendCoupledA2`.
    function _verifyIdentityMembership(
        bytes memory identityMembershipProof,
        uint256 px,
        uint256 py
    )
        internal
    {
        IIdentityMembershipVerifier verifier = identityMembershipVerifier;
        if (address(verifier) == address(0)) return;
        if (identityMembershipProof.length == 0) return;

        IdentityRegistry reg = identityRegistry;
        require(address(reg) != address(0), "Notes: identity registry not set");
        uint256 root = reg.identityRoot();
        require(root != 0, "Notes: identity root not set");

        require(
            verifier.verifyMembership(identityMembershipProof, root, px, py),
            "Notes: bad identity membership proof"
        );
    }

    /// @dev Shared note<->eEnc tie check — called by the ADDRESSED (A1/A2)
    ///      spend path only.  Verifies that `eEnc` (the deposit-coupling
    ///      ciphertext) re-encrypts, under the recipient Identity M_rec, the
    ///      ciphertext the spent note committed in its idHash — binding the
    ///      deposit gate to THIS note (handle: `nullifier`; shared point:
    ///      `dc.P_I`).  See INoteBindingVerifier for the relation.
    ///
    ///      Reverts if the verifier is wired and the proof is invalid.  Silently
    ///      passes if the verifier is not set (address(0)) or the proof is empty
    ///      (backward-compat skip) — while skipped, the addressed-binding and
    ///      A2-collusion guarantees are NOT enforced.  The production relation
    ///      is circuits/note_binding.circom (soundness: Proofs Theorem 12).
    function _verifyNoteBinding(
        bytes memory noteBindingProof,
        uint256 nullifier,
        IdentityRegistry.ElGamalCT calldata eEnc,
        uint256 piX,
        uint256 piY
    )
        internal
    {
        INoteBindingVerifier verifier = noteBindingVerifier;
        if (address(verifier) == address(0)) return;
        if (noteBindingProof.length == 0) return;

        require(
            verifier.verifyNoteBinding(
                noteBindingProof, nullifier,
                eEnc.R.X, eEnc.R.Y, eEnc.C.X, eEnc.C.Y, piX, piY),
            "Notes: bad note binding"
        );
    }

    // ---- Identity-M-bound addressed spend (unilateral A1 / A2) -----------

    /// @dev Shared identity-M-bound deposit for the *addressed* flavors (A1, A2).
    ///      Both close their respective naming gap with the SAME on-chain gadget:
    ///      a deposit-coupling sigma + a membership proof bound to the single
    ///      committed point `dc.P_I`.  The flavors differ only in what the note
    ///      ciphertext `eEnc` encrypts (hence what `dc.P_I`'s underlying point is):
    ///
    ///        A2:  eEnc = eIss = (r'G, M_iss + r'·M_rec)  -> dc.P_I = M_iss + b·H
    ///             (membership of the private *issuer*; closes the A2 collusion gap)
    ///        A1:  eEnc = eRec = (r'G, M_rec + r'·M_rec)  -> dc.P_I = M_rec + b·H
    ///             (membership of the *recipient*; the issuer is public, named at mint)
    ///
    ///      1. verifyDepositCoupling(msg.sender, eEnc, dc): the account is bound to
    ///         m_rec AND `eEnc` decrypts under m_rec to the point committed
    ///         (blinded) in `dc.P_I`.
    ///      2. _verifyIdentityMembership(membershipProof, dc.P_I.X, dc.P_I.Y): a
    ///         Groth16 proof that `dc.P_I`'s underlying point is a registered
    ///         Identity.  The SAME `dc.P_I` flows into both, so the membership is
    ///         bound to exactly the point the coupling decrypted `eEnc` to -- a
    ///         colluding pair cannot key `eEnc` to a non-member and still spend.
    ///      3. _verifyNoteBinding(noteBindingProof, nullifier, eEnc, dc.P_I): the
    ///         note<->eEnc re-encryption tie (INoteBindingVerifier) — proves
    ///         `eEnc` re-encrypts the ciphertext the SPENT note committed, so the
    ///         coupling is bound to THIS note, not a depositor-substituted one.
    ///
    ///      The note's commitment + nullifier are proven by the generic spend
    ///      SNARK (cm in the pool tree, nullifier well-formed).
    ///
    ///      CAVEAT.  Steps 1-2 establish that `eEnc` decrypts (under the
    ///      depositor's authenticated m_rec) to a registered member — but NOT
    ///      that `eEnc` is the note's committed ciphertext: the spend SNARK is
    ///      flavor-agnostic and exposes no idHash.  Step 3 is what makes the
    ///      addressed-binding ("only M_rec can spend") and A2-collusion
    ///      ("un-nameable note un-spendable") guarantees hold; if governance
    ///      leaves the binding verifier unset (or a spend passes an empty
    ///      proof), they are NOT enforced for that spend.  See
    ///      INoteBindingVerifier and Proofs Theorem 12.
    function _spendCoupled(
        bytes   calldata proof,
        uint256          root,
        uint256          nullifier,
        uint256          face,
        address          recipient,
        IdentityRegistry.ElGamalCT          calldata eEnc,
        IdentityRegistry.DepositCouplingProof calldata dc,
        bytes   calldata membershipProof,
        bytes   calldata noteBindingProof
    ) internal {
        require(address(identityRegistry) != address(0), "Notes: identity registry not set");
        require(recipient != address(0),  "Notes: zero recipient");
        require(face      > 0,            "Notes: zero face");
        require(_isAcceptedRoot(root),    "Notes: unknown root");
        require(!nullifiers[nullifier],   "Notes: already spent");

        // Note commitment + nullifier: cm in the pool tree, nullifier well-formed.
        require(
            spendVerifier.verifySpend(
                proof, root, nullifier, face, recipient, block.chainid
            ),
            "Notes: bad spend proof"
        );

        // Identity-M binding, half 1: the deposit-coupling sigma.
        require(
            identityRegistry.verifyDepositCoupling(msg.sender, eEnc, dc),
            "Notes: bad deposit coupling"
        );

        nullifiers[nullifier] = true;
        noteFaceSum          -= face;

        // Identity-M binding, half 2: membership of dc.P_I's point, bound to the
        // SAME dc.P_I the coupling just constrained.
        _verifyIdentityMembership(membershipProof, dc.P_I.X, dc.P_I.Y);

        // Identity-M binding, half 3: the note<->eEnc re-encryption tie, binding
        // `eEnc` to THIS note (skipped only if governance left the slot unset).
        _verifyNoteBinding(noteBindingProof, nullifier, eEnc, dc.P_I.X, dc.P_I.Y);

        require(
            buck.transfer(recipient, face),
            "Notes: transfer failed"
        );
    }

    /// @notice Redeem an identity-targeted (unilateral) A2 note -- addressed,
    ///         *private* issuer.  `eIss` encrypts the issuer's own registered
    ///         Identity under the recipient identity point M_rec; the membership
    ///         certifies the decrypted issuer is registered, closing the A2
    ///         recipient-key collusion gap.  See alberta-buck-notes-unilateral.org.
    function spendCoupledA2(
        bytes   calldata proof,
        uint256          root,
        uint256          nullifier,
        uint256          face,
        address          recipient,
        IdentityRegistry.ElGamalCT          calldata eIss,
        IdentityRegistry.DepositCouplingProof calldata dc,
        bytes   calldata membershipProof,
        bytes   calldata noteBindingProof
    ) external {
        _spendCoupled(proof, root, nullifier, face, recipient, eIss, dc,
                      membershipProof, noteBindingProof);
        emit SpentCoupledA2(nullifier, face, recipient, dc.P_I.X, dc.P_I.Y);
    }

    /// @notice Redeem an identity-targeted A1 note -- addressed, *public* issuer.
    ///         `eRec` encrypts the recipient's identity under itself, so the SAME
    ///         deposit coupling proves the spender is the addressed identity and
    ///         the membership certifies that recipient identity is registered.
    ///         The issuer is public and named at mint (the batch Schnorr); the
    ///         recipient produces a bilateral receipt off chain
    ///         (alberta_buck.wallet.unilateral_a1).  On-chain logic is identical
    ///         to spendCoupledA2 -- only the committed point's meaning differs.
    function spendCoupledA1(
        bytes   calldata proof,
        uint256          root,
        uint256          nullifier,
        uint256          face,
        address          recipient,
        IdentityRegistry.ElGamalCT          calldata eRec,
        IdentityRegistry.DepositCouplingProof calldata dc,
        bytes   calldata membershipProof,
        bytes   calldata noteBindingProof
    ) external {
        _spendCoupled(proof, root, nullifier, face, recipient, eRec, dc,
                      membershipProof, noteBindingProof);
        emit SpentCoupledA1(nullifier, face, recipient, dc.P_I.X, dc.P_I.Y);
    }

    // ---- Identity-M-bound B1 spend (bearer, public issuer) ---------------

    /// @notice Redeem an identity-M-bound B1 note -- bearer, *public* issuer.
    ///         The depositor (= `recipient`, the payout account) re-encrypts its
    ///         own registered Identity M_dep under the public issuer's key
    ///         (`eDepForIss`) and proves, hiding every Identity, that the
    ///         ciphertext encrypts the Identity its account is bound to AND that
    ///         that Identity is a registered member -- via two co-bound checks
    ///         over the single committed point `b1Proof.P_dep = M_dep + b·H`:
    ///
    ///           1. verifyDepositorBinding(recipient, issuer, eDepForIss, b1Proof):
    ///              the Okamoto sigma (E2/E4/F1/F2) coupling the payout account to
    ///              m_dep and `eDepForIss` to the same m_dep, plus the P relation
    ///              committing `P_dep`.
    ///           2. _verifyIdentityMembership(membershipProof, P_dep.X, P_dep.Y):
    ///              the G1-tie proof that `P_dep`'s underlying M_dep is registered.
    ///
    ///         The SAME `P_dep` flows into both, so the depositor is provably a
    ///         registered Identity the issuer can name (it decrypts `eDepForIss`
    ///         with sk_iss off chain).  Membership-bound counterpart of the
    ///         `verifyDepositorForIssuer`-based B-spend overload, which it does not
    ///         disturb.  See alberta-buck-notes-identity-axis.org (the B1 dual).
    function spendCoupledB1(
        bytes   calldata proof,
        uint256          root,
        uint256          nullifier,
        uint256          face,
        address          recipient,
        address          issuer,
        IdentityRegistry.ElGamalCT            calldata eDepForIss,
        IdentityRegistry.DepositorBindingProof calldata b1Proof,
        bytes   calldata membershipProof
    ) external {
        require(address(identityRegistry) != address(0), "Notes: identity registry not set");
        require(recipient != address(0),  "Notes: zero recipient");
        require(issuer    != address(0),  "Notes: zero issuer");
        require(face      > 0,            "Notes: zero face");
        require(_isAcceptedRoot(root),    "Notes: unknown root");
        require(!nullifiers[nullifier],   "Notes: already spent");

        // Note commitment + nullifier.
        require(
            spendVerifier.verifySpend(
                proof, root, nullifier, face, recipient, block.chainid
            ),
            "Notes: bad spend proof"
        );

        // Identity-M binding, half 1: the depositor binding sigma (incl. the
        // P_dep commitment).
        require(
            identityRegistry.verifyDepositorBinding(
                recipient, issuer, eDepForIss, b1Proof),
            "Notes: bad depositor binding"
        );

        nullifiers[nullifier] = true;
        noteFaceSum          -= face;

        // Identity-M binding, half 2: membership of P_dep's point M_dep, bound to
        // the SAME P_dep the binding just constrained.
        _verifyIdentityMembership(membershipProof, b1Proof.P_dep.X, b1Proof.P_dep.Y);

        require(
            buck.transfer(recipient, face),
            "Notes: transfer failed"
        );

        emit SpentCoupledB1(nullifier, face, recipient, issuer, eDepForIss);
    }
}
