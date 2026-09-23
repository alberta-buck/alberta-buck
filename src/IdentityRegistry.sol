// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BN254} from "./BN254.sol";
import {IContractBindingAdapter} from "./IContractBindingAdapter.sol";
import {IPoseidonT3} from "./IPoseidonT3.sol";
import {IPoseidonT4} from "./IPoseidonT4.sol";

/// @title IdentityRegistry — on-chain registry of identity-bound public keys.
/// @notice Each Ethereum address binds to (pk, E_addr) where:
///         * pk     = ElGamal recipient public key (G1)
///         * E_addr = (R, C) = ElGamal ciphertext of the identity point M = m*G,
///                    encrypted under pk and witnessed by an issuer-signed
///                    Pointcheval-Sanders credential shown in HIDING form.
///         The registrant publishes a presentation (A, B) = (a*sigma_1,
///         a*sigma_2 + b*Y1) of its credential -- a uniform G1 pair that is not
///         a signature anyone can verify or re-present -- and a registration
///         NIZK proving, without revealing m, b, r or sk, that (A, B) presents
///         a valid credential on m, that E_addr really encrypts m, and that
///         the registrant holds sk for pk.  (A rerandomized signature, the
///         previous design, was candidate-testable by anyone knowing m.)
///         A Chaum-Pedersen NIZK proves a re-encryption sends the same M to a
///         second registered recipient AND that the sender holds the registered
///         account key (used by Buck.approve()).
contract IdentityRegistry {

    // ---- types --------------------------------------------------------------

    /// @notice Issuer key: (X, Y) = (x*G2, y*G2) plus the G1 image Y1 = y*G that
    ///         holders blind with.  trustIssuer checks e(Y1, g_2) == e(G, Y).
    struct PSPubKey {
        BN254.G2Point X;
        BN254.G2Point Y;
        BN254.G1Point Y1;
    }

    struct ElGamalCT {
        BN254.G1Point R;
        BN254.G1Point C;
    }

    /// @notice Hiding presentation of a PS credential (matches
    ///         alberta_buck.wallet.ps.PSPresentation): A = a*sigma_1,
    ///         B = (x + m*y)*A + b*Y1 for fresh secret a, b.
    struct PSPresentation {
        BN254.G1Point A;
        BN254.G1Point B;
    }

    /// @notice Registration NIZK proof (matches alberta_buck.wallet.nizk).
    ///         One commitment C1 covers both credential exponents (m, b); the
    ///         account-key relation pk = sk*G (T_key / s_sk) stops a NUMS
    ///         public key from registering.
    struct RegistrationProof {
        uint256 e;
        uint256 s_m;          // response for m:  m_tilde + e*m
        uint256 s_b;          // response for b:  b_tilde + e*b
        uint256 s_r;          // response for r:  r_tilde + e*r
        uint256 s_sk;         // response for sk: sk_tilde + e*sk
        BN254.G1Point C1;     // credential commitment: m_tilde*A + b_tilde*G
        BN254.G1Point T_C;    // ElGamal C commitment: m_tilde*G + r_tilde*pk
        BN254.G1Point T_R;    // ElGamal R commitment: r_tilde * G
        BN254.G1Point T_key;  // account-key commitment: sk_tilde * G
    }

    /// @notice Holder authorization for an exact contract-binding policy.
    ///         This prevents a bearer registration proof from being copied
    ///         and submitted first with different public/carrying flags.
    struct ContractBindingProof {
        uint256 e;
        uint256 s;
        BN254.G1Point T;
    }

    /// @notice 6-element Chaum-Pedersen proof (matches alberta_buck.wallet.chaum_pedersen).
    ///         ABI is unchanged; the three T fields are reinterpreted as the
    ///         three-relation commitments T_key, T_diff, T_R (see _verifyApprove).
    struct CPProof {
        uint256 e;
        uint256 s1;           // u = a + e*sk
        uint256 s2;           // v = b + e*r'
        BN254.G1Point T1;     // T_key  = a*G
        BN254.G1Point T2;     // T_diff = a*R_a - b*pk_b
        BN254.G1Point T3;     // T_R    = b*G
    }

    /// @notice Schnorr signature over a note-batch commitment by an issuer's
    ///         registered identity key -- the public-issuer half of the BUCK
    ///         Notes deferred-approve handshake (mutual-decryptability, Phase 1;
    ///         see alberta-buck-notes.org "The Non-Deniable-Receipt Invariant").  Matches
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
        BN254.G1Point A3;   // k_r*Q - k_b*U + k_g*H
        BN254.G1Point A4;   // k_s*G
        BN254.G1Point A5;   // k_s*R_reg + k_g*H
        BN254.G1Point Q;    // pk_rec + beta*H            (blinded recipient key)
        BN254.G1Point U;    // r'*H
        BN254.G1Point T;    // r'*pk_rec + gamma*H        (blinds M_iss; in idHash_a2)
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

    /// @notice The one hiding generator: hashed to the curve under
    ///         `AlbertaBuck/Pedersen/H/v2` rather than multiplied out of G, so
    ///         nobody knows its discrete log.  Mirrors
    ///         alberta_buck.wallet.nums.H_PEDERSEN.
    ///
    ///         Every blind in this contract sits on it, because every blind here
    ///         must BIND.  B1 publishes P_dep = M_dep + b*H and proves two things
    ///         about it -- this sigma opens it as m_dep*G + b*H, a membership
    ///         proof opens it as M + b'*H -- and with a known h = log_G(H) those
    ///         openings need not agree: a depositor holding any registered
    ///         identity scalar m' sets b' = b + (m_dep - m')/h and spends
    ///         unregistered.  The A2 mint binding blinds its key commitment Q and
    ///         its tie point T here for the same reason: on a known-log base, a
    ///         minter could key one ciphertext to two issuers.
    uint256 internal constant H_PED_X =
        4615963717079593411766916164683734882826555887582375840543993270654189828194;
    uint256 internal constant H_PED_Y =
        13797334798855956307628720739803197698357468544121783721646444392020868779330;

    /// @dev Fiat-Shamir protocol domains, one per transcript this contract
    ///      verifies: full keccak words, not reduced mod R.  Transcript
    ///      metadata, not contract API.  The v2 convention is one tag per
    ///      transcript, `AlbertaBuck/<Area>/<Name>/v2`; without one, a
    ///      transcript of one sigma protocol is structurally a candidate
    ///      transcript of another.  Mirrored by alberta_buck/wallet/domains.py
    ///      and core/rust/buck-identity/src/domains.rs.
    uint256 internal constant REGISTER_DOMAIN = uint256(
        keccak256("AlbertaBuck/FiatShamir/IdentityRegistry/Register/v2")
    );
    uint256 internal constant APPROVE_DOMAIN = uint256(
        keccak256("AlbertaBuck/FiatShamir/IdentityRegistry/Approve/v2")
    );
    uint256 internal constant ISSUER_SCHNORR_DOMAIN = uint256(
        keccak256("AlbertaBuck/FiatShamir/IdentityRegistry/IssuerSchnorr/v2")
    );
    uint256 internal constant ISSUER_REENC_DOMAIN = uint256(
        keccak256("AlbertaBuck/FiatShamir/IdentityRegistry/IssuerReenc/v2")
    );
    uint256 internal constant DEPOSITOR_BINDING_DOMAIN = uint256(
        keccak256("AlbertaBuck/FiatShamir/IdentityRegistry/DepositorBinding/v2")
    );

    uint256 public constant CONTRACT_BINDING_DOMAIN = uint256(
        keccak256("AlbertaBuck/FiatShamir/IdentityRegistry/ContractBinding/v2")
    );

    /// @notice Depth of the aggregator tree, whose root is `identityRoot`.
    /// @dev    Its leaves are subtree roots, one per enrolled subtree, at the
    ///         slot the root authority assigned.  Twenty, because authorities
    ///         are a population rather than a roster: clubs, community boards,
    ///         congregations and delegated sub-regulators are all attribute
    ///         authorities, and 2**10 = 1024 subtrees is the wrong order of
    ///         magnitude.  Python and Rust: AGGREGATOR_DEPTH.
    uint8   public constant IDENTITY_TREE_DEPTH = 20;

    /// @notice Depth of a membership path through an identity registry: the
    ///         registry's subtree (12) and then the aggregator (20).  Every
    ///         level folds with the same Poseidon, so the circuits prove it as
    ///         one path (accumulator specification, section 11.1).
    uint8   public constant MEMBERSHIP_PATH_DEPTH = 32;

    /// @notice Root records the ring retains.  Ten days at an hourly posting,
    ///         which covers the longest maximum age a consumer declares.
    uint256 public constant ROOT_RING_SIZE = 256;

    /// @notice The maximum root age of a consumer that has not declared one.
    uint32  public constant DEFAULT_MAX_ROOT_AGE = 7 days;

    /// @notice The consumers the protocol names.  Each bounds the age of the
    ///         roots it accepts, because tolerance for proofs in flight wants a
    ///         long window and revocation urgency a short one.
    bytes32 public constant CONSUMER_NOTES_MEMBERSHIP =
        keccak256("AlbertaBuck/Accumulator/Consumer/NotesMembership/v2");
    bytes32 public constant CONSUMER_INSURER_ATTESTATION =
        keccak256("AlbertaBuck/Accumulator/Consumer/InsurerAttestation/v2");

    /// @notice keccak("AlbertaBuck/Accumulator/Leaf/Identity/v2") mod F_R: the
    ///         leading input of the public identity leaf Poseidon(TAG, M.x, M.y).
    uint256 public constant LEAF_TAG_IDENTITY =
        6089190410636387123103508027202099632965250507789735297680287822653321493477;

    uint256 internal constant FIELD_R =
        21888242871839275222246405745257275088548364400416034343698204186575808495617;

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

    /// @notice The certified operator responsible for this binding. This is
    ///         msg.sender on direct binds and the adapter-authenticated
    ///         operator on adapter binds. Only that operator may call
    ///         setIsCarrying() before the flag is frozen by a counterparty's
    ///         approve. EOAs are self-registered and have no binder
    ///         (binderOf[eoa] == 0), so setIsCarrying() cannot target an EOA.
    mapping(address => address)       public  binderOf;

    /// @notice Exact, one-shot binding authorization recorded by a target
    ///         contract for an already-certified operator. The target itself
    ///         must publish this commitment before bindContract can consume it.
    mapping(address => bytes32)       public  pendingBindingAuthorization;

    /// @notice Narrow, governance-audited adapters permitted to bind contracts
    ///         whose provenance and authority semantics they validate.
    mapping(address => bool)          public  isBindingAdapter;
    /// @notice Provenance address recorded when an adapter is approved.
    mapping(address => address)       public  bindingAdapterProvenance;

    /// @notice Authorised Buck contract -- the only address permitted to
    ///         call markApproved() to freeze the carrying flag.  Set once
    ///         by governance via setBuck() after Buck is deployed.
    address                           public  buck;

    /// @notice The most recently posted aggregator root.  A consumer checks a
    ///         proof against any root the ring retains, within its own maximum
    ///         age (acceptsRoot); this is only the newest of them.
    uint256                           public  identityRoot;

    /// @notice Poseidon T3 contract: hashes Merkle nodes.  Set by governance.
    address                           public  identityPoseidon;

    /// @notice Poseidon T4 contract: hashes the public identity leaf
    ///         Poseidon(TAG, M.x, M.y).  Set by governance.
    address                           public  identityPoseidonT4;

    /// @notice The deployment's root authority (accumulator specification,
    ///         section 17.1): it enrolls and evicts subtrees and appoints the
    ///         aggregator.  One address; as decentralized as whoever holds it.
    address                           public  rootAuthority;

    /// @notice The aggregator, appointed by the root authority: the only
    ///         address that posts roots.  Accountable rather than trustless --
    ///         each posting names the hash of the leaf list it combined.
    address                           public  aggregator;

    /// @notice One enrolled subtree.  An authority with several predicates
    ///         enrolls each of its subtrees, under one posting key.
    struct Subtree {
        uint32  slot;          // its leaf in the aggregator tree
        uint8   depth;         // its own depth
        bool    enrolled;
        bool    isPublic;      // public: membership provable by a plain path
        address poster;        // the authority's posting key
    }

    /// @notice Enrolled subtrees, keyed by keccak256 of the namespaced name.
    mapping(bytes32 => Subtree)       public  subtrees;

    /// @notice slot + 1 -> the subtree enrolled there (0 when free).
    mapping(uint32 => bytes32)        public  subtreeAtSlot;

    /// @notice The root ring: the last ROOT_RING_SIZE postings, by sequence.
    uint256[ROOT_RING_SIZE]           internal _rootRing;

    /// @notice Postings so far; the next posting's sequence.
    uint64                            public  rootSequence;

    /// @notice When each retained root was (most recently) posted; 0 if never
    ///         posted or evicted.
    mapping(uint256 => uint64)        public  rootPostedAt;

    /// @notice The sequence of each retained root's most recent posting, so
    ///         evicting an older posting of a re-posted root keeps it.
    mapping(uint256 => uint64)        internal _rootLatestSequence;

    /// @notice Declared maximum root ages, per consumer; 0 means the default.
    mapping(bytes32 => uint32)        internal _maxRootAge;

    // ---- events -------------------------------------------------------------

    event GovernanceTransferred(address indexed previous, address indexed next);
    event IssuerTrusted(address indexed issuer);
    event IssuerRevoked(address indexed issuer);
    event Registered(address indexed account, address indexed issuer);
    event ContractBound(address indexed target, address indexed binder, bool isPublicIdentity);
    event BuckSet(address indexed buck);
    event CarryingFlagSet(address indexed target, bool isCarrying);
    event CarryingFrozen(address indexed target);
    event IdentityRootPosted(
        uint256 indexed root, uint64 indexed sequence, uint64 postedAt, bytes32 leafListHash
    );
    event IdentityPoseidonSet(address indexed previous, address indexed next);
    event IdentityPoseidonT4Set(address indexed previous, address indexed next);
    event RootAuthoritySet(address indexed previous, address indexed next);
    event AggregatorSet(address indexed previous, address indexed next);
    event SubtreeEnrolled(
        bytes32 indexed id, uint32 slot, uint8 depth, bool isPublic, address poster, string publication
    );
    event SubtreeEvicted(bytes32 indexed id, uint32 slot);
    event MaxRootAgeSet(bytes32 indexed consumer, uint32 maxAge);
    event BindingAdapterSet(
        address indexed adapter,
        address indexed provenance,
        bool approved
    );
    event ContractBindingAuthorized(
        address indexed target,
        address indexed binder,
        bytes32 indexed authorization
    );
    event ContractBindingAuthorizationRevoked(address indexed target);

    // ---- constructor / governance ------------------------------------------

    constructor(address _governance) {
        require(_governance != address(0), "governance=0");
        governance = _governance;
        // The insurer attests against a fresh root: the aggregator posts at
        // least daily (accumulator specification, section 16.2).
        _maxRootAge[CONSUMER_INSURER_ATTESTATION] = 1 days;
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
        require(!BN254.isInfinity(pk.Y1), "Y1=O");
        // e(Y1, g_2) * e(-G, Y) == 1  <=>  Y1 = y*G for the same y as Y = y*G2.
        // A wrong Y1 cannot help a forger (the verifier never uses it); it
        // would only make every honest presentation fail, so refuse it here.
        {
            BN254.G1Point[] memory a = new BN254.G1Point[](2);
            BN254.G2Point[] memory b = new BN254.G2Point[](2);
            a[0] = pk.Y1;
            b[0] = BN254.g2();
            a[1] = BN254.neg(BN254.g1());
            b[1] = pk.Y;
            require(BN254.pairingCheck(a, b), "Y1 inconsistent");
        }
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

    /// @notice Approve or revoke a narrowly audited binding adapter.
    /// @dev Approval is meaningful only together with review of the adapter's
    ///      provenance checks and binding-authority semantics. Revocation does
    ///      not call adapter code, so governance can always remove approval.
    function setBindingAdapter(address adapter, bool approved) external {
        require(msg.sender == governance, "not governance");
        address provenance = bindingAdapterProvenance[adapter];
        if (approved) {
            require(adapter.code.length > 0, "adapter not a contract");
            IContractBindingAdapter bindingAdapter = IContractBindingAdapter(adapter);
            require(bindingAdapter.registry() == address(this), "adapter registry mismatch");
            provenance = bindingAdapter.provenance();
            require(provenance.code.length > 0, "provenance not a contract");
            bindingAdapterProvenance[adapter] = provenance;
        }
        isBindingAdapter[adapter] = approved;
        emit BindingAdapterSet(adapter, provenance, approved);
    }

    // ---- the accumulator root: authority, enrollment, posting ---------------
    //
    // Admission is never a registry call.  register and bindContract refuse a
    // caller-supplied identityLeaf, permanently -- a decision, not a stopgap
    // (accumulator specification, section 6): an on-chain register that took a
    // caller's leaf would take ANY leaf, which is the opposite of what the
    // folded gates' relation (3) relies on.  An authority admits a leaf to its
    // own subtree, the aggregator composes the subtree roots, and posts the
    // result here.  That holds for receiving and mailbox leaves too.

    /// @notice Appoint the root authority.  Governance only.
    function setRootAuthority(address next) external {
        require(msg.sender == governance, "not governance");
        emit RootAuthoritySet(rootAuthority, next);
        rootAuthority = next;
    }

    /// @notice Appoint the aggregator.  Root authority only.
    function setAggregator(address next) external {
        require(msg.sender == rootAuthority, "not root authority");
        emit AggregatorSet(aggregator, next);
        aggregator = next;
    }

    /// @notice Enroll a subtree at an aggregator slot.  Root authority only.
    ///         `id` is keccak256 of the subtree's namespaced name; `poster` is
    ///         the owning authority's posting key, which signs the subtree roots
    ///         the aggregator combines; `publication` says where the aggregator's
    ///         leaf lists are published, so omission and forgery are provable
    ///         (accumulator specification, sections 17.2 and 17.3).
    function enrollSubtree(
        bytes32 id,
        uint32 slot,
        uint8 depth,
        bool isPublic,
        address poster,
        string calldata publication
    ) external {
        require(msg.sender == rootAuthority,              "not root authority");
        require(id != bytes32(0),                         "subtree id=0");
        require(!subtrees[id].enrolled,                   "subtree enrolled");
        require(slot < (uint256(1) << IDENTITY_TREE_DEPTH), "slot out of range");
        require(subtreeAtSlot[slot + 1] == bytes32(0),    "slot taken");
        require(depth > 0 && depth <= 32,                 "bad depth");
        require(poster != address(0),                     "poster=0");
        subtrees[id] = Subtree(slot, depth, true, isPublic, poster);
        subtreeAtSlot[slot + 1] = id;
        emit SubtreeEnrolled(id, slot, depth, isPublic, poster, publication);
    }

    /// @notice Evict a subtree, freeing its slot.  Root authority only.  Its
    ///         members' proofs age out of every consumer with the roots that
    ///         still contain it.
    function evictSubtree(bytes32 id) external {
        require(msg.sender == rootAuthority, "not root authority");
        Subtree memory t = subtrees[id];
        require(t.enrolled, "not enrolled");
        delete subtreeAtSlot[t.slot + 1];
        delete subtrees[id];
        emit SubtreeEvicted(id, t.slot);
    }

    /// @notice Post an aggregator root.  Aggregator only.  `leafListHash` is the
    ///         hash of the canonical document listing every leaf combined, each
    ///         with the signature that authorized it, so the aggregator cannot
    ///         show different lists to different readers.
    function postIdentityRoot(uint256 root, bytes32 leafListHash) external {
        require(msg.sender == aggregator, "not aggregator");
        _postRoot(root, leafListHash);
    }

    function _postRoot(uint256 root, bytes32 leafListHash) internal {
        require(root != 0 && root < FIELD_R, "bad root");
        uint64 seq = rootSequence;
        uint256 at = seq % ROOT_RING_SIZE;
        if (seq >= ROOT_RING_SIZE) {
            uint256 evicted = _rootRing[at];
            if (_rootLatestSequence[evicted] == seq - ROOT_RING_SIZE) {
                delete rootPostedAt[evicted];
                delete _rootLatestSequence[evicted];
            }
        }
        _rootRing[at] = root;
        rootPostedAt[root] = uint64(block.timestamp);
        _rootLatestSequence[root] = seq;
        rootSequence = seq + 1;
        identityRoot = root;
        emit IdentityRootPosted(root, seq, uint64(block.timestamp), leafListHash);
    }

    /// @notice Declare a consumer's maximum root age.  Governance only.  Refused
    ///         when the ring is full and does not reach back that far: a window
    ///         the ring cannot honour would be truncated silently.
    function setMaxRootAge(bytes32 consumer, uint32 maxAge) external {
        require(msg.sender == governance, "not governance");
        require(maxAge > 0, "maxAge=0");
        if (rootSequence >= ROOT_RING_SIZE) {
            uint256 oldest = _rootRing[rootSequence % ROOT_RING_SIZE];
            require(block.timestamp - rootPostedAt[oldest] >= maxAge,
                    "maxAge beyond the ring");
        }
        _maxRootAge[consumer] = maxAge;
        emit MaxRootAgeSet(consumer, maxAge);
    }

    /// @notice The maximum root age `consumer` accepts.
    function maxRootAge(bytes32 consumer) public view returns (uint32) {
        uint32 a = _maxRootAge[consumer];
        return a == 0 ? DEFAULT_MAX_ROOT_AGE : a;
    }

    /// @notice Whether `consumer` accepts a proof against `root`: a nonzero
    ///         root the ring retains, posted no longer ago than its maximum age.
    function acceptsRoot(uint256 root, bytes32 consumer) external view returns (bool) {
        if (root == 0) return false;
        uint64 at = rootPostedAt[root];
        return at != 0 && block.timestamp - at <= maxRootAge(consumer);
    }

    /// @notice Set the Poseidon T3 contract (Merkle nodes).  Governance only.
    function setIdentityPoseidon(address _poseidon) external {
        require(msg.sender == governance, "not governance");
        emit IdentityPoseidonSet(identityPoseidon, _poseidon);
        identityPoseidon = _poseidon;
    }

    /// @notice Set the Poseidon T4 contract (the public leaf).  Governance only.
    function setIdentityPoseidonT4(address _poseidon) external {
        require(msg.sender == governance, "not governance");
        emit IdentityPoseidonT4Set(identityPoseidonT4, _poseidon);
        identityPoseidonT4 = _poseidon;
    }

    // ---- public membership (accumulator specification, section 11.2) -------

    /// @notice The public identity leaf Poseidon(TAG, M.x, M.y).
    function publicIdentityLeaf(BN254.G1Point calldata M) public view returns (uint256) {
        address p = identityPoseidonT4;
        require(p != address(0), "poseidon T4 not set");
        uint256[3] memory inputs = [LEAF_TAG_IDENTITY, M.X % FIELD_R, M.Y % FIELD_R];
        return IPoseidonT4(p).poseidon(inputs);
    }

    /// @notice Whether `leaf` is in the enrolled PUBLIC subtree `id` under
    ///         aggregator root `root`: its subtree path, then the aggregator
    ///         path from the subtree's enrolled slot.  The slot comes from
    ///         enrollment, never from the caller, and it is what makes a path a
    ///         claim about a named subtree.  Root acceptance is the consumer's.
    function verifyPublicMembership(
        bytes32 id,
        uint256 leaf,
        uint256[] calldata subSiblings,
        uint256 subIndex,
        uint256[] calldata aggSiblings,
        uint256 root
    ) external view returns (bool) {
        Subtree memory t = subtrees[id];
        if (!t.enrolled || !t.isPublic) return false;
        if (subSiblings.length != t.depth || aggSiblings.length != IDENTITY_TREE_DEPTH) return false;
        if (subIndex >= (uint256(1) << t.depth)) return false;
        uint256 cur = _fold(leaf, subSiblings, subIndex);
        return _fold(cur, aggSiblings, t.slot) == root;
    }

    function _fold(uint256 cur, uint256[] calldata siblings, uint256 index)
        internal view returns (uint256)
    {
        for (uint256 d = 0; d < siblings.length; d++) {
            cur = (index >> d) & 1 == 0 ? _hashPair(cur, siblings[d]) : _hashPair(siblings[d], cur);
        }
        return cur;
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
    ///         under another.  Does not update the identity Merkle accumulator.
    function register(
        address issuer,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        PSPresentation calldata pres,
        RegistrationProof calldata proof
    ) external {
        _register(issuer, pk, E, pres, proof, msg.sender, 0);
    }

    /// @notice Register.  identityLeaf must be 0, permanently: admission is the
    ///         certifying authority's, into its own subtree, and never a
    ///         registry call (accumulator specification, section 6).  Nonzero
    ///         values revert `unchecked identity leaf`.
    function register(
        address issuer,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        PSPresentation calldata pres,
        RegistrationProof calldata proof,
        uint256 identityLeaf
    ) external {
        _register(issuer, pk, E, pres, proof, msg.sender, identityLeaf);
    }

    /// @dev Shared registration logic.  identityLeaf != 0 is refused, by
    ///      decision: a caller's leaf is not the certifier's attestation.
    function _register(
        address issuer,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        PSPresentation calldata pres,
        RegistrationProof calldata proof,
        address registrant,
        uint256 identityLeaf
    ) internal {
        require(!_isRegistered(registrant), "already registered");
        require(identityLeaf == 0,          "unchecked identity leaf");
        _verifyCredential(issuer, pk, E, pres, proof, registrant);

        _pk[registrant]     = pk;
        _E_addr[registrant] = E;
        issuerOf[registrant] = issuer;
        emit Registered(registrant, issuer);
    }

    /// @dev PS signature + registration NIZK, Fiat-Shamir bound to `registrant`.
    ///      Used by register (registrant = msg.sender) and by credential
    ///      bindContract (registrant = target).
    function _verifyCredential(
        address issuer,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        PSPresentation calldata pres,
        RegistrationProof calldata proof,
        address registrant
    ) internal view {
        require(isTrustedIssuer[issuer],    "untrusted issuer");
        // A = O makes the credential term vanish and (a') holds for every m:
        // this check is security critical, not hygiene.
        require(!BN254.isInfinity(pres.A),  "A=O");
        require(!BN254.isInfinity(pres.B),  "B=O");
        require(!BN254.isInfinity(pk),      "pk=O");
        require(!BN254.isInfinity(E.R),     "R=O");
        require(
            _canonical(proof.e) && _canonical(proof.s_m) && _canonical(proof.s_b)
            && _canonical(proof.s_r) && _canonical(proof.s_sk),
            "bad scalar"
        );

        // (fs) Fiat-Shamir
        require(
            proof.e == _fsRegister(pres, E, pk, proof, registrant, block.chainid),
            "bad FS challenge"
        );

        // (b) ElGamal C consistency: s_m*G + s_r*pk == e*C + T_C
        require(_checkElGamalC(proof.s_m, proof.s_r, pk, E.C, proof.T_C, proof.e), "bad NIZK C");

        // (c) ElGamal R consistency: s_r*G == e*R + T_R
        require(_checkElGamalR(proof.s_r, E.R, proof.T_R, proof.e), "bad NIZK R");

        // (k) Account-key ownership: s_sk*G == T_key + e*pk
        require(_checkKeyOwnership(proof.s_sk, pk, proof.T_key, proof.e), "bad NIZK key");

        // (a') presentation pairing product
        require(_checkPresentation(pres, proof, _trustedIssuers[issuer]), "bad presentation");
    }

    // ---- contract identity binding -----------------------------------------

    /// @notice Commit to an exact binding of this contract. Existing targets
    ///         call this through their own controller/governance mechanism;
    ///         merely being the first registered EOA to call bindContract is
    ///         not evidence of control over the target.
    function authorizeContractBinding(
        address binder,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        bool isPublicIdentity_,
        bool isCarrying_
    ) external {
        require(msg.sender.code.length > 0, "target not a deployed contract");
        require(!_isRegistered(msg.sender), "already bound");
        require(binder != address(0), "binder=0");
        bytes32 authorization = _bindingAuthorizationHash(
            msg.sender, binder, pk, E, isPublicIdentity_, isCarrying_
        );
        pendingBindingAuthorization[msg.sender] = authorization;
        emit ContractBindingAuthorized(msg.sender, binder, authorization);
    }

    /// @notice Revoke this contract's unconsumed binding authorization.
    function revokeContractBindingAuthorization() external {
        require(msg.sender.code.length > 0, "target not a deployed contract");
        delete pendingBindingAuthorization[msg.sender];
        emit ContractBindingAuthorizationRevoked(msg.sender);
    }

    function bindingAuthorizationHash(
        address target,
        address binder,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        bool isPublicIdentity_,
        bool isCarrying_
    ) external view returns (bytes32) {
        return _bindingAuthorizationHash(
            target, binder, pk, E, isPublicIdentity_, isCarrying_
        );
    }

    /// @notice Bind a (pk, E_addr) Identity to a deployed contract address.
    ///
    ///         Three independent checks:
    ///         1. Control: `target` is a deployed contract and unbound. The
    ///            target authorizes the exact proposed binding. Contracts with
    ///            known external provenance use a separately audited adapter.
    ///         2. Certification: either (a) this exception -- msg.sender is a
    ///            registered account and the supplied (pk, E) equal that
    ///            account's stored identity -- or (b) the credential overload,
    ///            which verifies the same PS signature + registration NIZK as
    ///            register(), Fiat-Shamir registrant = uint160(target).
    ///         3. Membership: identityLeaf != 0 is refused.  A caller-chosen
    ///            leaf is not evidence of KYC admission.
    ///
    ///         The 5-arg overload is (a): an already-certified operator copies
    ///         their registered identity onto `target`.  An unregistered
    ///         caller cannot bind a fabricated identity.  Existing contracts
    ///         must first authorize the exact binder, identity, and policy via
    ///         authorizeContractBinding. Known-provenance contracts instead
    ///         use bindContractFromAdapter.
    ///
    ///         `isPublicIdentity_` records that the operator discloses m
    ///         off-chain (AMM pools, BUCK-unaware contracts).  `isCarrying_`
    ///         selects demurrage flavour.  msg.sender is recorded as binder.
    function bindContract(
        address target,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        bool isPublicIdentity_,
        bool isCarrying_
    ) external virtual {
        _bindCertifiedOperator(target, pk, E, isPublicIdentity_, isCarrying_);
    }

    /// @notice Bind.  identityLeaf must be 0 (unchecked identity leaf otherwise).
    function bindContract(
        address target,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        bool isPublicIdentity_,
        bool isCarrying_,
        uint256 identityLeaf
    ) external virtual {
        require(identityLeaf == 0, "unchecked identity leaf");
        _bindCertifiedOperator(target, pk, E, isPublicIdentity_, isCarrying_);
    }

    /// @notice Bind `target` under a fresh credential.  Fiat-Shamir registrant
    ///         is uint160(target), so a proof valid for an EOA (or another
    ///         contract) cannot be replayed here. Certification is the
    ///         credential itself; target authorization is still mandatory.
    function bindContract(
        address target,
        address issuer,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        PSPresentation calldata pres,
        RegistrationProof calldata proof,
        ContractBindingProof calldata bindingProof,
        bool isPublicIdentity_,
        bool isCarrying_
    ) external {
        require(target.code.length > 0, "target not a deployed contract");
        require(!_isRegistered(target), "already bound");
        _verifyCredential(issuer, pk, E, pres, proof, target);
        _verifyContractBindingProof(
            target, msg.sender, pk, bindingProof,
            isPublicIdentity_, isCarrying_
        );
        _consumeBindingControl(
            target, msg.sender, pk, E, isPublicIdentity_, isCarrying_
        );
        _storeBinding(target, pk, E, isPublicIdentity_, isCarrying_, msg.sender, issuer);
    }

    /// @notice Bind a known-provenance contract through an audited adapter.
    /// @dev The adapter authenticates `operator` against its immutable
    ///      provenance contract. The registry copies only the operator's
    ///      already-certified identity, so the adapter cannot inject keys,
    ///      ciphertext, or issuer data. Each adapter must constrain policy
    ///      flags to the values appropriate for its target type.
    function bindContractFromAdapter(
        address target,
        address operator,
        bool isPublicIdentity_,
        bool isCarrying_
    ) external {
        require(isBindingAdapter[msg.sender], "not binding adapter");
        require(target.code.length > 0, "target not a deployed contract");
        require(!_isRegistered(target), "already bound");
        require(_isRegistered(operator), "operator not registered");

        BN254.G1Point memory pk = _pk[operator];
        ElGamalCT memory E = _E_addr[operator];
        _storeBinding(
            target, pk, E, isPublicIdentity_, isCarrying_,
            operator, issuerOf[operator]
        );
    }

    function contractBindingChallenge(
        address target,
        address binder,
        BN254.G1Point calldata pk,
        BN254.G1Point calldata T,
        bool isPublicIdentity_,
        bool isCarrying_
    ) external view returns (uint256) {
        return _fsContractBinding(
            target, binder, pk, T, isPublicIdentity_, isCarrying_
        );
    }

    function _verifyContractBindingProof(
        address target,
        address binder,
        BN254.G1Point calldata pk,
        ContractBindingProof calldata proof,
        bool isPublicIdentity_,
        bool isCarrying_
    ) internal view {
        require(_canonical(proof.e) && _canonical(proof.s), "bad binding scalar");
        require(!BN254.isInfinity(proof.T), "binding T=O");
        require(proof.e == _fsContractBinding(
            target, binder, pk, proof.T, isPublicIdentity_, isCarrying_
        ), "bad binding challenge");
        require(BN254.eq(
            BN254.mul(BN254.g1(), proof.s),
            BN254.add(proof.T, BN254.mul(pk, proof.e))
        ), "bad binding authorization");
    }

    function _fsContractBinding(
        address target,
        address binder,
        BN254.G1Point calldata pk,
        BN254.G1Point calldata T,
        bool isPublicIdentity_,
        bool isCarrying_
    ) internal view returns (uint256) {
        BN254.G1Point[] memory pts = new BN254.G1Point[](2);
        pts[0] = pk;
        pts[1] = T;
        uint256[] memory scl = new uint256[](7);
        scl[0] = CONTRACT_BINDING_DOMAIN;
        scl[1] = uint256(uint160(address(this)));
        scl[2] = block.chainid;
        scl[3] = uint256(uint160(target));
        scl[4] = uint256(uint160(binder));
        scl[5] = isPublicIdentity_ ? 1 : 0;
        scl[6] = isCarrying_ ? 1 : 0;
        return BN254.fsChallenge(pts, scl);
    }

    /// @dev Already-certified operator exception: binder is registered and
    ///      the supplied (pk, E) match that binder's stored identity.
    function _bindCertifiedOperator(
        address target,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        bool isPublicIdentity_,
        bool isCarrying_
    ) internal {
        require(target.code.length > 0,     "target not a deployed contract");
        require(!_isRegistered(target),     "already bound");
        require(_isRegistered(msg.sender),  "binder not registered");
        require(_samePkE(msg.sender, pk, E), "uncertified identity");
        _consumeBindingControl(
            target, msg.sender, pk, E, isPublicIdentity_, isCarrying_
        );
        _storeBinding(
            target, pk, E, isPublicIdentity_, isCarrying_,
            msg.sender, issuerOf[msg.sender]
        );
    }

    function _consumeBindingControl(
        address target,
        address binder,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        bool isPublicIdentity_,
        bool isCarrying_
    ) internal {
        bytes32 expected = _bindingAuthorizationHash(
            target, binder, pk, E, isPublicIdentity_, isCarrying_
        );
        require(
            pendingBindingAuthorization[target] == expected,
            "target did not authorize binding"
        );
        delete pendingBindingAuthorization[target];
    }

    function _bindingAuthorizationHash(
        address target,
        address binder,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E,
        bool isPublicIdentity_,
        bool isCarrying_
    ) internal view returns (bytes32) {
        return keccak256(abi.encode(
            keccak256("AlbertaBuck/IdentityRegistry/ContractBindingControl/v2"),
            address(this), block.chainid, target, binder,
            pk.X, pk.Y, E.R.X, E.R.Y, E.C.X, E.C.Y,
            isPublicIdentity_, isCarrying_
        ));
    }

    function _samePkE(
        address a,
        BN254.G1Point calldata pk,
        ElGamalCT calldata E
    ) internal view returns (bool) {
        BN254.G1Point storage p = _pk[a];
        ElGamalCT storage e = _E_addr[a];
        return p.X == pk.X && p.Y == pk.Y
            && e.R.X == E.R.X && e.R.Y == E.R.Y
            && e.C.X == E.C.X && e.C.Y == E.C.Y;
    }

    function _storeBinding(
        address target,
        BN254.G1Point memory pk,
        ElGamalCT memory E,
        bool isPublicIdentity_,
        bool isCarrying_,
        address binder,
        address issuer
    ) internal {
        _pk[target]              = pk;
        _E_addr[target]          = E;
        isPublicIdentity[target] = isPublicIdentity_;
        isCarrying[target]       = isCarrying_;
        binderOf[target]         = binder;
        issuerOf[target]         = issuer;
        emit ContractBound(target, binder, isPublicIdentity_);
        emit CarryingFlagSet(target, isCarrying_);
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
    ///         the registry deployment, parties and chain into the transcript.
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
        if (!_canonical(pi.e) || !_canonical(pi.s1) || !_canonical(pi.s2)) return false;

        ElGamalCT memory E_a = _E_addr[sender];
        BN254.G1Point memory pkA = _pk[sender];
        BN254.G1Point memory pkB = _pk[spender];

        if (BN254.isInfinity(pkA) || BN254.isInfinity(pkB)) return false;
        if (BN254.isInfinity(E_a.R) || BN254.isInfinity(E_bob.R)) return false;

        // Check 1: s1*G == T1 + e*pk_a  (key ownership; T1 = T_key)
        if (!BN254.eq(
            BN254.mul(BN254.g1(), pi.s1),
            BN254.add(pi.T1, BN254.mul(pkA, pi.e))
        )) return false;

        // Check 2: s2*G == T3 + e*R_b  (T3 = T_R)
        if (!BN254.eq(
            BN254.mul(BN254.g1(), pi.s2),
            BN254.add(pi.T3, BN254.mul(E_bob.R, pi.e))
        )) return false;

        // Check 3: s1*R_a - s2*pk_b == T2 + e*(C_a - C_b)  (T2 = T_diff)
        BN254.G1Point memory lhs2 = BN254.add(
            BN254.mul(E_a.R, pi.s1),
            BN254.neg(BN254.mul(pkB, pi.s2))
        );
        BN254.G1Point memory rhs2 = BN254.add(
            pi.T2,
            BN254.mul(BN254.add(E_a.C, BN254.neg(E_bob.C)), pi.e)
        );
        if (!BN254.eq(lhs2, rhs2)) return false;

        // Check 4: Fiat-Shamir
        return pi.e == _fsApprove(
            E_a, E_bob, pkA, pkB, pi, sender, spender, block.chainid
        );
    }

    // ---- public-issuer note binding (Notes mutual-decryptability, Phase 1) --

    /// @notice Verify a Schnorr signature by `issuer`'s registered identity key
    ///         over a note-batch commitment `hBatch` (= keccak256 of the minted
    ///         commitments).  This is the *issuer half* of the BUCK Notes
    ///         deferred-approve handshake for public issuers: it binds the
    ///         issuer's decrypted Identity to every leaf in the batch, so a
    ///         depositor can later produce a cryptographically sound receipt
    ///         naming the payer (see alberta-buck-notes.org "The Non-Deniable-Receipt Invariant").
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
    ///         alberta-buck-notes.org "The Non-Deniable-Receipt Invariant" and
    ///         alberta_buck.wallet.issuer_reenc.
    /// @dev    Reads the issuer's registered `(pk_iss, E_reg) = (_pk, _E_addr)`
    ///         from storage so a caller cannot substitute either.  Checks the
    ///         five Okamoto relations via EIP-196 BN254 precompiles, then the
    ///         Fiat-Shamir challenge.  `pk_rec` never appears: the verifier sees
    ///         only the blinded `Q` and the uniform `U`, `T`.
    ///
    ///         This binds `eIss` to the key committed in `pi.Q`, not yet to the
    ///         recipient's.  That is the spend's to prove: Notes.mint commits
    ///         `pi.T` in the leaf, and the A2 deposit fold proves `T` opens under
    ///         the spender's own key (doc/review/notes-receiving-key.org, 4.6).
    function verifyIssuerReenc(
        address issuer,
        ElGamalCT calldata eIss,
        IssuerReencProof calldata pi
    ) external view returns (bool) {
        if (!_isRegistered(issuer)) return false;

        BN254.G1Point memory pkIss = _pk[issuer];
        ElGamalCT     memory E_reg = _E_addr[issuer];
        // Every blind here sits on H_PEDERSEN, whose logarithm nobody knows:
        // on a known-log base the key in Q and the point in T would bind
        // nothing, and one ciphertext could name two issuers.
        BN254.G1Point memory H     = BN254.G1Point(H_PED_X, H_PED_Y);

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

        // L3: s_r*Q - s_b*U + s_g*H == A3 + e*T   (=> T = r'*pk_rec + gamma*H)
        if (!BN254.eq(
            BN254.add(
                BN254.add(BN254.mul(pi.Q, pi.s_r), BN254.neg(BN254.mul(pi.U, pi.s_b))),
                BN254.mul(H, pi.s_g)
            ),
            BN254.add(pi.A3, BN254.mul(pi.T, pi.e))
        )) return false;

        // L4: s_s*G == A4 + e*pk_iss
        if (!BN254.eq(
            BN254.mul(BN254.g1(), pi.s_s),
            BN254.add(pi.A4, BN254.mul(pkIss, pi.e))
        )) return false;

        // L5: s_s*R_reg + s_g*H == A5 + e*(C_reg + T - C_i)
        BN254.G1Point memory Y =
            BN254.add(E_reg.C, BN254.add(pi.T, BN254.neg(eIss.C)));
        if (!BN254.eq(
            BN254.add(BN254.mul(E_reg.R, pi.s_s), BN254.mul(H, pi.s_g)),
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
        uint256[] memory scl = new uint256[](3);
        scl[0] = uint256(uint160(issuer));
        scl[1] = chainid;
        scl[2] = ISSUER_REENC_DOMAIN;
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
            BN254.G1Point memory H = BN254.G1Point(H_PED_X, H_PED_Y);
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
        uint256[] memory scl = new uint256[](3);
        scl[0] = uint256(uint160(depositor));
        scl[1] = chainid;
        scl[2] = DEPOSITOR_BINDING_DOMAIN;
        return BN254.fsChallenge(pts, scl);
    }

    function _fsRegister(
        PSPresentation calldata pres,
        ElGamalCT calldata E,
        BN254.G1Point calldata pk,
        RegistrationProof calldata proof,
        address registrant,
        uint256 chainid
    ) internal view returns (uint256) {
        BN254.G1Point[] memory pts = new BN254.G1Point[](9);
        pts[0] = pres.A;
        pts[1] = pres.B;
        pts[2] = E.R;
        pts[3] = E.C;
        pts[4] = pk;
        pts[5] = proof.C1;
        pts[6] = proof.T_C;
        pts[7] = proof.T_R;
        pts[8] = proof.T_key;
        uint256[] memory scl = new uint256[](4);
        scl[0] = uint256(uint160(registrant));
        scl[1] = chainid;
        scl[2] = uint256(uint160(address(this)));
        scl[3] = REGISTER_DOMAIN;
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
    ) internal view returns (uint256) {
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
        uint256[] memory scl = new uint256[](5);
        scl[0] = uint256(uint160(sender));
        scl[1] = uint256(uint160(spender));
        scl[2] = chainid;
        scl[3] = uint256(uint160(address(this)));
        scl[4] = APPROVE_DOMAIN;
        return BN254.fsChallenge(pts, scl);
    }

    /// @dev Fiat-Shamir challenge for the public-issuer Schnorr binding.
    ///      Order: points (pk_iss, R) then scalars (hBatch, issuer, chainid,
    ///      domain).  Must match alberta_buck.wallet.schnorr byte-for-byte.
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
        uint256[] memory scl = new uint256[](4);
        scl[0] = uint256(hBatch);
        scl[1] = uint256(uint160(issuer));
        scl[2] = chainid;
        scl[3] = ISSUER_SCHNORR_DOMAIN;
        return BN254.fsChallenge(pts, scl);
    }

    function _canonical(uint256 s) internal pure returns (bool) {
        return s < BN254.R;
    }

    function _checkKeyOwnership(
        uint256 s_sk,
        BN254.G1Point calldata pk,
        BN254.G1Point calldata T_key,
        uint256 e
    ) internal view returns (bool) {
        return BN254.eq(
            BN254.mul(BN254.g1(), s_sk),
            BN254.add(T_key, BN254.mul(pk, e))
        );
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
    ///   e(s_m*A + s_b*G - C1, Y) * e(e*A, X) * e(-e*B, g_2) == 1
    /// @dev (a')  e(s_m*A + s_b*G - C1, Y) * e(e*A, X) * e(-e*B, g_2) == 1.
    ///      Three pairs: the message and blinding exponents share the G2 base
    ///      Y, so one G1 commitment C1 serves both and no proof field reveals
    ///      m*A on its own.
    function _checkPresentation(
        PSPresentation calldata pres,
        RegistrationProof calldata proof,
        PSPubKey storage ipk
    ) internal view returns (bool) {
        BN254.G1Point[] memory a = new BN254.G1Point[](3);
        BN254.G2Point[] memory b = new BN254.G2Point[](3);
        a[0] = BN254.add(
            BN254.add(BN254.mul(pres.A, proof.s_m), BN254.mul(BN254.g1(), proof.s_b)),
            BN254.neg(proof.C1)
        );
        b[0] = ipk.Y;
        a[1] = BN254.mul(pres.A, proof.e);
        b[1] = ipk.X;
        a[2] = BN254.neg(BN254.mul(pres.B, proof.e));
        b[2] = BN254.g2();
        return BN254.pairingCheck(a, b);
    }
}
