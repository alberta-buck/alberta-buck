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

contract MintBatchA2N1Groth16Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 7549294814323827502936695978060188423488862674981313507963769737970485521942;
    uint256 constant alphay  = 13656373556740987983134468882473560637791190898266838822094605792558064685421;
    uint256 constant betax1  = 17325233985537892530497345758915901751385861705282152995340401085987429107178;
    uint256 constant betax2  = 996562795036396760288251232068221184454781316360710824196075669780355968621;
    uint256 constant betay1  = 3612701804398618295030966933258232629463770950891552691505128794980798912842;
    uint256 constant betay2  = 11772154396501769287215286255543758042979089918982188708009462579276804671540;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 30797160978407403116196367997776970447834735783062789864077919638644994502;
    uint256 constant deltax2 = 21486891692634190040286419269278814424276768586637683100135363163239588157164;
    uint256 constant deltay1 = 21581440234402374226038133027871040765948847118364871304522211147768817959892;
    uint256 constant deltay2 = 829779364588453758555400478254225307361792751202923243984040932766043952040;

    
    uint256 constant IC0x = 12040338065055154423951372258543002086818426799479386756178480879399105801990;
    uint256 constant IC0y = 2427120261962001002401902596990877417912719861909354568492791053029463310161;
    
    uint256 constant IC1x = 13434917001559364440951866954335796960373054619263945774247143336086926225673;
    uint256 constant IC1y = 489883602440121817885822104142330425632948215636469812082546040302163698233;
    
    uint256 constant IC2x = 5388021639150484015396600220850542973831164439883956041867471852932108469078;
    uint256 constant IC2y = 9511155275027340854934150959526111660087356095847674389711204143725019526487;
    
    uint256 constant IC3x = 7728152784604158279384888662139408596470068289935770512853868589511701803333;
    uint256 constant IC3y = 10383920760598932374761035483399592384033625315809238160711142847299478307530;
    
    uint256 constant IC4x = 19220188710373107899745841324598115680033345749174572467975710188388804708798;
    uint256 constant IC4y = 12378208579205078388019913984252957771779271931437574265239801409647621136581;
    
    uint256 constant IC5x = 954857002127035492137407037057618240004753422718534594189941782822631276324;
    uint256 constant IC5y = 10978963645055099640892024152283825290294464672642543094739914761173906098855;
    
    uint256 constant IC6x = 18113360912563649973515298753380106109893873933315264802475432517826312104447;
    uint256 constant IC6y = 5477237529988825203859108537286235079110574611747231811197919344771414561949;
    
    uint256 constant IC7x = 4223432717190729641830895584232839473804756976316662904923111216386316393013;
    uint256 constant IC7y = 21697200489910614189988487651026859163964526903383410357520537682401565461117;
    
    uint256 constant IC8x = 3612321220325780562164375149182808060222713797955480289541081425489660361548;
    uint256 constant IC8y = 16318749942174246877538292847863628162523767531605356367361826961661118541155;
    
    uint256 constant IC9x = 8138647531282570674994039668357724778646233252451254565601238847914150623316;
    uint256 constant IC9y = 16529953525696104371590645302200128629484855663849559419805810503404664180954;
    
    uint256 constant IC10x = 12299444536717822791756289393894082069141306571197933379146723182419574591484;
    uint256 constant IC10y = 40182106027405684581946102346456505990617249918070315094320897824689982888;
    
    uint256 constant IC11x = 21768015032404321491704587750749879290577362348787652200004491625229374359945;
    uint256 constant IC11y = 15768092421737515063067340821919096878128279850477462633979677101449758839355;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[11] calldata _pubSignals) public view returns (bool) {
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
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
