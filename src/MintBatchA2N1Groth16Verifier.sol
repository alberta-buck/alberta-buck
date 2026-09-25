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
    uint256 constant deltax1 = 17353312018655867602228226148893597128078240091256525404597090929655099688156;
    uint256 constant deltax2 = 4383919802883529585267883822266274205063057443874361702172408210419260450752;
    uint256 constant deltay1 = 8456031222492227505087504169403860422819418618718522607111748871713675475788;
    uint256 constant deltay2 = 19415337551598838607337564955048466680443109733390841080319158314717850341977;

    
    uint256 constant IC0x = 13781841577355486213030520600117390065700841352873896916440274280333590282223;
    uint256 constant IC0y = 9111188395074496422225641182324332195742814657703137403601816367040862080699;
    
    uint256 constant IC1x = 2795366253289201908131100611558281350257049151281416737865624491439852933405;
    uint256 constant IC1y = 12546339515938317298607175990994382957688978465123720071280391632524120822416;
    
    uint256 constant IC2x = 21471120239427411134196804946987275342602394370159155629131706374010582913348;
    uint256 constant IC2y = 10204023903065535921571392684480279610765022838172932411664204543643494679368;
    
    uint256 constant IC3x = 12362758979536676145868304998358660793062570527027365849239419032495523672834;
    uint256 constant IC3y = 10702591521715475161483871786537916784668828170980514909830705380095935790778;
    
    uint256 constant IC4x = 14723171598650017484714438364165421083793469906129480965772808330733558662059;
    uint256 constant IC4y = 10933627913880245375623344421495099832896384267808264556859046084905890835474;
    
    uint256 constant IC5x = 2131980799317234159335068387648612012223771253105569192879402196232780753060;
    uint256 constant IC5y = 8530887717206568421471444798740391308934263843585835247149183179063242964243;
    
    uint256 constant IC6x = 6517895016144194158461713356301696285279462242055504654284396357629217138832;
    uint256 constant IC6y = 10514640385139689832957079524077235041673698754504836292508676000728892834986;
    
    uint256 constant IC7x = 9590736008631458797561481880153611006859478619235565206048604069579365101;
    uint256 constant IC7y = 9947926494959473771649309167619466542728895772045929901229877077999105713017;
    
    uint256 constant IC8x = 10425907488496840210484054619380937815963600515774546293454178500914140502495;
    uint256 constant IC8y = 14384119761422037761751813647947984597569754025675338824318205810535443880342;
    
    uint256 constant IC9x = 14419941792043140030144258411705821542009413255460564377147951070960559205463;
    uint256 constant IC9y = 7775202456313145153137316767234055577815646874255912359598488236135810733156;
    
    uint256 constant IC10x = 6029130721168430177943594011625450331012441355101030816337160582052683821540;
    uint256 constant IC10y = 16990378902668502371471651821133906505973053071603729116917052234713599610425;
    
    uint256 constant IC11x = 16675315364510172454006771200574912542127596876985036507565320957894189891100;
    uint256 constant IC11y = 19553538239387219964941506426208831250744710892457102928231337526971757633992;
    
 
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
