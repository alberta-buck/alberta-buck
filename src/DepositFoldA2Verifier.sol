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

contract DepositFoldA2Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 20901312453931986983070371975751072566398443441960436590636518297529489694139;
    uint256 constant alphay  = 21697131480926335546036040991413011755153140230317664815989518148158724370212;
    uint256 constant betax1  = 4869487231785253308212429570614741443191402866210819877611793555879218415484;
    uint256 constant betax2  = 8494223465375928023871052138064538434365863294294498107508764578998413175191;
    uint256 constant betay1  = 13692213518456195651330939167802977105042737180923530430942149449680303886878;
    uint256 constant betay2  = 6329045905833941605730059747255744716806210507261219104759569890353192015645;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 15786805278869067015286373887336618244345863050477596964001992107774862083215;
    uint256 constant deltax2 = 4065780586849027098295336090577577093279299474912288896992075197351893496755;
    uint256 constant deltay1 = 4722397050672318090654687775181341453528109193355678448945866635606318367922;
    uint256 constant deltay2 = 559946055390492414330898705936667909608021484195322856093469290753799778707;

    
    uint256 constant IC0x = 20839465141556008245853855952401410152546560814157731847649521682606469993748;
    uint256 constant IC0y = 9088501775865017336254531986783680347477560671208307342206529752416112075103;
    
    uint256 constant IC1x = 3342184755942862780412534901987626999416558035482622384203172771541636741673;
    uint256 constant IC1y = 5835735761527240566011561571598563505999878418094274068069242730060348500575;
    
    uint256 constant IC2x = 20299717897478496945263113887511930639968038427868369863368464981902683453006;
    uint256 constant IC2y = 15479648501373744352756282279511421589684977026003307146879151262368327727908;
    
    uint256 constant IC3x = 11007294119492225254294023929742912379352642536374293650792242098312686537722;
    uint256 constant IC3y = 4015627226126882592243773900131546975704642013713008309569846449459927091689;
    
    uint256 constant IC4x = 18662027954495350730512659106304252769383231759266628547041984839277868018034;
    uint256 constant IC4y = 18905870738907892630066165192400231318316765191800757238205401830313239906670;
    
    uint256 constant IC5x = 6840064330752666025944799342687151175018939402845478536832950446175048356874;
    uint256 constant IC5y = 5212798583941611756350726533806927801833979561791113376611203274547112818662;
    
    uint256 constant IC6x = 18453060505729411271596092820219575873360874809485316246544427577782115926828;
    uint256 constant IC6y = 15590196464137634798383172652723548259031439466023801489725301794601300971091;
    
    uint256 constant IC7x = 14791300722639159684851900458848464954894090100740956798914157124494920340186;
    uint256 constant IC7y = 3930723788107037437317793229349778696337849860491939637572387590985603124316;
    
    uint256 constant IC8x = 1706492978517384577280024650137432009171813662279059501367932828312247702696;
    uint256 constant IC8y = 9147329242165419713880079089293878573260935736872225327075850743650589985390;
    
    uint256 constant IC9x = 19709981049623237674300819172608350017428698162047902536113969453949727171397;
    uint256 constant IC9y = 7669122811319344394756310882453340006543356735460616615092863513466288718436;
    
    uint256 constant IC10x = 11890397846183333945227477241948839482892834212728331859713169755451833139277;
    uint256 constant IC10y = 18786856071408397429414989402223766942810356465410535990121461533456114857795;
    
    uint256 constant IC11x = 7974944711298088813112692213684117162714802665164412576250062345990498526720;
    uint256 constant IC11y = 12476369904190617801592474954995578741101717414846613612363450659285862802969;
    
    uint256 constant IC12x = 14005513153002235683293688913787215389885012220973657718024782612896469903422;
    uint256 constant IC12y = 16074227420949308366077283110703434553907223519861681985020241834617836468449;
    
    uint256 constant IC13x = 9740105707109959211548445043734639710120393345833865101079066661886587744889;
    uint256 constant IC13y = 5052265705746827207072502730888343360695964209018929601173560833372420542192;
    
    uint256 constant IC14x = 4476085142182338133413819250555179585168764789163114021651662367390596470281;
    uint256 constant IC14y = 10426965206191421489909508769639312471687199144914500920151923490335482999725;
    
    uint256 constant IC15x = 942314020959624574185156493107748566619630010281538174422888609468666163183;
    uint256 constant IC15y = 13899654054942653209210761350727024232666576235372130342093362193814142101647;
    
    uint256 constant IC16x = 13662476311489690502886913322302617546209968143513393416663727985875064757661;
    uint256 constant IC16y = 13182187063788195564060139225506898143486768618217459804111905002765535281331;
    
    uint256 constant IC17x = 973590679316392901690119955187778244575310724391038896061323333197385035080;
    uint256 constant IC17y = 16238039675242291078540090693500011382062067194940764710940056603157774586028;
    
    uint256 constant IC18x = 3824426324326283841108752859616612226515050818127559917127347729307182035078;
    uint256 constant IC18y = 328891559692527225843761971668075292222779169855062604601857439159128790075;
    
    uint256 constant IC19x = 6766994544209460897941051022424934331657039390463223710150842837387526612038;
    uint256 constant IC19y = 12655559989094486288464414482780192742040499622346860401812694910476282438113;
    
    uint256 constant IC20x = 15552637489523531620646094582338031176667736099080980528938221995805252029663;
    uint256 constant IC20y = 16633301231333658560602436112245539561549245437807211447271689500749430155949;
    
    uint256 constant IC21x = 7790794298219765058409991311992109375303786014351910942973151891112581194826;
    uint256 constant IC21y = 16463190357008813570817828186358738335943875793368728897676961159867850377564;
    
    uint256 constant IC22x = 14655807119445345645599956989100677647557410582136615682175845220568172116511;
    uint256 constant IC22y = 5103929370236668518164762604789539612463212518715078944242439538937220798199;
    
    uint256 constant IC23x = 10321673865901520275443083023108247491397448801066219254690130035641130046651;
    uint256 constant IC23y = 7581587523894517406985706788402450024593847150799520039172355651880182584525;
    
    uint256 constant IC24x = 4458663033463724923448587734688733292104319050815057752612817601421779638245;
    uint256 constant IC24y = 7377563565399781005905867873841171417299638837713294561288668452539166022106;
    
    uint256 constant IC25x = 12193567713484278736275981681190675129153746161156095731613559105317097952397;
    uint256 constant IC25y = 16155286573184774694544278686883826035996881770542176659394962532960649540931;
    
    uint256 constant IC26x = 3717711120393985366003360773747258336838806720937731401854205974679236801765;
    uint256 constant IC26y = 8947042015700695788858279843488454467876361041826006591072140429924980086678;
    
    uint256 constant IC27x = 14208160896085095618879647382932546635249486589973078584389343269711404211121;
    uint256 constant IC27y = 10978490309766230789160748712501696578338888419036119976538260666671321783216;
    
    uint256 constant IC28x = 6019671039831697920455816451027123786746408325588404204641831922825644974288;
    uint256 constant IC28y = 20221661174187082638734397480492396357089422804781272545383425700275320980993;
    
    uint256 constant IC29x = 12996191816213541797610697961951997877724054254749779369768716695357196393190;
    uint256 constant IC29y = 7813044011296774377634621025810060521085741552016578489487509956809459064384;
    
    uint256 constant IC30x = 10050906707279779414922463694556132380032424018204327751999135801557919895;
    uint256 constant IC30y = 11658107675777081720794892841252923446052856050920901277402614080465797993012;
    
    uint256 constant IC31x = 8564258633421129251316413040367592904381493510111122243625886143587772200397;
    uint256 constant IC31y = 12111857302649537179722909368384028657195095353935965125294477948006133660561;
    
    uint256 constant IC32x = 1088035570750354362689228586953433188341359545333058904809420211338788381559;
    uint256 constant IC32y = 6027067035012382741575046343817934477678442483011254501773124620286278313051;
    
    uint256 constant IC33x = 12763147314756309744375610912439337168966207856685350331063783022306023891997;
    uint256 constant IC33y = 12824200366422710608111033040556992852725430793231781591060159476558519038691;
    
    uint256 constant IC34x = 4460897165215275879263045740432015700298850194096800983029113292820239689881;
    uint256 constant IC34y = 3071015897379684084098265601376814465510193476392872902473032455175141688278;
    
    uint256 constant IC35x = 13465831736172215304179256002531609132375839818660511254488958771345810113727;
    uint256 constant IC35y = 13959444064087177489684143569145315609366226499321124636600115645983381812407;
    
    uint256 constant IC36x = 13006215981264670222228801634054835759103319187477759970160430195006077096394;
    uint256 constant IC36y = 8739424797065565546238169068211121153368330836653666482425165613048383673666;
    
    uint256 constant IC37x = 9914767183055784498268828832412446460205932987546871083198732959192419948599;
    uint256 constant IC37y = 2198120666788796583370862491229421478185143053710173094894390642335590952478;
    
    uint256 constant IC38x = 13492884590330658399237609742692303500459279944732781724879075600432783837992;
    uint256 constant IC38y = 5740245155744754716200033053062509165730633092715390426641114670834881681869;
    
    uint256 constant IC39x = 16432491273573956284581522794784563032423658525984169286861287546617122851999;
    uint256 constant IC39y = 2749365302376839393836703530969184048357087048762161226998596691879725670856;
    
    uint256 constant IC40x = 5371569326559552670126395435085460353400677803248061720177586307713418775659;
    uint256 constant IC40y = 21745740629894581515716501096335571314627390512418717042955060463842564360111;
    
    uint256 constant IC41x = 16733012066959288236857069906967144781283949209860708473566681692117367752148;
    uint256 constant IC41y = 9462758811782100115406378135187617204024993163683412971384101306280609381907;
    
    uint256 constant IC42x = 8628267470803078413494642823278739357739170025179116075285598212078622090287;
    uint256 constant IC42y = 20733517168777461118499053770470558603163050936740820371989625826582582412575;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[42] calldata _pubSignals) public returns (bool) {
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
                
                g1_mulAccC(_pVk, IC33x, IC33y, calldataload(add(pubSignals, 1024)))
                
                g1_mulAccC(_pVk, IC34x, IC34y, calldataload(add(pubSignals, 1056)))
                
                g1_mulAccC(_pVk, IC35x, IC35y, calldataload(add(pubSignals, 1088)))
                
                g1_mulAccC(_pVk, IC36x, IC36y, calldataload(add(pubSignals, 1120)))
                
                g1_mulAccC(_pVk, IC37x, IC37y, calldataload(add(pubSignals, 1152)))
                
                g1_mulAccC(_pVk, IC38x, IC38y, calldataload(add(pubSignals, 1184)))
                
                g1_mulAccC(_pVk, IC39x, IC39y, calldataload(add(pubSignals, 1216)))
                
                g1_mulAccC(_pVk, IC40x, IC40y, calldataload(add(pubSignals, 1248)))
                
                g1_mulAccC(_pVk, IC41x, IC41y, calldataload(add(pubSignals, 1280)))
                
                g1_mulAccC(_pVk, IC42x, IC42y, calldataload(add(pubSignals, 1312)))
                

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
            
            checkField(calldataload(add(_pubSignals, 1024)))
            
            checkField(calldataload(add(_pubSignals, 1056)))
            
            checkField(calldataload(add(_pubSignals, 1088)))
            
            checkField(calldataload(add(_pubSignals, 1120)))
            
            checkField(calldataload(add(_pubSignals, 1152)))
            
            checkField(calldataload(add(_pubSignals, 1184)))
            
            checkField(calldataload(add(_pubSignals, 1216)))
            
            checkField(calldataload(add(_pubSignals, 1248)))
            
            checkField(calldataload(add(_pubSignals, 1280)))
            
            checkField(calldataload(add(_pubSignals, 1312)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
