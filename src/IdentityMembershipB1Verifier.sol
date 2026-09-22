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
    uint256 constant deltax1 = 17993691252840104352697476205901564533024343855887828508508670418598828106770;
    uint256 constant deltax2 = 12753720781059907591610353693601380148482980199136151860466319274199724692530;
    uint256 constant deltay1 = 21635538675880398738659739062248766413034651196312279062011005497050758502845;
    uint256 constant deltay2 = 1974447289634854948057773202137160044889923518644586858111457789953146837042;

    
    uint256 constant IC0x = 6102685019474549664234678236445665383771427264808840892206689290803956454485;
    uint256 constant IC0y = 16980408215620902513070883677391799428852327522297072517195464320292330858725;
    
    uint256 constant IC1x = 12911460458928893924490111142124308182286014472822843880546242812818827305179;
    uint256 constant IC1y = 8025974527253583706451848167925262417586292198482245512800892506577488739264;
    
    uint256 constant IC2x = 14954049894286867728928567534727142923691721093766191800686681137720035332827;
    uint256 constant IC2y = 13992047840133237733277993545042402312894599338868295169834301002377702990025;
    
    uint256 constant IC3x = 4356653133364864956184343033192215888078747565577639115122207884427322318629;
    uint256 constant IC3y = 7522430059363148277921116611318421096512816660909016456153710021234370541000;
    
    uint256 constant IC4x = 1660205683797782300111568182666672551618968933350846540854361470420058844307;
    uint256 constant IC4y = 2234427497104183282761453214765300600554564891851802916127544086529097177711;
    
    uint256 constant IC5x = 13927160631422073279740890619726289520559767876044764523955983355755030647209;
    uint256 constant IC5y = 5932959479727357413572156886356011794612352782376459247984075609954431896664;
    
    uint256 constant IC6x = 165202855822505753614218935960123438949964688925649986070412632507255242113;
    uint256 constant IC6y = 1341582474165200519929971080002723145161997664504847082461332923012693698146;
    
    uint256 constant IC7x = 2645465095410279288941513389880123834180382259576401329534184310488969191614;
    uint256 constant IC7y = 5337611436834984028832757196849767509327307855784873097566394159147229972555;
    
    uint256 constant IC8x = 3289891314525260617417739651261713116665180744212635597875433923024850559790;
    uint256 constant IC8y = 7349182673966182761103883631697148568412829216033535599305095277232807681943;
    
    uint256 constant IC9x = 19354999295796482533881182803349899632556160601644320788823702608796284418266;
    uint256 constant IC9y = 3601112097112926691749022984221493262481187861925618938743199388820778775332;
    
 
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
