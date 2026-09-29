// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test}    from "forge-std/Test.sol";
import {ERC20}   from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20}  from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {BN254}                   from "../../src/BN254.sol";
import {IdentityRegistry}        from "../../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "../harness/IdentityRegistryHarness.sol";
import {bindCarryingPool}        from "../harness/CarryingPool.sol";
import {Buck}                    from "../../src/Buck.sol";
import {BuckCredit}              from "../../src/BuckCredit.sol";
import {BuckBasketEquity, IEquityWheel} from "../../src/basket/BuckBasketEquity.sol";
import {BuckBasketEquityWheel} from "../../src/basket/BuckBasketEquityWheel.sol";
import {BuckBasketEquityStorage} from "../../src/basket/BuckBasketEquityStorage.sol";
import {BuckBasketUniswapV3}   from "../../src/basket/BuckBasketUniswapV3.sol";
import {BuckBasketReceipt}     from "../../src/basket/BuckBasketReceipt.sol";

/// @dev Buck's K source and the basket's controller in one: K is set by hand.
contract EqController {
    uint256 public k = 0.75e18;
    function setK(uint256 k_) external { k = k_; }
    function currentBuckK() external view returns (uint256) { return k; }
    function compute() external view returns (uint256) { return k; }
    function fundingFactor() external pure returns (uint256) { return 0; }
    function reprime() external {}
}

contract EqToken is ERC20 {
    uint8 immutable _dec;
    constructor(string memory n, string memory s, uint8 d) ERC20(n, s) { _dec = d; }
    function decimals() public view override returns (uint8) { return _dec; }
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

/// @dev A depositor's code: a bound identity must be a contract, and the
///      receipt is a safe-minted ERC-721.
contract EqHolder {
    function onERC721Received(address, address, uint256, bytes calldata) external pure returns (bytes4) {
        return this.onERC721Received.selector;
    }
}

interface IPool {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function slot0() external view returns (uint160, int24, uint16, uint16, uint16, uint8, bool);
    function liquidity() external view returns (uint128);
    function mint(address recipient, int24 tickLower, int24 tickUpper, uint128 amount,
                  bytes calldata data) external returns (uint256, uint256);
    function swap(address recipient, bool zeroForOne, int256 amountSpecified,
                  uint160 sqrtPriceLimitX96, bytes calldata data) external returns (int256, int256);
}

/// @title The equity BuckBasket as a credit holder (doc/BASKET-EQUITY.org 13.6,
///        doc/JUBILEE-ISSUANCE.org section 4), on the real Buck and BuckCredit:
///        its limit is K x its mark, its debt its lien, its relief real.  The
///        test contract is the basket's work wheel (it steps the components
///        itself) and an outside LP and arbitrageur in every pool, drawing its
///        BUCK on a credit of its own.
contract BuckBasketEquityTest is Test {
    uint160 internal constant MIN_SQRT = 4295128739;
    uint160 internal constant MAX_SQRT = 1461446703485210103287273052203988822378723970342;
    address constant GOV  = address(0xA0);
    address constant POOL = address(0xBA51C);            // Buck's insurance pool
    uint256 constant B    = 1e6;                          // one BUCK, raw
    uint256 constant FACE = 1e17;                         // the basket's credit: K x 1e11 BUCK at most

    IdentityRegistry internal reg;
    BuckCredit       internal credit;
    Buck             internal buck;
    EqController     internal ctrl;
    BuckBasketEquity internal b;
    EqToken[3]       internal tok;
    address[3]       internal pools;
    uint256[3]       internal price   = [uint256(1 * B), 2 * B, 3 * B];   // BUCK raw per TOKEN
    uint256[3]       internal price18 = [uint256(1e18), 2e18, 3e18];
    uint160[3]       internal ref;           // each pool's reference price (the outside market)

    address internal alice = address(0xA11CE);
    address internal bob   = address(0xB0B);

    function _bind(address a, bool carrying) internal {
        reg.bindContract(a, BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), true, carrying);
    }

    function setUp() public virtual {
        vm.warp(1_000_000);
        reg    = new IdentityRegistryHarness(GOV);
        credit = new BuckCredit();
        ctrl   = new EqController();
        buck   = new Buck(address(credit), address(ctrl), address(reg), POOL);
        bindCarryingPool(reg, POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));
        credit.setBuck(address(buck));

        // The outside market (this contract) draws its BUCK on a credit of its own.
        _bind(address(this), false);
        credit.setCreditIssuer(address(this), true);
        uint256 own = credit.createCredit(address(this), 0, 1e14, 1e14,
                                          BuckCredit.DepreciationType.NONE, 0, 0, 0);
        uint256[] memory ids = new uint256[](1);
        ids[0] = own;
        buck.mint(1e14, ids);
        address holder = address(new EqHolder());
        for (uint256 j = 0; j < 3; j++) {
            address who = j == 0 ? alice : (j == 1 ? bob : GOV);
            vm.etch(who, holder.code);
            _bind(who, false);
        }

        address factory = deployCode("out/UniswapV3Factory.sol/UniswapV3Factory.json");
        b = _newBasket(factory);
        _bind(address(b), false);                         // a credit holder: non-Carrying
        vm.startPrank(GOV);
        b.setVenue(address(new BuckBasketUniswapV3()));
        b.setEquityWheel(address(new BuckBasketEquityWheel()));
        (bool ok,) = address(b).call(abi.encodeWithSignature("setWheel(address)", address(this)));
        require(ok, "setWheel");
        b.openCredit(address(credit), FACE);
        vm.stopPrank();

        for (uint256 i = 0; i < 3; i++) {
            tok[i] = new EqToken(string(abi.encodePacked("T", bytes1(uint8(48 + i)))), "T", 18);
            tok[i].mint(address(this), 1e30);
            tok[i].mint(alice, 1e24);
            tok[i].mint(bob, 1e24);
            vm.prank(GOV);
            pools[i] = b.addBasketToken(address(tok[i]), 18, price[i], 0, 3000);
            bindCarryingPool(reg, pools[i]);
            _seed(i, uint128(1e27 / _sqrt18(price18[i])));  // an outside LP: 1M BUCK a side
            (ref[i],,,,,,) = IPool(pools[i]).slot0();
        }
        buck.transfer(alice, 1_000_000 * B);
        buck.transfer(bob, 1_000_000 * B);
        _age();
    }

    // ---- the world ------------------------------------------------------------ //

    function _newBasket(address factory) internal virtual returns (BuckBasketEquity) {
        return new BuckBasketEquity(address(buck), address(ctrl), factory, GOV,
                                    3000, 600, 64, 500, 1e3);
    }

    function _sqrt18(uint256 x) internal pure returns (uint256 y) {       // sqrt(x) for 1e18 x, in 1e9
        uint256 z = (x + 1) / 2; y = x;
        while (z < y) { y = z; z = (x / z + z) / 2; }
    }

    function _seed(uint256 i, uint128 L) internal {
        (,,,,,, int24 lo, int24 hi,,,) = b.constituents(i);
        IPool(pools[i]).mint(address(this), lo, hi, L, "");
    }

    function uniswapV3MintCallback(uint256 a0, uint256 a1, bytes calldata) external {
        if (a0 > 0) IERC20(IPool(msg.sender).token0()).transfer(msg.sender, a0);
        if (a1 > 0) IERC20(IPool(msg.sender).token1()).transfer(msg.sender, a1);
    }

    function uniswapV3SwapCallback(int256 a0, int256 a1, bytes calldata) external {
        if (a0 > 0) IERC20(IPool(msg.sender).token0()).transfer(msg.sender, uint256(a0));
        if (a1 > 0) IERC20(IPool(msg.sender).token1()).transfer(msg.sender, uint256(a1));
    }

    /// @dev An outside trade moving pool i: sell `amount` of BUCK (up) or TOKEN (down).
    function _push(uint256 i, bool buckIn, uint256 amount) internal {
        address tin = buckIn ? address(buck) : address(tok[i]);
        bool z = IPool(pools[i]).token0() == tin;
        IPool(pools[i]).swap(address(this), z, int256(amount), z ? MIN_SQRT + 1 : MAX_SQRT - 1, "");
    }

    /// @dev Let the TWAPs catch up with the spots.
    function _age() internal {
        vm.warp(block.timestamp + 700);
        vm.roll(block.number + 1);
    }

    /// @dev The outside arbitrageur: pool i back to its reference price.
    function _repin(uint256 i) internal {
        (uint160 sp,,,,,,) = IPool(pools[i]).slot0();
        if (sp == ref[i]) return;
        bool z = sp > ref[i];                            // price down: sell token0
        IPool(pools[i]).swap(address(this), z, int256(1e30), ref[i], "");
    }

    /// @dev A new reference: the outside market moved pool i to where it is.
    function _setRef(uint256 i) internal {
        (ref[i],,,,,,) = IPool(pools[i]).slot0();
    }

    /// @dev Run the Daily component alone, as the wheel would.
    function _daily() internal virtual {
        IEquityWheel(address(b)).wheelStep(0, 0);
    }

    /// @dev The wheel: step every due component, the arbitrage re-pinning the
    ///      pools after each round, `rounds` times; a day apart when `days_`,
    ///      else a TWAP window.
    function _wheel(uint256 rounds, bool days_) internal virtual {
        for (uint256 r = 0; r < rounds; r++) {
            for (uint8 kind = 0; kind < 5; kind++) {
                uint256 slots = (kind == 1 || kind == 2) ? 3 : 1;
                for (uint256 i = 0; i < slots; i++) {
                    if (IEquityWheel(address(b)).wheelDue(kind, i)) {
                        IEquityWheel(address(b)).wheelStep(kind, i);
                    }
                }
            }
            for (uint256 i = 0; i < 3; i++) _repin(i);
            vm.warp(block.timestamp + (days_ ? 1 days : 700));
            vm.roll(block.number + 1);
        }
    }

    function _deposit(address who, address asset, uint256 amount) internal returns (uint256 id) {
        vm.startPrank(who);
        IERC20(asset).approve(address(b), amount);
        id = b.deposit(asset, amount, 0);
        vm.stopPrank();
    }

    function _placed() internal returns (uint256 id) {
        id = _deposit(alice, address(buck), 300_000 * B);
        _wheel(60, false);
    }

    function _lien() internal view returns (uint256) {
        int256 s_ = buck.signedBalanceOf(address(b));
        return s_ < 0 ? uint256(-s_) : 0;
    }

    /// @dev The books: equity is the gross, plus the account at Buck and the
    ///      relief accrued on it, less the desk's position; the wallet's TOKEN
    ///      is backed.
    function _books() internal view {
        int256 signed = buck.signedBalanceOf(address(b));
        uint256 relief = buck.reliefOf(address(b));
        (int256 deskBuck,) = b.deskPosition(relief, _lien());
        int256 e = int256(b.gross()) + signed + int256(relief) - deskBuck;
        assertEq(b.equity(), e > 0 ? uint256(e) : 0, "equity = gross + the account + relief");
        for (uint256 j = 0; j < 3; j++) {
            assertGe(tok[j].balanceOf(address(b)), b.idleToken(j), "the wallet is backed");
        }
    }

    // ---- the credit ------------------------------------------------------------ //

    function test_credit_isTheBasketsOwnMarkedCredit() public {
        uint256 id = b.creditId();
        assertEq(credit.ownerOf(id), address(b), "held by the basket");
        (address insurer,,,,, BuckCredit.DepreciationType dt,,,,,,) = credit.credits(id);
        assertEq(insurer, address(b), "its own insurer");
        assertEq(uint8(dt), uint8(BuckCredit.DepreciationType.MARKED));
        _deposit(alice, address(tok[1]), 1000e18);
        assertTrue(b.creditLive(), "activated by the first mark");
        assertEq(b.markNow(), b.grossAt(2), "marked at its equity at the exit marks");
        assertEq(buck.creditLimit(address(b)), ctrl.k() * b.markNow() / 1e18,
                 "Buck enforces K x the mark");
    }

    // ---- deposit --------------------------------------------------------------- //

    function test_deposit_booksEquityAndBringsItsCredit() public {
        uint256 id = _deposit(alice, address(tok[1]), 1000e18);      // 2000 BUCK of T1
        assertEq(b.idleToken(1), 1000e18, "the TOKEN waits in the wallet");
        assertEq(_lien(), 0, "nothing issued: the credit waits");
        assertApproxEqRel(b.liquidity(), 0.75e18 * 2000 * B / 1e18, 1e14, "K x its value, to spend");
        assertEq(b.liquidityOf(1), 0, "nothing placed by the deposit");
        (uint128 shares, uint128 basis) = b.holdings(id);
        assertApproxEqRel(basis, 2000 * B, 1e14, "valued at the pool's price (tick-rounded)");
        uint256 charge = uint256(3000) * 1e12 * 0.25e18 / 2e18;     // fee (1-K)/2
        assertApproxEqRel(shares, uint256(basis) * (1e18 - charge) / 1e18, 1e12);
        _books();
    }

    function test_deposit_buckPaysItsDeployment() public {
        uint256 id = _deposit(alice, address(buck), 1000 * B);
        (uint128 shares,) = b.holdings(id);
        uint256 charge = uint256(3000) * 1e12 * 1.75e18 / 2e18;     // fee (1+K)/2
        assertApproxEqRel(shares, 1000 * B * (1e18 - charge) / 1e18, 1e12);
    }

    function test_deposit_guardRefusesAPushedPool() public {
        _push(0, true, 200_000 * B);                                // spot far above TWAP
        vm.startPrank(alice);
        buck.approve(address(b), B);
        vm.expectRevert();
        b.deposit(address(buck), B, 0);
        vm.stopPrank();
    }

    /// Ruled (JUBILEE-ISSUANCE 6.2.2): an under-water account stops issuing,
    /// so a deposit's credit first restores the limit.
    /// @dev A K that leaves the basket's lien 25% over its limit.
    function _cutUnderWater() internal returns (uint256 k2) {
        k2 = _lien() * 1e18 / b.markNow() * 8 / 10;
        ctrl.setK(k2);
    }

    function test_deposit_underWaterRestoresTheLimitFirst() public {
        _placed();
        uint256 k2 = _cutUnderWater();
        int256 h0 = b.headroom();
        assertLt(h0, 0, "the cut left it under water");
        uint256 lien0 = _lien();
        _deposit(bob, address(tok[0]), 10_000e18);                  // 10k BUCK of T0
        assertEq(_lien(), lien0, "a deposit calls nothing back and issues nothing");
        assertApproxEqRel(uint256(b.headroom() - h0), k2 * 10_000 * B / 1e18, 0.01e18,
                          "its K x value goes to the limit first");
        _books();
    }

    // ---- placing --------------------------------------------------------------- //

    function test_wheel_placesADepositAcrossThePools() public {
        _placed();
        uint256[] memory w = b.weightsBp();
        for (uint256 i = 0; i < 3; i++) {
            assertGt(b.liquidityOf(i), 0, "every pool placed");
            // Within one Fund step (at most 2 x 1% of a pool's depth): with no
            // director, Fund places only while the spare passes half the target.
            assertApproxEqAbs(w[i], 3333, 500, "near the declared targets");
        }
        assertApproxEqRel(b.equity(), 300_000 * B, 0.01e18, "equity about what was brought");
        assertGt(_lien(), 0, "placing issued on its credit");
        assertLe(_lien(), ctrl.k() * b.equity() / 1e18, "within K x equity");
        assertLe(b.liquidity(), b.liquidityTarget() * 3 / 2 + b.gross() / 1000,
                 "only liquidity waits unspent");
        _books();
    }

    // ---- redemption ---------------------------------------------------------------- //

    function test_redeem_paysBuckTouchingNoPool() public {
        _placed();
        uint256 id = _deposit(bob, address(buck), 1000 * B);
        uint128[3] memory before;
        for (uint256 i = 0; i < 3; i++) before[i] = b.liquidityOf(i);
        (uint160 s0,,,,,,) = IPool(pools[0]).slot0();
        uint256 value = b.valueOf(id);
        uint256 got0 = buck.balanceOf(bob);
        vm.prank(bob);
        uint256 paid = b.redeem(id, 10000);
        assertEq(buck.balanceOf(bob) - got0, paid, "paid in BUCK");
        for (uint256 i = 0; i < 3; i++) assertEq(b.liquidityOf(i), before[i], "no pool touched");
        (uint160 s1,,,,,,) = IPool(pools[0]).slot0();
        assertEq(s1, s0, "no price moved");
        assertApproxEqRel(paid, value, 0.01e18, "about its value, less the charge");
        assertLe(paid, value);
        _books();
    }

    function test_redeem_theWheelRefillsLiquidityAfterAnExit() public {
        _placed();
        uint256 id = _deposit(bob, address(buck), 50_000 * B);
        _wheel(30, false);
        vm.prank(bob);
        uint256 paid = b.redeem(id, 10000);
        assertGt(paid, 0);
        _wheel(40, false);
        uint256 t = b.liquidityTarget();
        assertGe(b.liquidity() + b.gross() / 1000, t * (10000 - 5000) / 10000,
                 "liquidity back within its band");
        assertLe(_lien(), ctrl.k() * b.equity() / 1e18, "within K x equity");
        _books();
    }

    function test_redeem_aRoundTripTakesBackOnlyItsOwn() public {
        _placed();
        uint256 id = _deposit(bob, address(buck), 1000 * B);
        vm.prank(bob);
        uint256 paid = b.redeem(id, 10000);
        assertLe(paid, 1000 * B, "no more than it brought");
    }

    function test_redeem_leavesTheOthersPriceWhole() public {
        _placed();
        uint256 id = _deposit(bob, address(buck), 20_000 * B);
        _wheel(20, false);
        uint256 p0 = b.sharePrice();
        vm.prank(bob);
        b.redeem(id, 10000);
        assertGe(b.sharePrice(), p0 - p0 / 1e9, "the stayers' price whole");
    }

    function test_redeem_theBasketTakesAQuarterOfTheGain() public {
        uint256 id = _placed();
        for (uint256 i = 0; i < 3; i++) { _push(i, true, 100_000 * B); _setRef(i); }  // TOKENs up
        _age();
        uint256 value = b.valueOf(id);
        (, uint128 basis) = b.holdings(id);
        assertGt(value, basis, "a gain");
        uint256 t0 = b.treasuryShares();
        vm.prank(alice);
        b.redeem(id, 5000);
        uint256 half = (value - basis) / 2 * 2500 / 10000 * 1e18 / b.sharePrice();
        assertApproxEqRel(b.treasuryShares() - t0, half, 0.02e18, "25% of the gain, in shares");
    }

    function test_redeem_noCutOnALoss() public {
        uint256 id = _placed();
        for (uint256 i = 0; i < 3; i++) { _push(i, false, 50_000e18); _setRef(i); }  // TOKENs down
        _age();
        vm.prank(alice);
        b.redeem(id, 5000);
        assertEq(b.treasuryShares(), 0);
    }

    /// Ruled (6.2.6): an exit the account cannot pay in BUCK is paid in kind
    /// -- no swap, so no pool's price moves.
    function test_redeem_paysInKindBeyondTheLimit() public {
        _placed();
        uint256 id = _deposit(bob, address(buck), 300_000 * B);
        _wheel(80, false);
        _cutUnderWater();                      // a cut: under water, nothing to spend
        uint160[3] memory s0;
        for (uint256 i = 0; i < 3; i++) (s0[i],,,,,,) = IPool(pools[i]).slot0();
        uint256 p0 = b.sharePrice();
        uint256 v0 = b.valueOf(id);
        uint256[3] memory t0;
        for (uint256 i = 0; i < 3; i++) t0[i] = tok[i].balanceOf(bob);
        uint256 buck0 = buck.balanceOf(bob);
        vm.prank(bob);
        uint256 paid = b.redeem(id, 10000);
        uint256 got = paid + buck.balanceOf(bob) - buck0 - paid;     // BUCK received
        for (uint256 i = 0; i < 3; i++) {
            (uint160 s1,,,,,,) = IPool(pools[i]).slot0();
            assertEq(s1, s0[i], "no pool's price moved");
            uint256 dt = tok[i].balanceOf(bob) - t0[i];
            assertGt(dt, 0, "its TOKEN, in kind");
            got += dt * price[i] / 1e18;
        }
        (uint128 left,) = b.holdings(id);
        got += uint256(left) * b.sharePrice() / 1e18;              // any shares left unpaid
        assertApproxEqRel(got, v0, 0.02e18, "its value, in TOKEN, BUCK and shares");
        assertGe(b.sharePrice(), p0 * 999 / 1000, "the stayers' price about whole");
        // The credit is marked at what stays (its positions are gone), never at
        // what was: the limit must not outlive the collateral.
        int256 eLow = int256(b.grossAt(2)) + buck.signedBalanceOf(address(b))
                    + int256(buck.reliefOf(address(b)));
        (, int256 deskValue) = b.deskPosition(buck.reliefOf(address(b)), _lien());
        assertEq(b.markNow(), uint256(eLow + deskValue), "marked at the equity that stays");
        _books();
    }

    // ---- K --------------------------------------------------------------------------- //

    /// No margin calls: a K cut calls nothing back; the wheel then brings the
    /// basket back under its new limit at its own pace.
    function test_kCut_callsNothingBack_theWheelDeleverages() public {
        _placed();
        uint256 lien0 = _lien();
        _cutUnderWater();
        assertLt(b.headroom(), 0, "the cut took the headroom");
        assertEq(_lien(), lien0, "nothing called back");
        _wheel(80, false);
        assertLt(_lien(), lien0, "the wheel repaid the lien, trimming");
        assertGe(b.headroom(), -int256(b.gross() / 1000), "back under the limit");
        uint256 id = _deposit(bob, address(buck), 1000 * B);
        vm.prank(bob);
        b.redeem(id, 10000);
        _books();
    }

    // ---- relief ---------------------------------------------------------------------- //

    /// The basket's lien earns the Jubilee relief like any issuer's: it
    /// accretes to the share price, and Daily collects it.
    function test_relief_accretesToTheSharePrice() public {
        _placed();
        uint256 lien0 = _lien();
        uint256 p0 = b.sharePrice();
        vm.warp(block.timestamp + 365 days + 6 hours);
        uint256 r = buck.reliefOf(address(b));
        assertApproxEqRel(r, lien0 * 2 / 100, 0.01e18, "2% of the lien over the year");
        uint256 e0 = b.equity();
        _daily();
        assertLe(buck.reliefOf(address(b)), 1, "collected");
        assertApproxEqAbs(_lien(), lien0 - r, 1, "the lien fell by it");
        assertApproxEqAbs(b.equity(), e0, e0 / 1e9 + 2, "equity already counted it");
        assertGt(b.sharePrice(), p0, "the holders earned it");
    }

    // ---- the wheel's credits ---------------------------------------------------------- //

    function test_credits_landInTheWallet() public virtual {
        _placed();
        uint256 t0 = b.idleToken(2);
        tok[2].approve(address(b), 5e18);
        b.creditDepositors(2, 5e18);
        assertEq(b.idleToken(2), t0 + 5e18);
        vm.prank(bob);
        vm.expectRevert();
        b.creditTreasury(1);
    }

    function test_treasury_leavesByTheSameDoor() public {
        uint256 id = _placed();
        for (uint256 i = 0; i < 3; i++) { _push(i, true, 100_000 * B); _setRef(i); }
        _age();
        vm.prank(alice);
        b.redeem(id, 5000);
        uint256 ts = b.treasuryShares();
        assertGt(ts, 0);
        vm.prank(GOV);
        uint256 paid = b.redeemTreasury(10000, GOV);
        assertEq(b.treasuryShares(), 0);
        assertEq(buck.balanceOf(GOV), paid);
        _books();
    }

    // ---- the whole machine -------------------------------------------------------------- //

    function test_theWholeMachineKeepsItsBooks() public {
        _placed();
        uint256[] memory open_ = new uint256[](64);
        uint256 nOpen;
        uint256 seed = 7;
        for (uint256 step = 0; step < 60; step++) {
            seed = uint256(keccak256(abi.encode(seed)));
            uint256 i = seed % 3;
            bool up = (seed >> 8) % 2 == 0;
            uint256 amt = 500 + (seed >> 16) % 20_000;
            _push(i, up, up ? amt * B : amt * 1e18);
            _setRef(i);
            _age();
            ctrl.setK(0.6e18 + ((seed >> 32) % 30) * 0.01e18);
            if ((seed >> 40) % 2 == 0 && nOpen < 64) {
                bool inBuck = (seed >> 48) % 3 == 0;
                uint256 d = 100 + (seed >> 56) % 5000;
                open_[nOpen++] = _deposit(bob, inBuck ? address(buck) : address(tok[i]),
                                          inBuck ? d * B : d * 1e18);
            }
            if (nOpen > 0 && (seed >> 64) % 3 == 0) {
                uint256 k = (seed >> 72) % nOpen;
                vm.prank(bob);
                b.redeem(open_[k], 10000);
                (uint128 left,) = b.holdings(open_[k]);
                if (left == 0) open_[k] = open_[--nOpen];
            }
            _wheel(1, true);
            _books();
        }
    }
}
