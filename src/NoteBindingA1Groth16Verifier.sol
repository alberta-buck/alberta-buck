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

contract NoteBindingA1Groth16Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 17269339007125420435017385076298032648558905116036800167317165580267863063327;
    uint256 constant alphay  = 8298769059208403406707536659738406107699218080610564322476779891334978218882;
    uint256 constant betax1  = 19287679028377239150943411209093196901533308024455742180196324837626175744698;
    uint256 constant betax2  = 15446589398518190454274420272385151265977637145422343165550062847443974749946;
    uint256 constant betay1  = 5858245119689183400807064004799128131011760842567486047011829007784080776273;
    uint256 constant betay2  = 11545678145891239261012950117434374641078746363998546951851739104882335488744;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 15640717942898521247008744313035683314818067527255894649814996196557987408965;
    uint256 constant deltax2 = 18371514141086236844139417177699578134076186289082630042243017663986993254937;
    uint256 constant deltay1 = 10384478535832510900594499956951773913726448092197098946869965595096153571743;
    uint256 constant deltay2 = 9415194870005655894422483094791625122035937802953664640237181648668542910325;

    
    uint256 constant IC0x = 18838577401059708285857228608023323439093399045027730302796311861491598409359;
    uint256 constant IC0y = 9243159193696291881527061640113831874323331639302655850482341394679239321397;
    
    uint256 constant IC1x = 9773271769783597067471109104237995356240288911050047494614399300529161101938;
    uint256 constant IC1y = 4908164921625175033241214299745051020997083004640023733337055211996396034459;
    
    uint256 constant IC2x = 2995368288250971642970866903646548081302038621190218132522786857461271852849;
    uint256 constant IC2y = 21484908530048040932226149219434309679532375308160084481272329854694848780418;
    
    uint256 constant IC3x = 3396318044609049869025348503100104448227950736850946019497793928296660492977;
    uint256 constant IC3y = 3215973156058276506611246115222532923783507859868766632705568438468916288980;
    
    uint256 constant IC4x = 10109030916998308057900627952988298847264965048929264783162009066169168406345;
    uint256 constant IC4y = 2217899537102737906546417804552448784060307025502192350705835707277275894201;
    
    uint256 constant IC5x = 15950144975695710774399725169745769581241389173265159826223627395030352061337;
    uint256 constant IC5y = 6987019261903183901347332512781482999761092110497651950546410921611139021015;
    
    uint256 constant IC6x = 13906385268529556437265130344935486408022544380396126435162302985576631720448;
    uint256 constant IC6y = 5407560480980306594268833105811244604463585252384006948514308215296892998483;
    
    uint256 constant IC7x = 17267051982680673122205862794739715621044502253148544196963268865685874319414;
    uint256 constant IC7y = 1359161500092430123305302502921491723296680938303559121046256340390997268586;
    
    uint256 constant IC8x = 15871513228353881852058633529332082043076890725493816344961019330660204904877;
    uint256 constant IC8y = 15576943557680803355828322237527554440625477963081508828664631751837301179297;
    
    uint256 constant IC9x = 13878122355438769926329470281078055907709194062284303051263855862934045981075;
    uint256 constant IC9y = 18689836615699582376036313820629596864785519978566749072748328449954122038392;
    
    uint256 constant IC10x = 6655734315795395805494464917676487181870624569283136259765487062898699658962;
    uint256 constant IC10y = 16535769365501343518998204594654691398349949905505678395608760574136753884200;
    
    uint256 constant IC11x = 15768279513852332025417400248611018015729834464767843709847976318062419783575;
    uint256 constant IC11y = 1092098761374541845171928826076426569431729220317613794408635011811504131879;
    
    uint256 constant IC12x = 17811907919545073473044962077088544104725481787249231371208321908872839215161;
    uint256 constant IC12y = 14443984006346718820894358943348642587432094622191009225961901100266462910674;
    
    uint256 constant IC13x = 14013633155379832734552270242906022485500313395423930906308329754052828385919;
    uint256 constant IC13y = 19181985444133234059189501083041498589015422323687611669063858806184547268044;
    
    uint256 constant IC14x = 5048908775763501012646876794389162378540461151035363413476652361685039529267;
    uint256 constant IC14y = 9479989070176087391431830373452198035806734647011273296671491660953320737547;
    
    uint256 constant IC15x = 11970017968740741498442273717733428771291150851794842643338567745596496605081;
    uint256 constant IC15y = 2365564122586222845844459320021971774707920989489893357123852294265336992283;
    
    uint256 constant IC16x = 16764265362697939848244698679216312276522370995437774801011207833027143073894;
    uint256 constant IC16y = 1462617983649028913689157031748119256264460084269875139017738630459113319397;
    
    uint256 constant IC17x = 3613548041890318408090911671967505276757091676577643825442373999362849687499;
    uint256 constant IC17y = 14387976725515308811685121077987444842588752875322321270336724877511354692733;
    
    uint256 constant IC18x = 16692577271003285149462576857222692876180684166712227467937065923435811829884;
    uint256 constant IC18y = 16442749143388180561742496285618832024421797321679112716932843717272301471025;
    
    uint256 constant IC19x = 9720434391981983924648717853167038930517298074624889495853995697762176483486;
    uint256 constant IC19y = 17928533043470777338447314977124673087213233310307335909539948907236098663884;
    
    uint256 constant IC20x = 674975376437640883738991695082170439439692296793406770712120307524354503595;
    uint256 constant IC20y = 21466944868003880329016364993721738629460427165920539429257822771110736665386;
    
    uint256 constant IC21x = 9046222675590415088668613714213810270658080682544789820693623725473425952233;
    uint256 constant IC21y = 6837243070457142378419609594904767763939681031092279483686410950448062638584;
    
    uint256 constant IC22x = 12956113448594155000214198307792153588511447933168560354520613050077626340882;
    uint256 constant IC22y = 10795799442203225728470684204405780415190509362016405502061203105576394855610;
    
    uint256 constant IC23x = 16937890752996635248262857307323580997412317315169755427490902337326946268631;
    uint256 constant IC23y = 21707216837521291155020120584133091881668364623671691009352454320672239589838;
    
    uint256 constant IC24x = 3555470269337215022508982687950811984140499562368987107268251331778385760631;
    uint256 constant IC24y = 10592889992115082289428224644820978391055364538631425775064319150927278605601;
    
    uint256 constant IC25x = 19229578953817799595879964930847272139775779994255023002229613997115911194601;
    uint256 constant IC25y = 6741135301059635981528707402776042983858151667839218008203080922610226863063;
    
    uint256 constant IC26x = 12055249485190774587127232643975192609394876023255383710199598261047290875433;
    uint256 constant IC26y = 18524102511194803621959116702303022956968476632084248589848114872804128269128;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[26] calldata _pubSignals) public returns (bool) {
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
                
                g1_mulAccC(_pVk, IC25x, IC25y, calldataload(add(pubSignals, 768)))
                
                g1_mulAccC(_pVk, IC26x, IC26y, calldataload(add(pubSignals, 800)))
                

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
            
            checkField(calldataload(add(_pubSignals, 768)))
            
            checkField(calldataload(add(_pubSignals, 800)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
