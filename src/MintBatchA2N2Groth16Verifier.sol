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

contract MintBatchA2N2Groth16Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 16687924211486229758165918923800607259795439541342563453622599329645768045430;
    uint256 constant alphay  = 13972112323960598802739659443244783936530620095135180342331894451565456479462;
    uint256 constant betax1  = 223621484836081502100249029064101252959689485219452074783224832164701900074;
    uint256 constant betax2  = 14111154281875135313082301094403786413136164046859005530104405348562507603251;
    uint256 constant betay1  = 1919035705713236948179909672950089738803839000996186569485634529664886901729;
    uint256 constant betay2  = 18875778559066603253835302770387291779487957969879716584837663132685737077927;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 16375763911374284051447754713020107662438030652890676268680273407395638322622;
    uint256 constant deltax2 = 17984932610233908004336301474034638728119537033278676847236189453898659613544;
    uint256 constant deltay1 = 1576430670879051974293300646750063199613180145043868458377668047951672976917;
    uint256 constant deltay2 = 7851115275369727066949748549370010108932447935321763145650545967440306385040;

    
    uint256 constant IC0x = 11679711271976888188598351569330097515226076759943753888497253268738758363389;
    uint256 constant IC0y = 4152632594730537631644403830189366810229050676763244741335105101995392399312;
    
    uint256 constant IC1x = 12127680580013264223317754458435317971188860472444709072322520435653649011260;
    uint256 constant IC1y = 9839349829195869894296474587016478639242618640596764553439606384488799630867;
    
    uint256 constant IC2x = 1618891645676690542277644588069104269106356265217646778546791593312279047899;
    uint256 constant IC2y = 7191005302570020548125023837029133032237335533223450355717197537461247246143;
    
    uint256 constant IC3x = 8802120372815062181316820707522549563149322561675362890184379166841640183485;
    uint256 constant IC3y = 12780330675436444618278499995781631557306812402542726925628059486871290801507;
    
    uint256 constant IC4x = 5697247190678025058298697326365837950560625403606431983979502399402594663039;
    uint256 constant IC4y = 2637772857268444665507757964519419236747686379662882549429210854961538455285;
    
    uint256 constant IC5x = 16811656715916275077377202171491636468376658645833360909557507210516238791068;
    uint256 constant IC5y = 560930182115858273556886160474943799245524641804526379703263766661321882647;
    
    uint256 constant IC6x = 21218039946034043697981382777098648938119659039786742456430673510857365539062;
    uint256 constant IC6y = 17277999387198481349698313839249614169299539175308425202234754326144050812406;
    
    uint256 constant IC7x = 3266937071293098161879671357309325188789321869111717635183675307871794195875;
    uint256 constant IC7y = 4788060648064661834739455220438566491885274734249122096116664263120683367634;
    
    uint256 constant IC8x = 19840282532141491284307056290135330601005023178748784475510711963888029875226;
    uint256 constant IC8y = 5578165681108686426666079466171602780202236169229212783773840523958410006466;
    
    uint256 constant IC9x = 13802780718238458725043385103129756371287133540755073784263362819335584481762;
    uint256 constant IC9y = 1203408077496133656645132108947281485489726416666503492816756442868440880057;
    
    uint256 constant IC10x = 21764319836025921382402775218603667657982295519565484509032617577795542329053;
    uint256 constant IC10y = 11972731065765257985219694676493928887056116760549125368057069510064304332819;
    
    uint256 constant IC11x = 8083629491944384467532517320703712879530605129355815787941354013773906449490;
    uint256 constant IC11y = 12558869503269468762451623349571995152080038209953260145574315231823386186195;
    
    uint256 constant IC12x = 3395429586725680752288424028547068857491882405913237408434440769937260443791;
    uint256 constant IC12y = 8527825199947226535345897321466364447951317706196985059522707234212630679799;
    
    uint256 constant IC13x = 5244962058551226119798074869893839661430621836166647047046260672888075952196;
    uint256 constant IC13y = 16737684344239600064701061789695112591379468836598837650374788921459746631120;
    
    uint256 constant IC14x = 451407552234343429592247843842765175813060215679023519568646639307358304878;
    uint256 constant IC14y = 4078888997340033030557494329345868583971680148485514503936896671775096539460;
    
    uint256 constant IC15x = 17610758571083573312324814554466798350624483243111276603373457156129473095066;
    uint256 constant IC15y = 13465461583911319621620632677425825838271682253476626401516900569553292734193;
    
    uint256 constant IC16x = 3348150711708585750970744052199346588848526951263170484031407679882410537639;
    uint256 constant IC16y = 3509850097276969011251284535739055829203151329661327852643908198264047820857;
    
    uint256 constant IC17x = 19338180740630235387020581365643463767719411821884404509669040241221579364698;
    uint256 constant IC17y = 11457141511129785644834584267879550574042693083998511589106734543011341986572;
    
    uint256 constant IC18x = 505383870741559419093561080502844514752417761752027716515024109108998276868;
    uint256 constant IC18y = 1051518802163967817918092052388606350283558945814712359088268054262392214618;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[18] calldata _pubSignals) public view returns (bool) {
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
                
                g1_mulAccC(_pVk, IC13x, IC13y, calldataload(add(pubSignals, 384)))
                
                g1_mulAccC(_pVk, IC14x, IC14y, calldataload(add(pubSignals, 416)))
                
                g1_mulAccC(_pVk, IC15x, IC15y, calldataload(add(pubSignals, 448)))
                
                g1_mulAccC(_pVk, IC16x, IC16y, calldataload(add(pubSignals, 480)))
                
                g1_mulAccC(_pVk, IC17x, IC17y, calldataload(add(pubSignals, 512)))
                
                g1_mulAccC(_pVk, IC18x, IC18y, calldataload(add(pubSignals, 544)))
                

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
            
            checkField(calldataload(add(_pubSignals, 384)))
            
            checkField(calldataload(add(_pubSignals, 416)))
            
            checkField(calldataload(add(_pubSignals, 448)))
            
            checkField(calldataload(add(_pubSignals, 480)))
            
            checkField(calldataload(add(_pubSignals, 512)))
            
            checkField(calldataload(add(_pubSignals, 544)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
