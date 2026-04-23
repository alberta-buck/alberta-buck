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

contract MintBatchN8Groth16Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 5222163254956769720861560326401075741386994847086210558405366783377756092216;
    uint256 constant alphay  = 19475436559984623554357953129009359053333542731694135281922495325049604221230;
    uint256 constant betax1  = 3059516489220240630186271195914183140946064923114682236776148214202533588863;
    uint256 constant betax2  = 4726638434264925561921847005269901493966648479077249500650785888021982438689;
    uint256 constant betay1  = 20695777602333613504155353915066064094342222466636704397235542841177800495176;
    uint256 constant betay2  = 4930004227668553506141963663146714216566264478789620269782604620259282120034;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 14851534337896417176020628398995440716351381811970379100280439875531140272648;
    uint256 constant deltax2 = 262133721892609424143899341288967932852002302323775232932718323326062942698;
    uint256 constant deltay1 = 19709818400118578003836702587568603115368735178426900188842775444093664298526;
    uint256 constant deltay2 = 17201112009232932919472231595376804160998174704505226339569302596106345416569;

    
    uint256 constant IC0x = 9514656341480311693226334375096882792833853320302659693155738449360462177712;
    uint256 constant IC0y = 9676320244886001804251259399661354304300308597804031131675496630268052617170;
    
    uint256 constant IC1x = 12987707410347608905551771177601442801568949504812913586612609362684552905396;
    uint256 constant IC1y = 11021155154190236114537889459810841361256072948566093616551784497018043551993;
    
    uint256 constant IC2x = 8826920535154042916746529152643032705327835094982797983769893457626023815616;
    uint256 constant IC2y = 20131729954830948309027929842798621313270484486381845187651380865469109031450;
    
    uint256 constant IC3x = 20730738195194260773698090277296603210013856472406604796233016551412169645291;
    uint256 constant IC3y = 18205960675110790392758644703776443582603445174649073545539023323662083773655;
    
    uint256 constant IC4x = 11407698447372567050654352299764173204404283234515871598608635301285245499843;
    uint256 constant IC4y = 9686823919052623113558866949753404176989599631678936979964520911066607414901;
    
    uint256 constant IC5x = 6275053268109198920975751933821801752680447589108775976038915787610651108910;
    uint256 constant IC5y = 4018386548920184748984431492671471026415690723923559328182512840440649645999;
    
    uint256 constant IC6x = 19894716763762765177893563464129979619382191753394812151601065465735691314460;
    uint256 constant IC6y = 5219317565236917921338398804557166256014274688676857533250164797809721713437;
    
    uint256 constant IC7x = 7732973269808565876458390432816279723839487278931842197776181568606860233144;
    uint256 constant IC7y = 16502346212561568583795295129503939430046983708922531371983130409508441943442;
    
    uint256 constant IC8x = 15969109373315774927632132164571887749552949642686914888498451868181376924016;
    uint256 constant IC8y = 6350817493535622787071357804620868481588651183045731632013981315749243281306;
    
    uint256 constant IC9x = 4234750093286728050920697011454537441245851132855685067218625039439549239456;
    uint256 constant IC9y = 17630162843795123530356477045265793224411344218731889847243485328620097887700;
    
    uint256 constant IC10x = 3250278655694150837564197838417805957666471232055294912148013603880029707212;
    uint256 constant IC10y = 10436225315426766500965655291295477026536847392194337610729668187379369448939;
    
    uint256 constant IC11x = 20182846932028980139006997152016408113888493430047144257673906639169128740202;
    uint256 constant IC11y = 15676934399189081490073017860705178407208038703009152266536433083772022959596;
    
    uint256 constant IC12x = 417481532377044766869477064897813334273415512124363108211498583097080551290;
    uint256 constant IC12y = 1772852480593613526960275310619224130957024894329879607521867958496519199577;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[12] calldata _pubSignals) public view returns (bool) {
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
                
                g1_mulAccC(_pVk, IC10x, IC10y, calldataload(add(pubSignals, 288)))
                
                g1_mulAccC(_pVk, IC11x, IC11y, calldataload(add(pubSignals, 320)))
                
                g1_mulAccC(_pVk, IC12x, IC12y, calldataload(add(pubSignals, 352)))
                

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
            
            checkField(calldataload(add(_pubSignals, 288)))
            
            checkField(calldataload(add(_pubSignals, 320)))
            
            checkField(calldataload(add(_pubSignals, 352)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
