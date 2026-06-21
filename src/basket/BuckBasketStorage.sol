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

    /// @notice Treasury BUCK profit awaiting re-LP by the venue facet.
    uint256 public treasuryBuckPending;

    /// @notice BUCK profit share to treasury, in basis points (default 50%).
    uint16 public treasuryBp;

    // --- Wiring (plain storage so facets see it via delegatecall) ---------- //

    IBuckMintBurn     public buck;
    BuckBasketReceipt public receipt;
    IBuckKController  public controller;
    IUniswapV3Factory public v3Factory;
    IBuckBasketVenue  public venue;        // the delegatecall AMM-venue facet
    address           public governance;

    uint24  public defaultFeeTier;
    uint32  public twapWindow;
    uint16  public observationCardinality;
    uint256 public defaultMaxDeviationBp;
    uint256 public minSeedLiquidity;

    // --- Constants -------------------------------------------------------- //

    uint160 internal constant MIN_SQRT_RATIO = 4295128739;
    uint160 internal constant MAX_SQRT_RATIO =
        1461446703485210103287273052203988822378723970342;
    uint16  public  constant  MAX_TREASURY_BP = 9000;      // cap governance take
    uint256 internal constant MAX_DUST_WEI = 1e9;          // 1e-9 BUCK
    uint256 internal constant MIN_REINVEST_BUCK = 1e15;    // 0.001 BUCK
    uint256 public  constant  DEFAULT_CONVERSION_LOSS_BP = 100;   // 1%

    // --- Callback re-entry guards ----------------------------------------- //
    address internal _callbackPool;
    address internal _swapCallbackPool;

    // --- Events ----------------------------------------------------------- //

    event BasketTokenAdded(address indexed token, uint256 weightBp, uint256 initialPriceInBuck, address pool);
    event Deposited(address indexed who, uint256 indexed receiptId, address token, uint256 tokenAmount, uint256 buckMinted, uint128 liquidity);
    event Redeemed(address indexed who, uint256 indexed receiptId, uint256 burned, uint256 depositorBuck, uint256 treasuryBuck, uint256 remainingBp);
    event TreasuryAccrued(uint256 amount, uint256 pending);
    event TreasuryWithdrawn(address indexed to, uint256 amount);
    event TreasuryReinvested(uint256 indexed poolIdx, uint256 buckConsumed, uint128 liquidity);
    event VenueSet(address indexed venue);
    event TreasuryBpSet(uint16 treasuryBp);

    // --- Errors (custom errors save bytecode vs require-strings) ----------- //
    error AlreadyPresent();
    error Amount0();
    error BUCKDepositTODO();
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
    error EmptyDeposit();
    error EmptyPool();
    error ExceedsPending();
    error Gov0();
    error InvalidRescale();
    error L0();
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
    error SwapDeltaSign();
    error To0();
    error TokenIn0();
    error TokenTooThin();
    error TreasuryBpTooHigh();
    error Underwater();
    error VenueUnset();

    modifier onlyGov() {
        if (!(msg.sender == governance)) revert NotGovernance();
        _;
    }
}
