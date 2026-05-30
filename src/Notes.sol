// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {IMintVerifier}   from "./IMintVerifier.sol";
import {ISpendVerifier}  from "./ISpendVerifier.sol";
import {ISpendAVerifier} from "./ISpendAVerifier.sol";
import {IdentityRegistry} from "./IdentityRegistry.sol";
import {BN254}            from "./BN254.sol";

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
    ISpendVerifier public spendVerifier;
    /// @notice A-flavor spend verifier (Phase 8 V2 -- spend_a.circom).
    ///         Optional at construction (zero-address means A-spends are
    ///         disabled until governance wires it).  Distinct interface from
    ///         `spendVerifier` because spend_a V2 has 9 public inputs (the
    ///         5-tuple plus the four BN254 G1 coordinates of the publicly
    ///         revealed note ciphertext E_n), bound to the leaf via the
    ///         in-circuit Poseidon-8 idHash gate.
    ISpendAVerifier public spendAVerifier;

    /// @notice Identity registry consulted on every A-spend for the off-chain
    ///         CP-DLEQ identity binding (Phase 8 V2).  Optional at
    ///         construction (zero-address disables A-spends just like a
    ///         zero `spendAVerifier`).
    IdentityRegistry public identityRegistry;

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
    event SpendAVerifierUpdated(address indexed previous, address indexed next);
    event IdentityRegistryUpdated(address indexed previous, address indexed next);

    /// @notice Emitted when a note is successfully spent.
    event Spent(uint256 indexed nullifier, uint256 face, address indexed recipient);

    /// @notice Emitted when an A-flavor note is successfully spent.  The
    ///         nullifier domain (tag 4243) is disjoint from B-spend (tag
    ///         4242) so off-chain indexers can dedupe on `nullifier` alone.
    event SpentA(uint256 indexed nullifier, uint256 face, address indexed recipient);

    /// @notice Emitted on a bearer-note spend that completes the
    ///         depositor->issuer half of the mutual-decryptability handshake:
    ///         `eDepForIss` re-encrypts the recipient's registered Identity
    ///         under the issuer's key (verified via verifyDepositorForIssuer),
    ///         so the public issuer can recover who cashed the note.
    event SpentB(
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

    function setSpendVerifier(address next) external {
        require(msg.sender == governance, "not governance");
        require(next != address(0),       "verifier=0");
        emit SpendVerifierUpdated(address(spendVerifier), next);
        spendVerifier = ISpendVerifier(next);
    }

    /// @notice Wire (or rotate) the A-flavor spend verifier.  Passing
    ///         `address(0)` *disables* A-spends (the next `spendACP()` call
    ///         will revert on the verifier dispatch); use that path during
    ///         emergency lockdowns rather than redeploying Notes.
    function setSpendAVerifier(address next) external {
        require(msg.sender == governance, "not governance");
        emit SpendAVerifierUpdated(address(spendAVerifier), next);
        spendAVerifier = ISpendAVerifier(next);
    }

    /// @notice Wire (or rotate) the identity registry consulted by
    ///         `spendACP`.  Passing `address(0)` disables A-spends just like
    ///         a zero `spendAVerifier`; emergency lockdowns may flip either.
    function setIdentityRegistry(address next) external {
        require(msg.sender == governance, "not governance");
        emit IdentityRegistryUpdated(address(identityRegistry), next);
        identityRegistry = IdentityRegistry(next);
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
        // Legacy / non-public path.  A registered PUBLIC issuer cannot use this
        // overload: the zero signature fails the binding check in _mint.
        _mint(proof, oldRoot, newRoot, nextLeafIndex_, totalFace, cms,
              IdentityRegistry.SchnorrProof(0, 0, BN254.G1Point(0, 0)));
    }

    /// @notice Mint with a public-issuer Schnorr binding (Notes
    ///         mutual-decryptability, Phase 1).  A registered Public-Identity
    ///         issuer MUST use this overload: `issuerSig` binds their decrypted
    ///         Identity to every leaf so a depositor can later produce a sound
    ///         receipt naming the payer.  (Private A2 issuers are bound
    ///         in-SNARK in a later phase.)
    function mint(
        bytes   calldata proof,
        uint256          oldRoot,
        uint256          newRoot,
        uint32           nextLeafIndex_,
        uint256          totalFace,
        uint256[] calldata cms,
        IdentityRegistry.SchnorrProof calldata issuerSig
    ) external {
        _mint(proof, oldRoot, newRoot, nextLeafIndex_, totalFace, cms, issuerSig);
    }

    /// @notice One A2 (addressed, private-issuer) leaf's recipient-blinded
    ///         re-encryption binding: the leaf ciphertext `eIss` (E_iss-for-rec)
    ///         plus its proof.  See IdentityRegistry.verifyIssuerReenc and
    ///         alberta_buck.wallet.issuer_reenc.
    struct A2Binding {
        IdentityRegistry.ElGamalCT        eIss;
        IdentityRegistry.IssuerReencProof proof;
    }

    /// @notice Mint a private-issuer (A2) batch, anchoring the per-leaf
    ///         re-encryption bindings on chain (Notes mutual-decryptability,
    ///         Phase 2).  Each binding is verified against `msg.sender`'s
    ///         registered credential via IdentityRegistry.verifyIssuerReenc, so
    ///         an invalid binding reverts the whole mint; the `IssuerReencBound`
    ///         event anchors them for tier-2 receipt verification.
    ///
    /// @dev    *Scope.*  This verifies the bindings the issuer supplies and
    ///         records that they were anchored at mint.  It does NOT yet (a) tie
    ///         each `eIss` to a specific committed leaf, nor (b) enforce that
    ///         every A2 leaf carries a binding -- both require the mint SNARK to
    ///         expose a per-leaf `issuerMode` and the leaf's `eIss` (the same
    ///         circuit signal the bearer-from-non-public gate needs).  Until
    ///         then the binding's leaf-tie rests on the off-chain note artifact
    ///         + the recipient's verifiable decryption (see the receipt
    ///         verifier).  The Schnorr path is unused here: an A2 issuer is a
    ///         registered *private* Identity, so the `_mint` public-issuer gate
    ///         is skipped (a public minter would revert on the zero signature).
    function mint(
        bytes   calldata proof,
        uint256          oldRoot,
        uint256          newRoot,
        uint32           nextLeafIndex_,
        uint256          totalFace,
        uint256[] calldata cms,
        A2Binding[] calldata a2Bindings
    ) external {
        require(address(identityRegistry) != address(0),
                "Notes: identity registry not set");
        uint256 m = a2Bindings.length;
        require(m > 0, "Notes: no A2 bindings");
        for (uint256 i = 0; i < m; i++) {
            require(
                identityRegistry.verifyIssuerReenc(
                    msg.sender, a2Bindings[i].eIss, a2Bindings[i].proof),
                "Notes: bad A2 binding"
            );
        }
        _mint(proof, oldRoot, newRoot, nextLeafIndex_, totalFace, cms,
              IdentityRegistry.SchnorrProof(0, 0, BN254.G1Point(0, 0)));
        emit IssuerReencBound(msg.sender, newRoot, m);
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
    /// @dev    Scope.  The gate consumes `issuerMode[]` and is sound for an
    ///         honest issuer today; the *binding of issuerMode to each leaf's
    ///         committed flavor* lands with the per-N mint-verifier regen (the
    ///         circuit already emits issuerMode -- see circuits/mint_batch.circom
    ///         and alberta-buck-notes-decryptability.org, The Required Mint SNARK
    ///         Signal).  Until that governance cutover the legacy 6-/7-arg
    ///         overloads remain for the unbound / batch-Schnorr paths.
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
        _verifyMintOrRevert(proof, oldRoot, newRoot, nextLeafIndex_, totalFace, cms);
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

    /// @notice Mint a PRIVATE-issuer (A2) batch with per-leaf issuerMode gating.
    ///         Every leaf must be PRIVATE-mode (issuerMode[i] == MODE_PRIVATE):
    ///         `msg.sender` must be a registered PRIVATE Identity and supply one
    ///         verified A2 re-encryption binding per private leaf (count
    ///         completeness).  Supersedes the bindings-only A2 overload by also
    ///         enforcing, via issuerMode, that the batch carries no PUBLIC leaf
    ///         a private issuer could not bind.
    ///
    /// @dev    Count completeness (one binding per private leaf) is enforced
    ///         here; tying each binding's `eIss` to a *specific* committed leaf
    ///         still rests on the off-chain note artifact + the recipient's
    ///         verifiable decryption until the mint SNARK exposes per-leaf
    ///         `eIss` (The Required Mint SNARK Signal).  issuerMode binding is
    ///         deferred to the same verifier regen as the PUBLIC path above.
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
        _verifyMintOrRevert(proof, oldRoot, newRoot, nextLeafIndex_, totalFace, cms);
        require(address(identityRegistry) != address(0),
                "Notes: identity registry not set");
        (uint256 nPublic, uint256 nPrivate) = _classifyModes(issuerMode, cms.length);
        require(nPublic  == 0, "Notes: public leaf needs Schnorr overload");
        require(nPrivate  > 0, "Notes: no private leaves");
        require(!identityRegistry.isPublicIdentity(msg.sender),
                "Notes: private-mode leaf needs private issuer");
        require(a2Bindings.length == nPrivate, "Notes: A2 binding count");
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

    function _mint(
        bytes   calldata proof,
        uint256          oldRoot,
        uint256          newRoot,
        uint32           nextLeafIndex_,
        uint256          totalFace,
        uint256[] calldata cms,
        IdentityRegistry.SchnorrProof memory issuerSig
    ) internal {
        _verifyMintOrRevert(proof, oldRoot, newRoot, nextLeafIndex_, totalFace, cms);

        // Public-issuer binding (Notes mutual-decryptability, Phase 1).  When
        // the minter is a registered PUBLIC Identity, require a Schnorr
        // signature over keccak256(cms) so the issuer's decrypted Identity is
        // provably bound to every leaf -- the depositor can then produce a
        // sound receipt naming the payer.  This legacy path gates the whole
        // batch on `msg.sender` being public; the per-leaf `issuerMode`
        // overloads below gate each leaf (and reject a bearer-from-non-public).
        bool issuerBound;
        if (address(identityRegistry) != address(0)
            && identityRegistry.isPublicIdentity(msg.sender)) {
            require(
                identityRegistry.verifyIssuerSchnorr(
                    msg.sender, keccak256(abi.encodePacked(cms)), issuerSig),
                "Notes: bad issuer binding"
            );
            issuerBound = true;
        }

        uint256 startIndex =
            _advanceAndPull(newRoot, nextLeafIndex_, totalFace, cms.length);
        emit Minted(msg.sender, totalFace, startIndex, cms.length, newRoot);
        if (issuerBound) emit IssuerBound(msg.sender, newRoot);
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
        uint256          oldRoot,
        uint256          newRoot,
        uint32           nextLeafIndex_,
        uint256          totalFace,
        uint256[] calldata cms
    ) internal view {
        require(cms.length > 0,                            "Notes: empty mint");
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
                proof, oldRoot, newRoot, uint256(nextLeafIndex_), totalFace, cms
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
        _spend(
            proof, root, nullifier, face, recipient, address(0),
            IdentityRegistry.ElGamalCT(BN254.G1Point(0, 0), BN254.G1Point(0, 0)),
            IdentityRegistry.CPProof(0, 0, 0,
                BN254.G1Point(0, 0), BN254.G1Point(0, 0), BN254.G1Point(0, 0))
        );
    }

    /// @notice Spend a bearer (B) note and complete the depositor->issuer half
    ///         of the mutual-decryptability handshake.  `eDepForIss`
    ///         re-encrypts the recipient's registered Identity under the public
    ///         `issuer`'s key; `cpProof` (a Chaum-Pedersen re-encryption proof)
    ///         is checked via IdentityRegistry.verifyDepositorForIssuer, and the
    ///         SpentB event publishes `eDepForIss` so the issuer can recover who
    ///         cashed the note.  An encrypted-Identity recipient uses this
    ///         overload; a public recipient is already recoverable from the
    ///         registry via the plain spend.  (Binding the issuer to the note
    ///         itself awaits the B-spend circuit revealing it; see
    ///         alberta-buck-notes-decryptability.org.)
    function spend(
        bytes   calldata proof,
        uint256          root,
        uint256          nullifier,
        uint256          face,
        address          recipient,
        address          issuer,
        IdentityRegistry.ElGamalCT calldata eDepForIss,
        IdentityRegistry.CPProof    calldata cpProof
    ) external {
        require(issuer != address(0), "Notes: zero issuer");
        _spend(proof, root, nullifier, face, recipient, issuer, eDepForIss, cpProof);
    }

    function _spend(
        bytes   calldata proof,
        uint256          root,
        uint256          nullifier,
        uint256          face,
        address          recipient,
        address          issuer,
        IdentityRegistry.ElGamalCT memory eDepForIss,
        IdentityRegistry.CPProof    memory cpProof
    ) internal {
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

        bool bound = issuer != address(0);
        if (bound) {
            // Depositor->issuer binding: the recipient re-encrypts their
            // registered Identity under the issuer's key so the issuer can
            // recover who cashed the note from the SpentB event.
            require(address(identityRegistry) != address(0),
                    "Notes: identity registry not set");
            require(
                identityRegistry.verifyDepositorForIssuer(
                    recipient, issuer, eDepForIss, cpProof),
                "Notes: bad depositor binding"
            );
        }

        require(
            buck.transfer(recipient, face),
            "Notes: transfer failed"
        );

        if (bound) emit SpentB(nullifier, face, recipient, issuer, eDepForIss);
        else       emit Spent(nullifier, face, recipient);
    }

    // ---- A-flavor spend (Phase 8 V2) -------------------------------------

    /// @notice Redeem an A-flavor note (spend_a.circom V2).
    ///
    /// The A-spend SNARK is structurally identical to spend (same Merkle
    /// path + Poseidon-5 opening + face binding) with three A-specific
    /// differentiators:
    ///   - flavor in {1,2}: spend_a rejects B-flavor openings, where spend
    ///     accepts any flavor;
    ///   - nullifier tag = 4243 (vs 4242 for spend), so the nullifier
    ///     preimage spaces are disjoint and the same `nullifiers` mapping
    ///     can serve both flavors with no cross-flavor collision risk;
    ///   - the publicly-revealed note ciphertext E_n = (R_n, C_n) is bound
    ///     to the leaf via Poseidon-8(eNoteR, eNoteC, issuerData[4]) ===
    ///     idHash, so the on-chain CP-DLEQ verifier sees the same E_n that
    ///     was committed at mint time.
    ///
    /// V2 ships the cryptographic "must be the registered recipient to
    /// spend" binding *off-chain* relative to the SNARK -- the spender
    /// (msg.sender) provides a 4-element Chaum-Pedersen DLEQ proof showing
    /// that their wallet's secret key sk_dep decrypts both the registered
    /// E_addr[msg.sender] and the freshly-revealed E_n to the same
    /// identity point M.  IdentityRegistry.verifySpendCP runs that check
    /// using EIP-196 BN254 precompiles (~36K gas) and binds (recipient,
    /// chainid) into the Fiat-Shamir transcript so a proof tied to one
    /// (recipient, chain) tuple cannot be replayed against another.  The
    /// Poseidon-8 idHash gate inside the SNARK forces the prover to reveal
    /// the same E_n the credential was minted against -- a spender who
    /// substitutes a different ciphertext (so they can satisfy the CP-DLEQ
    /// with their own key) would fail the in-circuit binding.  Together
    /// the two checks reject every spender other than the address whose
    /// (pk, E_addr) the credential was issued to.
    function spendACP(
        bytes   calldata proof,
        uint256          root,
        uint256          nullifier,
        uint256          face,
        address          recipient,
        IdentityRegistry.ElGamalCT calldata E_n,
        IdentityRegistry.SpendCPProof calldata cpProof
    ) external {
        require(address(spendAVerifier)   != address(0), "Notes: A-spend disabled");
        require(address(identityRegistry) != address(0), "Notes: identity registry not set");
        require(recipient != address(0),  "Notes: zero recipient");
        require(face      > 0,            "Notes: zero face");
        require(_isAcceptedRoot(root),    "Notes: unknown root");
        require(!nullifiers[nullifier],   "Notes: already spent");

        // SNARK: 9 public inputs bind the 5-tuple plus E_n's coords.
        require(
            spendAVerifier.verifySpendA(
                proof, root, nullifier, face, recipient, block.chainid,
                E_n.R.X, E_n.R.Y, E_n.C.X, E_n.C.Y
            ),
            "Notes: bad spend proof"
        );

        // Off-chain identity binding: msg.sender must own the sk_dep that
        // decrypts both E_n and their registered E_addr to the same M.
        require(
            identityRegistry.verifySpendCP(msg.sender, recipient, E_n, cpProof),
            "Notes: bad identity proof"
        );

        nullifiers[nullifier] = true;
        noteFaceSum          -= face;

        require(
            buck.transfer(recipient, face),
            "Notes: transfer failed"
        );

        emit SpentA(nullifier, face, recipient);
    }
}
