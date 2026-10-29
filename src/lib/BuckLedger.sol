// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BuckTypes} from "../BuckTypes.sol";
import {NetDemurrageMath} from "./NetDemurrageMath.sol";
import {WideCounter} from "./WideCounter.sol";

/// @notice Net-value accounting for a future Buck token. Internal library
///         only: the token must enforce identity, authorization, facility
///         attribution, K, premium pools, reentrancy, events and settlement.
/// @dev BUCK raw units have six decimals. Internal balances use 1e36
///      fractional raw units to avoid rounding a fee up on every checkpoint.
///      Account quantities retain BuckTypes.MAX_BALANCE; the separate fund
///      and aggregate counters use uint256. Total issuance must fit after
///      multiplication by SCALE, explicitly checked before each issue.
library BuckLedger {
    using WideCounter for WideCounter.Count;
    uint256 internal constant SCALE = 1e36;
    uint256 internal constant RELIEF_SECONDS = 50 * (365 days + 6 hours);
    uint8 private constant ORDINARY = 1;
    uint8 private constant CARRYING = 2;

    error AlreadyInitialized();
    error NotInitialized();
    error BadAccount();
    error AmountZero();
    error CarryingModeChanged();
    error CarryingCannotIssue();
    error AccountOverflow();
    error IssuanceOverflow();
    error InsufficientBalance();
    error InsufficientIssued();
    error FundExceedsIssuance();
    error TimestampOverflow();

    struct Account {
        // Ordinary: net received BUCK and net issued-but-unspent BUCK.
        // Carrying: net value and zero unspent; gross includes carried fees.
        uint256 held;
        uint256 unspent;
        uint256 gross;
        uint64 checkpoint;
        uint8 mode;
    }

    struct Ledger {
        mapping(address => Account) accounts;
        address fund;
        uint64 fundCheckpoint;
        uint256 issued;
        uint256 fundGross;
        uint256 fundNet;
        WideCounter.Count grossDemurrage;
        WideCounter.Count netDemurrage;
        WideCounter.Count fundTransfersIn;
        WideCounter.Count fundTransfersOut;
        WideCounter.Count fundRelief;
    }

    /// @notice Cumulative fund flows in raw BUCK units, each with a 1e36
    ///         fractional-unit remainder. Gross demurrage includes the fund's
    ///         own demurrage; net demurrage is new value from other holders.
    struct FundFlows {
        WideCounter.Count grossDemurrage;
        WideCounter.Count netDemurrage;
        WideCounter.Count transfersIn;
        WideCounter.Count transfersOut;
        WideCounter.Count relief;
    }

    function initialize(Ledger storage self, address fund) internal {
        if (self.fund != address(0)) revert AlreadyInitialized();
        if (fund == address(0)) revert BadAccount();
        self.fund = fund;
        self.fundCheckpoint = _now();
    }

    /// @notice Issue existing principal into an ordinary account's unspent
    ///         balance. The caller must attribute this issuance to an NFT.
    function issue(Ledger storage self, address owner, uint256 amount, bool carrying) internal {
        _requireInitialized(self);
        if (owner == address(0) || owner == self.fund) revert BadAccount();
        if (carrying) revert CarryingCannotIssue();
        if (amount == 0) revert AmountZero();
        if (amount > type(uint256).max / SCALE - self.issued) revert IssuanceOverflow();
        _checkpointFund(self);
        Account storage a = _bind(self, owner, false);
        _checkpointAccount(a);
        a.unspent += amount * SCALE;
        _checkLimit(a);
        self.issued += amount;
    }

    /// @notice Extinguish BUCK from an ordinary owner's held, then unspent,
    ///         balances and reduce aggregate issuance. The caller must reduce
    ///         the selected NFT and settle relief in the same transaction.
    function retire(Ledger storage self, address owner, uint256 amount, bool carrying) internal {
        _requireInitialized(self);
        if (owner == address(0) || owner == self.fund) revert BadAccount();
        if (carrying) revert CarryingCannotIssue();
        if (amount == 0) revert AmountZero();
        if (amount > self.issued) revert InsufficientIssued();
        _checkpointFund(self);
        Account storage a = _bind(self, owner, false);
        _checkpointAccount(a);
        _debitOrdinary(a, amount);
        self.issued -= amount;
        if (self.fundNet > self.issued * SCALE) revert FundExceedsIssuance();
    }

    /// @notice Atomically extinguish principal using owner net and a specified
    ///         full-value fund contribution, both in fractional raw units.
    /// @dev This permits sub-raw-unit balances at the owner and the fund to
    ///      jointly retire a whole raw unit without creating value. The caller
    ///      must choose a policy-authorized relief amount, settle the selected
    ///      NFT, and record its own ERC-20 and fractional settlement events.
    /// @return nominalFundSpent Fractional nominal Carrying units consumed.
    function retireWithFund(Ledger storage self, address owner, uint256 amount,
                            uint256 reliefScaled, bool carrying) internal returns (uint256 nominalFundSpent)
    {
        _requireInitialized(self);
        if (owner == address(0) || owner == self.fund) revert BadAccount();
        if (carrying) revert CarryingCannotIssue();
        if (amount == 0) revert AmountZero();
        if (amount > self.issued) revert InsufficientIssued();
        _checkpointFund(self);
        Account storage a = _bind(self, owner, false);
        _checkpointAccount(a);
        uint256 total = amount * SCALE;
        if (reliefScaled > total || reliefScaled > self.fundNet) revert InsufficientBalance();
        _debitOrdinaryScaled(a, total - reliefScaled);
        if (reliefScaled != 0) {
            nominalFundSpent = Math.mulDiv(self.fundGross, reliefScaled, self.fundNet, Math.Rounding.Ceil);
            self.fundGross -= nominalFundSpent;
            self.fundNet -= reliefScaled;
            self.fundRelief.add(reliefScaled);
        }
        self.issued -= amount;
        if (self.fundNet > self.issued * SCALE) revert FundExceedsIssuance();
    }

    /// @notice Transfer nominal raw units. Returns the recipient's net amount
    ///         in fractional raw units; a Carrying sender can deliver less net
    ///         than the nominal amount. Identity and ERC-20 events belong to
    ///         the caller, which supplies the registry's Carrying flags.
    function transfer(Ledger storage self, address from, address to, uint256 amount,
                      bool fromCarrying, bool toCarrying) internal returns (uint256 netReceived)
    {
        _requireInitialized(self);
        if (from == address(0) || to == address(0)) revert BadAccount();
        if ((from == self.fund && !fromCarrying) || (to == self.fund && !toCarrying)) {
            revert CarryingModeChanged();
        }
        if (amount > type(uint256).max / SCALE) revert InsufficientBalance();
        if (amount == 0) return 0;
        if (from == to) {
            if (amount > balanceOf(self, from)) revert InsufficientBalance();
            return 0;
        }
        if (from == self.fund || to == self.fund) _checkpointFund(self);
        uint256 nominal = amount * SCALE;
        if (from == self.fund) {
            if (nominal > self.fundGross) revert InsufficientBalance();
            netReceived = nominal == 0 ? 0 : Math.mulDiv(self.fundNet, nominal, self.fundGross);
            self.fundGross -= nominal;
            self.fundNet -= netReceived;
            self.fundTransfersOut.add(netReceived);
        } else {
            Account storage a = _bind(self, from, fromCarrying);
            _checkpointAccount(a);
            if (fromCarrying) {
                if (nominal > a.gross) revert InsufficientBalance();
                netReceived = nominal == 0 ? 0 : Math.mulDiv(a.held, nominal, a.gross);
                a.gross -= nominal;
                a.held -= netReceived;
            } else {
                _debitOrdinary(a, amount);
                netReceived = nominal;
            }
        }
        if (to == self.fund) {
            self.fundGross += nominal;
            self.fundNet += netReceived;
            self.fundTransfersIn.add(netReceived);
            if (self.fundNet > self.issued * SCALE) revert FundExceedsIssuance();
        } else {
            Account storage b = _bind(self, to, toCarrying);
            _checkpointAccount(b);
            b.held += netReceived;
            if (toCarrying) b.gross += nominal;
            _checkLimit(b);
        }
    }

    /// @notice Pay at least `net` external raw BUCK from the Carrying fund.
    ///         Returns nominal raw sent and actual fractional net delivered.
    /// @dev Integer nominal transfers can leave less than one raw unit of
    ///      fund dust. maxNetPayout is the executable net bound, which can be
    ///      slightly less than the fund's displayed net balance.
    function payFundNet(Ledger storage self, address to, uint256 net, bool toCarrying)
        internal returns (uint256 nominal, uint256 delivered)
    {
        _requireInitialized(self);
        if (to == address(0) || to == self.fund) revert BadAccount();
        _checkpointFund(self);
        if (net == 0) return (0, 0);
        nominal = fundNominalForNet(self, net);
        delivered = transfer(self, self.fund, to, nominal, true, toCarrying);
        if (delivered < net * SCALE) revert InsufficientBalance();
    }

    /// @notice Smallest whole nominal fund transfer delivering at least `net`
    ///         whole raw units, at the current Carrying average. A subsequent
    ///         transfer can still fail if the fund's state changes first.
    function fundNominalForNet(Ledger storage self, uint256 net) internal view returns (uint256) {
        _requireInitialized(self);
        if (net == 0) return 0;
        if (net > maxNetPayout(self)) revert InsufficientBalance();
        (uint256 gross, uint256 fundNet) = _fundView(self);
        uint256 scaled = Math.mulDiv(net * SCALE, gross, fundNet, Math.Rounding.Ceil);
        return Math.ceilDiv(scaled, SCALE);
    }

    function balanceOf(Ledger storage self, address owner) internal view returns (uint256) {
        if (owner == self.fund) {
            (uint256 gross,) = _fundView(self);
            return gross / SCALE;
        }
        Account storage a = self.accounts[owner];
        if (a.mode == CARRYING) return a.gross / SCALE;
        (uint256 held, uint256 unspent) = _accountView(a);
        return (held + unspent) / SCALE;
    }

    function netOf(Ledger storage self, address owner) internal view returns (uint256) {
        if (owner == self.fund) {
            (, uint256 net) = _fundView(self);
            return net / SCALE;
        }
        (uint256 held, uint256 unspent) = _accountView(self.accounts[owner]);
        return (held + unspent) / SCALE;
    }

    function partsOf(Ledger storage self, address owner) internal view returns (uint256 held, uint256 unspent) {
        if (owner == self.fund) {
            (, held) = _fundView(self);
            return (held, 0);
        }
        return _accountView(self.accounts[owner]);
    }

    function fundParts(Ledger storage self) internal view returns (uint256 gross, uint256 net) {
        return _fundView(self);
    }

    /// @notice Lifetime flows, including accrued demurrage since the last
    ///         checkpoint. Net demurrage + incoming transfers - outgoing
    ///         transfers - relief equals the fund's current net cash.
    function fundFlows(Ledger storage self) internal view returns (FundFlows memory flows) {
        _requireInitialized(self);
        (uint256 gross, uint256 net) = _fundView(self);
        flows.grossDemurrage = WideCounter.plus(self.grossDemurrage, gross - self.fundGross);
        flows.netDemurrage = WideCounter.plus(self.netDemurrage, net - self.fundNet);
        flows.transfersIn = self.fundTransfersIn;
        flows.transfersOut = self.fundTransfersOut;
        flows.relief = self.fundRelief;
    }

    function maxNetPayout(Ledger storage self) internal view returns (uint256) {
        (uint256 gross, uint256 net) = _fundView(self);
        uint256 nominal = gross / SCALE * SCALE;
        return gross == 0 ? 0 : Math.mulDiv(net, nominal, gross) / SCALE;
    }

    function _debitOrdinary(Account storage a, uint256 amount) private {
        if (amount > type(uint256).max / SCALE) revert InsufficientBalance();
        _debitOrdinaryScaled(a, amount * SCALE);
    }

    function _debitOrdinaryScaled(Account storage a, uint256 scaled) private {
        if (scaled > a.held + a.unspent) revert InsufficientBalance();
        uint256 held = scaled < a.held ? scaled : a.held;
        a.held -= held;
        a.unspent -= scaled - held;
    }

    function _bind(Ledger storage self, address owner, bool carrying) private returns (Account storage a) {
        a = self.accounts[owner];
        uint8 wanted = carrying ? CARRYING : ORDINARY;
        if (a.mode == 0) {
            a.mode = wanted;
            a.checkpoint = _now();
        } else if (a.mode != wanted) {
            // A production token must either freeze the registry posture or
            // provide a separately authorized, conservation-preserving change.
            revert CarryingModeChanged();
        }
    }

    function _checkpointAccount(Account storage a) private {
        (a.held, a.unspent) = _accountView(a);
        a.checkpoint = _now();
    }

    function _accountView(Account storage a) private view returns (uint256 held, uint256 unspent) {
        if (a.mode == 0) return (0, 0);
        uint256 elapsed = block.timestamp - uint256(a.checkpoint);
        held = NetDemurrageMath.decay(a.held, elapsed);
        unspent = NetDemurrageMath.decay(a.unspent, elapsed);
    }

    function _checkpointFund(Ledger storage self) private {
        (uint256 gross, uint256 net) = _fundView(self);
        self.grossDemurrage.add(gross - self.fundGross);
        self.netDemurrage.add(net - self.fundNet);
        self.fundGross = gross;
        self.fundNet = net;
        self.fundCheckpoint = _now();
    }

    function _fundView(Ledger storage self) private view returns (uint256 gross, uint256 net) {
        uint256 issuance = self.issued * SCALE;
        uint256 elapsed = block.timestamp - uint256(self.fundCheckpoint);
        if (self.fundNet > issuance) revert FundExceedsIssuance();
        gross = self.fundGross + Math.mulDiv(issuance, elapsed, RELIEF_SECONDS);
        net = NetDemurrageMath.fundNetAfter(issuance, self.fundNet, elapsed);
    }

    function _checkLimit(Account storage a) private view {
        uint256 cap = BuckTypes.MAX_BALANCE * SCALE;
        if (a.mode == CARRYING ? a.gross > cap : a.held + a.unspent > cap) revert AccountOverflow();
    }

    function _now() private view returns (uint64) {
        if (block.timestamp > type(uint64).max) revert TimestampOverflow();
        return uint64(block.timestamp);
    }

    function _requireInitialized(Ledger storage self) private view {
        if (self.fund == address(0)) revert NotInitialized();
    }
}
