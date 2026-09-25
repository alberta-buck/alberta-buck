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
    uint256 constant deltax1 = 7367784755560764570968257246860852956781221543671523211695245932247129352475;
    uint256 constant deltax2 = 11083733527368815835393375032011463507669262570164787448075948210353735830847;
    uint256 constant deltay1 = 7808935555104769452064061695631560693935558843515946348217875497818768429701;
    uint256 constant deltay2 = 14114164506282440574062015221037937956800905480254000206117687886328395875472;

    
    uint256 constant IC0x = 717062973236510776690260985516016054999303839208967371193427685432930810219;
    uint256 constant IC0y = 16041774821274725059547547929461486679346638222977996805511114949613626005842;
    
    uint256 constant IC1x = 9214936316556283729840109640204581574122124328468637509709740952133866062327;
    uint256 constant IC1y = 19257665450230465177456916168511597090257649018374582941165268720617826698693;
    
    uint256 constant IC2x = 15445927600431787741859301681887977524002590376158117139257052928096249771050;
    uint256 constant IC2y = 4957839968048249605586772925001928630013489844193982901509293871411045924658;
    
    uint256 constant IC3x = 8145514426038768393601549989174733141724137873476034933392888644377991611119;
    uint256 constant IC3y = 21333282746176649150891493232801342908437864692521039458306132908430566638858;
    
    uint256 constant IC4x = 9213382528752175335530714798725760901369186737646053905398009096048675665673;
    uint256 constant IC4y = 2047737698003762762960531601652392855786547770791561927669454434443360585786;
    
    uint256 constant IC5x = 2398224593355589173830681123957270384949045415233263701534121504721168101364;
    uint256 constant IC5y = 7682186104413469774664660816646307298640419924744741681765698653153599510153;
    
    uint256 constant IC6x = 4540463471534570160603571333476979641460656779896678245389031071403698943633;
    uint256 constant IC6y = 10347300745543071935225230942762244975620136102851426376670458739154249063160;
    
    uint256 constant IC7x = 9409568035847346256577708889846114393474853332891854397606380242737231949937;
    uint256 constant IC7y = 16022301405862517030847309689141815371953937095029412134240090853067738899782;
    
    uint256 constant IC8x = 1227480249539595535156948793023421656616252279082375622264829559991509925932;
    uint256 constant IC8y = 19520981656105137654203516785456458810884266762189027247437732492197235614661;
    
    uint256 constant IC9x = 138298103921546280433965817482150356579898449589933242830574651748535725127;
    uint256 constant IC9y = 12712075238948735666384566814869887245807176807428520028279177145381789163780;
    
    uint256 constant IC10x = 14300482165496003013716676912979932945614406033885136588759524420432542620516;
    uint256 constant IC10y = 9101537214048191745802865902025250517644003214027135390163812975055937801051;
    
    uint256 constant IC11x = 17686872212113798889440496096530743690613437820993298889600673977392720903515;
    uint256 constant IC11y = 15121455807243839031713281227976056417946021667128561471568802893598222069888;
    
    uint256 constant IC12x = 12191275821644073812095697124968889292235647543273407734884345521879258735517;
    uint256 constant IC12y = 13956025921899152039666226999299789745226230097560898228468574837101640077703;
    
    uint256 constant IC13x = 1979798186658322223629815686223337034799403881832343629078402889795354885757;
    uint256 constant IC13y = 1867550619166872960011661697905544999750484356591327069156894714102738492245;
    
    uint256 constant IC14x = 13974045148602755178448711558454077484462073278629231816712260395351681883799;
    uint256 constant IC14y = 1202969359308332148486708775930825671020864161700837150353097848022815970418;
    
    uint256 constant IC15x = 18549863211033769038077389578207333536837495646521146057071950340711453809673;
    uint256 constant IC15y = 2195979561041714356934165653136116352928643727104861124420260198215005083918;
    
    uint256 constant IC16x = 20738294163695952507830536176041075431220586547473898296414571467010513930303;
    uint256 constant IC16y = 12106967537264074673830637186057779249357414842096556217783804996238610243972;
    
    uint256 constant IC17x = 9125554992546301141122188907010764809610310258324918942105948873276447155739;
    uint256 constant IC17y = 6774687865597054752644398229360059711848516074461888384790958439219514819495;
    
    uint256 constant IC18x = 17588298429481807487539819596975193895692388514042722571436072530655076200639;
    uint256 constant IC18y = 13861347598731168841897407482445819565429909678722093104719096576505903930358;
    
    uint256 constant IC19x = 10625688033033525348942987630530915380478318940623991120264028144866566206694;
    uint256 constant IC19y = 669152247879131332268891840211384823540960249873658904388163268068899069668;
    
    uint256 constant IC20x = 573726978034276091299038369598693158152112513667333150800217745283111876445;
    uint256 constant IC20y = 3543776773670146674120842588989378533945565705500507065681079013932325041623;
    
    uint256 constant IC21x = 10313906718025098016866678040415924034287506233667140049842685489084465890213;
    uint256 constant IC21y = 13671845105475046306877383680056650133072383147447110711648476063086098056976;
    
    uint256 constant IC22x = 12475659877796981650999825744307168174584393342622047969005005286390220312112;
    uint256 constant IC22y = 16948223756605604922381260022637965524176468436745232090023592821406241999444;
    
    uint256 constant IC23x = 12624748402490071429150949315519039967768418560208249445032747587853645552696;
    uint256 constant IC23y = 5879613861429997112618753194249725781164763164684729016511584944731873863667;
    
    uint256 constant IC24x = 9749884844758826934936179193166091627268123767907430800631015960229304660405;
    uint256 constant IC24y = 6873492794080276219171400800514800279343912224091156827944458637115625631791;
    
    uint256 constant IC25x = 14879742860141148632060368015350447537939905480719626775204860294402702051119;
    uint256 constant IC25y = 8371550183178706313735134070062565574536930883275570185418164113153546501885;
    
    uint256 constant IC26x = 705421643163217236748540020529000072782399418308178130174961718913996675722;
    uint256 constant IC26y = 1547147302248536573059084959713755131139511476002365477488014898271521742827;
    
    uint256 constant IC27x = 20881885860034924512753964479861241733978637219056152502887189419210138744107;
    uint256 constant IC27y = 6481961947157813801683357020265130018753526978045265728665249442858349605095;
    
    uint256 constant IC28x = 14599830258803476595311363278892276243465426441644691211632776901969106863152;
    uint256 constant IC28y = 11403385067577912521193400872617314292167347100205238107297140280019687553695;
    
    uint256 constant IC29x = 5744438975443348514555278423236362428373335201848398130765347046674982974036;
    uint256 constant IC29y = 10503553388952100482126980726314042460700290045325246364163459351044601571868;
    
    uint256 constant IC30x = 4642122084364616462372777799033435852667420884251356413554035212512044840484;
    uint256 constant IC30y = 196409040553326380684957888447762835485334447084440111944308079623153590188;
    
    uint256 constant IC31x = 10768357595893912121408063544088147899340409663060051609242220431847103141097;
    uint256 constant IC31y = 11651324765627171950373025684546942875571657850705823789333034179612013336328;
    
    uint256 constant IC32x = 2733200192895417503328102629398522699715344657932256330308940748858263640230;
    uint256 constant IC32y = 1042576253550667542348488459394559243753023384033232336095497962876936171246;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[32] calldata _pubSignals) public view returns (bool) {
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
                
                g1_mulAccC(_pVk, IC27x, IC27y, calldataload(add(pubSignals, 832)))
                
                g1_mulAccC(_pVk, IC28x, IC28y, calldataload(add(pubSignals, 864)))
                
                g1_mulAccC(_pVk, IC29x, IC29y, calldataload(add(pubSignals, 896)))
                
                g1_mulAccC(_pVk, IC30x, IC30y, calldataload(add(pubSignals, 928)))
                
                g1_mulAccC(_pVk, IC31x, IC31y, calldataload(add(pubSignals, 960)))
                
                g1_mulAccC(_pVk, IC32x, IC32y, calldataload(add(pubSignals, 992)))
                

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
            
            checkField(calldataload(add(_pubSignals, 768)))
            
            checkField(calldataload(add(_pubSignals, 800)))
            
            checkField(calldataload(add(_pubSignals, 832)))
            
            checkField(calldataload(add(_pubSignals, 864)))
            
            checkField(calldataload(add(_pubSignals, 896)))
            
            checkField(calldataload(add(_pubSignals, 928)))
            
            checkField(calldataload(add(_pubSignals, 960)))
            
            checkField(calldataload(add(_pubSignals, 992)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
