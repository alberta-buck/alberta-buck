// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BuckTypes} from "../BuckTypes.sol";
import {NetDemurrageMath} from "./NetDemurrageMath.sol";
import {WideCounter} from "./WideCounter.sol";

/// @notice Unsigned cash accounting for Buck. Credit belongs to BuckCredit.
/// @dev The host authenticates identities, credit/fund authority and mode
///      changes, holds its monetary lock, and emits ERC-20/identity receipts.
///      This library makes no external calls. All economic amounts below are
///      fractional raw BUCK scaled by Q unless a parameter says raw/nominal.
///      Sponsored balanceOf is a protected target until settlement; netOf
///      always reports the actual declining backing. An unfunded target can
///      never be spent: every debit first settles its complete fee or reverts.
///      Explicit detachment instead charges normal demurrage to the account.
library BuckCash {
    using WideCounter for WideCounter.Count;
    uint256 internal constant Q = 1e36;
    uint256 private constant MATURITY = 50 * (365 days + 6 hours);
    uint16 private constant ORDINARY = 1;
    uint16 private constant CARRYING = 2;
    uint16 private constant MODE = 3;
    uint16 private constant SPONSORED = 4;

    /// @dev The monetary hot state occupies one slot. The checked net cap
    ///      is stricter than uint200. Nominal Carrying history is separate.
    struct Account {
        uint200 net;
        uint40 timestamp;
        uint16 flags;
    }
    struct ReliefBudget {
        uint256 blockNumber;
        uint256 remaining;
        bool started;
    }
    struct Book {
        mapping(address => Account) accounts;
        mapping(address => uint256) nominal;
        mapping(address => address) sponsor;
        mapping(address => uint32) dependants;
        address fund;
        uint40 fundTimestamp;
        uint256 issued;
        uint256 fundGross;
        uint256 fundNet;
        WideCounter.Count grossDemurrage;
        WideCounter.Count netDemurrage;
        WideCounter.Count transfersIn;
        WideCounter.Count transfersOut;
        WideCounter.Count relief;
        ReliefBudget budget;
    }
    struct FundFlows {
        WideCounter.Count grossDemurrage;
        WideCounter.Count netDemurrage;
        WideCounter.Count transfersIn;
        WideCounter.Count transfersOut;
        WideCounter.Count relief;
    }

    error AlreadyInitialized();
    error NotInitialized();
    error BadAccount();
    error BadAmount();
    error AccountOverflow();
    error IssuanceOverflow();
    error TimestampOverflow();
    error CarryingModeChanged();
    error InsufficientSpendableBalance();
    error InsufficientNominalBalance();
    error InsufficientIssued();
    error FundExceedsIssuance();
    error ReliefLiquidityLimit();
    error SelfSponsorship();
    error AlreadySponsored();
    error SponsorIsSponsored();
    error AccountIsSponsor();
    error SponsoredAccountCarrying();
    error SponsorCarrying();
    error NotSponsored();
    error DemurrageSponsorUnderfunded(address account, address sponsor,
                                     uint256 requiredNetScaled, uint256 availableNetScaled);

    event DemurrageSponsor(address indexed account, address indexed sponsor);
    event DemurrageSponsored(address indexed account, address indexed sponsor, uint256 netScaled);
    event DemurragePayerSet(address indexed account, address indexed payer);
    event DemurragePayerCleared(address indexed account, address indexed payer);

    function initialize(Book storage self, address fund) public {
        if (self.fund != address(0)) revert AlreadyInitialized();
        if (fund == address(0)) revert BadAccount();
        self.fund = fund;
        self.fundTimestamp = _now();
    }

    /// @dev Host attributes every issued raw unit to an NFT in this same
    ///      transaction. Supply and net cash rise equally, including premiums.
    function issue(Book storage self, address owner, uint256 raw, bool carrying) public {
        _ordinaryAddress(self, owner);
        if (raw == 0) revert BadAmount();
        if (raw > type(uint256).max / Q - self.issued) revert IssuanceOverflow();
        _checkpointFund(self);
        _bind(self, owner, carrying);
        _touch(self, owner);
        _credit(self, owner, raw * Q, raw * Q, carrying);
        self.issued += raw;
    }

    /// @dev Preconditions: selected principal, marked full-value relief and
    ///      insurer refund are authorized by the host; its cash budget is
    ///      consumed atomically. Net owner cash plus net fund cash burn raw.
    ///      Carrying nominal consumption is rounded up in internal units.
    function retire(Book storage self, address owner, uint256 raw, uint256 reliefScaled, bool carrying)
        public returns (uint256 ownerNominal, uint256 fundNominal)
    {
        _ordinaryAddress(self, owner);
        if (raw == 0) revert BadAmount();
        if (raw > self.issued) revert InsufficientIssued();
        _checkpointFund(self);
        _bind(self, owner, carrying);
        _touch(self, owner);
        uint256 total = raw * Q;
        if (reliefScaled > total || reliefScaled > self.fundNet) revert InsufficientSpendableBalance();
        ownerNominal = _debitNet(self, owner, total - reliefScaled, carrying);
        fundNominal = _consumeFund(self, reliefScaled);
        self.issued -= raw;
        if (self.fundNet > self.issued * Q) revert FundExceedsIssuance();
    }

    /// @dev The host authenticates this against deferred matured principal.
    function retireFundDebt(Book storage self, uint256 raw) public returns (uint256 nominal) {
        if (raw == 0) revert BadAmount();
        if (raw > self.issued) revert InsufficientIssued();
        _checkpointFund(self);
        nominal = _consumeFund(self, raw * Q);
        self.issued -= raw;
        if (self.fundNet > self.issued * Q) revert FundExceedsIssuance();
    }

    /// @notice Move raw nominal units without changing issued principal.
    /// @dev A Carrying sender delivers its proportional actual net value;
    ///      an ordinary sender delivers full-value BUCK after settlement.
    function move(Book storage self, address from, address to, uint256 raw,
                  bool fromCarrying, bool toCarrying) public returns (uint256 netReceived)
    {
        _initialized(self);
        if (from == address(0) || to == address(0) || raw > type(uint256).max / Q) revert BadAccount();
        if ((from == self.fund && !fromCarrying) || (to == self.fund && !toCarrying)) revert BadAccount();
        if (from == self.fund || to == self.fund) _checkpointFund(self);
        uint256 nominal = raw * Q;
        if (from != self.fund) {
            _bind(self, from, fromCarrying);
            _touch(self, from);
        }
        if (to != self.fund && to != from) {
            _bind(self, to, toCarrying);
            _touch(self, to);
        }
        // Touching the recipient can debit its sponsor, who may be `from`.
        // Read the sender only after both complete settlement.
        if (from == self.fund) {
            if (nominal > self.fundGross) revert InsufficientNominalBalance();
            netReceived = nominal == 0 ? 0 : Math.mulDiv(self.fundNet, nominal, self.fundGross);
        } else {
            uint256 net = self.accounts[from].net;
            if (fromCarrying) {
                uint256 gross = self.nominal[from];
                if (nominal > gross) revert InsufficientNominalBalance();
                netReceived = nominal == 0 ? 0 : Math.mulDiv(net, nominal, gross);
            } else {
                if (nominal > net) revert InsufficientSpendableBalance();
                netReceived = nominal;
            }
        }
        if (from == to) return netReceived;
        uint256 recipientNet = netReceived;
        if (fromCarrying && self.sponsor[to] != address(0) && nominal > netReceived) {
            // Preserve Buck's complete fee offload for an aged pool receipt.
            // The incoming pool component remains netReceived; the separately
            // recorded sponsor payment restores its carried fee in full.
            _paySponsor(self, to, self.sponsor[to], nominal - netReceived);
            recipientNet = nominal;
        }
        if (from == self.fund) {
            self.fundGross -= nominal;
            self.fundNet -= netReceived;
            self.transfersOut.add(netReceived);
        } else {
            self.accounts[from].net -= uint200(netReceived);
            if (fromCarrying) self.nominal[from] -= nominal;
        }
        if (to == self.fund) {
            self.fundGross += nominal;
            self.fundNet += netReceived;
            self.transfersIn.add(netReceived);
            if (self.fundNet > self.issued * Q) revert FundExceedsIssuance();
        } else {
            _credit(self, to, nominal, recipientNet, toCarrying);
        }
    }

    function settle(Book storage self, address account) public {
        _initialized(self);
        if (account == self.fund) _checkpointFund(self);
        else {
            if (account == address(0)) revert BadAccount();
            _touch(self, account);
        }
    }

    /// @dev Host verifies two-party consent and both ordinary identities.
    ///      Existing fees settle before protection starts; chains are forbidden.
    function establishSponsor(Book storage self, address account, address payer) public {
        _ordinaryAddress(self, account);
        _ordinaryAddress(self, payer);
        if (account == payer) revert SelfSponsorship();
        if (self.sponsor[account] != address(0)) revert AlreadySponsored();
        if (self.sponsor[payer] != address(0)) revert SponsorIsSponsored();
        if (self.dependants[account] != 0) revert AccountIsSponsor();
        if ((self.accounts[account].flags & MODE) == CARRYING) revert SponsoredAccountCarrying();
        if ((self.accounts[payer].flags & MODE) == CARRYING) revert SponsorCarrying();
        _bind(self, account, false);
        _bind(self, payer, false);
        _touch(self, account);
        _touch(self, payer);
        self.accounts[account].flags |= SPONSORED;
        self.sponsor[account] = payer;
        self.dependants[payer] += 1;
        emit DemurragePayerSet(account, payer);
        emit DemurrageSponsor(account, payer);
    }

    /// @dev Host verifies the caller is one of the two parties. Clear first:
    ///      detachment must remain possible without funding the old sponsor.
    ///      The account pays its entire outstanding normal demurrage itself.
    function clearSponsor(Book storage self, address account) public {
        if (self.sponsor[account] == address(0)) revert NotSponsored();
        _detach(self, account);
        _touch(self, account);
    }

    function syncMode(Book storage self, address account, bool carrying) public {
        _ordinaryAddress(self, account);
        Account storage a = self.accounts[account];
        uint16 wanted = carrying ? CARRYING : ORDINARY;
        uint16 mode = a.flags & MODE;
        if (mode == 0 || mode == wanted) return;
        if (carrying && self.sponsor[account] != address(0)) revert SponsoredAccountCarrying();
        if (carrying && self.dependants[account] != 0) revert SponsorCarrying();
        _touch(self, account);
        self.nominal[account] = carrying ? a.net : 0;
        a.flags = wanted;
    }

    function balanceOf(Book storage self, address account) public view returns (uint256) {
        if (account == self.fund) { (uint256 gross,) = _fundView(self); return gross / Q; }
        Account storage a = self.accounts[account];
        if ((a.flags & MODE) == CARRYING) return self.nominal[account] / Q;
        return ((a.flags & SPONSORED) != 0 ? uint256(a.net) : _live(a)) / Q;
    }

    function netOf(Book storage self, address account) public view returns (uint256) {
        if (account == self.fund) { (, uint256 net) = _fundView(self); return net; }
        return _live(self.accounts[account]);
    }

    function grossAndNetOf(Book storage self, address account) public view returns (uint256 gross, uint256 net) {
        if (account == self.fund) return _fundView(self);
        Account storage a = self.accounts[account];
        net = _live(a);
        gross = (a.flags & MODE) == CARRYING ? self.nominal[account]
              : (a.flags & SPONSORED) != 0 ? uint256(a.net) : net;
    }

    function fundParts(Book storage self) public view returns (uint256 gross, uint256 net) { return _fundView(self); }

    function fundFlows(Book storage self) public view returns (FundFlows memory flows) {
        (uint256 gross, uint256 net) = _fundView(self);
        flows.grossDemurrage = WideCounter.plus(self.grossDemurrage, gross - self.fundGross);
        flows.netDemurrage = WideCounter.plus(self.netDemurrage, net - self.fundNet);
        flows.transfersIn = self.transfersIn;
        flows.transfersOut = self.transfersOut;
        flows.relief = self.relief;
    }

    function fundNominalForNet(Book storage self, uint256 raw) public view returns (uint256) {
        if (raw == 0) return 0;
        if (raw > type(uint256).max / Q) revert BadAmount();
        (uint256 gross, uint256 net) = _fundView(self);
        if (raw * Q > net) revert InsufficientSpendableBalance();
        return Math.ceilDiv(Math.mulDiv(raw * Q, gross, net, Math.Rounding.Ceil), Q);
    }

    function maxNetPayout(Book storage self) public view returns (uint256) {
        (uint256 gross, uint256 net) = _fundView(self);
        return gross == 0 ? 0 : Math.mulDiv(net, gross / Q * Q, gross) / Q;
    }

    function availableRelief(Book storage self) public view returns (uint256 available) {
        (, uint256 net) = _fundView(self);
        available = net / 10;
        if (self.budget.started && self.budget.blockNumber == block.number) {
            available = Math.min(available, self.budget.remaining);
        }
    }

    /// @dev Host must apply to all protected outflows under the same lock.
    function consumeRelief(Book storage self, uint256 value) public {
        if (value == 0) return;
        (, uint256 net) = _fundView(self);
        uint256 cap = net / 10;
        if (value > cap) revert ReliefLiquidityLimit();
        ReliefBudget storage b = self.budget;
        if (!b.started || b.blockNumber != block.number) {
            b.started = true;
            b.blockNumber = block.number;
            b.remaining = cap;
        }
        if (value > b.remaining) revert ReliefLiquidityLimit();
        b.remaining -= value;
    }

    function _touch(Book storage self, address owner) private {
        Account storage a = self.accounts[owner];
        uint256 beforeNet = a.net;
        uint256 live = _live(a);
        address payer = self.sponsor[owner];
        if (payer != address(0)) {
            uint256 fee = beforeNet - live;
            if (fee != 0) {
                _paySponsor(self, owner, payer, fee);
                live = beforeNet;
            }
        }
        a.net = uint200(live);
        a.timestamp = _now();
    }

    function _paySponsor(Book storage self, address owner, address payer, uint256 fee) private {
        // No chains: a payer is ordinary and cannot itself be sponsored.
        Account storage p = self.accounts[payer];
        uint256 payerNet = _live(p);
        if (payerNet < fee) revert DemurrageSponsorUnderfunded(owner, payer, fee, payerNet);
        p.net = uint200(payerNet - fee);
        p.timestamp = _now();
        emit DemurrageSponsored(owner, payer, fee);
    }

    function _detach(Book storage self, address account) private {
        address payer = self.sponsor[account];
        delete self.sponsor[account];
        self.accounts[account].flags &= ~SPONSORED;
        self.dependants[payer] -= 1;
        emit DemurragePayerCleared(account, payer);
        emit DemurrageSponsor(account, address(0));
    }

    function _credit(Book storage self, address owner, uint256 nominal, uint256 net, bool carrying) private {
        Account storage a = self.accounts[owner];
        uint256 value = uint256(a.net) + net;
        uint256 gross = carrying ? self.nominal[owner] + nominal : value;
        if (value > BuckTypes.MAX_BALANCE * Q || gross > BuckTypes.MAX_BALANCE * Q) revert AccountOverflow();
        a.net = uint200(value);
        if (carrying) self.nominal[owner] = gross;
    }

    function _debitNet(Book storage self, address owner, uint256 net, bool carrying) private returns (uint256 nominal) {
        Account storage a = self.accounts[owner];
        if (net > a.net) revert InsufficientSpendableBalance();
        nominal = carrying && net != 0 ? Math.mulDiv(self.nominal[owner], net, a.net, Math.Rounding.Ceil) : net;
        if (carrying) self.nominal[owner] -= nominal;
        a.net -= uint200(net);
    }

    function _consumeFund(Book storage self, uint256 value) private returns (uint256 nominal) {
        if (value == 0) return 0;
        if (value > self.fundNet) revert InsufficientSpendableBalance();
        nominal = Math.mulDiv(self.fundGross, value, self.fundNet, Math.Rounding.Ceil);
        self.fundGross -= nominal;
        self.fundNet -= value;
        self.relief.add(value);
    }

    function _bind(Book storage self, address owner, bool carrying) private {
        Account storage a = self.accounts[owner];
        uint16 wanted = carrying ? CARRYING : ORDINARY;
        uint16 mode = a.flags & MODE;
        if (mode == 0) { a.flags = wanted; a.timestamp = _now(); }
        else if (mode != wanted) revert CarryingModeChanged();
    }

    function _live(Account storage a) private view returns (uint256) {
        return NetDemurrageMath.decay(a.net, block.timestamp - uint256(a.timestamp));
    }

    function _checkpointFund(Book storage self) private {
        (uint256 gross, uint256 net) = _fundView(self);
        self.grossDemurrage.add(gross - self.fundGross);
        self.netDemurrage.add(net - self.fundNet);
        self.fundGross = gross;
        self.fundNet = net;
        self.fundTimestamp = _now();
    }

    function _fundView(Book storage self) private view returns (uint256 gross, uint256 net) {
        _initialized(self);
        uint256 issuance = self.issued * Q;
        uint256 elapsed = block.timestamp - uint256(self.fundTimestamp);
        gross = self.fundGross + Math.mulDiv(issuance, elapsed, MATURITY);
        net = NetDemurrageMath.fundNetAfter(issuance, self.fundNet, elapsed);
    }

    function _ordinaryAddress(Book storage self, address account) private view {
        _initialized(self);
        if (account == address(0) || account == self.fund) revert BadAccount();
    }

    function _initialized(Book storage self) private view {
        if (self.fund == address(0)) revert NotInitialized();
    }

    function _now() private view returns (uint40) {
        if (block.timestamp > type(uint40).max) revert TimestampOverflow();
        return uint40(block.timestamp);
    }
}
