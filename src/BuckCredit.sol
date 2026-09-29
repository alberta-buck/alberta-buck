// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC721/extensions/ERC721Enumerable.sol";

import "@openzeppelin/contracts/utils/Strings.sol";

import {BuckTypes, BuckQty, toBuckQty, CreditSlice} from "./BuckTypes.sol";
import {BN254} from "./BN254.sol";
import {IAccumulatorRegistry} from "./IAccumulatorRegistry.sol";

/// @title BuckCredit — ERC-721 Insured Asset NFT
/// @notice Each token represents an insurer's offer of parametric insurance on a
///         real-world asset, with deterministic depreciation and piecemeal activation.
///
/// When insurance is executed, it protects a certain asset valuation, and
/// each piecemeal contract covers a certain fraction of the asset (eg. 65%), up to a total
/// of the asset's current value.
///
/// When claimed, the insurance contracts each pay the insured fraction of a drop in its value from
/// the insured valuation, net deductible.
///
/// All monetary fields (faceValue, depreciationFloor, activatedValue) are
/// 6-decimal BUCK amounts and packed into uint80 slots — same storage type as
/// Buck.sol's ERC-20 balances.  Bounds and precision come from BuckTypes so a
/// future change propagates to both contracts in lockstep.
///
/// === Architectural note: why BuckCredit is its own contract ===
///
/// BuckCredit and Buck currently communicate via:
///   Buck -> BuckCredit: totalCurrentValue, batchCreditInfo, ownerOf,
///                       balanceOf, tokenOfOwnerByIndex, activateFromBuck
///   BuckCredit -> Buck: nothing.  The call graph is one-way.
///
/// It is tempting to collapse the pair into a single Diamond (EIP-2535)
/// with separate facets, eliminating the cross-contract calls and the
/// setBuck() wiring.  This is a dead end:
///
///   ERC-20 and ERC-721 share function selectors with incompatible
///   semantics.  balanceOf(address) is 0x70a08231 in both standards
///   (BUCK amount vs. NFT count), transferFrom(address,address,uint256)
///   is 0x23b872dd in both (amount vs. tokenId), and the Transfer event
///   has different indexed-argument counts.  A Diamond router can
///   dispatch one of these per selector; whichever loses stops being
///   standards-compliant and silently breaks wallets, indexers,
///   marketplaces, and routers.  ERC-1155 exists precisely because no
///   production system can safely mix raw ERC-20 + ERC-721 on one
///   address.
///
/// What IS available for modularization:
///   1. Buck as a Diamond (identity / demurrage / mint-burn / ERC-20
///      facets) -- selectors don't collide within ERC-20.  Future work.
///   2. Make BuckCredit independently upgradeable (UUPS / Transparent
///      proxy) without merging into Buck's Diamond -- Buck's
///      `immutable buckCredit` address stays stable, BuckCredit's logic
///      can be patched in place.  Also future work.
///   3. Amortize the cross-contract call cost via batch reads --
///      batchCreditInfo() below packs the per-NFT view that Buck's
///      _allocateMint / _allocateBurn loops need into one external call.
///
/// === End architectural note ===

contract BuckCredit is ERC721Enumerable {

    enum DepreciationType {
        NONE,              // Non-depreciating (land, gold, crypto)
        LINEAR,            // Constant annual reduction
        DECLINING_BALANCE, // Percentage of remaining value per year
        MARKED             // Valued at its insurer's latest mark (see `mark`)
    }

    /// @dev Field declaration order is chosen to pack into 3 storage slots:
    ///      slot 0: insurer (20) + assetClass (1) + createdAt (6)            = 27
    ///      slot 1: faceValue (10) + depreciationFloor (10) + depType (1)
    ///              + depRate (4) + depStartAt (6)                           = 31
    ///      slot 2: premiumRate (4) + lastUpdated (6) + activatedValue (10) = 20
    struct CreditParams {
        // Immutable (set at creation)
        address insurer;            // Vendor who can update this credit
        uint8   assetClass;         // Asset classification (immutable)
        uint48  createdAt;          // Creation timestamp

        // Insurer-mutable (reappraisal, schedule changes)
        BuckQty faceValue;          // Maximum insured value           (uint80 BUCK, 6 decimals)
        BuckQty depreciationFloor;  // Minimum value after depreciation (uint80 BUCK, 6 decimals)

        DepreciationType depType;   // Depreciation model
        uint32  depRate;            // Annual rate in basis points (10000 = 100%)
        uint48  depStartAt;         // When depreciation begins

        uint32  premiumRate;        // Annual premium: basis points of activated value
        uint48  lastUpdated;        // Timestamp of last insurer update

        // Client-mutable (activation, through Buck only)
        BuckQty activatedValue;     // Currently activated portion (<= faceValue) (uint80 BUCK, 6 decimals)
    }

    // ── Depreciation constants ──────────────────────────────────────
    uint256 internal constant BP                = 10_000;
    uint256 internal constant SECONDS_PER_YEAR  = 365 days + 6 hours;   // matches Buck.sol
    /// Cap declining-balance compounding to bound gas; by then the value is
    /// indistinguishable from `floor` for any rate >= a few hundred bps.
    uint256 internal constant MAX_DEP_YEARS     = 100;

    mapping(uint256 => CreditParams) public credits;
    uint256 private _nextTokenId;

    /// @notice The Buck contract permitted to drive activation.  Wired
    ///         one-shot post-deployment via setBuck(...).  BuckCredit never
    ///         calls Buck -- this is an authorisation record, nothing more.
    address public buck;

    // ── Recipient opt-in ────────────────────────────────────────────
    //
    // A credit only lands where its recipient asked for it.  Without this,
    // `createCredit` mints an ERC-721 to an address that never consented --
    // and a credit costs its holder gas forever after, because
    // `totalCurrentValue` walks every token they own on every outbound BUCK
    // transfer.  An attacker could raise a chosen address's transfer cost
    // without bound for the price of the mints.
    //
    // The rule is uniform: it applies to self-issuance too.  There is no
    // reading of "I am my own insurer" that needs a carve-out, and declining
    // to open one keeps the invariant a reader can state in one line.

    /// @notice Insurers a client is willing to receive credits from.
    mapping(address => mapping(address => bool)) public acceptsCreditFrom;

    // --- Events ---
    event CreditIssuerSet(address indexed client, address indexed insurer, bool accepted);
    event CreditCreated(uint256 indexed tokenId, address indexed insurer,
                        address indexed owner, uint256 faceValue, bytes32 scope);
    event CreditUpdated(uint256 indexed tokenId, address indexed insurer,
                        uint256 newFaceValue, uint32 newDepRate, uint32 newPremiumRate);
    event CreditActivated(uint256 indexed tokenId, address indexed owner,
                          uint256 additionalValue, uint256 totalActivated);
    event BuckSet(address indexed buck);
    event CreditMarked(uint256 indexed tokenId, uint256 value);
    event InsurerGateConfigured(address indexed registry, string regulatorNamespace,
                                uint32 attestationPeriod);
    event InsurerAttested(address indexed insurer, uint256 indexed root, uint48 expiresAt,
                          uint8 faceBand, uint8 depTypes, uint32 maxDepRate,
                          uint32 maxPremiumRate, bytes32[] scopes);

    // ── The insurer gate (doc/review/accumulator-spec.org, section 12) ──
    //
    // An insurer may write a credit only inside the envelope its regulator
    // attested: standing, a face band, the depreciation models, maximum
    // depreciation and premium rates, and the scopes it may declare.  The
    // regulator publishes the envelope as membership in public subtrees; the
    // insurer proves it once per review period (attestInsurer), and issuance
    // is then a storage read.  Configured once, by the deployer, and never
    // removable.  A BuckCredit never configured issues ungated: the economic
    // simulations deploy that way, and a deployment profile MUST configure it.

    /// @notice The accumulator the regulator's subtrees are proven against.
    IAccumulatorRegistry public insurerRegistry;
    /// @notice The regulator's namespace, e.g. "regulator:ca-ab": every
    ///         predicate subtree and scope is named under it.
    string  public regulatorNamespace;
    /// @notice How long an attestation lasts from the root it was proven on.
    uint32  public attestationPeriod;
    address private immutable _deployer;

    /// @notice The attested envelope an insurer's issuance is checked against.
    struct Envelope {
        uint48 expiresAt;       // 0: never attested
        uint8  faceBand;        // faces below 10**faceBand BUCK
        uint8  depTypes;        // bit d: DepreciationType(d) permitted
        uint32 maxDepRate;      // basis points per year
        uint32 maxPremiumRate;  // basis points per year
        uint32 epoch;           // bumped at each attestation; scopes carry it
    }
    mapping(address => Envelope) public envelopeOf;
    /// @dev A scope is attested iff its epoch equals the envelope's, so a new
    ///      attestation withdraws every scope it does not repeat.
    mapping(address => mapping(bytes32 => uint32)) internal _scopeEpoch;

    // ── Marked credits ──────────────────────────────────────────────
    //
    // A MARKED credit is valued at a mark its insurer keeps current, in place
    // of a depreciation schedule: collateral whose value is on-chain and
    // moves every block (a BuckBasket's equity).  Like depreciation, the mark
    // moves the credit limit without touching the coverage, so it can fall
    // below the activated coverage -- which is exactly why only a SELF-issued
    // credit may be marked (insurer == holder, fixed at creation, and never
    // switched to or from MARKED by updateCredit).  An insurer marking
    // someone else's credit down would be revoking a purchased policy, the
    // one thing an insurer may never do.

    /// @notice A MARKED credit's present value, as its insurer last marked
    ///         it (capped by the face when read).
    mapping(uint256 => uint256) public markOf;

    /// @notice The scope the eight-argument createCredit declares: a regulator
    ///         grants it to a general insurer and withholds it from a scoped one.
    bytes32 public constant GENERAL_SCOPE = bytes32(0);

    /// @notice keccak("AlbertaBuck/Accumulator/Consumer/InsurerAttestation/v2").
    bytes32 public constant INSURER_ATTESTATION_CONSUMER =
        keccak256("AlbertaBuck/Accumulator/Consumer/InsurerAttestation/v2");
    uint256 internal constant IDENTITY_OPENING_DOMAIN = uint256(
        keccak256("AlbertaBuck/FiatShamir/IdentityRegistry/IdentityOpening/v2")
    );
    uint8   internal constant FACE_BAND_MAX = 8;

    /// @notice What the insurer claims its regulator attested.
    struct EnvelopeClaim {
        uint8    faceBand;
        uint8    depTypes;
        uint32   maxDepRate;
        uint32   maxPremiumRate;
        bool     general;
        string[] scopes;        // asset paths, e.g. "asset:vehicle:car"
    }

    /// @notice One public membership path: the predicate subtree's own path
    ///         and the aggregator path above it (the registry fixes that
    ///         path's index from the subtree's enrolled slot).
    struct Path {
        uint256[] sub;
        uint256   subIndex;
        uint256[] agg;
    }

    /// @notice A Chaum-Pedersen proof that the attesting account's registered
    ///         credential decrypts, under its registered key, to M.
    struct IdentityOpening {
        uint256       e;
        uint256       s;
        BN254.G1Point T1;
        BN254.G1Point T2;
    }

    constructor() ERC721("BuckCredit", "BUCK_CREDIT") {
        _deployer = msg.sender;
    }

    /// @notice Configure the insurer gate.  Once, by the deployer.
    function configureInsurerGate(
        address registry,
        string calldata namespace_,
        uint32 period
    ) external {
        require(msg.sender == _deployer, "BuckCredit: not deployer");
        require(address(insurerRegistry) == address(0), "BuckCredit: gate configured");
        require(registry != address(0) && bytes(namespace_).length != 0 && period != 0,
                "BuckCredit: bad gate");
        insurerRegistry = IAccumulatorRegistry(registry);
        regulatorNamespace = namespace_;
        attestationPeriod = period;
        emit InsurerGateConfigured(registry, namespace_, period);
    }

    // ── Attestation ─────────────────────────────────────────────────

    /// @notice Prove, once per review period, the envelope the regulator
    ///         attested for msg.sender.  `M` is the insurer's Identity; the
    ///         opening shows its registered credential decrypts to M, so no
    ///         account can claim another's envelope.  `paths` proves M's public
    ///         leaf in each predicate subtree against `root`, in this order:
    ///         standing, face band, each permitted depreciation type
    ///         (ascending), maximum depreciation rate, maximum premium rate,
    ///         the general scope if claimed, then each scope.
    function attestInsurer(
        BN254.G1Point calldata M,
        IdentityOpening calldata opening,
        EnvelopeClaim calldata claim,
        uint256 root,
        Path[] calldata paths
    ) external {
        IAccumulatorRegistry reg = insurerRegistry;
        require(address(reg) != address(0), "BuckCredit: gate not configured");
        require(reg.acceptsRoot(root, INSURER_ATTESTATION_CONSUMER),
                "BuckCredit: root not accepted");
        require(claim.faceBand >= 1 && claim.faceBand <= FACE_BAND_MAX, "BuckCredit: bad face band");
        require(claim.depTypes != 0 && claim.depTypes < 8, "BuckCredit: bad depreciation types");
        require(_opens(reg, M, opening), "BuckCredit: identity opening fails");

        uint256 leaf = reg.publicIdentityLeaf(M);
        uint256 k = 0;
        _claim(reg, leaf, root, paths[k++], "insurer");
        _claim(reg, leaf, root, paths[k++],
               string.concat("insurer:face:", Strings.toString(claim.faceBand)));
        for (uint8 d = 0; d < 3; d++) {
            if ((claim.depTypes >> d) & 1 == 1) {
                _claim(reg, leaf, root, paths[k++],
                       string.concat("insurer:dep:", Strings.toString(d)));
            }
        }
        _claim(reg, leaf, root, paths[k++],
               string.concat("insurer:depRate:", Strings.toString(claim.maxDepRate)));
        _claim(reg, leaf, root, paths[k++],
               string.concat("insurer:premium:", Strings.toString(claim.maxPremiumRate)));

        Envelope storage env = envelopeOf[msg.sender];
        uint32 epoch = env.epoch + 1;
        bytes32[] memory scopes = new bytes32[](claim.scopes.length + (claim.general ? 1 : 0));
        uint256 n = 0;
        if (claim.general) {
            _claim(reg, leaf, root, paths[k++], "insurer:general");
            _scopeEpoch[msg.sender][GENERAL_SCOPE] = epoch;
            scopes[n++] = GENERAL_SCOPE;
        }
        for (uint256 i = 0; i < claim.scopes.length; i++) {
            bytes32 id = scopeId(claim.scopes[i]);
            require(reg.verifyPublicMembership(
                        id, leaf, paths[k].sub, paths[k].subIndex, paths[k].agg, root),
                    "BuckCredit: scope not attested");
            k++;
            _scopeEpoch[msg.sender][id] = epoch;
            scopes[n++] = id;
        }
        require(k == paths.length, "BuckCredit: extra paths");

        uint48 expiresAt = uint48(reg.rootPostedAt(root)) + attestationPeriod;
        envelopeOf[msg.sender] = Envelope(expiresAt, claim.faceBand, claim.depTypes,
                                          claim.maxDepRate, claim.maxPremiumRate, epoch);
        emit InsurerAttested(msg.sender, root, expiresAt, claim.faceBand, claim.depTypes,
                             claim.maxDepRate, claim.maxPremiumRate, scopes);
    }

    /// @notice The identifier of a scope under this gate's regulator: keccak
    ///         of "<namespace>:scope:<asset path>".  A name, not a code, so
    ///         refinement needs no version and no taxonomy on chain.
    function scopeId(string calldata assetPath) public view returns (bytes32) {
        return keccak256(bytes(string.concat(regulatorNamespace, ":scope:", assetPath)));
    }

    /// @notice Whether `scope` is in the insurer's current envelope.
    function scopeAttested(address insurer, bytes32 scope) external view returns (bool) {
        uint32 epoch = envelopeOf[insurer].epoch;
        return epoch != 0 && _scopeEpoch[insurer][scope] == epoch;
    }

    function _claim(
        IAccumulatorRegistry reg,
        uint256 leaf,
        uint256 root,
        Path calldata p,
        string memory predicate
    ) internal view {
        bytes32 id = keccak256(bytes(string.concat(regulatorNamespace, ":", predicate)));
        require(reg.verifyPublicMembership(id, leaf, p.sub, p.subIndex, p.agg, root),
                string.concat("BuckCredit: not attested: ", predicate));
    }

    /// @dev pk = s*G and C - M = s*R, one s: the registered credential opens
    ///      to M under the registered key.  The transcript binds the account,
    ///      the chain and the registry, under its own v2 tag.
    function _opens(
        IAccumulatorRegistry reg,
        BN254.G1Point calldata M,
        IdentityOpening calldata op
    ) internal view returns (bool) {
        if (!reg.isVerified(msg.sender)) return false;
        if (op.e >= BN254.R || op.s >= BN254.R) return false;
        BN254.G1Point memory pk = reg.pkOf(msg.sender);
        IAccumulatorRegistry.Ciphertext memory E = reg.ciphertextOf(msg.sender);
        if (!BN254.eq(BN254.mul(BN254.g1(), op.s), BN254.add(op.T1, BN254.mul(pk, op.e)))) {
            return false;
        }
        BN254.G1Point memory X = BN254.add(E.C, BN254.neg(M));
        if (!BN254.eq(BN254.mul(E.R, op.s), BN254.add(op.T2, BN254.mul(X, op.e)))) {
            return false;
        }
        BN254.G1Point[] memory pts = new BN254.G1Point[](6);
        pts[0] = E.R;
        pts[1] = E.C;
        pts[2] = pk;
        pts[3] = M;
        pts[4] = op.T1;
        pts[5] = op.T2;
        uint256[] memory scl = new uint256[](4);
        scl[0] = uint256(uint160(msg.sender));
        scl[1] = block.chainid;
        scl[2] = uint256(uint160(address(reg)));
        scl[3] = IDENTITY_OPENING_DOMAIN;
        return op.e == BN254.fsChallenge(pts, scl);
    }

    // ── Issuance against the envelope ───────────────────────────────

    /// @notice The smallest band admitting `face` (BuckCredit units, six
    ///         decimals), or FACE_BAND_MAX + 1 above the whole ladder.
    function bandForFace(uint256 face) public pure returns (uint8) {
        uint256 ceiling = 10 ** 7;              // band 1: below 10 BUCK
        for (uint8 b = 1; b <= FACE_BAND_MAX; b++) {
            if (face < ceiling) return b;
            ceiling *= 10;
        }
        return FACE_BAND_MAX + 1;
    }

    /// @dev The gate, as alberta_buck/registry/regulator.py check_issuance
    ///      specifies it, with its reasons as revert strings.  `scoped` is
    ///      false on reappraisal: the scope is emitted at creation, not stored.
    function _requireEnvelope(
        bool scoped,
        bytes32 scope,
        uint256 face,
        DepreciationType depType,
        uint32 depRate,
        uint32 premiumRate
    ) internal view {
        if (address(insurerRegistry) == address(0)) return;
        Envelope memory env = envelopeOf[msg.sender];
        require(env.expiresAt != 0, "insurer not in good standing");
        require(block.timestamp <= env.expiresAt, "attestation expired");
        if (scoped) {
            require(_scopeEpoch[msg.sender][scope] == env.epoch, "scope not attested");
        }
        require(bandForFace(face) <= env.faceBand, "face above attested band");
        require((env.depTypes >> uint8(depType)) & 1 == 1, "depreciation model not attested");
        require(depRate <= env.maxDepRate, "depreciation rate above attested maximum");
        require(premiumRate <= env.maxPremiumRate, "premium rate above attested maximum");
    }

    /// @notice One-shot wiring of the Buck contract permitted to call
    ///         activateFromBuck / deactivateFromBuck.  Callable by anyone
    ///         (the Buck address is public and the function is idempotent
    ///         once set), but immutable after first set.  Mirrors
    ///         Buck.setBasket(...) for the symmetric BuckBasket wiring style.
    function setBuck(address _buck) external {
        require(buck == address(0), "BuckCredit: buck already set");
        require(_buck != address(0), "BuckCredit: buck=0");
        buck = _buck;
        emit BuckSet(_buck);
    }

    /// @dev A credit that is currently backing BUCK cannot change hands.
    ///
    ///      Buck derives a holder's credit limit from the activated value of
    ///      the credits they own, while the BUCK drawn against that limit
    ///      stays as a negative balance on the account that drew it.  Let the
    ///      token move and the two separate: the seller keeps an obligation
    ///      with nothing behind it, and the buyer receives headroom against
    ///      coverage that has already been spent.  The same activated value
    ///      would back BUCK twice.
    ///
    ///      `activatedValue` is the right predicate rather than Buck's
    ///      `mintsBacked` because it is local: BuckCredit enforces this
    ///      without consulting Buck, so the property does not depend on
    ///      another contract being correct or even reachable.  The two agree
    ///      by construction -- activation happens only inside
    ///      `Buck._allocateMint`, and `updateCredit` may no longer clamp one
    ///      without the other.
    ///
    ///      Release is by burning the position down (`Buck.burn`), which
    ///      deactivates the coverage.  At `activatedValue == 0` the credit
    ///      is freely transferable again.  ERC721Enumerable also overrides
    ///      `_update`; super() preserves its enumeration bookkeeping.
    function _update(address to, uint256 tokenId, address auth)
        internal override returns (address from)
    {
        from = super._update(to, tokenId, auth);
        require(
            from == address(0) || credits[tokenId].activatedValue.isZero(),
            "BuckCredit: credit in use"
        );
    }

    /// @notice Accept, or stop accepting, credits issued by `insurer`.
    /// @dev    Revocable, and it only governs *new* issuance: credits already
    ///         held are unaffected, since they may be backing BUCK.
    function setCreditIssuer(address insurer, bool accepted) external {
        acceptsCreditFrom[msg.sender][insurer] = accepted;
        emit CreditIssuerSet(msg.sender, insurer, accepted);
    }

    /// @dev The opt-in gate.  Virtual so `BuckCreditHarness` can stand it
    ///      down for fixtures whose holders never send a transaction of their
    ///      own; the gate itself is exercised against this contract, not the
    ///      harness, in `BuckCreditIssuance.t.sol`.
    function _requireAccepted(address client) internal view virtual {
        require(acceptsCreditFrom[client][msg.sender],
                "BuckCredit: insurer not accepted by client");
    }

    /// @notice Insurer creates a new BUCK_CREDIT NFT for a client, declaring
    ///         the general scope.
    /// @dev faceValue / depreciationFloor are accepted as uint256 for ABI
    ///      ergonomics but must fit in BuckTypes.MAX_BALANCE (uint80 cap)
    ///      since they are stored alongside the BUCK supply.
    function createCredit(
        address client,
        uint8 assetClass,
        uint256 faceValue,
        uint256 depreciationFloor,
        DepreciationType depType,
        uint32 depRate,
        uint48 depStartAt,
        uint32 premiumRate
    ) external returns (uint256) {
        return _createCredit(client, assetClass, faceValue, depreciationFloor, depType,
                             depRate, depStartAt, premiumRate, GENERAL_SCOPE);
    }

    /// @notice Insurer creates a new BUCK_CREDIT NFT for a client, declaring
    ///         `scope` -- the insurer's public claim about what the asset is,
    ///         which its envelope must cover.  A false claim is evidence.
    function createCredit(
        address client,
        uint8 assetClass,
        uint256 faceValue,
        uint256 depreciationFloor,
        DepreciationType depType,
        uint32 depRate,
        uint48 depStartAt,
        uint32 premiumRate,
        bytes32 scope
    ) external returns (uint256) {
        return _createCredit(client, assetClass, faceValue, depreciationFloor, depType,
                             depRate, depStartAt, premiumRate, scope);
    }

    function _createCredit(
        address client,
        uint8 assetClass,
        uint256 faceValue,
        uint256 depreciationFloor,
        DepreciationType depType,
        uint32 depRate,
        uint48 depStartAt,
        uint32 premiumRate,
        bytes32 scope
    ) internal returns (uint256) {
        _requireAccepted(client);
        require(depreciationFloor <= faceValue, "floor > face");
        require(depType != DepreciationType.MARKED || client == msg.sender,
                "BuckCredit: a marked credit is self-issued");
        _requireEnvelope(true, scope, faceValue, depType, depRate, premiumRate);

        uint256 tokenId = _nextTokenId++;
        _mint(client, tokenId);

        credits[tokenId] = CreditParams({
            insurer: msg.sender,
            assetClass: assetClass,
            createdAt: uint48(block.timestamp),
            faceValue: toBuckQty(faceValue),
            depreciationFloor: toBuckQty(depreciationFloor),
            depType: depType,
            depRate: depRate,
            depStartAt: depStartAt,
            premiumRate: premiumRate,
            lastUpdated: uint48(block.timestamp),
            activatedValue: BuckQty.wrap(0)
        });

        emit CreditCreated(tokenId, msg.sender, client, faceValue, scope);
        return tokenId;
    }

    // ── Depreciation ────────────────────────────────────────────────

    /// @notice Current depreciated value of the activated portion of this credit.
    /// @dev Pure computation from on-chain state — no oracle needed.
    function currentValue(uint256 tokenId) public view returns (uint256) {
        CreditParams storage c = credits[tokenId];
        if (c.activatedValue.isZero()) return 0;

        // Activated portion depreciates proportionally.
        return depreciatedFaceValue(tokenId) * c.activatedValue.asUint()
             / c.faceValue.asUint();
    }

    /// @notice The whole asset's appraised value on today's schedule, before
    ///         any activation is taken into account.
    /// @dev    This is the ceiling on what can actually be insured now, and
    ///         therefore what a premium is charged against.  `currentValue`
    ///         is this scaled by the holder's activated share;
    ///         `depreciatedFaceValue` is the share-independent figure Buck's
    ///         allocator needs to convert between face units and present
    ///         insured value.
    function depreciatedFaceValue(uint256 tokenId) public view returns (uint256) {
        CreditParams storage c = credits[tokenId];
        if (c.depType == DepreciationType.MARKED) {
            uint256 face = c.faceValue.asUint();
            uint256 m = markOf[tokenId];
            return m < face ? m : face;
        }
        return _depreciate(
            c.faceValue.asUint(), c.depType, c.depRate,
            c.depreciationFloor.asUint(), c.depStartAt
        );
    }

    /// @dev Discrete-time depreciation.  No transcendental approximations —
    ///      DECLINING_BALANCE compounds the per-year factor (BP - rate)/BP
    ///      whole-year by whole-year, then linearly interpolates across the
    ///      trailing partial year.  Matches the convention most accounting
    ///      systems use for declining-balance schedules and avoids the
    ///      accuracy / range-reduction headaches of a fixed-point exp(-x).
    function _depreciate(
        uint256 faceValue,
        DepreciationType depType,
        uint32 depRate,        // basis points per year
        uint256 floor,
        uint48 startAt
    ) internal view returns (uint256) {
        if (depType == DepreciationType.NONE || block.timestamp <= startAt) {
            return faceValue;
        }
        if (faceValue <= floor) return floor;

        uint256 elapsed     = block.timestamp - startAt;
        uint256 depreciable = faceValue - floor;

        if (depType == DepreciationType.LINEAR) {
            uint256 loss = depreciable * uint256(depRate) * elapsed
                           / (SECONDS_PER_YEAR * BP);
            if (loss >= depreciable) return floor;
            return faceValue - loss;
        }

        if (depType == DepreciationType.DECLINING_BALANCE) {
            if (depRate == 0)        return faceValue;
            if (depRate >= BP)       return floor;

            uint256 wholeYears = elapsed / SECONDS_PER_YEAR;
            if (wholeYears >= MAX_DEP_YEARS) return floor;

            uint256 keep = BP - uint256(depRate);
            uint256 v    = depreciable;
            for (uint256 i = 0; i < wholeYears; i++) {
                v = v * keep / BP;
                if (v == 0) return floor;
            }
            // Linear interpolation across the remaining partial year:
            //   v(t) = v - (v - v_next) * fracSec / SECONDS_PER_YEAR
            uint256 fracSec = elapsed - wholeYears * SECONDS_PER_YEAR;
            if (fracSec != 0) {
                uint256 vNext = v * keep / BP;
                v = v - (v - vNext) * fracSec / SECONDS_PER_YEAR;
            }
            return floor + v;
        }

        return faceValue; // fallback
    }

    // ── Activation ──────────────────────────────────────────────────
    //
    // Activation and deactivation are NOT public operations.  Both are
    // bundled into the holder's `Buck.mint(N, [tids])` / `Buck.burn(N,
    // [tids])` calls, which atomically (a) compute the per-NFT take /
    // unwind via the cheapest-first / most-expensive-first inversion,
    // (b) move the corresponding pool principal between the holder and
    // insurancePool, and (c) grow / shrink activatedValue.  The
    // economic reason: a policy is a one-time purchase.  Paying the
    // pool principal at activation time and earning the 10%-ROI yield
    // on it from the insurance pool exactly funds the annual premium
    // on the activated coverage in perpetuity -- so the activation
    // doesn't need a recurring fee, but it MUST come with the upfront
    // principal payment or the pool has no yield to draw from.
    //
    // Allowing a free standalone `activate(tid, A)` would let a holder
    // self-issue arbitrary credit headroom without ever paying the
    // pool: balanceOf would jump by `A * buckK / 1e18` from "unused
    // credit" headroom that nothing backs.  That's why this surface
    // exposes only `activateFromBuck` / `deactivateFromBuck`, restricted
    // to the registered Buck contract.

    /// @notice Activate `amount` of coverage on behalf of `holder`,
    ///         restricted to the registered Buck contract.  Called from
    ///         Buck._allocateMint as part of the atomic activate-pay-draw
    ///         sequence; the funding-factor reserve check runs upstream
    ///         in Buck._mintAllocated against the holder's pre-mint
    ///         balanceOf (held + unused credit).
    function activateFromBuck(uint256 tokenId, address holder, uint256 amount) external {
        require(msg.sender == buck && buck != address(0), "BuckCredit: not buck");
        require(ownerOf(tokenId) == holder, "BuckCredit: not holder");
        _activate(tokenId, holder, amount);
    }

    /// @notice Deactivate `amount` of coverage on behalf of `holder`, restricted
    ///         to Buck.  Mirror of activateFromBuck for the burn-side unwind.
    ///         (Jubilee relief is Buck's: it accrues on the holder's lien,
    ///         not on coverage.)
    function deactivateFromBuck(uint256 tokenId, address holder, uint256 amount) external {
        require(msg.sender == buck && buck != address(0), "BuckCredit: not buck");
        require(ownerOf(tokenId) == holder, "BuckCredit: not holder");
        CreditParams storage c = credits[tokenId];
        uint256 current = c.activatedValue.asUint();
        require(amount <= current, "BuckCredit: deactivate > active");
        c.activatedValue  = toBuckQty(current - amount);

        emit CreditActivated(tokenId, holder, 0, current - amount);
    }

    function _activate(uint256 tokenId, address holder, uint256 amount) internal {
        if (amount == 0) return;
        CreditParams storage c = credits[tokenId];
        uint256 newActivated = c.activatedValue.asUint() + amount;
        require(newActivated <= c.faceValue.asUint(), "Exceeds face value");

        c.activatedValue  = toBuckQty(newActivated);

        emit CreditActivated(tokenId, holder, amount, newActivated);
    }

    /// @notice Compact (faceValue, activatedValue, premiumRate) view used by
    ///         Buck.mint() to walk a holder's NFTs without unpacking the full
    ///         CreditParams tuple per token.  Returned in uint256 form for
    ///         arithmetic ergonomics on the consumer side.
    function creditInfo(uint256 tokenId)
        external view returns (uint256 faceValue, uint256 activatedValue, uint32 premiumRate)
    {
        CreditParams storage c = credits[tokenId];
        return (c.faceValue.asUint(), c.activatedValue.asUint(), c.premiumRate);
    }

    /// @notice Bulk variant of creditInfo + ownerOf: returns one CreditSlice
    ///         per tokenId, all in a single external call.  Used by
    ///         Buck._allocateMint / _allocateBurn to walk a holder's NFT
    ///         list without paying per-NFT cross-contract dispatch overhead
    ///         (~700 gas warm per call, ~2 calls per NFT in the old per-iter
    ///         pattern).  Reverts on the first unknown tokenId (via
    ///         ownerOf), matching the per-iter pattern's failure mode.
    function batchCreditInfo(uint256[] calldata tokenIds)
        external view returns (CreditSlice[] memory slices)
    {
        slices = new CreditSlice[](tokenIds.length);
        for (uint256 i = 0; i < tokenIds.length; i++) {
            uint256 tid = tokenIds[i];
            CreditParams storage c = credits[tid];
            slices[i] = CreditSlice({
                owner:           ownerOf(tid),
                faceValue:       c.faceValue.asUint(),
                depreciatedFace: depreciatedFaceValue(tid),
                activatedValue:  c.activatedValue.asUint(),
                premiumRate:     c.premiumRate
            });
        }
    }

    /// @notice Aggregate current value of all BuckCredits owned by an account.
    /// @dev Called by Buck.mint() to compute the credit limit.
    /// 
    /// The sum of all currently available BuckCredit insurable assets, at their
    /// present value.
    function totalCurrentValue(address account) external view returns (uint256) {
        uint256 total = 0;
        uint256 count = balanceOf(account);
        for (uint256 i = 0; i < count; i++) {
            total += currentValue(tokenOfOwnerByIndex(account, i));
        }
        return total;
    }

    // ── Insurer Updates ─────────────────────────────────────────────

    /// @notice Insurer updates credit parameters (reappraisal, schedule change).
    function updateCredit(
        uint256 tokenId,
        uint256 newFaceValue,
        uint256 newDepreciationFloor,
        DepreciationType newDepType,
        uint32 newDepRate,
        uint48 newDepStartAt,
        uint32 newPremiumRate
    ) external {
        CreditParams storage c = credits[tokenId];
        require(msg.sender == c.insurer,                "Not insurer");
        require(newDepreciationFloor <= newFaceValue,   "floor > face");
        // A credit is MARKED from creation or never (see `mark`).
        require((c.depType == DepreciationType.MARKED) == (newDepType == DepreciationType.MARKED),
                "BuckCredit: MARKED is fixed at creation");

        // An insurer may reappraise freely down to the coverage the holder
        // has already bought, and no further.  Activated coverage is a
        // completed purchase: its pool principal was paid up front and funds
        // its premium in perpetuity, so writing it down would be revoking a
        // policy, not revaluing an asset.  An asset that has genuinely lost
        // value is what the claim path is for; ordinary decline is what the
        // depreciation schedule below is for, and that still moves the
        // holder's credit limit without touching the coverage itself.
        //
        // This is also what keeps `activatedValue` and Buck's `mintsBacked`
        // equal.  Clamping one without the other used to leave the holder
        // unable to unwind: Buck sizes the burn from `mintsBacked` while
        // `deactivateFromBuck` measures it against `activatedValue`, so a
        // clamp stranded the position permanently -- burnable only down to
        // the clamped line, with the remainder stuck and the credit
        // disqualified from ever backing BUCK again.
        require(newFaceValue >= c.activatedValue.asUint(),
                "BuckCredit: face below activated coverage");
        // A reappraisal stays inside the envelope too, or an insurer writes
        // small and updates large.
        _requireEnvelope(false, bytes32(0), newFaceValue, newDepType, newDepRate, newPremiumRate);

        BuckQty newFace = toBuckQty(newFaceValue);  // bound-check up front

        c.faceValue         = newFace;
        c.depreciationFloor = toBuckQty(newDepreciationFloor);
        c.depType           = newDepType;
        c.depRate           = newDepRate;
        c.depStartAt        = newDepStartAt;
        c.premiumRate       = newPremiumRate;
        c.lastUpdated       = uint48(block.timestamp);

        emit CreditUpdated(tokenId, msg.sender, newFaceValue, newDepRate, newPremiumRate);
    }

    /// @notice Mark a MARKED credit at `value`: its present value from now,
    ///         capped by the face.  Only its insurer (who is its holder).
    function mark(uint256 tokenId, uint256 value) external {
        CreditParams storage c = credits[tokenId];
        require(msg.sender == c.insurer, "Not insurer");
        require(c.depType == DepreciationType.MARKED, "BuckCredit: not marked");
        markOf[tokenId] = value;
        emit CreditMarked(tokenId, value);
    }
}
