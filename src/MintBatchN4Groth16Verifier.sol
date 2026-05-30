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

contract MintBatchN4Groth16Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 3631344086447600850896190419206420896657050492374191812883457347673571379922;
    uint256 constant alphay  = 14989005213188149616231819742625774045352845752597498751888940135169747957878;
    uint256 constant betax1  = 9288001167759986798601287554112931403268890387295497334378334514907605789422;
    uint256 constant betax2  = 18225411497800851856763395424903998811621518034192521220589788367768147826470;
    uint256 constant betay1  = 4912977382675421359068396990903804674786508328236952129039925973168636116033;
    uint256 constant betay2  = 10553400317604699926820584447706587103271842941642344264770658229076039827844;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 4190968914274222471678596190173664847380998310478303713969933033821469091660;
    uint256 constant deltax2 = 5130313443004731337298801767389831193541654383207893629204755879032015395007;
    uint256 constant deltay1 = 15070297098833947356953454080921138824288199552792817194366824454167024188857;
    uint256 constant deltay2 = 19047404364724446087310276666091539930883023062236133908817228767873582302573;

    
    uint256 constant IC0x = 7576897787898298428094021184347724826816096301199588450912825749112957978841;
    uint256 constant IC0y = 21191415017255894691335822059783439770575898650756859508499788888123166534895;
    
    uint256 constant IC1x = 14631909630129630946952341132865705696162421447161089847683495867102497194224;
    uint256 constant IC1y = 13016096315356056875848272459402932282552964458841722006422063701129004091556;
    
    uint256 constant IC2x = 7426882930565784693468145666033516559533670373799861066494679364255753328231;
    uint256 constant IC2y = 15740104935454245499533706699331671947379044746545362424597083931409791824667;
    
    uint256 constant IC3x = 2529140346491744236971517604429418281731916119043800847984891570952516054530;
    uint256 constant IC3y = 19596591345344063407583508123521598771039952710385764952738123873784589071257;
    
    uint256 constant IC4x = 7681714541378454250924847423761064856021849819931967772249429955334302687035;
    uint256 constant IC4y = 3872662065349830249676202268059159867297952864408752483360858202529890103329;
    
    uint256 constant IC5x = 7459391389461358243775438273238159577535102888651178404236689401510350884924;
    uint256 constant IC5y = 5389110104139013679713537273629012111065001878051635645955126570096323143069;
    
    uint256 constant IC6x = 20573478011070811977556216692749737591985779727537751856838828303572170696976;
    uint256 constant IC6y = 1260697509711168719520991219080427551102437884630399740235289882254250303804;
    
    uint256 constant IC7x = 9198068547226864209028410352440852632505835859699246711759500214912975672314;
    uint256 constant IC7y = 16850596540793481185095388273300573672253113627555786211715038490832022542767;
    
    uint256 constant IC8x = 18874294077850571200120731517180365922301127791292487253585153123323402232958;
    uint256 constant IC8y = 18262569543119595305599620751620117079580878318974613428775296961502517511686;
    
    uint256 constant IC9x = 8309989314916315860501569218695554722493825485842996981634424352029869719011;
    uint256 constant IC9y = 20597255056820798806084412568120379513407786311159711264648734821397294410801;
    
    uint256 constant IC10x = 10179121258655561844990445732025388562231226750761469841011184132251077802526;
    uint256 constant IC10y = 5565981747935766250879597836309593562414262245794031746064151895798278659567;
    
    uint256 constant IC11x = 10633197930020564770952618811484601519865589864225509978124423958982258982561;
    uint256 constant IC11y = 7855627403546135554125738888355474850947615351720864501270032966634910852719;
    
    uint256 constant IC12x = 20760450759871501143752961764041317559935677966150283546077093993774483514412;
    uint256 constant IC12y = 505700412887947579515283514194370519835979074446531008693366260593816341729;
    
 
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
