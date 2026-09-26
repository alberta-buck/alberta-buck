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
    uint256 constant deltax1 = 21216565892272808471004100447459318560133668418152682523879519631146181861452;
    uint256 constant deltax2 = 15788859612462312695458753342505324607548183962167712218669054112499261818155;
    uint256 constant deltay1 = 13432717352569558989453771589611360771787414482273067597730187439404688563521;
    uint256 constant deltay2 = 1321470445698080409670879665993027264621423794179157742276140239832937647254;

    
    uint256 constant IC0x = 5315179055847738248043889798727478820304226966737105047594187502708165474431;
    uint256 constant IC0y = 7975763143217357019126505629886981257362201363562020341555411862885145431010;
    
    uint256 constant IC1x = 21674325286383304789906368852787992329773698372350197600642737122050981936352;
    uint256 constant IC1y = 14199158247280533932967011417743981757032332456543979541903676359428398669303;
    
    uint256 constant IC2x = 19349280056561500295681844411488267949873068933842309261165438249334962538403;
    uint256 constant IC2y = 18175176685843017034987578552756117901744979213422925990817399739073817518629;
    
    uint256 constant IC3x = 19801044386584943132302910147696355293533015933438769177161465676897682910999;
    uint256 constant IC3y = 12367578014543936012389848645486341140951523447573243735708038054442663409563;
    
    uint256 constant IC4x = 7433610069965082319919927417407355500990239931502450361729279612030140673019;
    uint256 constant IC4y = 20918850988738686629633559371661432108671440740426722934028366343530506169938;
    
    uint256 constant IC5x = 7801248137466833291759831421030397371461216922142646569950299963184408162037;
    uint256 constant IC5y = 9487325975200737230983957323429568351687484571410510774339191434168147817554;
    
    uint256 constant IC6x = 12071920366106919007403946983611671911590089645364791889631334201891748119873;
    uint256 constant IC6y = 880921721867975099803429920143149376854510488613194502630654809897351113840;
    
    uint256 constant IC7x = 18747138393719597632888993513320071851668765847402172376026564233789515499434;
    uint256 constant IC7y = 1849357143272479237930844653302369455002513855446673769751327229675335866074;
    
    uint256 constant IC8x = 10400076902883072760850088181277421173717266921557128472235650126086654713992;
    uint256 constant IC8y = 8582349373858601081283918906652764376517032737575398497131171450217539319517;
    
    uint256 constant IC9x = 11080675255027143755120634529992414217321639406832811355285921672054156922181;
    uint256 constant IC9y = 10371426271196921150368382918822403934263897622652747910709195568933423534283;
    
    uint256 constant IC10x = 695686079207197122162212130012482698939019227461424003267849036680097682777;
    uint256 constant IC10y = 11764654418284562528161607934133398391568101816374747839444105666917925757118;
    
    uint256 constant IC11x = 19030001983633590066306092727727091135878027746106361693001159240781740284569;
    uint256 constant IC11y = 15526301503375491899785679188243697640886753335316961144406520495806488507857;
    
    uint256 constant IC12x = 17503779290640661825863288661826992173837850752704438572707540645879048240287;
    uint256 constant IC12y = 13564659615029893550377025132309310537918458858374846210694376834822549429003;
    
 
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
