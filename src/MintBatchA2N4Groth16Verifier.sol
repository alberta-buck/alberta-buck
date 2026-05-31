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

contract MintBatchA2N4Groth16Verifier {
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
    uint256 constant deltax1 = 15579790631376504007741196438349891630531819503057018162841906519140342969524;
    uint256 constant deltax2 = 19927485056815656753056386240753942039525788946380476931234472205046711750232;
    uint256 constant deltay1 = 15951974419248533955602886633080494781247123842108339088236120823591630295291;
    uint256 constant deltay2 = 7985339658854011026593522454846215675315225738092271644552365427665246310786;

    
    uint256 constant IC0x = 12617069240107098080014414682651245789216316788655288187104701829106796973333;
    uint256 constant IC0y = 6110407023223708485714606079723120477604973249562181156142544311773582003188;
    
    uint256 constant IC1x = 8209504462922269336782575926096343365461640425770047837722304166516725741727;
    uint256 constant IC1y = 3329377791796943324703466328194541000079925288632401198666053670133729989945;
    
    uint256 constant IC2x = 18795480313011545340304211128777433181418237763445155935961303587618204749219;
    uint256 constant IC2y = 21061069278531006006981289194063271004851963195031382457879200148316017183910;
    
    uint256 constant IC3x = 10921355566388085616758715691871599964000744211440205063125857636960802329842;
    uint256 constant IC3y = 10854767439641257770431711749711685381464807056226931086906962993706604553265;
    
    uint256 constant IC4x = 13840374676830139580574406160627300799478011667744960586953119504599016287370;
    uint256 constant IC4y = 8545404813646689107220461436913405695383860430504817228588217498279634051166;
    
    uint256 constant IC5x = 17647170929391352885136928230283443504676203575911825359525871298733499441868;
    uint256 constant IC5y = 19556124146752580543154446078099302291341740444369279851223107451915233793620;
    
    uint256 constant IC6x = 17617735276297994635186042736665677463600688110457647003599622280649401687007;
    uint256 constant IC6y = 6101411018649072123819974710225180048683871001948153563677338068479803599716;
    
    uint256 constant IC7x = 1793443558734756818914140177869832520207449340661549163661531727858529371166;
    uint256 constant IC7y = 5468524707001599223100478131334631850747564308919903137628600378015347898479;
    
    uint256 constant IC8x = 18034681831482246408577640939220423803011226038248245315249837647735386748693;
    uint256 constant IC8y = 267936620744749841126100710602076702532284889769165873942453582207376770486;
    
    uint256 constant IC9x = 13639508596935276405702514807597000512755761780458750771145718905932723675643;
    uint256 constant IC9y = 18404335428973935382450647525902821108768376881917764545496402931108261761295;
    
    uint256 constant IC10x = 21699115308393569239924525093777673579065237472190904650941959294490064065023;
    uint256 constant IC10y = 2627906412434603131195453160384679481458531283736408470358752916689745571526;
    
    uint256 constant IC11x = 18392301712350037504466243701089549882139217983922111411402331043058501126148;
    uint256 constant IC11y = 7447967243822341915712465699595035727740838760605174003379486946418834144376;
    
    uint256 constant IC12x = 20498187594554443993061933724395081999240311203471412082689551314698703405058;
    uint256 constant IC12y = 524188791677166089865863212571240860343046993982443965937989033311984213918;
    
    uint256 constant IC13x = 785295510072428692825328897021868634086355604822561991437063106896304354407;
    uint256 constant IC13y = 2796336149314233743925154277930994597318507174521656646486492782769443904472;
    
    uint256 constant IC14x = 20845032119261863535832320090325778940734208756125826204692624945451906190850;
    uint256 constant IC14y = 2307826169218363181287366786332649966004838228518388683667494059166942347213;
    
    uint256 constant IC15x = 18013312142159019322442083322977826815472020964163138775645242424873632303713;
    uint256 constant IC15y = 11493078091315822839946979983869313253606203582422423056311516640301428416921;
    
    uint256 constant IC16x = 4604892408814973996592147960974865204522932650202855193869974139582412689106;
    uint256 constant IC16y = 7477539682939013444465075347829494509332001532289432006125199550204502687330;
    
    uint256 constant IC17x = 18394264898571827139221033620243612588887708269114322295362916242754957787808;
    uint256 constant IC17y = 15549399387830634126691990845966208750329995784444396234471196485241105123103;
    
    uint256 constant IC18x = 11917231132348187360086610853375486101264401240254466262469791255244140208922;
    uint256 constant IC18y = 899536365822801956200805213092578437354235341167799356495434522902873135956;
    
    uint256 constant IC19x = 6372805099508273766884304885578944965742415037914635191603257657062195739586;
    uint256 constant IC19y = 6969816498621379720997012129296222624759275720932966450502241720766497159780;
    
    uint256 constant IC20x = 4389401224059446124163312092556482125431816560924320956217882108898990734947;
    uint256 constant IC20y = 12661359210329870952082210587599808518776838982583586284587091836370846886977;
    
    uint256 constant IC21x = 6333772986860922441350855373716304497313176184530727910566779819411535747676;
    uint256 constant IC21y = 175133335865476382051468872100371897399231884778109882595876524557703031926;
    
    uint256 constant IC22x = 19884250592632966163929314675017337287877093148270953935412720146934483138751;
    uint256 constant IC22y = 21074050192303404106977947158640850782911397074273279882029584965723391865249;
    
    uint256 constant IC23x = 19934433197867645470466540767991566474849986254257009871537060301345751963282;
    uint256 constant IC23y = 13039565094066609995578631167202918432922407495046563875926402536290501949176;
    
    uint256 constant IC24x = 2120921478675207808362258410995700733031903367128016823501102033455413297584;
    uint256 constant IC24y = 10710227140898922382713662825613717603832985294057948230206278049634114458139;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[24] calldata _pubSignals) public view returns (bool) {
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
                
                g1_mulAccC(_pVk, IC19x, IC19y, calldataload(add(pubSignals, 576)))
                
                g1_mulAccC(_pVk, IC20x, IC20y, calldataload(add(pubSignals, 608)))
                
                g1_mulAccC(_pVk, IC21x, IC21y, calldataload(add(pubSignals, 640)))
                
                g1_mulAccC(_pVk, IC22x, IC22y, calldataload(add(pubSignals, 672)))
                
                g1_mulAccC(_pVk, IC23x, IC23y, calldataload(add(pubSignals, 704)))
                
                g1_mulAccC(_pVk, IC24x, IC24y, calldataload(add(pubSignals, 736)))
                

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
            
            checkField(calldataload(add(_pubSignals, 576)))
            
            checkField(calldataload(add(_pubSignals, 608)))
            
            checkField(calldataload(add(_pubSignals, 640)))
            
            checkField(calldataload(add(_pubSignals, 672)))
            
            checkField(calldataload(add(_pubSignals, 704)))
            
            checkField(calldataload(add(_pubSignals, 736)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
