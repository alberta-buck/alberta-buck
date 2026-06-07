// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BN254} from "./BN254.sol";
import {IPoseidonT3} from "./IPoseidonT3.sol";

/// @title IdentityRegistry — on-chain registry of identity-bound public keys.
/// @notice Each Ethereum address binds to (pk, E_addr) where:
///         * pk     = ElGamal recipient public key (G1)
///         * E_addr = (R, C) = ElGamal ciphertext of the identity point M = m*G,
///                    encrypted under pk and witnessed by an issuer-signed
///                    Pointcheval-Sanders credential.
///         A registration NIZK proves -- without revealing m or r -- that the
///         credential is valid and that E_addr really encrypts m.
///         A Chaum-Pedersen NIZK proves a re-encryption sends the same M to a
///         second registered recipient (used by Buck.approve()).
contract IdentityRegistry {

    // ---- types --------------------------------------------------------------

    struct PSPubKey {
        BN254.G2Point X;
        BN254.G2Point Y;
    }

    struct ElGamalCT {
        BN254.G1Point R;
        BN254.G1Point C;
    }

    struct PSSig {
        BN254.G1Point sigma_1;
        BN254.G1Point sigma_2;
    }

    /// @notice 6-element registration NIZK proof (matches alberta_buck.wallet.nizk).
    struct RegistrationProof {
        uint256 e;
        uint256 s_m;
        uint256 s_r;
        BN254.G1Point A_ps;   // PS-side commitment: m_tilde * sigma'_1
        BN254.G1Point T_C;    // ElGamal C commitment: m_tilde*G + r_tilde*pk
        BN254.G1Point T_R;    // ElGamal R commitment: r_tilde * G
    }

    /// @notice 6-element Chaum-Pedersen proof (matches alberta_buck.wallet.chaum_pedersen).
    struct CPProof {
        uint256 e;
        uint256 s1;
        uint256 s2;
        BN254.G1Point T1;
        BN254.G1Point T2;
        BN254.G1Point T3;
    }

    /// @notice Schnorr signature over a note-batch commitment by an issuer's
    ///         registered identity key -- the public-issuer half of the BUCK
    ///         Notes deferred-approve handshake (mutual-decryptability, Phase 1;
    ///         see alberta-buck-notes-decryptability.org).  Matches
    ///         alberta_buck.wallet.schnorr.SchnorrProof.
    struct SchnorrProof {
        uint256 e;          // Fiat-Shamir challenge (== _fsIssuerSchnorr)
        uint256 s;          // response: k + e*sk_iss  (mod R)
        BN254.G1Point R;    // nonce commitment: k*G
    }

    /// @notice A2 issuer re-encryption binding -- the recipient-blinded proof
    ///         that a private issuer's leaf ciphertext E_iss-for-rec re-encrypts
    ///         the issuer's registered Identity under the recipient's key,
    ///         without revealing the recipient (Notes mutual-decryptability,
    ///         Phase 2).  Matches alberta_buck.wallet.issuer_reenc.IssuerReencProof.
    ///         A 5-relation, 4-witness Okamoto sigma over (r', beta, sk_iss, gamma):
    ///           L1 R_i = r'*G        L2 U = r'*H
    ///           L3 T = r'*Q - beta*U + gamma*G    (=> T = r'*pk_rec + gamma*G)
    ///           L4 pk_iss = sk_iss*G
    ///           L5 C_reg + T - C_i = sk_iss*R_reg + gamma*G
    ///         Q = pk_rec + beta*H hides pk_rec; the gamma*G blind in T hides
    ///         M_iss = C_i - r'*pk_rec (else any observer recovers it as C_i - T,
    ///         de-anonymising the private A2 issuer since msg.sender is public).
    struct IssuerReencProof {
        uint256 e;
        uint256 s_r;        // response for r'
        uint256 s_b;        // response for beta
        uint256 s_s;        // response for sk_iss
        uint256 s_g;        // response for gamma
        BN254.G1Point A1;   // k_r*G
        BN254.G1Point A2;   // k_r*H
        BN254.G1Point A3;   // k_r*Q - k_b*U + k_g*G
        BN254.G1Point A4;   // k_s*G
        BN254.G1Point A5;   // k_s*R_reg + k_g*G
        BN254.G1Point Q;    // pk_rec + beta*H            (blinded recipient key)
        BN254.G1Point U;    // r'*H
        BN254.G1Point T;    // T_hat = r'*pk_rec + gamma*G (blinds M_iss)
    }

    /// @notice Identity-targeted unilateral-A2 deposit coupling proof.  The
    ///         depositor proves knowledge of (m_rec, sk_dep, b) such that its
    ///         registered account is bound to the identity m_rec and the leaf's
    ///         eIss decrypts under m_rec to the point committed (hidden) in P_I.
    ///         Mirrors alberta_buck.wallet.unilateral_a2.DepositCouplingProof.
    struct DepositCouplingProof {
        uint256 e;
        uint256 s_m;        // response for m_rec (identity scalar)
        uint256 s_s;        // response for sk_dep (account key)
        uint256 s_b;        // response for b (P_I blind)
        BN254.G1Point A2;   // k_m*G + k_s*R_d
        BN254.G1Point A3;   // k_m*R_e - k_b*H
        BN254.G1Point A4;   // k_s*G
        BN254.G1Point P_I;  // M_I + b*H  (hides the decrypted issuer identity)
    }

    /// @notice B1 depositor binding proof (the dual of the A2 issuer binding).
    ///         A bearer-note depositor proves it re-encrypted its own registered
    ///         Identity M_dep under the public issuer's key pk_iss, bound to the
    ///         Identity of its payout account -- revealing nothing.  Mirrors
    ///         alberta_buck.wallet.b1_binding.DepositorBindingProof.
    struct DepositorBindingProof {
        uint256 e;
        uint256 s_m;        // response for m_dep (identity scalar)
        uint256 s_s;        // response for sk_dep (payout-account key)
        uint256 s_r;        // response for r (E_dep_for_iss randomness)
        uint256 s_b;        // response for b (P_dep blind)
        BN254.G1Point A2;   // k_m*G + k_s*R_d
        BN254.G1Point A4;   // k_s*G
        BN254.G1Point B1;   // k_r*G
        BN254.G1Point B2;   // k_m*G + k_r*pk_iss
        BN254.G1Point A_p;  // k_m*G + k_b*H            (P-relation commitment)
        BN254.G1Point P_dep;// M_dep + b*H             (blinded commitment of M_dep)
    }

    /// @notice Second generator H for the A2 binding -- a nothing-up-my-sleeve
    ///         point, H = keccak256("AlbertaBuck:IssuerReenc:H") (mod R) * G.
    ///         Mirrors alberta_buck.wallet.issuer_reenc.H_POINT.  Used only to
    ///         hide pk_rec in Q, so a known discrete log is acceptable.
    uint256 internal constant H_X =
        6790145969673496972519463000972766565107694238233578011858059027187477289586;
    uint256 internal constant H_Y =
        3372178911466361414640845512261989709787490420390555908180501907382229222644;

    /// @notice Depth of the registry-Identity Merkle accumulator.  Must match
    ///         the depth used by circuits/identity_membership.circom and the
    ///         Python alberta_buck.registry.tree.IdentityMerkleTree.
    uint8   public constant IDENTITY_TREE_DEPTH = 10;

    /// @notice Empty-subtree roots at each depth, precomputed as
    ///         ZERO_{d+1} = Poseidon([ZERO_d, ZERO_d]) with ZERO_0 = 0.
    ///         Matches alberta_buck.registry.tree.IdentityMerkleTree._zeros.
    uint256 internal constant ZERO_0  = 0;
    uint256 internal constant ZERO_1  = 14744269619966411208579211824598458697587494354926760081771325075741142829156;
    uint256 internal constant ZERO_2  = 7423237065226347324353380772367382631490014989348495481811164164159255474657;
    uint256 internal constant ZERO_3  = 11286972368698509976183087595462810875513684078608517520839298933882497716792;
    uint256 internal constant ZERO_4  = 3607627140608796879659380071776844901612302623152076817094415224584923813162;
    uint256 internal constant ZERO_5  = 19712377064642672829441595136074946683621277828620209496774504837737984048981;
    uint256 internal constant ZERO_6  = 20775607673010627194014556968476266066927294572720319469184847051418138353016;
    uint256 internal constant ZERO_7  = 3396914609616007258851405644437304192397291162432396347162513310381425243293;
    uint256 internal constant ZERO_8  = 21551820661461729022865262380882070649935529853313286572328683688269863701601;
    uint256 internal constant ZERO_9  = 6573136701248752079028194407151022595060682063033565181951145966236778420039;
    uint256 internal constant ZERO_10 = 12413880268183407374852357075976609371175688755676981206018884971008854919922;

    /// @notice Root of the empty tree (depth 10).  Equal to ZERO_10.
    uint256 public constant EMPTY_IDENTITY_ROOT = ZERO_10;

    /// @notice Convenience: the zero-value at each depth as a Solidity array
    ///         (can't be constant, so we return from a pure function).
    function IDENTITY_ZEROS(uint8 d) public pure returns (uint256) {
        if      (d == 0)  return ZERO_0;
        else if (d == 1)  return ZERO_1;
        else if (d == 2)  return ZERO_2;
        else if (d == 3)  return ZERO_3;
        else if (d == 4)  return ZERO_4;
        else if (d == 5)  return ZERO_5;
        else if (d == 6)  return ZERO_6;
        else if (d == 7)  return ZERO_7;
        else if (d == 8)  return ZERO_8;
        else if (d == 9)  return ZERO_9;
        else if (d == 10) return ZERO_10;
        revert("IdentityRegistry: depth out of range");
    }

    // ---- storage ------------------------------------------------------------

    address public governance;

    // Trusted issuer registry: keyed by issuer's Ethereum address.
    mapping(address => PSPubKey)  internal _trustedIssuers;
    mapping(address => bool)      public  isTrustedIssuer;

    // Per-account identity record.  An address is "verified" iff its pk has
    // been written -- both register() and bindContract() write _pk, so the
    // presence of a non-zero pk is the canonical signal.  isVerified() is
    // exposed as a view (selector-compatible with the prior public mapping)
    // so external callers and indexers see no ABI change.
    mapping(address => BN254.G1Point) internal _pk;
    mapping(address => ElGamalCT)     internal _E_addr;
    mapping(address => address)       public  issuerOf;

    /// @notice Marks a binding whose plaintext identity m is publicly disclosed
    ///         off-chain (e.g., Uniswap pair operated by a known counterparty).
    ///         The on-chain (pk, E_addr) record is identical in shape to an
    ///         encrypted-identity binding; the flag signals to indexers and
    ///         auditors that off-chain attestation pins m to a known operator.
    mapping(address => bool)          public  isPublicIdentity;

    /// @notice True if outflows from this address dispatch through the
    ///         demurrage Carrying path (recipient absorbs the proportional
    ///         age basis via `_demurrage[to]`).  Default is false (Non-
    ///         Carrying) for EOAs registered via register().  bindContract()
    ///         takes an explicit flag; service contracts (AMM pools, Notes,
    ///         the Jubilee fund) bind with isCarrying_=true; user-controlled
    ///         multisig / AA wallets bind with isCarrying_=false.
    mapping(address => bool)          public  isCarrying;

    /// @notice True once any counterparty has issued an identity-bound
    ///         approve naming this address as the spender.  Once true,
    ///         setIsCarrying() can no longer change isCarrying[a] -- the
    ///         flavour the recipient consented to is locked in.  The
    ///         freeze is one-way; there is no unfreeze.
    mapping(address => bool)          public  carryingFrozen;

    /// @notice The msg.sender of the bindContract() call that bound this
    ///         address.  Only the binder may call setIsCarrying() before
    ///         the flag is frozen by a counterparty's approve.  EOAs are
    ///         self-registered and have no binder (binderOf[eoa] == 0),
    ///         so setIsCarrying() can never target an EOA.
    mapping(address => address)       public  binderOf;

    /// @notice Authorised Buck contract -- the only address permitted to
    ///         call markApproved() to freeze the carrying flag.  Set once
    ///         by governance via setBuck() after Buck is deployed.
    address                           public  buck;

    /// @notice Registry-Identity Merkle accumulator root.  Updated on each
    ///         registration when the incremental accumulator is active
    ///         (identityPoseidon != address(0) and a non-zero identity leaf
    ///         is provided).  Also settable by governance for batch updates.
    ///         Consumed by the identity membership SNARK at Notes spend time
    ///         to prove "the counterparty identity M is a registered identity".
    ///         See alberta-buck-notes-identity-axis.org.
    uint256                           public  identityRoot;

    /// @notice Poseidon T3 hash contract for incremental Merkle tree updates.
    ///         Set by governance via setIdentityPoseidon.  When zero, the
    ///         incremental accumulator is disabled and identityRoot must be
    ///         managed via governance (setIdentityRoot).
    address                           public  identityPoseidon;

    /// @notice Number of identity leaves inserted into the incremental
    ///         accumulator.  Capped at 2**IDENTITY_TREE_DEPTH.
    uint32                            public  identityNextLeafIndex;

    /// @notice Tornado-style filled subtrees for the incremental accumulator.
    ///         filledSubtrees[d] is the rightmost known node at depth d.
    uint256[IDENTITY_TREE_DEPTH]      internal _identityFilledSubtrees;

    // ---- events -------------------------------------------------------------

    event GovernanceTransferred(address indexed previous, address indexed next);
    event IssuerTrusted(address indexed issuer);
    event IssuerRevoked(address indexed issuer);
    event Registered(address indexed account, address indexed issuer);
    event ContractBound(address indexed target, address indexed binder, bool isPublicIdentity);
    event BuckSet(address indexed buck);
    event CarryingFlagSet(address indexed target, bool isCarrying);
    event CarryingFrozen(address indexed target);
    event IdentityRootUpdated(uint256 indexed previous, uint256 indexed next);
    event IdentityPoseidonSet(address indexed previous, address indexed next);

    // ---- constructor / governance ------------------------------------------

    constructor(address _governance) {
        require(_governance != address(0), "governance=0");
        governance = _governance;
        // Initialize filled subtrees with empty-subtree roots.
        for (uint8 d = 0; d < IDENTITY_TREE_DEPTH; d++) {
            _identityFilledSubtrees[d] = IDENTITY_ZEROS(d);
        }
        emit GovernanceTransferred(address(0), _governance);
    }

    function transferGovernance(address next) external {
        require(msg.sender == governance, "not governance");
        require(next != address(0),       "governance=0");
        emit GovernanceTransferred(governance, next);
        governance = next;
    }

    function trustIssuer(address issuer, PSPubKey calldata pk) external {
        require(msg.sender == governance, "not governance");
        require(issuer != address(0),     "issuer=0");
        _trustedIssuers[issuer] = pk;
        isTrustedIssuer[issuer] = true;
        emit IssuerTrusted(issuer);
    }

    function revokeIssuer(address issuer) external {
        require(msg.sender == governance, "not governance");
        require(isTrustedIssuer[issuer],  "not trusted");
        isTrustedIssuer[issuer] = false;
        delete _trustedIssuers[issuer];
        emit IssuerRevoked(issuer);
    }

    /// @notice One-time governance setter for the authorised Buck contract.
    ///         Must be called once after Buck is deployed; the registry then
    ///         accepts markApproved() calls only from this address.
    function setBuck(address _buck) external {
        require(msg.sender == governance, "not governance");
        require(buck == address(0),       "buck already set");
        require(_buck != address(0),      "buck=0");
        buck = _buck;
        emit BuckSet(_buck);
    }

    /// @notice Post the current registry-Identity Merkle accumulator root.
    ///         Called by governance (or an authorised aggregator contract)
    ///         once per batch of registrations.  The new root must be non-zero.
    ///         Emits IdentityRootUpdated so off-chain indexers can track the
    ///         root history for membership proof generation.
    function setIdentityRoot(uint256 _root) external {
        require(msg.sender == governance, "not governance");
        require(_root != 0,               "root=0");
        emit IdentityRootUpdated(identityRoot, _root);
        identityRoot = _root;
    }

    /// @notice Set the Poseidon T3 contract used for incremental Merkle tree
    ///         updates.  When set to a non-zero address, register() and
    ///         bindContract() overloads that accept an identityLeaf parameter
    ///         will update the identityRoot incrementally.  Governance may
    ///         clear it (set to 0) to revert to governance-managed roots.
    function setIdentityPoseidon(address _poseidon) external {
        require(msg.sender == governance, "not governance");
        emit IdentityPoseidonSet(identityPoseidon, _poseidon);
        identityPoseidon = _poseidon;
    }

    // ---- incremental accumulator --------------------------------------------

    /// @dev Insert one identity leaf into the incremental Merkle tree and
    ///      return the new root.  Tornado-style: uses _identityFilledSubtrees
    ///      to track the rightmost node at each level.  The caller must ensure
    ///      identityPoseidon is set and the tree is not full.
    function _insertIdentityLeaf(uint256 leaf) internal returns (uint256) {
        uint256 index = identityNextLeafIndex;
        require(index < (uint256(1) << IDENTITY_TREE_DEPTH), "id tree full");
        uint256 current = leaf;
        for (uint8 d = 0; d < IDENTITY_TREE_DEPTH; d++) {
            if (index & 1 == 0) {
                // Left child: store current as the new filled node.
                _identityFilledSubtrees[d] = current;
                current = _hashPair(current, IDENTITY_ZEROS(d));
            } else {
                // Right child: fold with the stored left sibling.
                current = _hashPair(_identityFilledSubtrees[d], current);
            }
            index >>= 1;
        }
        identityNextLeafIndex++;
        return current;
    }

    /// @dev Wrapper around the Poseidon T3 contract call.  Reverts if the
    ///      poseidon contract is not set.
    function _hashPair(uint256 left, uint256 right) internal view returns (uint256) {
        address poseidonAddr = identityPoseidon;
        require(poseidonAddr != address(0), "poseidon not set");
        uint256[2] memory inputs;
        inputs[0] = left;
        inputs[1] = right;
        return IPoseidonT3(poseidonAddr).poseidon(inputs);
    }

    // ---- views --------------------------------------------------------------

    function pkOf(address account) external view returns (BN254.G1Point memory) {
        return _pk[account];
    }

    function ciphertextOf(address account) external view returns (ElGamalCT memory) {
        return _E_addr[account];
    }

    function trustedIssuerKey(address issuer) external view returns (PSPubKey memory) {
        return _trustedIssuers[issuer];
    }

    /// @dev True iff `a` has a registered (pk, E_addr) binding -- written by
    ///      both register() and bindContract().  Default G1 point is (0, 0)
    ///      (point at infinity); a non-zero coordinate means the slot has
    ///      been initialized.
    function _isRegistered(address a) internal view returns (bool) {
        BN254.G1Point storage k = _pk[a];
        return k.X != 0 || k.Y != 0;
    }

    /// @notice True iff `a` has registered an identity binding (EOA via
    ///         register() or contract via bindContract()).
    function isVerified(address a) external view returns (bool) {
        return _isRegistered(a);
    }

    // ---- registration ------------------------------------------------------

    /// @notice Register caller's identity binding under issuer-signed credential.
    ///         msg.sender is the registrant -- bound into the Fiat-Shamir
    ///         transcript so a proof valid for one address cannot be replayed
    ///         under another.  This overload does NOT update the identity
    ///         Merkle accumulator; use the 6-arg overload with identityLeaf.
    function register(
        address issuer,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        PSSig calldata sigma,
        RegistrationProof calldata proof
    ) external {
        _register(issuer, pk, E, sigma, proof, msg.sender, 0);
    }

    /// @notice Register with an identity Merkle leaf for incremental
    ///         accumulator update.  identityLeaf = Poseidon([M.x, M.y] % F_R)
    ///         where M is the identity point encrypted in E_addr.  Pass 0 to
    ///         skip the tree update (identical behaviour to the 5-arg overload).
    function register(
        address issuer,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        PSSig calldata sigma,
        RegistrationProof calldata proof,
        uint256 identityLeaf
    ) external {
        _register(issuer, pk, E, sigma, proof, msg.sender, identityLeaf);
    }

    /// @dev Shared registration logic.  If identityLeaf != 0 and the
    ///      incremental accumulator is active, inserts the leaf and updates
    ///      identityRoot.
    function _register(
        address issuer,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        PSSig calldata sigma,
        RegistrationProof calldata proof,
        address registrant,
        uint256 identityLeaf
    ) internal {
        require(!_isRegistered(registrant), "already registered");
        require(isTrustedIssuer[issuer],    "untrusted issuer");
        require(!BN254.isInfinity(sigma.sigma_1), "sigma_1=O");

        // (d) Fiat-Shamir
        require(proof.e == _fsRegister(sigma, E, pk, proof, registrant), "bad FS challenge");

        // (b) ElGamal C consistency: s_m*G + s_r*pk == e*C + T_C
        require(_checkElGamalC(proof.s_m, proof.s_r, pk, E.C, proof.T_C, proof.e), "bad NIZK C");

        // (c) ElGamal R consistency: s_r*G == e*R + T_R
        require(_checkElGamalR(proof.s_r, E.R, proof.T_R, proof.e), "bad NIZK R");

        // (a) PS pairing product
        require(_checkPSPairing(sigma, proof, _trustedIssuers[issuer]), "bad PS sig");

        _pk[registrant]     = pk;
        _E_addr[registrant] = E;
        issuerOf[registrant] = issuer;
        emit Registered(registrant, issuer);

        if (identityLeaf != 0 && identityPoseidon != address(0)) {
            uint256 newRoot = _insertIdentityLeaf(identityLeaf);
            emit IdentityRootUpdated(identityRoot, newRoot);
            identityRoot = newRoot;
        }
    }

    // ---- contract identity binding -----------------------------------------

    /// @notice Bind a (pk, E_addr) Identity to a deployed contract address.
    ///         No PSSig / NIZK is required: trust derives from atomic
    ///         deploy+bind (use `BuckAwareDeployer.deployAndBind` to deploy
    ///         and bind in a single transaction so no front-runner has a
    ///         window to register a competing binding before the operator).
    ///         For pre-existing contracts the first binder wins.
    /// @dev    The binding shape is identical to a self-registered EOA:
    ///         (pk, E_addr) is stored, isVerified is set true.  The
    ///         `isPublicIdentity_` flag records that the operator has chosen
    ///         to publicly disclose m off-chain (typical for AMM pools and
    ///         other BUCK-unaware contracts whose operator wants on-chain
    ///         counterparty audit trails to be openable on subpoena).  An
    ///         encrypted-identity binding (isPublicIdentity_ = false) is
    ///         supported by the same call but currently exercised only by
    ///         BUCK-aware contracts that ship the operator's off-chain
    ///         per-counterparty pre-approval flow (deferred).
    /// @dev    `isCarrying_` selects the demurrage transfer flavour for
    ///         outflows from this address.  Service contracts that hold
    ///         BUCK on behalf of others (Notes pool, AMM pools, the Jubilee
    ///         fund itself) bind with `isCarrying_=true` so recipients
    ///         absorb the proportional age basis on disbursement.  Multisig
    ///         and AA wallets that act on behalf of a single user bind
    ///         with `isCarrying_=false`.  msg.sender is recorded as the
    ///         binder; only the binder may later call setIsCarrying() to
    ///         change the flag, and only before any counterparty has
    ///         frozen it via approve().
    function bindContract(
        address target,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        bool isPublicIdentity_,
        bool isCarrying_
    ) external {
        _bindContract(target, pk, E, isPublicIdentity_, isCarrying_, 0);
    }

    /// @notice Bind with an identity Merkle leaf for incremental accumulator
    ///         update.  identityLeaf = Poseidon([M.x, M.y] % F_R) where M is
    ///         the identity point encrypted in E_addr.  Pass 0 to skip the
    ///         tree update (identical behaviour to the 5-arg overload).
    function bindContract(
        address target,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        bool isPublicIdentity_,
        bool isCarrying_,
        uint256 identityLeaf
    ) external {
        _bindContract(target, pk, E, isPublicIdentity_, isCarrying_, identityLeaf);
    }

    function _bindContract(
        address target,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        bool isPublicIdentity_,
        bool isCarrying_,
        uint256 identityLeaf
    ) internal {
        require(target.code.length > 0,  "target not a deployed contract");
        require(!_isRegistered(target),  "already bound");

        _pk[target]              = pk;
        _E_addr[target]          = E;
        isPublicIdentity[target] = isPublicIdentity_;
        isCarrying[target]       = isCarrying_;
        binderOf[target]         = msg.sender;
        emit ContractBound(target, msg.sender, isPublicIdentity_);
        emit CarryingFlagSet(target, isCarrying_);

        if (identityLeaf != 0 && identityPoseidon != address(0)) {
            uint256 newRoot = _insertIdentityLeaf(identityLeaf);
            emit IdentityRootUpdated(identityRoot, newRoot);
            identityRoot = newRoot;
        }
    }

    /// @notice Pre-approval reconfiguration of the carrying flag.  Only the
    ///         original binder may call this, and only while no counterparty
    ///         has yet issued an approve naming `target` as the spender.
    function setIsCarrying(address target, bool value) external {
        require(msg.sender == binderOf[target], "not binder");
        require(!carryingFrozen[target],        "carrying frozen by approval");
        isCarrying[target] = value;
        emit CarryingFlagSet(target, value);
    }

    /// @notice Freeze `spender`'s carrying flag.  Called from Buck.approve()
    ///         the first time a counterparty issues an identity-bound
    ///         approve naming `spender`; idempotent thereafter.
    function markApproved(address spender) external {
        require(msg.sender == buck, "only Buck");
        if (!carryingFrozen[spender]) {
            carryingFrozen[spender] = true;
            emit CarryingFrozen(spender);
        }
    }

    // ---- approve verification ----------------------------------------------

    /// @notice Verify Alice's Chaum-Pedersen proof of equal-plaintext re-encryption
    ///         that ``E_bob`` encrypts the same M as ``E_addr[sender]``.
    /// @dev    Reads E_alice from storage (caller cannot substitute), and binds
    ///         (sender, spender, chainid) into the transcript.
    function verifyApprove(
        address sender,
        address spender,
        ElGamalCT calldata E_bob,
        CPProof calldata pi
    ) external view returns (bool) {
        return _verifyApprove(sender, spender, E_bob, pi);
    }

    function _verifyApprove(
        address sender,
        address spender,
        ElGamalCT calldata E_bob,
        CPProof calldata pi
    ) internal view returns (bool) {
        if (!_isRegistered(sender) || !_isRegistered(spender)) return false;

        ElGamalCT memory E_a = _E_addr[sender];
        BN254.G1Point memory pkA = _pk[sender];
        BN254.G1Point memory pkB = _pk[spender];

        // Check 1: s2*G == T3 + e*R_b
        if (!BN254.eq(
            BN254.mul(BN254.g1(), pi.s2),
            BN254.add(pi.T3, BN254.mul(E_bob.R, pi.e))
        )) return false;

        // Check 2: s1*R_a - s2*pk_b == (T1 - T2) + e*(C_a - C_b)
        BN254.G1Point memory lhs2 = BN254.add(
            BN254.mul(E_a.R, pi.s1),
            BN254.neg(BN254.mul(pkB, pi.s2))
        );
        BN254.G1Point memory rhs2 = BN254.add(
            BN254.add(pi.T1, BN254.neg(pi.T2)),
            BN254.mul(BN254.add(E_a.C, BN254.neg(E_bob.C)), pi.e)
        );
        if (!BN254.eq(lhs2, rhs2)) return false;

        // Check 3: Fiat-Shamir
        return pi.e == _fsApprove(E_a, E_bob, pkA, pkB, pi, sender, spender, block.chainid);
    }

    // ---- public-issuer note binding (Notes mutual-decryptability, Phase 1) --

    /// @notice Verify a Schnorr signature by `issuer`'s registered identity key
    ///         over a note-batch commitment `hBatch` (= keccak256 of the minted
    ///         commitments).  This is the *issuer half* of the BUCK Notes
    ///         deferred-approve handshake for public issuers: it binds the
    ///         issuer's decrypted Identity to every leaf in the batch, so a
    ///         depositor can later produce a cryptographically sound receipt
    ///         naming the payer (see alberta-buck-notes-decryptability.org).
    /// @dev    `issuer` must be a registered *public* Identity: a bearer (B)
    ///         note's issuer must be public because the depositor is unknown at
    ///         mint, so the in-the-clear M is the only path to a receipt; A1
    ///         (addressed, public issuer) reuses the same binding.  pk_iss is
    ///         read from storage so a caller cannot substitute it, and
    ///         (issuer, chainid) are folded into the Fiat-Shamir transcript so a
    ///         signature is bound to this issuer and chain and cannot be replayed.
    function verifyIssuerSchnorr(
        address issuer,
        bytes32 hBatch,
        SchnorrProof calldata sig
    ) external view returns (bool) {
        if (!_isRegistered(issuer))    return false;
        if (!isPublicIdentity[issuer]) return false;

        BN254.G1Point memory pkIss = _pk[issuer];

        // Check 1: s*G == R + e*pk_iss
        //   s = k + e*sk_iss  =>  s*G = k*G + e*(sk_iss*G) = R + e*pk_iss.
        if (!BN254.eq(
            BN254.mul(BN254.g1(), sig.s),
            BN254.add(sig.R, BN254.mul(pkIss, sig.e))
        )) return false;

        // Check 2: Fiat-Shamir binds (pk_iss, R, hBatch, issuer, chainid).
        return sig.e == _fsIssuerSchnorr(pkIss, sig.R, hBatch, issuer, block.chainid);
    }

    // ---- A2 issuer re-encryption binding (Notes mutual-decryptability, Phase 2) --

    /// @notice Verify the recipient-blinded A2 issuer re-encryption binding: that
    ///         `eIss` (= E_iss-for-rec, the leaf ciphertext) re-encrypts the
    ///         `issuer`'s registered Identity under the recipient's key, without
    ///         revealing the recipient.  The issuer half of mutual decryptability
    ///         for the A2 flavor (addressed, private issuer); see
    ///         alberta-buck-notes-decryptability.org and
    ///         alberta_buck.wallet.issuer_reenc.
    /// @dev    Reads the issuer's registered `(pk_iss, E_reg) = (_pk, _E_addr)`
    ///         from storage so a caller cannot substitute either.  Checks the
    ///         five Okamoto relations via EIP-196 BN254 precompiles, then the
    ///         Fiat-Shamir challenge.  `pk_rec` never appears: the verifier sees
    ///         only the blinded `Q` and the uniform `U`, `T`.
    ///
    ///         This binds `eIss` to the key committed in `pi.Q`; proving that key
    ///         is the addressed recipient's (the `E_note` <-> `Q` coupling) is a
    ///         separate step keyed off the note ciphertext at spend time.
    function verifyIssuerReenc(
        address issuer,
        ElGamalCT calldata eIss,
        IssuerReencProof calldata pi
    ) external view returns (bool) {
        if (!_isRegistered(issuer)) return false;

        BN254.G1Point memory pkIss = _pk[issuer];
        ElGamalCT     memory E_reg = _E_addr[issuer];
        BN254.G1Point memory H     = BN254.G1Point(H_X, H_Y);

        // L1: s_r*G == A1 + e*R_i
        if (!BN254.eq(
            BN254.mul(BN254.g1(), pi.s_r),
            BN254.add(pi.A1, BN254.mul(eIss.R, pi.e))
        )) return false;

        // L2: s_r*H == A2 + e*U
        if (!BN254.eq(
            BN254.mul(H, pi.s_r),
            BN254.add(pi.A2, BN254.mul(pi.U, pi.e))
        )) return false;

        // L3: s_r*Q - s_b*U + s_g*G == A3 + e*T   (=> T = r'*pk_rec + gamma*G)
        if (!BN254.eq(
            BN254.add(
                BN254.add(BN254.mul(pi.Q, pi.s_r), BN254.neg(BN254.mul(pi.U, pi.s_b))),
                BN254.mul(BN254.g1(), pi.s_g)
            ),
            BN254.add(pi.A3, BN254.mul(pi.T, pi.e))
        )) return false;

        // L4: s_s*G == A4 + e*pk_iss
        if (!BN254.eq(
            BN254.mul(BN254.g1(), pi.s_s),
            BN254.add(pi.A4, BN254.mul(pkIss, pi.e))
        )) return false;

        // L5: s_s*R_reg + s_g*G == A5 + e*(C_reg + T - C_i)
        BN254.G1Point memory Y =
            BN254.add(E_reg.C, BN254.add(pi.T, BN254.neg(eIss.C)));
        if (!BN254.eq(
            BN254.add(BN254.mul(E_reg.R, pi.s_s), BN254.mul(BN254.g1(), pi.s_g)),
            BN254.add(pi.A5, BN254.mul(Y, pi.e))
        )) return false;

        // Fiat-Shamir
        return pi.e == _fsIssuerReenc(pkIss, E_reg, eIss, pi, issuer, block.chainid);
    }

    // ---- internal verifier helpers (factored to manage stack depth) ---------

    function _fsIssuerReenc(
        BN254.G1Point memory pkIss,
        ElGamalCT memory E_reg,
        ElGamalCT calldata eIss,
        IssuerReencProof calldata pi,
        address issuer,
        uint256 chainid
    ) internal pure returns (uint256) {
        BN254.G1Point[] memory pts = new BN254.G1Point[](13);
        pts[0]  = pkIss;
        pts[1]  = E_reg.R;
        pts[2]  = E_reg.C;
        pts[3]  = eIss.R;
        pts[4]  = eIss.C;
        pts[5]  = pi.Q;
        pts[6]  = pi.U;
        pts[7]  = pi.T;
        pts[8]  = pi.A1;
        pts[9]  = pi.A2;
        pts[10] = pi.A3;
        pts[11] = pi.A4;
        pts[12] = pi.A5;
        uint256[] memory scl = new uint256[](2);
        scl[0] = uint256(uint160(issuer));
        scl[1] = chainid;
        return BN254.fsChallenge(pts, scl);
    }

    /// @notice Verify an identity-targeted unilateral-A2 deposit coupling.  Reads
    ///         the depositor's registered (pk_dep, E_addr) and confirms the three
    ///         Okamoto relations -- without learning any identity (m_rec, M_rec
    ///         and M_I all stay hidden; M_I is committed, blinded, in P_I).  The
    ///         companion membership proof of P_I's point in the identity tree
    ///         (off chain / SNARK) closes the collusion gate.  Mirrors
    ///         alberta_buck.wallet.unilateral_a2.deposit_couple_verify.
    function verifyDepositCoupling(
        address depositor,
        ElGamalCT calldata eIss,
        DepositCouplingProof calldata pi
    ) external view returns (bool) {
        if (!_isRegistered(depositor)) return false;

        BN254.G1Point memory pkDep = _pk[depositor];
        ElGamalCT     memory E_dep = _E_addr[depositor];
        BN254.G1Point memory H     = BN254.G1Point(H_X, H_Y);

        // E4: s_s*G == A4 + e*pk_dep            (sk_dep is the real account key)
        if (!BN254.eq(
            BN254.mul(BN254.g1(), pi.s_s),
            BN254.add(pi.A4, BN254.mul(pkDep, pi.e))
        )) return false;

        // E2: s_m*G + s_s*R_d == A2 + e*C_d     (account bound to M_rec = m_rec*G)
        if (!BN254.eq(
            BN254.add(BN254.mul(BN254.g1(), pi.s_m), BN254.mul(E_dep.R, pi.s_s)),
            BN254.add(pi.A2, BN254.mul(E_dep.C, pi.e))
        )) return false;

        // E3: s_m*R_e - s_b*H == A3 + e*(C_e - P_I)   (eIss decrypts under m_rec)
        BN254.G1Point memory X = BN254.add(eIss.C, BN254.neg(pi.P_I));
        if (!BN254.eq(
            BN254.add(BN254.mul(eIss.R, pi.s_m), BN254.neg(BN254.mul(H, pi.s_b))),
            BN254.add(pi.A3, BN254.mul(X, pi.e))
        )) return false;

        // Fiat-Shamir
        return pi.e == _fsDepositCoupling(pkDep, E_dep, eIss, pi, depositor, block.chainid);
    }

    function _fsDepositCoupling(
        BN254.G1Point memory pkDep,
        ElGamalCT memory E_dep,
        ElGamalCT calldata eIss,
        DepositCouplingProof calldata pi,
        address depositor,
        uint256 chainid
    ) internal pure returns (uint256) {
        BN254.G1Point[] memory pts = new BN254.G1Point[](9);
        pts[0] = pkDep;
        pts[1] = E_dep.R;
        pts[2] = E_dep.C;
        pts[3] = eIss.R;
        pts[4] = eIss.C;
        pts[5] = pi.P_I;
        pts[6] = pi.A2;
        pts[7] = pi.A3;
        pts[8] = pi.A4;
        uint256[] memory scl = new uint256[](2);
        scl[0] = uint256(uint160(depositor));
        scl[1] = chainid;
        return BN254.fsChallenge(pts, scl);
    }

    /// @notice Verify a B1 depositor binding.  Reads the depositor's registered
    ///         (pk_dep, E_addr) and the public issuer's pk_iss, and confirms the
    ///         four Okamoto relations -- without learning any Identity (M_dep
    ///         stays hidden from all but the issuer, who decrypts eDepForIss).
    ///         Mirrors alberta_buck.wallet.b1_binding.b1_bind_verify.
    function verifyDepositorBinding(
        address depositor,
        address issuer,
        ElGamalCT calldata eDepForIss,
        DepositorBindingProof calldata pi
    ) external view returns (bool) {
        if (!_isRegistered(depositor) || !_isRegistered(issuer)) return false;

        BN254.G1Point memory pkDep = _pk[depositor];
        ElGamalCT     memory E_dep = _E_addr[depositor];
        BN254.G1Point memory pkIss = _pk[issuer];

        // E4: s_s*G == A4 + e*pk_dep
        if (!BN254.eq(
            BN254.mul(BN254.g1(), pi.s_s),
            BN254.add(pi.A4, BN254.mul(pkDep, pi.e))
        )) return false;

        // E2: s_m*G + s_s*R_d == A2 + e*C_d     (payout account bound to M_dep)
        if (!BN254.eq(
            BN254.add(BN254.mul(BN254.g1(), pi.s_m), BN254.mul(E_dep.R, pi.s_s)),
            BN254.add(pi.A2, BN254.mul(E_dep.C, pi.e))
        )) return false;

        // F1: s_r*G == B1 + e*R_f
        if (!BN254.eq(
            BN254.mul(BN254.g1(), pi.s_r),
            BN254.add(pi.B1, BN254.mul(eDepForIss.R, pi.e))
        )) return false;

        // F2: s_m*G + s_r*pk_iss == B2 + e*C_f  (eDepForIss encrypts M_dep)
        if (!BN254.eq(
            BN254.add(BN254.mul(BN254.g1(), pi.s_m), BN254.mul(pkIss, pi.s_r)),
            BN254.add(pi.B2, BN254.mul(eDepForIss.C, pi.e))
        )) return false;

        // P: s_m*G + s_b*H == A_p + e*P_dep  (P_dep = m_dep*G + b*H, same m_dep)
        {
            BN254.G1Point memory H = BN254.G1Point(H_X, H_Y);
            if (!BN254.eq(
                BN254.add(BN254.mul(BN254.g1(), pi.s_m), BN254.mul(H, pi.s_b)),
                BN254.add(pi.A_p, BN254.mul(pi.P_dep, pi.e))
            )) return false;
        }

        // Fiat-Shamir
        return pi.e == _fsDepositorBinding(pkDep, E_dep, pkIss, eDepForIss, pi,
                                           depositor, block.chainid);
    }

    function _fsDepositorBinding(
        BN254.G1Point memory pkDep,
        ElGamalCT memory E_dep,
        BN254.G1Point memory pkIss,
        ElGamalCT calldata eDepForIss,
        DepositorBindingProof calldata pi,
        address depositor,
        uint256 chainid
    ) internal pure returns (uint256) {
        BN254.G1Point[] memory pts = new BN254.G1Point[](12);
        pts[0] = pkDep;
        pts[1] = E_dep.R;
        pts[2] = E_dep.C;
        pts[3] = pkIss;
        pts[4] = eDepForIss.R;
        pts[5] = eDepForIss.C;
        pts[6] = pi.A2;
        pts[7] = pi.A4;
        pts[8] = pi.B1;
        pts[9] = pi.B2;
        pts[10] = pi.A_p;
        pts[11] = pi.P_dep;
        uint256[] memory scl = new uint256[](2);
        scl[0] = uint256(uint160(depositor));
        scl[1] = chainid;
        return BN254.fsChallenge(pts, scl);
    }

    function _fsRegister(
        PSSig calldata sigma,
        ElGamalCT calldata E,
        BN254.G1Point calldata pk,
        RegistrationProof calldata proof,
        address registrant
    ) internal pure returns (uint256) {
        BN254.G1Point[] memory pts = new BN254.G1Point[](8);
        pts[0] = sigma.sigma_1;
        pts[1] = sigma.sigma_2;
        pts[2] = E.R;
        pts[3] = E.C;
        pts[4] = pk;
        pts[5] = proof.A_ps;
        pts[6] = proof.T_C;
        pts[7] = proof.T_R;
        uint256[] memory scl = new uint256[](1);
        scl[0] = uint256(uint160(registrant));
        return BN254.fsChallenge(pts, scl);
    }

    function _fsApprove(
        ElGamalCT memory E_a,
        ElGamalCT calldata E_b,
        BN254.G1Point memory pkA,
        BN254.G1Point memory pkB,
        CPProof calldata pi,
        address sender,
        address spender,
        uint256 chainid
    ) internal pure returns (uint256) {
        BN254.G1Point[] memory pts = new BN254.G1Point[](9);
        pts[0] = E_a.R;
        pts[1] = E_a.C;
        pts[2] = E_b.R;
        pts[3] = E_b.C;
        pts[4] = pkA;
        pts[5] = pkB;
        pts[6] = pi.T1;
        pts[7] = pi.T2;
        pts[8] = pi.T3;
        uint256[] memory scl = new uint256[](3);
        scl[0] = uint256(uint160(sender));
        scl[1] = uint256(uint160(spender));
        scl[2] = chainid;
        return BN254.fsChallenge(pts, scl);
    }

    /// @dev Fiat-Shamir challenge for the public-issuer Schnorr binding.
    ///      Order: points (pk_iss, R) then scalars (hBatch, issuer, chainid).
    ///      Must match alberta_buck.wallet.schnorr byte-for-byte.
    function _fsIssuerSchnorr(
        BN254.G1Point memory pkIss,
        BN254.G1Point memory R,
        bytes32 hBatch,
        address issuer,
        uint256 chainid
    ) internal pure returns (uint256) {
        BN254.G1Point[] memory pts = new BN254.G1Point[](2);
        pts[0] = pkIss;
        pts[1] = R;
        uint256[] memory scl = new uint256[](3);
        scl[0] = uint256(hBatch);
        scl[1] = uint256(uint160(issuer));
        scl[2] = chainid;
        return BN254.fsChallenge(pts, scl);
    }

    function _checkElGamalC(
        uint256 s_m,
        uint256 s_r,
        BN254.G1Point calldata pk,
        BN254.G1Point calldata C,
        BN254.G1Point calldata T_C,
        uint256 e
    ) internal view returns (bool) {
        BN254.G1Point memory lhs = BN254.add(BN254.mul(BN254.g1(), s_m), BN254.mul(pk, s_r));
        BN254.G1Point memory rhs = BN254.add(BN254.mul(C, e), T_C);
        return BN254.eq(lhs, rhs);
    }

    function _checkElGamalR(
        uint256 s_r,
        BN254.G1Point calldata R,
        BN254.G1Point calldata T_R,
        uint256 e
    ) internal view returns (bool) {
        BN254.G1Point memory lhs = BN254.mul(BN254.g1(), s_r);
        BN254.G1Point memory rhs = BN254.add(BN254.mul(R, e), T_R);
        return BN254.eq(lhs, rhs);
    }

    /// @dev PS pairing product:
    ///   e(s_m*sigma_1, Y) * e(-A_ps, Y) * e(e*sigma_1, X) * e(-e*sigma_2, g_2) == 1
    function _checkPSPairing(
        PSSig calldata sigma,
        RegistrationProof calldata proof,
        PSPubKey storage ipk
    ) internal view returns (bool) {
        BN254.G1Point[] memory a = new BN254.G1Point[](4);
        BN254.G2Point[] memory b = new BN254.G2Point[](4);
        a[0] = BN254.mul(sigma.sigma_1, proof.s_m);
        b[0] = ipk.Y;
        a[1] = BN254.neg(proof.A_ps);
        b[1] = ipk.Y;
        a[2] = BN254.mul(sigma.sigma_1, proof.e);
        b[2] = ipk.X;
        a[3] = BN254.neg(BN254.mul(sigma.sigma_2, proof.e));
        b[3] = BN254.g2();
        return BN254.pairingCheck(a, b);
    }
}
