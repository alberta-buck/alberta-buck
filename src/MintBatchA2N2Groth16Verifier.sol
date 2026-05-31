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
    uint256 constant deltax1 = 9730629490941339457400252958780783069842550717657049108585226138888668445863;
    uint256 constant deltax2 = 1694404807250650978426038003455900599021339431111489212582022120443718181420;
    uint256 constant deltay1 = 5977125182526562634236132239856004919217510243887771039232219481806187314553;
    uint256 constant deltay2 = 6539157787242450721211039140108194799028722938960055600648651884953976550923;

    
    uint256 constant IC0x = 4622147660886924760483730124482980230038245840308116343480099348585111842778;
    uint256 constant IC0y = 1687726158867485268948192924193403806182977340234492476883516127069628791285;
    
    uint256 constant IC1x = 8928975710941119371283160994470616436784043807385667179193573813755494776373;
    uint256 constant IC1y = 19907147084900395315802406031787647992176041850179593605133858476604642070139;
    
    uint256 constant IC2x = 21375865190562020691790371060311488177628718762049516389729136775084561270402;
    uint256 constant IC2y = 4835632847028385584309119424237389005433676950067482106330434560456912686776;
    
    uint256 constant IC3x = 3593569683413934076314633094240296388685962613068159889469725762591547186420;
    uint256 constant IC3y = 16745701141511771165962108779003671717233625706037415308290643909451864230717;
    
    uint256 constant IC4x = 1971537742572177271716388711494672571831108686383280791686758585005153887267;
    uint256 constant IC4y = 21454523333052625917590949748560819825842641102612371148327435876525486895066;
    
    uint256 constant IC5x = 1044867934849853494006558317237562971856759389421440889847502341729194885563;
    uint256 constant IC5y = 14645086274266620654741844834763501628078633791453584690155730880205545402220;
    
    uint256 constant IC6x = 7130704200466183454577308229648696181796682568333102018291835194192405544511;
    uint256 constant IC6y = 15183790469956499191648974817567267697315812629084859457246301826239859321845;
    
    uint256 constant IC7x = 14811654096878969854207109559159707099586456537169938764325158068111879051797;
    uint256 constant IC7y = 13463176882824817145318718288065614491697944343026580790065180123514929427446;
    
    uint256 constant IC8x = 3438055474549133973038109943117971185183497107650550761342132860445921710301;
    uint256 constant IC8y = 21809773731504234910612305359615237197208311320245425061142507857257807155767;
    
    uint256 constant IC9x = 6615635705411456503291097281053921433491851165290703739615283245086374320256;
    uint256 constant IC9y = 13353730943731260750884558267762899068581107295808078023416089213250495774780;
    
    uint256 constant IC10x = 17716504589909021415198612164618014580026750694266947038205802552261177076522;
    uint256 constant IC10y = 2214451046662276104108048915794893043340430001770649569768109065027909187222;
    
    uint256 constant IC11x = 3861861720607906136248360756694653107158224769547031358148502577778523914197;
    uint256 constant IC11y = 17088055356935193246944744143086057058174810291110350215645310026785658359536;
    
    uint256 constant IC12x = 8361360627085624240850076126007718801692325624834692274205017612189320033909;
    uint256 constant IC12y = 17681913035211139002339910208928240021533619388472370076561637980128654684042;
    
    uint256 constant IC13x = 3691137007731649844007782838264318279649200961386516641720521147767635714876;
    uint256 constant IC13y = 10988690977447713684118920155093171267498218574419418752530603274373307932498;
    
    uint256 constant IC14x = 4716753778677164657203621957797694015833842687993055307581258535360640848174;
    uint256 constant IC14y = 15929643347322350387495290571684989155871042578321293262992244719670509821109;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[14] calldata _pubSignals) public view returns (bool) {
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
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
