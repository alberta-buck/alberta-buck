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

contract MintBatchN32Groth16Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 709997778838307835264383870894323768967370809777328976677908306577656761867;
    uint256 constant alphay  = 12836297161486382489279136251040704944281163072493113933910361141757593083677;
    uint256 constant betax1  = 3684114477087397643340155885713584867253826796058431906225088186474534956028;
    uint256 constant betax2  = 21569416133113965215801657701571922097548973573516190817471638180089358098266;
    uint256 constant betay1  = 4326734521026516125659375800180265709604472291722677762145538662848384222069;
    uint256 constant betay2  = 16828383835347505433443117675638127444543738572256217033768785829213672209398;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 4458472169770738068572766332019240640195999232494265961108773116025796698795;
    uint256 constant deltax2 = 2240091163892694989591585107105498779756529677185612393044147017499897571214;
    uint256 constant deltay1 = 13816830100480112329666800437397013140642637294079333045467927136861092625003;
    uint256 constant deltay2 = 11218253089181053468170855873404842559245142084039290017951347590803713802732;

    
    uint256 constant IC0x = 14314208187318044151280492488861182160433451138218867677891312063941472093142;
    uint256 constant IC0y = 18267994163447512876339956834266930322634036347455786021851843546442322980353;
    
    uint256 constant IC1x = 6873572175312553439600229758855517338458652458917452017121163308849619553848;
    uint256 constant IC1y = 11308147439123261427150878095025268326578438713555362069927887961689678902050;
    
    uint256 constant IC2x = 827033192371845467025776243574142374151188323158946658873366620806273527595;
    uint256 constant IC2y = 17270537657989491675975751319617237146107690408129823799321716792535308848207;
    
    uint256 constant IC3x = 19609211753726225009028996692316784915919157476416153990902620260578236397557;
    uint256 constant IC3y = 14532804837777680722000754541486924885722416796854106666275919243518415299814;
    
    uint256 constant IC4x = 15785409589385729100499748266818035365588169110661086704291095290008374245781;
    uint256 constant IC4y = 21328087659468465274337614480252516903364329627114883564002719565985458393404;
    
    uint256 constant IC5x = 4797346924666559985787774322366591511035431318449909967034788622699059597102;
    uint256 constant IC5y = 11895677337998544823327979595848813034891649182365978520891299713009282589982;
    
    uint256 constant IC6x = 12383124491416469221206836907625701399828155267808367470598058438454706320638;
    uint256 constant IC6y = 20261316361162856424013401210380202323959606216970037941158342749977640975589;
    
    uint256 constant IC7x = 4672192589431945469847251708630078820069948394967361567033947250332981820344;
    uint256 constant IC7y = 4745135414679581606725533951211552089799768219734690133768990005490282185411;
    
    uint256 constant IC8x = 124471775338667322628701218311829742779128233428974924409076538312264630281;
    uint256 constant IC8y = 9790903200198949885334904682904927330529727444757059079973849295829837518149;
    
    uint256 constant IC9x = 20940458743032766627615569258581758495248915713232181344915721150540456127639;
    uint256 constant IC9y = 12582661334409084162813537558378620357716246344049353845838899099766462114902;
    
    uint256 constant IC10x = 16837248064020892085252894508617499234581986757843563529231098389251390036959;
    uint256 constant IC10y = 6580456932925613950644899157704546845111262623486883986627472928496342061477;
    
    uint256 constant IC11x = 9745374688657155746863757832043400174498703325811666372721622886320435927128;
    uint256 constant IC11y = 7029639463080939115134076146822592183861978270644442701967047001852409632161;
    
    uint256 constant IC12x = 18719035338270797000429616883494454865645690901987150919949514958079526736466;
    uint256 constant IC12y = 9812807618367411597717569469921516667709828313763636907575635721498409867946;
    
    uint256 constant IC13x = 181679855941958579275254305159881383733633019611914961677947600111570943300;
    uint256 constant IC13y = 8462751789650010496025837166322686766093317495251109766854195152756158355805;
    
    uint256 constant IC14x = 19885557884197168707391825404720858437721699931835945590879096638621096895377;
    uint256 constant IC14y = 12427759746865281849255351305675855277634633291952416355555210570082713480970;
    
    uint256 constant IC15x = 14963803436155077376116453015363887317982863264521440080556552787033706133555;
    uint256 constant IC15y = 12370300800174081394271049942177993460429805539805981585236233540043600239358;
    
    uint256 constant IC16x = 2184164225672099809820518514868652369974605828624594176480220219920523644468;
    uint256 constant IC16y = 16268540833382225686910729312401027369199753111424589780715837460017815823058;
    
    uint256 constant IC17x = 12601102155517402568761759407291989963639305839672063751624485455227950953240;
    uint256 constant IC17y = 18878115821136850940244968611409432962663557099497915914307748838586555986200;
    
    uint256 constant IC18x = 5299887748477104920354760132087583332719378284777903685566485431711003312619;
    uint256 constant IC18y = 640991228502867165688289322712988158463197216701065071464492098961196570952;
    
    uint256 constant IC19x = 21019722894288217801309006244784086620091682157020977471405043818919120315066;
    uint256 constant IC19y = 18894572629317197201641247219871119191272138943028552143170846549098837176463;
    
    uint256 constant IC20x = 12160354629019197034324794393946489732183069783182174641953104413674298679848;
    uint256 constant IC20y = 6039926509998912359525326262969271406677305824883311670077440300996438867649;
    
    uint256 constant IC21x = 11058858962651379091873342172640995757252441335124986498122615889652713933530;
    uint256 constant IC21y = 6988745068795476217976872148868772423065723342781646838829765758386004256721;
    
    uint256 constant IC22x = 6758953928949212583556391089973566499917898209624134930150692793477072676379;
    uint256 constant IC22y = 19114677658217616476109387593802153131443272123792843263821778835656384267981;
    
    uint256 constant IC23x = 3965901326410966243035014529650021465984643189449457371116851341638158263253;
    uint256 constant IC23y = 11384114534199251213163897555154234304675397581052696088899887500701995804806;
    
    uint256 constant IC24x = 4263566674095351960919576583455169142563405210714249521052604097213973648892;
    uint256 constant IC24y = 15035523773865309444905807401234955101910427738428895996094622882704289184945;
    
    uint256 constant IC25x = 4525402682936134587081425070746242723516216493954930298800612567209780293563;
    uint256 constant IC25y = 12336993568298796858101498666925960808373201574105986341354290382790503140062;
    
    uint256 constant IC26x = 7071058572588159979074473089856290149403449795337474868112470876483842901171;
    uint256 constant IC26y = 20473376085562389118325832340264140162224520206578812803064250575270766823492;
    
    uint256 constant IC27x = 20321809285156655779917064733405545133481568344282680294136360643373763001506;
    uint256 constant IC27y = 12393451118075866114559400761075714941645432422371621227242256451726697475358;
    
    uint256 constant IC28x = 19332184619446455240566119657015839271249890269478046080804486885912559603597;
    uint256 constant IC28y = 14459097036197967900552281393419491661141562286833747916150311088547864365221;
    
    uint256 constant IC29x = 16455225564157891567766557338944113985903140005975124695037117195411755858272;
    uint256 constant IC29y = 5238636491288620601068327882525420816888331027763422144231304713798351072930;
    
    uint256 constant IC30x = 6361364562035844565429135924470566889708671828345197688264761319166218083823;
    uint256 constant IC30y = 2089408610783011913009650654142236150417123887710713031246527479523750523036;
    
    uint256 constant IC31x = 5330944924901073438746811332773184394220490645853061206365672917592936793831;
    uint256 constant IC31y = 7828333197408305959742740066866572776541243288341102778808877502024807242128;
    
    uint256 constant IC32x = 20837600404342640552596969867645189138303416113972828721950761830663712985212;
    uint256 constant IC32y = 172038431575748385595418577012647419780442102049913652754561559418412065135;
    
    uint256 constant IC33x = 13505235947294914881632912080797740676078402892217535599341963556839034245061;
    uint256 constant IC33y = 965166435997078690062354637723339546034280370711186522765370613877899993140;
    
    uint256 constant IC34x = 881436254100998705512498418953598532311826753102975726374079238383092272546;
    uint256 constant IC34y = 1761255350670766411131523254943773218634321154657299455560321411175632545888;
    
    uint256 constant IC35x = 16139237945497969372368654312544285660932291642901708566351791373376619343207;
    uint256 constant IC35y = 6254073282047714687830864276613751164132824542286826811247847787080406138168;
    
    uint256 constant IC36x = 15424661982728523376990302668384020857144507694793204730182898231774338389148;
    uint256 constant IC36y = 3725605956515995882937927612123902928382164530610791752141249712018198485377;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[36] calldata _pubSignals) public view returns (bool) {
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
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
