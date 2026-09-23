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
    uint256 constant deltax1 = 19499971404720333932690817513740299666442137580241998976069973456466441825263;
    uint256 constant deltax2 = 17617901201457358761089661354283113183272128346590006786878826423472982819268;
    uint256 constant deltay1 = 9244853407304499912166759132444121541030199768378946213566318667316521255896;
    uint256 constant deltay2 = 8026366955082228168889858191633703994389557691837520652333082668684780173802;

    
    uint256 constant IC0x = 10964847024893760270840053900576440611318695005057661647303083943934454607978;
    uint256 constant IC0y = 8594493807464883167901385745881122664440357543788109087330451833006208163917;
    
    uint256 constant IC1x = 19449586128631751932020410330714317053858538522066635687833776781783659720069;
    uint256 constant IC1y = 1247105001376398207208017678810536599514968153510833954072743435802250735112;
    
    uint256 constant IC2x = 14177381642963374988426076171598452098851910394107908841799486740420583132833;
    uint256 constant IC2y = 6318804921980744233280724653949395804373465822143405839704673319910374490610;
    
    uint256 constant IC3x = 8239544589853065417108877953543078391050881382774366016676404191226320770096;
    uint256 constant IC3y = 19799000624845201960631083501814739323302989214569058523050205455135015640192;
    
    uint256 constant IC4x = 16122139754254516196416097645305009590506520444891505887302002046862841251720;
    uint256 constant IC4y = 9905709501776808670402442012095922137604868650136473961163444912155889953296;
    
    uint256 constant IC5x = 15354600478713952254558903243168528319294492044983479480405425495879990995602;
    uint256 constant IC5y = 18384665082493753971855151602078173393028743167152688511556045606290595947549;
    
    uint256 constant IC6x = 16975637036671531225377419978204508153727250032182135123270787969142654567500;
    uint256 constant IC6y = 2529244802201795342845950453882990567764042443181270602308875083244610904053;
    
    uint256 constant IC7x = 3884640533575282019832867084574818236980899575583931121156128179314629533492;
    uint256 constant IC7y = 2632324322483561620010913925925600039441598521670934155252529347090493857580;
    
    uint256 constant IC8x = 4560622676164806653630084212397448687872616580182362684752773957094839017421;
    uint256 constant IC8y = 11791528400591955391935775930126498797096385924783232635031729728156064296906;
    
    uint256 constant IC9x = 14968940678740710200107254320564830566881930866068145629292846912892177441869;
    uint256 constant IC9y = 16252365311629644907245155987869867280129058209232615688326021100490450129762;
    
    uint256 constant IC10x = 16753492979514876060719559741281811773738112627155724851002644180408697574810;
    uint256 constant IC10y = 3348636091009175106182798668759555697147151158134264590273556276172147239357;
    
    uint256 constant IC11x = 16799591848506654313085198087106108245012954068372631350741340447942591596471;
    uint256 constant IC11y = 3288318021682335979100689240658716328438694854628492714498757754618192192765;
    
    uint256 constant IC12x = 7251103406627864838410017341236486890231792902629118657847361404978732744710;
    uint256 constant IC12y = 11426581422114593500823061041651682935060460353012750478166313297836350013478;
    
    uint256 constant IC13x = 57452109601244007676392336871833288981975209464876293002481228012503575772;
    uint256 constant IC13y = 19573366320346880539498551459690061645990049128597848007787566607960978633432;
    
    uint256 constant IC14x = 21566850188384330545526505593300014028530796796368420303343679713975197427964;
    uint256 constant IC14y = 5209810026587427778644686323432047316324087425079776944252921627844718361351;
    
    uint256 constant IC15x = 4226400169162323743414432602929476296373178252554504626844145648810431251414;
    uint256 constant IC15y = 14152110954402745649551779870798266756606279224383194503032780259626078610438;
    
    uint256 constant IC16x = 10156118878212281094062310857361751005940223213377841148357646933742000037764;
    uint256 constant IC16y = 13619367989010897349061472205729573131385792384300485761382585816326311030009;
    
    uint256 constant IC17x = 7976690818350932900137343519021317157108104810573196928898982713871620565231;
    uint256 constant IC17y = 15247099344740504126932446179390518148804148697958812467818175869705649117337;
    
    uint256 constant IC18x = 12813951690832022979018768668228912710390409808659164754145575145153810878176;
    uint256 constant IC18y = 18052397442307298617708819768418979002662377024589091281284711983418948722404;
    
 
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
