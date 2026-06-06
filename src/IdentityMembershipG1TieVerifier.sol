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

contract IdentityMembershipG1TieVerifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 16687924211486229758165918923800607259795439541342563453622599329645768045430;
    uint256 constant alphay  = 13972112323960598802739659443244783936530620095135180342331894451565456479462;
    uint256 constant betax1  = 14111154281875135313082301094403786413136164046859005530104405348562507603251;
    uint256 constant betax2  = 223621484836081502100249029064101252959689485219452074783224832164701900074;
    uint256 constant betay1  = 18875778559066603253835302770387291779487957969879716584837663132685737077927;
    uint256 constant betay2  = 1919035705713236948179909672950089738803839000996186569485634529664886901729;
    uint256 constant gammax1 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammax2 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammay1 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant gammay2 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant deltax1 = 16972600581598584336808879407107409906688055209074321836005719324141609955595;
    uint256 constant deltax2 = 20893691393530933222799070499418514677640826139728406748765470020437533046900;
    uint256 constant deltay1 = 18436976974606041246666604028100838767668986485688762774616357371798175263566;
    uint256 constant deltay2 = 19230219667495274752730661233085620136270312661495580546287621233274054117809;

    
    uint256 constant IC0x = 7275369475961739889670744683496308344429731110506147300018775858871313928091;
    uint256 constant IC0y = 4109058351470064799984672708280265671268230503808628038010797294856997517187;
    
    uint256 constant IC1x = 16242650191171213450434933525490887824516657650024399141535630198361241559495;
    uint256 constant IC1y = 397928813849731449454775974000995360624842373546170398404173656520587097735;
    
    uint256 constant IC2x = 15896868772775791026508035297834406676601914414856188989086625423410199115681;
    uint256 constant IC2y = 1955631650386338190616164425448595409694786479951821451912761316166739315920;
    
    uint256 constant IC3x = 8884580115359014313618241953202537493125802900514318013522056348503825195169;
    uint256 constant IC3y = 2666501154209441644526685461140069717323902584592995664756265588911915532742;
    
    uint256 constant IC4x = 7947276810858310230624908922106685980428426184120197885881684159756099648165;
    uint256 constant IC4y = 14477500959422708775723719005979561325635145031828969600545025960503245358445;
    
    uint256 constant IC5x = 18586752372785183217158493015974296305075483076076820028287758441540968049722;
    uint256 constant IC5y = 20314609141922170774530327035970014670606936137236458794092160956762928048793;
    
    uint256 constant IC6x = 21443788717114181880329173777016686544243734130331944692694617499928492508698;
    uint256 constant IC6y = 2390127366263473365021628935246565533317396200376204891361444255844529654221;
    
    uint256 constant IC7x = 14040603470911256930238194550953262275908898411127809364799828120530113422295;
    uint256 constant IC7y = 14756971346948851980459468273936905613515395207382040200958671986433432349444;
    
    uint256 constant IC8x = 7571711543485395777520449427600320482003992734805558978706598564264263081326;
    uint256 constant IC8y = 4964489705522436162439620346484994054575629823826780538419366857889971530859;
    
    uint256 constant IC9x = 13954026562959773127485168304506450211194860977193240235706286323783575390414;
    uint256 constant IC9y = 1914190234031271440471061660578816818924359913232814752581332800234568920218;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[9] calldata _pubSignals) public view returns (bool) {
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
                mstore(add(_pPairing, 64), calldataload(add(pB, 32)))
                mstore(add(_pPairing, 96), calldataload(pB))
                mstore(add(_pPairing, 128), calldataload(add(pB, 96)))
                mstore(add(_pPairing, 160), calldataload(add(pB, 64)))

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
