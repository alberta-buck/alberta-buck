// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test}    from "forge-std/Test.sol";
import {ERC20}   from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20}  from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {BuckBasketEquity, IEquityWheel} from "../../src/basket/BuckBasketEquity.sol";
import {BuckBasketEquityWheel} from "../../src/basket/BuckBasketEquityWheel.sol";
import {BuckBasketEquityStorage} from "../../src/basket/BuckBasketEquityStorage.sol";
import {BuckBasketUniswapV3}   from "../../src/basket/BuckBasketUniswapV3.sol";
import {BuckBasketReceipt}     from "../../src/basket/BuckBasketReceipt.sol";

contract EqBuck is ERC20 {
    address public basket;
    constructor() ERC20("Buck", "BUCK") {}
    function setBasket(address b) external { basket = b; }
    function mintFromBasket(address to, uint256 amt) external {
        require(msg.sender == basket, "!basket"); _mint(to, amt);
    }
    function burnFromBasket(uint256 amt) external {
        require(msg.sender == basket, "!basket"); _burn(msg.sender, amt);
    }
    function mint(address to, uint256 amt) external { _mint(to, amt); }
}

contract EqController {
    uint256 public k = 0.75e18;
    function setK(uint256 k_) external { k = k_; }
    function currentBuckK() external view returns (uint256) { return k; }
    function compute() external pure returns (uint256) { return 1e18; }
    function reprime() external {}
}

contract EqToken is ERC20 {
    uint8 immutable _dec;
    constructor(string memory n, string memory s, uint8 d) ERC20(n, s) { _dec = d; }
    function decimals() public view override returns (uint8) { return _dec; }
    function mint(address to, uint256 amount) external { _mint(to, amount); }
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

/// @title The equity BuckBasket (doc/BASKET-EQUITY.org 13.6-13.7): the
///        Python prototype's promises (alberta_buck/test/test_reserve_basket.py,
///        test_equity_basket.py), one test each.  The test contract is the
///        basket's work wheel (it steps the components itself) and an outside
///        LP and arbitrageur in every pool.
contract BuckBasketEquityTest is Test {
    uint160 internal constant MIN_SQRT = 4295128739;
    uint160 internal constant MAX_SQRT = 1461446703485210103287273052203988822378723970342;
    address constant GOV = address(0xA0);

    EqBuck           internal buck;
    EqController     internal ctrl;
    BuckBasketEquity internal b;
    EqToken[3]       internal tok;
    address[3]       internal pools;
    uint256[3]       internal price = [uint256(1e18), 2e18, 3e18];
    uint160[3]       internal ref;           // each pool's reference price (the outside market)

    address internal alice = address(0xA11CE);
    address internal bob   = address(0xB0B);

    function setUp() public virtual {
        vm.warp(1_000_000);
        buck = new EqBuck();
        ctrl = new EqController();
        address factory = deployCode("out/UniswapV3Factory.sol/UniswapV3Factory.json");
        b = new BuckBasketEquity(address(buck), address(ctrl), factory, GOV,
                                 3000, 600, 64, 500, 1e3);
        buck.setBasket(address(b));
        vm.startPrank(GOV);
        b.setVenue(address(new BuckBasketUniswapV3()));
        b.setEquityWheel(address(new BuckBasketEquityWheel()));
        (bool ok,) = address(b).call(abi.encodeWithSignature("setWheel(address)", address(this)));
        require(ok, "setWheel");
        vm.stopPrank();

        buck.mint(address(this), 1e30);
        for (uint256 i = 0; i < 3; i++) {
            tok[i] = new EqToken(string(abi.encodePacked("T", bytes1(uint8(48 + i)))), "T", 18);
            tok[i].mint(address(this), 1e30);
            tok[i].mint(alice, 1e24);
            tok[i].mint(bob, 1e24);
            vm.prank(GOV);
            pools[i] = b.addBasketToken(address(tok[i]), 18, price[i], 0, 3000);
            _seed(i, uint128(1e6 * 1e18 / _sqrt18(price[i]) * 1e9));   // an outside LP: 1M BUCK a side
            (ref[i],,,,,,) = IPool(pools[i]).slot0();
        }
        buck.mint(alice, 1e24);
        buck.mint(bob, 1e24);
        _age();
    }

    // ---- the world ------------------------------------------------------------ //

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
        id = _deposit(alice, address(buck), 3e23);          // 300k BUCK
        _wheel(60, false);
    }

    function _books() internal view {
        assertEq(b.mintedTotal() - b.burnedTotal(), b.debt(), "minted - burned == debt");
    }

    // ---- deposit --------------------------------------------------------------- //

    function test_deposit_booksEquityAndMintsItsK() public {
        uint256 id = _deposit(alice, address(tok[1]), 1000e18);      // 2000 BUCK of T1
        assertEq(b.idleToken(1), 1000e18, "the TOKEN waits in the wallet");
        assertApproxEqRel(b.debt(), 0.75e18 * 2000, 1e14, "K x value minted at once");
        assertEq(b.idleBuck(), b.debt(), "the credit waits too");
        assertEq(b.liquidityOf(1), 0, "nothing placed by the deposit");
        (uint128 shares, uint128 basis) = b.holdings(id);
        assertApproxEqRel(basis, 2000e18, 1e14, "valued at the pool's price (tick-rounded)");
        uint256 charge = uint256(3000) * 1e12 * 0.25e18 / 2e18;     // fee (1-K)/2
        assertApproxEqRel(shares, uint256(basis) * (1e18 - charge) / 1e18, 1e12);
        _books();
    }

    function test_deposit_buckPaysItsDeployment() public {
        uint256 id = _deposit(alice, address(buck), 1000e18);
        (uint128 shares,) = b.holdings(id);
        uint256 charge = uint256(3000) * 1e12 * 1.75e18 / 2e18;     // fee (1+K)/2
        assertApproxEqRel(shares, 1000e18 * (1e18 - charge) / 1e18, 1e12);
    }

    function test_deposit_guardRefusesAPushedPool() public {
        _push(0, true, 200_000e18);                                 // spot far above TWAP
        vm.startPrank(alice);
        buck.approve(address(b), 1e18);
        vm.expectRevert();
        b.deposit(address(buck), 1e18, 0);
        vm.stopPrank();
    }

    // ---- placing --------------------------------------------------------------- //

    function test_wheel_placesADepositAcrossThePools() public {
        _placed();
        uint256[] memory w = b.weightsBp();
        for (uint256 i = 0; i < 3; i++) {
            assertGt(b.liquidityOf(i), 0, "every pool placed");
            assertApproxEqAbs(w[i], 3333, 300, "near the declared targets");
        }
        assertApproxEqRel(b.debt(), 0.75 * 3e23, 1e12, "placing mints nothing more");
        assertApproxEqRel(b.equity(), 3e23, 0.01e18, "equity about what was brought");
        assertLe(b.idleBuck(), b.liquidityTarget() * 3 / 2 + b.gross() / 1000,
                 "only liquidity waits");
        _books();
    }

    // ---- redemption ---------------------------------------------------------------- //

    function test_redeem_paysBuckFromTheWalletTouchingNoPool() public {
        _placed();
        uint256 id = _deposit(bob, address(buck), 1000e18);
        uint128[3] memory before;
        for (uint256 i = 0; i < 3; i++) before[i] = b.liquidityOf(i);
        (uint160 s0,,,,,,) = IPool(pools[0]).slot0();
        uint256 value = b.valueOf(id);
        uint256 debt0 = b.debt();
        uint256 got0 = buck.balanceOf(bob);
        vm.prank(bob);
        uint256 paid = b.redeem(id, 10000);
        assertEq(buck.balanceOf(bob) - got0, paid, "paid in BUCK");
        for (uint256 i = 0; i < 3; i++) assertEq(b.liquidityOf(i), before[i], "no pool touched");
        (uint160 s1,,,,,,) = IPool(pools[0]).slot0();
        assertEq(s1, s0, "no price moved");
        assertApproxEqRel(paid, value, 0.01e18, "about its value, less the charge");
        assertLe(paid, value);
        assertLt(b.debt() + b.owed(), debt0 + 1,
                 "its debt share burned from the wallet, or owed to the wheel");
        _books();
    }

    function test_redeem_theWheelRepaysWhatTheExitOwes() public {
        _placed();
        uint256 id = _deposit(bob, address(buck), 50_000e18);
        _wheel(30, false);
        uint256 debt0 = b.debt();
        vm.prank(bob);
        uint256 paid = b.redeem(id, 10000);
        _wheel(40, false);
        assertLe(b.owed(), b.gross() / 1000, "the owed repaid (to a grain)");
        assertLt(b.debt(), debt0 - 0.5 * 0.75 * 50_000e18, "its debt share burned");
        assertGt(paid, 0);
        _books();
    }

    function test_redeem_aRoundTripTakesBackOnlyItsOwn() public {
        _placed();
        uint256 id = _deposit(bob, address(buck), 1000e18);
        vm.prank(bob);
        uint256 paid = b.redeem(id, 10000);
        assertLe(paid, 1000e18, "no more than it brought");
    }

    function test_redeem_leavesTheOthersPriceWhole() public {
        uint256 first = _placed();
        uint256 id = _deposit(bob, address(buck), 20_000e18);
        _wheel(20, false);
        uint256 p0 = b.sharePrice();
        vm.prank(bob);
        b.redeem(id, 10000);
        assertGe(b.sharePrice(), p0 - p0 / 1e9, "the stayers' price whole");
        first;
    }

    function test_redeem_theBasketTakesAQuarterOfTheGain() public {
        uint256 id = _placed();
        for (uint256 i = 0; i < 3; i++) { _push(i, true, 100_000e18); _setRef(i); }  // TOKENs up
        _age();
        uint256 value = b.valueOf(id);
        (uint128 shares, uint128 basis) = b.holdings(id);
        assertGt(value, basis, "a gain");
        uint256 cutShares = (value - basis) * 2500 / 10000 * 1e18 / b.sharePrice();
        uint256 t0 = b.treasuryShares();
        vm.prank(alice);
        b.redeem(id, 5000);
        uint256 half = (value - basis) / 2 * 2500 / 10000 * 1e18 / b.sharePrice();
        assertApproxEqRel(b.treasuryShares() - t0, half, 0.02e18, "25% of the gain, in shares");
        shares; cutShares;
    }

    function test_redeem_noCutOnALoss() public {
        uint256 id = _placed();
        for (uint256 i = 0; i < 3; i++) { _push(i, false, 50_000e18); _setRef(i); }  // TOKENs down
        _age();
        vm.prank(alice);
        b.redeem(id, 5000);
        assertEq(b.treasuryShares(), 0);
    }

    function test_redeem_goesProRataBeyondTheLimit() public {
        _placed();
        uint256 id = _deposit(bob, address(buck), 300_000e18);
        _wheel(80, false);
        ctrl.setK(0.5e18);                     // a cut: no headroom to mint from
        uint256 p0 = b.sharePrice();
        vm.prank(bob);
        uint256 paid = b.redeem(id, 10000);
        assertGt(paid, 0);
        uint256 lsum;
        for (uint256 i = 0; i < 3; i++) lsum += b.liquidityOf(i);
        assertGe(b.sharePrice(), p0 * 999 / 1000, "the stayers' price about whole");
        _books();
        lsum;
    }

    // ---- K --------------------------------------------------------------------------- //

    function test_kCut_callsNothingBackAndHoldsLiquidity() public {
        _placed();
        uint256 debt0 = b.debt();
        ctrl.setK(0.6e18);
        assertLt(b.headroom(), 0, "the cut took the headroom");
        _wheel(40, false);
        assertEq(b.debt(), debt0, "nothing burned");
        assertGe(b.idleBuck(), b.liquidityTarget() / 2, "liquidity held as BUCK");
        uint256 id = _deposit(bob, address(buck), 1000e18);
        assertApproxEqRel(b.debt(), debt0 + 0.6e18 * 1000, 1e14, "a new deposit brings the new K");
        vm.prank(bob);
        b.redeem(id, 10000);
        _books();
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
        for (uint256 i = 0; i < 3; i++) { _push(i, true, 100_000e18); _setRef(i); }
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
            _push(i, (seed >> 8) % 2 == 0, 500e18 + (seed >> 16) % 20_000e18);
            _setRef(i);
            _age();
            ctrl.setK(0.6e18 + ((seed >> 32) % 30) * 0.01e18);
            if ((seed >> 40) % 2 == 0 && nOpen < 64) {
                address asset = (seed >> 48) % 3 == 0 ? address(buck) : address(tok[i]);
                open_[nOpen++] = _deposit(bob, asset, 100e18 + (seed >> 56) % 5000e18);
            }
            if (nOpen > 0 && (seed >> 64) % 3 == 0) {
                uint256 k = (seed >> 72) % nOpen;
                vm.prank(bob);
                b.redeem(open_[k], 10000);
                open_[k] = open_[--nOpen];
            }
            _wheel(1, true);
            _books();
            assertGe(IERC20(address(buck)).balanceOf(address(b)), b.idleBuck(),
                     "the wallet is backed");
            for (uint256 j = 0; j < 3; j++) {
                assertGe(tok[j].balanceOf(address(b)), b.idleToken(j), "the wallet is backed");
            }
        }
    }
}
