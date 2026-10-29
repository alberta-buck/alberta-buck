// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckCredit} from "./BuckCredit.sol";

library BuckRenewal {
    struct Terms {
        uint256 face;
        uint256 floor;
        uint256 activatedFace;
        uint256 additionalIssuance;
        uint256 maxPremium;
        uint256 minimumRelief;
        uint256 expectedPrincipal;
        bytes32 expectedCreditHash;
        BuckCredit.DepreciationType depType;
        uint32 depRate;
        uint48 depStartAt;
        uint32 premiumRate;
        uint64 deadline;
    }
}

/// @notice Independent insurer and NFT-owner consent for a BuckNet renewal.
/// @dev Created by and bound permanently to its token. It cannot change a
///      credit or settle BUCK. The fund validates before a sale; the token
///      validates again and alone can consume both authorizations.
contract BuckRenewalConsent {
    address public immutable buck;
    BuckCredit public immutable credit;
    mapping(uint256 => bytes32) public proposal;
    mapping(uint256 => bytes32) public authorization;

    event Proposed(address indexed insurer, uint256 indexed tokenId, bytes32 digest);
    event Authorized(address indexed owner, uint256 indexed tokenId, bytes32 digest);
    error NotInsurer();
    error NotOwner();
    error NotBuck();
    error BadAuthorization();
    error BadCreditSnapshot();

    constructor(address buck_, address credit_) {
        buck = buck_;
        credit = BuckCredit(credit_);
    }

    function currentCreditTermsHash(uint256 tokenId) public view returns (bytes32) {
        credit.ownerOf(tokenId);
        (bool ok, bytes memory data) = address(credit).staticcall(
            abi.encodeWithSignature("credits(uint256)", tokenId)
        );
        if (!ok || data.length == 0) revert BadCreditSnapshot();
        return keccak256(data);
    }

    function renewalDigest(address owner, uint256 tokenId, BuckRenewal.Terms calldata terms)
        public view returns (bytes32)
    {
        return keccak256(abi.encode(block.chainid, buck, owner, tokenId, terms));
    }

    /// @notice The recorded insurer can replace or cancel its proposal.
    function propose(uint256 tokenId, bytes32 digest) external {
        if (credit.insurerOf(tokenId) != msg.sender) revert NotInsurer();
        proposal[tokenId] = digest;
        emit Proposed(msg.sender, tokenId, digest);
    }

    /// @notice The NFT owner can independently replace or cancel consent.
    function authorize(uint256 tokenId, bytes32 digest) external {
        if (credit.ownerOf(tokenId) != msg.sender) revert NotOwner();
        authorization[tokenId] = digest;
        emit Authorized(msg.sender, tokenId, digest);
    }

    function checkRenewal(address owner, uint256 tokenId, BuckRenewal.Terms calldata terms)
        external view returns (uint256 principal, bytes32 digest)
    {
        if (credit.ownerOf(tokenId) != owner) revert NotOwner();
        digest = renewalDigest(owner, tokenId, terms);
        if (digest == bytes32(0) || proposal[tokenId] != digest || authorization[tokenId] != digest
            || block.timestamp > terms.deadline) revert BadAuthorization();
        (principal,) = credit.issuanceOf(tokenId);
        if (principal == 0 || principal != terms.expectedPrincipal || terms.minimumRelief > principal
            || terms.expectedCreditHash != currentCreditTermsHash(tokenId)) revert BadCreditSnapshot();
    }

    function consume(uint256 tokenId, bytes32 digest) external {
        if (msg.sender != buck) revert NotBuck();
        if (digest == bytes32(0) || proposal[tokenId] != digest || authorization[tokenId] != digest) {
            revert BadAuthorization();
        }
        delete proposal[tokenId];
        delete authorization[tokenId];
    }
}
