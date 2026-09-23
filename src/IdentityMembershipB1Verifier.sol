// SPDX-License-Identifier: GPL-3.0
/*
    Copyright 2021 0KIMS association.

    This file is generated with [snarkJS](https://github.com/iden3/snarkjs).

    snarkJS is a free software: you can redistribute it and/or modify it
    under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    snarkJS is distributed in the hope that it will be useful, but WITHOUT
    ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
    or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public
    License for more details.

    You should have received a copy of the GNU General Public License
    along with snarkJS. If not, see <https://www.gnu.org/licenses/>.
*/

pragma solidity >=0.7.0 <0.9.0;

contract IdentityMembershipB1Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 709997778838307835264383870894323768967370809777328976677908306577656761867;
    uint256 constant alphay  = 12836297161486382489279136251040704944281163072493113933910361141757593083677;
    uint256 constant betax1  = 3684114477087397643340155885713584867253826796058431906225088186474534956028;
    uint256 constant betax2  = 21569416133113965215801657701571922097548973573516190817471638180089358098266;
    uint256 constant betay1  = 4326734521026516125659375800180265709604472291722677762145538662848384222069;
    uint256 constant betay2  = 16828383835347505433443117675638127444543738572256217033768785829213672209398;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 4090254670264172194049564309668231266134749352365814916958422572212011595476;
    uint256 constant deltax2 = 7600603113952286276129716335450922235323071994895302473070470583216759205551;
    uint256 constant deltay1 = 3916892230086887712781742208472920686859032436482204777731374484526713181725;
    uint256 constant deltay2 = 1768462449995780498577174643220574744053759371276076727556425228652856124211;

    
    uint256 constant IC0x = 3479854242248144784018565720418162436164512520624746017675122123659391414342;
    uint256 constant IC0y = 10649413831759472084125899527131399236110082706282318893229821553543942489770;
    
    uint256 constant IC1x = 13673645187278842473686549353623019045142257265619705354577867459802508014204;
    uint256 constant IC1y = 19280417606539582080515483587765574448619593575057564058971483606473027159784;
    
    uint256 constant IC2x = 9248539110237848552418438232609947321400567160643032660273961418891556067634;
    uint256 constant IC2y = 18842458331878534879678179925616015016312032482114596987326334680313577910066;
    
    uint256 constant IC3x = 4909401777755582020550789159721898887673949411209422693838580156071223423639;
    uint256 constant IC3y = 2373772452383885198462450762688244480161530509942578364305152277946779987684;
    
    uint256 constant IC4x = 17032633771077009489507229733965632583412428211856784495883330588511203406469;
    uint256 constant IC4y = 18953647548629044779279403438867961034405425204877118378506396505172018962464;
    
    uint256 constant IC5x = 14828214801507363210650044611269463923893859872194584351159302537703597626070;
    uint256 constant IC5y = 18616913803282786310239213724991789091876044297283985637669929785417427473757;
    
    uint256 constant IC6x = 20727305200919002334671348459420807434552436381685038279077812688220318842299;
    uint256 constant IC6y = 16138507668887695203200764031390930548046173051452335046151093968088519971079;
    
    uint256 constant IC7x = 5791826298600513397683316378997300172842347087049935555525751667494348402942;
    uint256 constant IC7y = 11274689642273480110066605713142615812877557322761464961071250760644778319593;
    
    uint256 constant IC8x = 14217338409134125740238596017548837367895997297410858487892531869163505724666;
    uint256 constant IC8y = 17127972514192673456946358850076743043348554186292200605799656759735253545845;
    
    uint256 constant IC9x = 16666723879973981695635232545192393673308027356040271363817671219610860672624;
    uint256 constant IC9y = 4679610293068056199793405846723817997247398382604321437656794332037577409999;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[9] calldata _pubSignals) public returns (bool) {
        assembly {
            function checkField(v) {
                if iszero(lt(v, r)) {
                    mstore(0, 0)
                    return(0, 0x20)
                }
            }
            
            // G1 function to multiply a G1 value(x,y) to value in an address
            function g1_mulAccC(pR, x, y, s) {
                let success
                let mIn := mload(0x40)
                mstore(mIn, x)
                mstore(add(mIn, 32), y)
                mstore(add(mIn, 64), s)

                success := staticcall(sub(gas(), 2000), 7, mIn, 96, mIn, 64)

                if iszero(success) {
                    mstore(0, 0)
                    return(0, 0x20)
                }

                mstore(add(mIn, 64), mload(pR))
                mstore(add(mIn, 96), mload(add(pR, 32)))

                success := staticcall(sub(gas(), 2000), 6, mIn, 128, pR, 64)

                if iszero(success) {
                    mstore(0, 0)
                    return(0, 0x20)
                }
            }

            function checkPairing(pA, pB, pC, pubSignals, pMem) -> isOk {
                let _pPairing := add(pMem, pPairing)
                let _pVk := add(pMem, pVk)

                mstore(_pVk, IC0x)
                mstore(add(_pVk, 32), IC0y)

                // Compute the linear combination vk_x
                
                g1_mulAccC(_pVk, IC1x, IC1y, calldataload(add(pubSignals, 0)))
                
                g1_mulAccC(_pVk, IC2x, IC2y, calldataload(add(pubSignals, 32)))
                
                g1_mulAccC(_pVk, IC3x, IC3y, calldataload(add(pubSignals, 64)))
                
                g1_mulAccC(_pVk, IC4x, IC4y, calldataload(add(pubSignals, 96)))
                
                g1_mulAccC(_pVk, IC5x, IC5y, calldataload(add(pubSignals, 128)))
                
                g1_mulAccC(_pVk, IC6x, IC6y, calldataload(add(pubSignals, 160)))
                
                g1_mulAccC(_pVk, IC7x, IC7y, calldataload(add(pubSignals, 192)))
                
                g1_mulAccC(_pVk, IC8x, IC8y, calldataload(add(pubSignals, 224)))
                
                g1_mulAccC(_pVk, IC9x, IC9y, calldataload(add(pubSignals, 256)))
                

                // -A
                mstore(_pPairing, calldataload(pA))
                mstore(add(_pPairing, 32), mod(sub(q, calldataload(add(pA, 32))), q))

                // B
                mstore(add(_pPairing, 64), calldataload(pB))
                mstore(add(_pPairing, 96), calldataload(add(pB, 32)))
                mstore(add(_pPairing, 128), calldataload(add(pB, 64)))
                mstore(add(_pPairing, 160), calldataload(add(pB, 96)))

                // alpha1
                mstore(add(_pPairing, 192), alphax)
                mstore(add(_pPairing, 224), alphay)

                // beta2
                mstore(add(_pPairing, 256), betax1)
                mstore(add(_pPairing, 288), betax2)
                mstore(add(_pPairing, 320), betay1)
                mstore(add(_pPairing, 352), betay2)

                // vk_x
                mstore(add(_pPairing, 384), mload(add(pMem, pVk)))
                mstore(add(_pPairing, 416), mload(add(pMem, add(pVk, 32))))


                // gamma2
                mstore(add(_pPairing, 448), gammax1)
                mstore(add(_pPairing, 480), gammax2)
                mstore(add(_pPairing, 512), gammay1)
                mstore(add(_pPairing, 544), gammay2)

                // C
                mstore(add(_pPairing, 576), calldataload(pC))
                mstore(add(_pPairing, 608), calldataload(add(pC, 32)))

                // delta2
                mstore(add(_pPairing, 640), deltax1)
                mstore(add(_pPairing, 672), deltax2)
                mstore(add(_pPairing, 704), deltay1)
                mstore(add(_pPairing, 736), deltay2)


                let success := staticcall(sub(gas(), 2000), 8, _pPairing, 768, _pPairing, 0x20)

                isOk := and(success, mload(_pPairing))
            }

            let pMem := mload(0x40)
            mstore(0x40, add(pMem, pLastMem))

            // Validate that all evaluations ∈ F
            
            checkField(calldataload(add(_pubSignals, 0)))
            
            checkField(calldataload(add(_pubSignals, 32)))
            
            checkField(calldataload(add(_pubSignals, 64)))
            
            checkField(calldataload(add(_pubSignals, 96)))
            
            checkField(calldataload(add(_pubSignals, 128)))
            
            checkField(calldataload(add(_pubSignals, 160)))
            
            checkField(calldataload(add(_pubSignals, 192)))
            
            checkField(calldataload(add(_pubSignals, 224)))
            
            checkField(calldataload(add(_pubSignals, 256)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
