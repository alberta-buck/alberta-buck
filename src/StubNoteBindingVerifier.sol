// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {INoteBindingVerifier} from "./INoteBindingVerifier.sol";

/// @title StubNoteBindingVerifier — always-accepts note<->eEnc tie stub.
/// @notice Plumbing-only placeholder for the RESERVED re-encryption-tie circuit
///         (see INoteBindingVerifier).  Returns `enabled` (default true) for
///         every input, so it exercises the Notes spend-path wiring WITHOUT
///         enforcing the tie.  Governance swaps in the real verifier via
///         `Notes.setNoteBindingVerifier` once the circuit and its trusted setup
///         exist; until then the addressed-binding ('only M_rec can spend') and
///         A2-collusion ('un-nameable note un-spendable') guarantees are NOT
///         enforced on-chain.  `setEnabled(false)` lets tests drive the
///         reject branch.
contract StubNoteBindingVerifier is INoteBindingVerifier {
    bool public enabled = true;

    function setEnabled(bool _enabled) external {
        enabled = _enabled;
    }

    function verifyNoteBinding(
        bytes calldata /*proof*/,
        uint256 /*nullifier*/,
        uint256 /*eEncRx*/,
        uint256 /*eEncRy*/,
        uint256 /*eEncCx*/,
        uint256 /*eEncCy*/,
        uint256 /*piX*/,
        uint256 /*piY*/
    ) external view returns (bool) {
        return enabled;
    }
}
