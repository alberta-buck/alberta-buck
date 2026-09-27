// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IBuckKController}    from "../IBuckKController.sol";
import {BuckBasketReceipt}  from "./BuckBasketReceipt.sol";
import {IBuckBasketVenue}   from "./IBuckBasketVenue.sol";

interface IUniswapV3Factory {
    function createPool(address tokenA, address tokenB, uint24 fee) external returns (address pool);
    function getPool(address tokenA, address tokenB, uint24 fee) external view returns (address pool);
    function feeAmountTickSpacing(uint24 fee) external view returns (int24);
}

interface IUniswapV3MintCallback {
    function uniswapV3MintCallback(uint256 amount0Owed, uint256 amount1Owed, bytes calldata data) external;
}

interface IUniswapV3SwapCallback {
    function uniswapV3SwapCallback(int256, int256, bytes calldata) external;
}

interface IBuckMintBurn {
    function mintFromBasket(address to, uint256 amount) external;
    function burnFromBasket(uint256 amount) external;
    function balanceOf(address) external view returns (uint256);
}

/// @title BuckBasketStorage -- the single storage layout shared by the
///        BuckBasketProRata shell and its delegatecall facet(s).
///
/// @notice Both the shell and any facet inherit *only* this base and add no
///         state of their own, so their storage layouts are provably identical
///         and a facet run via `delegatecall` reads/writes the shell's slots
///         correctly.  The wiring addresses are plain storage (not `immutable`)
///         precisely so a facet sees them through the shared storage rather than
///         its own (immutables live in code, which would be the facet's, not the
///         shell's).
abstract contract BuckBasketStorage {

    // --- Constituents ----------------------------------------------------- //

    // Field order matches the legacy BuckBasket.Constituent so the public
    // `constituents(i)` getter is positionally compatible (the sim reads
    // `c[2] == basketAmount`); `treasuryLiquidity` is appended.
    struct Constituent {
        address token;
        uint8   decimals;
        uint256 basketAmount;         // 18-dec; Σ basketAmount*price = 1 BUCK at init
        uint256 initialPriceInBuck;   // 18-dec
        uint24  feeTier;
        address pool;
        int24   tickLower;
        int24   tickUpper;
        bool    buckIsToken0;
        uint256 targetWeightBp;       // declared weight, sums to 10000 across all
        uint128 treasuryLiquidity;    // treasury-owned L slice in this pool
    }

    Constituent[] public constituents;
    mapping(address => uint256) public indexOf;   // token -> 1+index (0 = absent)

    struct Deposit {
        uint256 buckPrincipal;     // BUCK minted at deposit (the share unit)
        uint256 tokenPrincipal;    // native-dec TOKEN deposited (ROI telemetry)
        address token;             // original deposit token
        uint64  depositTime;
    }
    mapping(uint256 => Deposit) public deposits;

    /// @notice Total outstanding BUCK principal == Σ buckPrincipal.
    uint256 public totalOutstandingBuck;

    /// @notice Treasury BUCK profit awaiting re-LP by the venue facet.  The
    ///         depositor is paid in TOKEN only; the entire BUCK profit (the
    ///         seigniorage the basket minted and burned on the depositor's
    ///         behalf) stays with the basket as treasury equity.
    uint256 public treasuryBuckPending;

    // --- Wiring (plain storage so facets see it via delegatecall) ---------- //

    IBuckMintBurn     public buck;
    BuckBasketReceipt public receipt;
    IBuckKController  public controller;
    IUniswapV3Factory public v3Factory;
    IBuckBasketVenue  public venue;        // the delegatecall AMM-venue facet
    address           public governance;
    address           public director;     // optional IRebalanceDirector advisor
    uint32            public lastRebalanceStepEpoch;   // +1-encoded; 0 = never

    uint24  public defaultFeeTier;
    uint32  public twapWindow;
    uint16  public observationCardinality;
    uint256 public defaultMaxDeviationBp;
    uint256 public minSeedLiquidity;

    // --- Monetary operations book (BuckBasketOps) -------------------------- //
    //
    // A THIRD kind of liquidity, alongside depositor and treasury.  The
    // depositor claim is computed from `poolBuckValues`, which reads the
    // basket's LP POSITION (`_positionLiquidity`) net of `treasuryLiquidity`
    // -- so a monetary book held as plain TOKEN/BUCK BALANCES is outside the
    // claim by construction, and no line of the redemption allocator changes.
    // That is deliberate: `totalOutstandingBuck` is the denominator of every
    // depositor payout (theta = R/O appears four times across
    // _allocateSellHigh and _allocateSingleToken), and the safest way to keep
    // monetary BUCK out of it is never to put it in.
    //
    // `mintFromBasket` does not touch totalOutstandingBuck either -- only
    // depositToken / _depositBuck / redeem do -- so issuance for a monetary
    // operation cannot dilute a receipt even by accident.
    //
    // The Constituent struct is deliberately NOT extended: two tests and the
    // director destructure it with fixed 11-slot arity, and appending a field
    // would break them silently.

    /// @notice Net BUCK put into circulation by monetary operations.
    ///         Positive = issued (Q4), negative = retired (Q2).  This is the
    ///         separate liability class the operations article requires.
    int256  public monetaryOutstanding;

    /// @notice BUCK absorbed by a temporary operation (Q1) and not yet
    ///         released (Q3) or burned (Q2).  Held as a balance, not as LP.
    uint256 public monetaryBuckHeld;

    /// @notice TOKEN acquired by monetary issuance, per constituent index.
    ///         The desk's asset side: bought with money it issued, exactly as
    ///         a central bank's is.
    mapping(uint256 => uint256) public monetaryTokenHeld;

    /// @notice +1-encoded epoch of the last monetary operation (0 = never),
    ///         so the keeper entry point is once-per-epoch like the rebalancer.
    uint32  public lastMonetaryEpoch;

    // --- Constants -------------------------------------------------------- //

    uint160 internal constant MIN_SQRT_RATIO = 4295128739;
    uint160 internal constant MAX_SQRT_RATIO =
        1461446703485210103287273052203988822378723970342;
    uint256 internal constant MAX_DUST_WEI = 1e9;          // 1e-9 BUCK
    uint256 internal constant MIN_REINVEST_BUCK = 1e15;    // 0.001 BUCK
    uint256 public  constant  DEFAULT_CONVERSION_LOSS_BP = 100;   // 1%

    // --- Callback re-entry guards ----------------------------------------- //
    address internal _callbackPool;
    address internal _swapCallbackPool;

    // --- Stress fee on duress exits (WP-5; CARRY-CONVEXITY D2) ------------ //
    //
    // APPENDED at the end of the shared layout: every slot above keeps its
    // position for the venue facet and for the ops / fence shells that add
    // state of their own after this base.
    //
    // A redemption that lands in the DEFLATION branch (Bw < R: BUCK dear,
    // basketValueInBuck < 1) while the TWAP deviation exceeds the deadband
    // pays a fee proportional to the deviation.  The fee is taken in TOKEN
    // from the payout and re-LP'd into its own pool with freshly minted
    // partner BUCK as DEPOSITOR liquidity (the mechanics of a deposit), so
    // it accrues to the remaining holders and never to the treasury.  The
    // partner BUCK is a burn obligation like any deposit's: it is carried in
    // `stressBonusPrincipal`, counted in `totalOutstandingBuck`, and retired
    // pro rata by every later redemption (the last receipt retires all of
    // it), so the basket still burns exactly what it minted.
    //
    //   totalOutstandingBuck == sum buckPrincipal + stressBonusPrincipal

    /// @notice No fee while the TWAP deviation below par is <= this (bp).
    uint256 public stressFeeDeadbandBp;
    /// @notice Fee, in bp of the redemption value V, per 1% of deviation
    ///         beyond the deadband.
    uint256 public stressFeeSlopeBp;
    /// @notice Cap on the fee (bp of V).  0 disables the fee.
    uint256 public stressFeeMaxBp;
    /// @notice Partner BUCK minted for re-LP'd stress fees -- and for the work
    ///         wheel's depositor credits, the same mechanics -- still outstanding.
    uint256 public stressBonusPrincipal;

    // --- The work wheel (doc/BASKET-WHEEL.org) ----------------------------- //
    //
    // APPENDED.  The wheel's consistency arbitrage credits what it captures:
    // TOKEN to the DEPOSITORS with the stress fee's mechanics (re-LP'd as
    // depositor liquidity, the partner BUCK booked in stressBonusPrincipal,
    // so the invariant above holds), BUCK to the treasury.

    /// @notice The work wheel allowed to credit the basket (0: none).
    address public wheel;

    // --- Events ----------------------------------------------------------- //

    event BasketTokenAdded(address indexed token, uint256 weightBp, uint256 initialPriceInBuck, address pool);
    event Deposited(address indexed who, uint256 indexed receiptId, address token, uint256 tokenAmount, uint256 buckMinted, uint128 liquidity);
    event Redeemed(address indexed who, uint256 indexed receiptId, uint256 burned, uint256 depositorBuck, uint256 treasuryBuck, uint256 remainingBp);
    event TreasuryAccrued(uint256 amount, uint256 pending);
    event TreasuryWithdrawn(address indexed to, uint256 amount);
    event TreasuryReinvested(uint256 indexed poolIdx, uint256 buckConsumed, uint128 liquidity);
    event VenueSet(address indexed venue);
    event DirectorSet(address indexed director);
    event RebalanceStepped(uint256 indexed sellIdx, uint256 indexed buyIdx,
                           uint256 valueMoved, uint256 buckReinvested, uint128 liquidity);
    /// @param quadrant 1=absorb 2=retire 3=supply 4=issue
    event MonetaryOperation(uint8 indexed quadrant, int32 effortBp, bool outright,
                            uint256 buckMoved, int256 outstanding, uint256 buckHeld);
    /// @notice A duress exit paid the stress fee (WP-5).  `deviation1e18` is
    ///         the TWAP deviation below par, `feeBp` the fee in bp of the value
    ///         claim V, `feeValueBuck` the TOKEN value actually taken (spot).
    event StressFee(uint256 indexed receiptId, uint256 deviation1e18, uint256 feeBp, uint256 feeValueBuck);
    event StressFeeSet(uint256 deadbandBp, uint256 slopeBp, uint256 maxBp);
    event WheelSet(address indexed wheel);
    event WheelCredit(uint256 indexed poolIdx, uint256 tokenAmount, uint256 partnerBuck);

    // --- Errors (custom errors save bytecode vs require-strings) ----------- //
    error AlreadyPresent();
    error Amount0();
    error BadCallback();
    error BadPrice();
    error BadSwapCallback();
    error BadTargetWeight();
    error BadToken();
    error BadWeight();
    error Bp10000();
    error Buck0();
    error BuckIn0();
    error ConversionLoss();
    error DirectorUnset();
    error EmptyDeposit();
    error EmptyPool();
    error ExceedsPending();
    error NotWheel();
    error NoDepositors();
    error Gov0();
    error InvalidRescale();
    error L0();
    error MonetaryBound();
    error MonetaryIdle();
    error NoAdvice();
    error NoLPWithdrawn();
    error NoOutstanding();
    error NoValue();
    error NotGovernance();
    error NotInBasket();
    error NotOwner();
    error NotSelf();
    error PayoutToken0();
    error RedeemZero();
    error ReinvestL0();
    error ScaledWeightsIncorrect();
    error SeedTooSmall();
    error Slippage();
    error StepAlreadyDone();
    error SwapDeltaSign();
    error To0();
    error TokenIn0();
    error TokenTooThin();
    error Underwater();
    error VenueUnset();

    modifier onlyGov() {
        if (!(msg.sender == governance)) revert NotGovernance();
        _;
    }
}
