// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {IMintVerifier}                 from "./IMintVerifier.sol";
import {IMintVerifierA2}               from "./IMintVerifierA2.sol";
import {ISpendVerifier}                from "./ISpendVerifier.sol";
import {IIdentityMembershipVerifier}   from "./IIdentityMembershipVerifier.sol";
import {IDepositFoldVerifier}          from "./IDepositFoldVerifier.sol";
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
///         Spend: the spender supplies a Groth16 proof binding
///         (noteRoot, nullifier, face, recipient, chainId, flavor) to a
///         Poseidon opening + Merkle membership under a recent root.  Each
///         spendCoupled* entry point passes its flavor constant so an
///         A-opening cannot redeem through the B1 path.
///
/// @dev    Tree shape: depth 20 (max 2^20 = ~1M notes), leaf hash is
///         Poseidon-6(T_CM, flavor, v, rho, idHash, predicate).  Internal nodes
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
    ///         `keccak256("AlbertaBuck/Notes/Zero/v2") % FIELD_R`.
    uint256 public constant ZERO_VALUE =
        460097596457234765974707969191747880107513410278794739541636231580225950866;

    /// @notice Empty-tree root: 20 levels of self-paired ZERO_VALUE.  The
    ///         constructor seeds `roots[0]` to this so a freshly-deployed
    ///         contract has a valid live root for the first mint's oldRoot
    ///         check.  Computed off-chain from the same Poseidon-2 chain the
    ///         circuit uses; pinned literal here keeps the constructor pure.
    uint256 public constant EMPTY_ROOT =
        6356158094200644324551957783828547278843291898127006657250053394249012399487;

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

    /// @notice Note flavor labels, mirrored from `circuits/spend.circom` and
    ///         `alberta_buck.wallet.notes`.  Each spendCoupled* entry point
    ///         passes its constant into the spend SNARK as a public input.
    /// @notice The accumulator consumer Notes is: each spend names the identity
    ///         root it proved against, and the registry accepts any root it
    ///         retains within this consumer's maximum age -- a registration
    ///         check, not a revocation check, so a generous one (7 days).
    bytes32 public constant NOTES_MEMBERSHIP_CONSUMER =
        keccak256("AlbertaBuck/Accumulator/Consumer/NotesMembership/v2");

    uint256 public constant FLAVOR_A1 = 1;
    uint256 public constant FLAVOR_A2 = 2;
    uint256 public constant FLAVOR_B1 = 3;

    // ---- governance + verifier --------------------------------------------

    address        public governance;
    IMintVerifier  public mintVerifier;

    /// @notice Private-issuer (A2) mint verifier -- the mint_batch_a2 circuit
    ///         family (src/MintBatchA2N*Groth16Verifier via MintVerifierA2Adapter).
    ///         Distinct from `mintVerifier` because A2 has a different public
    ///         arity (5N+4: per-leaf eIss exposed as outputs) and is used only by
    ///         the PRIVATE-mode mint path, where it ties each committed leaf to
    ///         its re-encryption binding's eIss (the collusion-resistant leaf-tie;
    ///         see alberta-buck-notes.org ("The Non-Deniable-Receipt Invariant"), the Required Mint SNARK Signal for issuer binding at mint
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
    ///         is a member of the registry-Identity accumulator under the
    ///         identity root the spend names, which the registry must accept
    ///         for NOTES_MEMBERSHIP_CONSUMER.  Required for the full
    ///         identity-binding spend path (A1/A2 deposit coupling + B1
    ///         depositor binding).  Optional at construction; governance
    ///         wires it via setIdentityMembershipVerifier.  Coupled spends
    ///         require a non-zero verifier and a nonempty proof (fail closed).
    ///         The setter still accepts address(0) so governance can rotate
    ///         through unset; those spends then revert rather than skip.
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

    /// @notice The folded deposit gate for ADDRESSED (A1/A2) spends.
    ///
    ///         The addressed paths verify ONE proof carrying every relation.
    ///         That is not a consolidation for tidiness: an addressed Note is
    ///         keyed to the
    ///         recipient's receiving key while its authority belongs to the
    ///         recipient's Identity, and those are two different secrets.
    ///         Proved side by side they say nothing about their owner, and a
    ///         thief holding a stolen payload satisfies both halves -- the
    ///         reading half with the stolen key, the Identity half with its
    ///         own registered Identity.  The fold states the tie instead.
    ///
    ///         An addressed spend with this slot unset reverts.  There is no
    ///         weaker path to fall back to, by construction.
    ///         See doc/review/notes-receiving-key.org section 3.3a.
    IDepositFoldVerifier public depositFoldVerifier;

    // ---- events -----------------------------------------------------------

    event GovernanceTransferred(address indexed previous, address indexed next);
    event MintVerifierUpdated(address indexed previous, address indexed next);
    event A2MintVerifierUpdated(address indexed previous, address indexed next);
    event SpendVerifierUpdated(address indexed previous, address indexed next);
    event IdentityRegistryUpdated(address indexed previous, address indexed next);
    event IdentityMembershipVerifierUpdated(address indexed previous, address indexed next);
    event DepositFoldVerifierUpdated(address indexed previous, address indexed next);

    /// @notice Emitted on an addressed A2 deposit.  The fold publishes no point:
    ///         every value it constrains is either already a public input the
    ///         adapter derived (the nullifier, the face, the identity root, the
    ///         re-encryption, the depositor) or private by design.  An earlier
    ///         shape logged a committed `P_I`; a log is not a constraint, and
    ///         logging a point no relation reads invites an auditor to believe
    ///         otherwise.
    event SpentCoupledA2(
        uint256 indexed nullifier,
        uint256 face,
        address indexed recipient
    );

    /// @notice Emitted on an addressed A1 deposit (public issuer).  Same shape
    ///         as SpentCoupledA2, and for the same reason.
    event SpentCoupledA1(
        uint256 indexed nullifier,
        uint256 face,
        address indexed recipient
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

    /// @notice Authenticated public-mint issuer for an exact note commitment.
    ///         The spend circuit reveals this handle only for B1 notes.
    mapping(uint256 => address) public publicIssuerOfCommitment;

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
    ///         Identity-M-bound spends (the folded gate's account and root, B1's
    ///         depositor binding).  Passing `address(0)` disables those spends.
    function setIdentityRegistry(address next) external {
        require(msg.sender == governance, "not governance");
        emit IdentityRegistryUpdated(address(identityRegistry), next);
        identityRegistry = IdentityRegistry(next);
    }

    /// @notice Let governance authorize an exact registry binding on behalf of
    ///         this contract. IdentityRegistry sees Notes itself as the caller,
    ///         providing the target-control half of contract enrollment.
    function authorizeIdentityBinding(
        address registry,
        address binder,
        BN254.G1Point calldata pk,
        IdentityRegistry.ElGamalCT calldata E,
        bool isPublicIdentity_,
        bool isCarrying_
    ) external {
        require(msg.sender == governance, "not governance");
        IdentityRegistry(registry).authorizeContractBinding(
            binder, pk, E, isPublicIdentity_, isCarrying_
        );
    }

    /// @notice Wire (or rotate) the identity membership verifier consulted
    ///         by the coupled spend paths.  Passing `address(0)` is allowed
    ///         (governance tests of the setter); coupled spends then revert
    ///         rather than skipping the check.
    function setIdentityMembershipVerifier(address next) external {
        require(msg.sender == governance, "not governance");
        emit IdentityMembershipVerifierUpdated(
            address(identityMembershipVerifier), next);
        identityMembershipVerifier = IIdentityMembershipVerifier(next);
    }

    /// @notice Wire (or rotate) the folded deposit gate the addressed spends
    ///         verify.  `address(0)` is refused: the fold IS the addressed gate,
    ///         not an upgrade to one, so there is no configuration in which
    ///         clearing it leaves a sound path behind.  Rotation is for a new
    ///         verifier -- a fresh setup, or a circuit at a new depth.
    function setDepositFoldVerifier(address next) external {
        require(msg.sender == governance, "not governance");
        require(next != address(0), "depositFoldVerifier=0");
        emit DepositFoldVerifierUpdated(address(depositFoldVerifier), next);
        depositFoldVerifier = IDepositFoldVerifier(next);
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
    ///   - cms[i] = Poseidon-6 opening of the per-leaf witness;
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
        // Persist the issuer authenticated by the batch Schnorr for every
        // exact commitment.  Refuse duplicates so a later public issuer cannot
        // overwrite the first note's issuance attribution.
        for (uint256 i = 0; i < cms.length; i++) {
            require(publicIssuerOfCommitment[cms[i]] == address(0),
                    "Notes: duplicate public commitment");
            publicIssuerOfCommitment[cms[i]] = msg.sender;
        }
        uint256 startIndex =
            _advanceAndPull(newRoot, nextLeafIndex_, totalFace, cms.length);
        emit Minted(msg.sender, totalFace, startIndex, cms.length, newRoot);
        emit IssuerBound(msg.sender, newRoot);
    }

    /// @notice Mint a PRIVATE-issuer (A2) batch with the collusion-resistant
    ///         per-leaf leaf-tie.  Every leaf must be PRIVATE-mode
    ///         (issuerMode[i] == MODE_PRIVATE); `msg.sender` must be a registered
    ///         PRIVATE Identity and supply exactly one A2 re-encryption binding
    ///         per committed leaf.  The proof is verified by the *A2* mint
    ///         circuit (mint_batch_a2), which constrains every leaf to flavor ==
    ///         A2 and exposes each leaf's committed `eIss` and `T` as public
    ///         outputs; we pass the bindings' `eIss` and `proof.T` as those
    ///         public inputs, so a Groth16 accept proves each binding answers
    ///         for exactly the leaf it claims -- the leaf-tie that closes the
    ///         floating-/missing-binding collusion sub-cases (per-batch count
    ///         alone could not).
    ///
    /// @dev    What the leaf-tie closes here, and what the spend closes.  It
    ///         binds each committed leaf to a *verified* re-encryption of the
    ///         issuer's registered Identity, so no leaf is left without a
    ///         binding and no binding can float to a different leaf.  The
    ///         binding speaks of the key hidden in its `Q`, which the issuer
    ///         chooses, and an ElGamal ciphertext does not bind its plaintext to
    ///         one key.  So the leaf also commits the binding's
    ///         `T = r'*pk + gamma*H`, and the A2 deposit fold proves `T` opens
    ///         under the spender's own key: a note keyed so that the recipient
    ///         reads someone other than its minter never spends
    ///         (doc/review/notes-receiving-key.org, section 4.6).  issuerMode
    ///         here is a caller-facing assertion (the A2 circuit independently
    ///         constrains flavor == A2).
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

        // Leaf-tie: the bindings' eIss and T are the A2 circuit's public
        // inputs, so a valid proof ties each committed leaf to the binding
        // answering for it.
        uint256[4][] memory eIss = new uint256[4][](a2Bindings.length);
        uint256[2][] memory T    = new uint256[2][](a2Bindings.length);
        for (uint256 i = 0; i < a2Bindings.length; i++) {
            eIss[i][0] = a2Bindings[i].eIss.R.X;
            eIss[i][1] = a2Bindings[i].eIss.R.Y;
            eIss[i][2] = a2Bindings[i].eIss.C.X;
            eIss[i][3] = a2Bindings[i].eIss.C.Y;
            T[i][0]    = a2Bindings[i].proof.T.X;
            T[i][1]    = a2Bindings[i].proof.T.Y;
        }
        _verifyA2MintOrRevert(proof, eIss, T, oldRoot, newRoot, nextLeafIndex_, totalFace, cms);

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
    ///      via its Poseidon-6 opening: a malformed cms[] entry >= FIELD_R would
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
    ///      `_verifyMintOrRevert`, plus the per-leaf `eIss` and `T` canonical
    ///      bounds, then the A2 Groth16 check.  `eIss` and `T` are what the
    ///      bindings carry; passing them as the A2 circuit's public inputs makes
    ///      the verifier's accept the leaf-tie (the circuit exposes the
    ///      *committed* leaf's, so equality with what we pass is a constraint,
    ///      not a contract-side compare).  View-only -- reverts here abort
    ///      before BUCK moves or tree state advances.
    function _verifyA2MintOrRevert(
        bytes   calldata proof,
        uint256[4][] memory eIss,
        uint256[2][] memory T,
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
            require(T[i][0] < FIELD_R && T[i][1] < FIELD_R, "Notes: T out of field");
        }

        require(
            a2MintVerifier.verifyMint(
                proof, eIss, T, oldRoot, newRoot, uint256(nextLeafIndex_), totalFace, cms
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

    // ---- identity membership (the B1 spend) ------------------------------

    /// @dev Identity membership check — called by the BEARER (B1) spend only.
    ///      Verifies that the committed point `P_dep = (px, py)` is a member of
    ///      the registry-Identity accumulator under the current root.  The point
    ///      is the one `verifyDepositorBinding`'s sigma constrained, so the two
    ///      are bound to the same M_dep.
    ///
    ///      Sharing a public point between two proofs is sound HERE and nowhere
    ///      else in this contract: B1's two facts rest on ONE secret (m_dep), so
    ///      the sigma's shared Fiat-Shamir nonce is a genuine tie, and the blind
    ///      is a multiple of H_PEDERSEN, whose discrete log is unknown.  The
    ///      addressed flavours have two secrets and no such luck, which is why
    ///      they fold.  See doc/review/notes-receiving-key.org section 4.5.
    ///
    ///      Reverts if the verifier is unset, the proof is empty or invalid,
    ///      or the registry does not accept `identityRoot` for this consumer:
    ///      a root the ring no longer retains, or older than Notes' maximum age.
    ///      Coupled spend entry points also require the verifier non-zero
    ///      (cheap, before the spend SNARK); this helper fails closed too so
    ///      a future caller cannot skip by omitting that require.  The real
    ///      G1-tie verifier has no valid proof for the point (0, 0) and so
    ///      fails closed there.
    function _verifyIdentityMembership(
        bytes memory identityMembershipProof,
        uint256 identityRoot,
        uint256 px,
        uint256 py
    )
        internal
    {
        IIdentityMembershipVerifier verifier = identityMembershipVerifier;
        require(address(verifier) != address(0),
                "Notes: membership verifier not set");
        require(identityMembershipProof.length != 0,
                "Notes: empty identity membership proof");

        IdentityRegistry reg = identityRegistry;
        require(address(reg) != address(0), "Notes: identity registry not set");
        require(reg.acceptsRoot(identityRoot, NOTES_MEMBERSHIP_CONSUMER),
                "Notes: identity root not accepted");

        require(
            verifier.verifyMembership(identityMembershipProof, identityRoot, px, py),
            "Notes: bad identity membership proof"
        );
    }

    // ---- Identity-M-bound addressed spend (unilateral A1 / A2) -----------

    /// @dev Shared identity-M-bound deposit for the *addressed* flavors (A1, A2).
    ///      Spending an addressed Note requires two facts about two DIFFERENT
    ///      secrets: the receiving secret `k` that opens the note's ciphertext,
    ///      and the Identity scalar `m_rec` the payout account is registered
    ///      under.  Proved side by side they say nothing about their owner -- a
    ///      thief holding a stolen payload answers the reading half with the
    ///      stolen key and the Identity half with its own registered Identity,
    ///      and both halves are true.  So the gate is ONE Groth16 proof over ONE
    ///      witness, carrying every relation:
    ///
    ///        (1) k decrypts the note ciphertext to the point committed
    ///        (2) the account credential decrypts under sk_dep to M_rec
    ///        (3) a registered leaf commits the pair (m_rec, k) under the
    ///            holder's salt -- the relation a split gate leaves out, and
    ///            the one the thief cannot satisfy
    ///        (4) that leaf's path folds to the posted identity root
    ///        (5) A2 only: the decrypted ISSUER Identity is itself registered,
    ///            under the salt the note shipped
    ///
    ///      `DepositFoldVerifierAdapter` derives EVERY public input on chain and
    ///      reads the depositor's registered key and credential from the
    ///      registry rather than accepting them, so relation (2) is necessarily
    ///      about an account that really is registered.  There is no committed
    ///      point `P_I` and no blind: those existed only so that a sigma and a
    ///      separate membership SNARK could share a hidden value, which is
    ///      review finding 5 -- an equality inferred from two proofs that merely
    ///      share a public point.  One witness states the tie instead.
    ///
    ///      The note's commitment + nullifier are proven by the generic spend
    ///      SNARK (cm in the pool tree, nullifier well-formed, flavor bound to
    ///      this entry point); `idHash` is the private handle joining the two.
    ///
    ///      See doc/review/notes-receiving-key.org section 3.3a and
    ///      alberta_buck/wallet/deposit_fold.py, the clear-text reference the
    ///      circuit is checked against.
    function _spendCoupled(
        bytes   calldata proof,
        uint256          root,
        uint256          identityRoot,
        uint256          nullifier,
        uint256          face,
        address          recipient,
        IdentityRegistry.ElGamalCT calldata eEnc,
        bytes   calldata foldProof,
        uint256          flavor,
        bool             a1Layout
    ) internal {
        require(address(identityRegistry) != address(0), "Notes: identity registry not set");
        require(address(depositFoldVerifier) != address(0),
                "Notes: deposit fold verifier not set");
        require(recipient != address(0),  "Notes: zero recipient");
        require(face      > 0,            "Notes: zero face");
        require(_isAcceptedRoot(root),    "Notes: unknown root");
        require(!nullifiers[nullifier],   "Notes: already spent");

        // Note commitment + nullifier: cm in the pool tree, nullifier well-formed.
        require(
            spendVerifier.verifySpend(
                proof, root, nullifier, face, recipient, block.chainid, flavor, 0
            ),
            "Notes: bad spend proof"
        );

        require(identityRegistry.acceptsRoot(identityRoot, NOTES_MEMBERSHIP_CONSUMER),
                "Notes: identity root not accepted");
        require(foldProof.length != 0, "Notes: empty fold proof");

        bool ok = a1Layout
            ? depositFoldVerifier.verifyFoldA1(
                foldProof, nullifier, face, identityRoot, eEnc, msg.sender)
            : depositFoldVerifier.verifyFoldA2(
                foldProof, nullifier, identityRoot, eEnc, msg.sender);
        require(ok, "Notes: bad folded deposit gate");

        nullifiers[nullifier] = true;
        noteFaceSum          -= face;

        require(
            buck.transfer(recipient, face),
            "Notes: transfer failed"
        );
    }

    /// @notice Redeem an addressed A2 note -- *private* issuer.  `eIss` is the
    ///         spend's re-encryption, under the recipient's receiving key, of the
    ///         issuer Identity the note committed at mint; relation (5) certifies
    ///         that Identity is registered, so a colluding issuer cannot key the
    ///         note to a throwaway point and leave the recipient un-nameable.
    function spendCoupledA2(
        bytes   calldata proof,
        uint256          root,
        uint256          identityRoot,
        uint256          nullifier,
        uint256          face,
        address          recipient,
        IdentityRegistry.ElGamalCT calldata eIss,
        bytes   calldata foldProof
    ) external {
        _spendCoupled(proof, root, identityRoot, nullifier, face, recipient, eIss,
                      foldProof, FLAVOR_A2, false);
        emit SpentCoupledA2(nullifier, face, recipient);
    }

    /// @notice Redeem an addressed A1 note -- *public* issuer, named at mint by
    ///         the batch Schnorr.  `eRec` is the spend's re-encryption, under the
    ///         recipient's receiving key, of the recipient's OWN Identity, so the
    ///         fold needs no fifth relation: relation (2) already proves that
    ///         Identity is the one the payout account is registered under.  A1
    ///         takes the `face`-bearing layout, because an A1 idHash commits
    ///         (eNote, m_issuer, sigma) and the public face pins eNote's
    ///         plaintext.
    function spendCoupledA1(
        bytes   calldata proof,
        uint256          root,
        uint256          identityRoot,
        uint256          nullifier,
        uint256          face,
        address          recipient,
        IdentityRegistry.ElGamalCT calldata eRec,
        bytes   calldata foldProof
    ) external {
        _spendCoupled(proof, root, identityRoot, nullifier, face, recipient, eRec,
                      foldProof, FLAVOR_A1, true);
        emit SpentCoupledA1(nullifier, face, recipient);
    }

    // ---- Identity-M-bound B1 spend (bearer, public issuer) ---------------

    /// @notice Redeem an identity-M-bound B1 note -- bearer, *public* issuer.
    ///         `issuanceCommitment` is the exact note commitment opened by the
    ///         spend SNARK.  Its mint-time issuer was recorded only after the
    ///         registered-key batch Schnorr passed; the supplied `issuer` must
    ///         match that immutable attribution.  This deliberately reveals a
    ///         B1 spend-to-mint handle: bearer issuers are public, while A1/A2
    ///         keep the corresponding circuit signal zero.
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
    ///         disturb.  See alberta-buck-notes.org ("Mutual Decryptability", B1 dual of the one-gadget) and alberta-buck-notes-flow.org "The Identity-M Spend Path".
    function spendCoupledB1(
        bytes   calldata proof,
        uint256          root,
        uint256          identityRoot,
        uint256          nullifier,
        uint256          face,
        address          recipient,
        uint256          issuanceCommitment,
        address          issuer,
        IdentityRegistry.ElGamalCT            calldata eDepForIss,
        IdentityRegistry.DepositorBindingProof calldata b1Proof,
        bytes   calldata membershipProof
    ) external {
        require(address(identityRegistry) != address(0), "Notes: identity registry not set");
        require(address(identityMembershipVerifier) != address(0),
                "Notes: membership verifier not set");
        require(recipient != address(0),  "Notes: zero recipient");
        require(issuer    != address(0),  "Notes: zero issuer");
        require(face      > 0,            "Notes: zero face");
        require(_isAcceptedRoot(root),    "Notes: unknown root");
        require(!nullifiers[nullifier],   "Notes: already spent");

        // Note commitment + nullifier.  Flavor 3 (B1) is a public input, so
        // an A1/A2 opening cannot verify here even with a nonempty membership.
        require(
            spendVerifier.verifySpend(
                proof, root, nullifier, face, recipient, block.chainid, FLAVOR_B1,
                issuanceCommitment
            ),
            "Notes: bad spend proof"
        );

        // The proof above opens this exact B1 commitment.  Its issuer was
        // recorded only after the mint batch's registered-key Schnorr passed.
        require(publicIssuerOfCommitment[issuanceCommitment] == issuer,
                "Notes: wrong B1 issuer");

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
        _verifyIdentityMembership(membershipProof, identityRoot, b1Proof.P_dep.X, b1Proof.P_dep.Y);

        require(
            buck.transfer(recipient, face),
            "Notes: transfer failed"
        );

        emit SpentCoupledB1(nullifier, face, recipient, issuer, eDepForIss);
    }
}
